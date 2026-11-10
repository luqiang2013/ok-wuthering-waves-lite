from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
MAX_EVENTS_PER_BATTLE = 20_000
DEDUPLICATE_WINDOW_SECONDS = 0.2
logger = logging.getLogger(__name__)


def default_battle_log_root() -> Path:
    from ok import Config

    folder = getattr(Config, "config_folder", None) or "configs"
    return Path(folder) / "battle_logs"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _character_name(character: Any) -> str:
    if character is None:
        return ""
    class_name = getattr(character, "name", None) or type(character).__name__
    char_name = getattr(character, "char_name", None)
    return f"{class_name} ({char_name})" if char_name and char_name != class_name else class_name


def _team_names(chars: Iterable[Any]) -> list[str]:
    return [name for name in (_character_name(char) for char in chars) if name]


class BattleLogRecorder:
    """In-memory battle trace that is written as one JSON file at combat end."""

    def __init__(self, task_name: str = "", root: Path | str | None = None):
        self.task_name = task_name or "Combat"
        self.root = Path(root) if root is not None else None
        self._lock = threading.RLock()
        self._enabled = True
        self._active = False
        self._session_id = ""
        self._started_at: datetime | None = None
        self._started_monotonic = 0.0
        self._team: list[str] = []
        self._metadata: dict[str, Any] = {}
        self._events: list[dict[str, Any]] = []
        self._saved_path: Path | None = None
        self._active_path: Path | None = None
        self._active_stream = None

    @property
    def active(self) -> bool:
        return self._enabled and self._active

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        with self._lock:
            if self._enabled == enabled:
                return
            self._enabled = enabled
            if not enabled:
                # Disabling must stop the current recording immediately and
                # never leave a partially generated battle file behind.
                self._active = False
                self._events = []
                self._saved_path = None
                self._discard_active_file()

    @property
    def saved_path(self) -> Path | None:
        return self._saved_path

    def ensure_started(self, chars: Iterable[Any] = (), metadata: dict[str, Any] | None = None) -> bool:
        with self._lock:
            if not self._enabled:
                return False
            if self._active:
                if not self._team:
                    self._team = _team_names(chars)
                    if self._team and self._events:
                        self._events[0]["detail"] = ", ".join(self._team)
                return False
            now = datetime.now().astimezone()
            self._session_id = f"{now:%Y%m%d_%H%M%S_%f}_{uuid.uuid4().hex[:6]}"
            self._started_at = now
            self._started_monotonic = time.monotonic()
            self._team = _team_names(chars)
            self._metadata = _json_safe(metadata or {})
            self._events = []
            self._saved_path = None
            self._active = True
            self._open_active_file()
            self._append(
                {
                    "kind": "lifecycle",
                    "character": "",
                    "condition": "进入战斗",
                    "result": True,
                    "action": "开始记录",
                    "detail": ", ".join(self._team),
                }
            )
            return True

    def record_decision(
        self,
        character: Any or str,
        condition: str,
        result: Any,
        action: str,
        detail: Any = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.record(
            kind="decision",
            character=character,
            condition=condition,
            result=result,
            action=action,
            detail=detail,
            data=data,
        )

    def record_action(
        self,
        character: Any or str,
        action: str,
        detail: Any = None,
        condition: str = "",
        result: Any = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.record(
            kind="action",
            character=character,
            condition=condition,
            result=result,
            action=action,
            detail=detail,
            data=data,
        )

    def note(self, condition: str, result: Any = None, action: str = "", detail: Any = None) -> None:
        self.record(
            kind="note",
            character="",
            condition=condition,
            result=result,
            action=action,
            detail=detail,
        )

    def record(
        self,
        kind: str,
        character: Any or str,
        condition: str,
        result: Any,
        action: str,
        detail: Any = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            if not self._enabled or not self._active or len(self._events) >= MAX_EVENTS_PER_BATTLE:
                return
            name = character if isinstance(character, str) else _character_name(character)
            event = {
                "kind": str(kind or "note"),
                "character": name,
                "condition": str(condition or ""),
                "result": _json_safe(result),
                "action": str(action or ""),
                "detail": _json_safe(detail),
                "data": _json_safe(data or {}),
            }
            elapsed = time.monotonic() - self._started_monotonic
            if self._events:
                previous = self._events[-1]
                compared = ("kind", "character", "condition", "result", "action", "detail")
                if (
                    elapsed - float(previous.get("elapsed_seconds", -100))
                    < DEDUPLICATE_WINDOW_SECONDS
                    and all(previous.get(key) == event.get(key) for key in compared)
                ):
                    return
            self._append(event)

    def _append(self, event: dict[str, Any]) -> None:
        event = dict(event)
        if "elapsed_seconds" not in event:
            event["elapsed_seconds"] = round(time.monotonic() - self._started_monotonic, 4)
        event["wall_time"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
        self._events.append(event)
        self._write_active_event(event)

    def _open_active_file(self) -> None:
        try:
            root = self.root or default_battle_log_root()
            root.mkdir(parents=True, exist_ok=True)
            self._active_path = root / f"{self._session_id}.active.jsonl"
            self._active_stream = self._active_path.open(
                "w", encoding="utf-8", buffering=1
            )
            header = {
                "record_type": "session",
                "schema_version": SCHEMA_VERSION,
                "session_id": self._session_id,
                "task": self.task_name,
                "started_at": self._started_at.isoformat(timespec="milliseconds"),
                "team": self._team,
                "metadata": self._metadata,
            }
            self._active_stream.write(json.dumps(header, ensure_ascii=False) + "\n")
            self._active_stream.flush()
        except OSError as error:
            logger.warning(f"failed to create active battle log: {error}")
            self._close_active_file(delete=False)

    def _write_active_event(self, event: dict[str, Any]) -> None:
        if self._active_stream is None:
            return
        try:
            self._active_stream.write(
                json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            self._active_stream.flush()
        except OSError as error:
            logger.warning(f"failed to append active battle log: {error}")
            self._close_active_file(delete=False)

    def _close_active_file(self, delete: bool = False) -> None:
        stream = self._active_stream
        self._active_stream = None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        if delete and self._active_path is not None:
            try:
                self._active_path.unlink(missing_ok=True)
            except OSError as error:
                logger.warning(f"failed to remove active battle log: {error}")
        if delete:
            self._active_path = None

    def _discard_active_file(self) -> None:
        self._close_active_file(delete=True)

    def finish(self, outcome: str = "completed", detail: Any = None) -> Path | None:
        with self._lock:
            if not self._enabled or not self._active:
                return None
            duration = time.monotonic() - self._started_monotonic
            self.record(
                kind="lifecycle",
                character="",
                condition="结束战斗",
                result=outcome,
                action="保存战斗日志",
                detail=detail,
            )
            ended_at = datetime.now().astimezone()
            payload = {
                "schema_version": SCHEMA_VERSION,
                "session_id": self._session_id,
                "task": self.task_name,
                "started_at": self._started_at.isoformat(timespec="milliseconds"),
                "ended_at": ended_at.isoformat(timespec="milliseconds"),
                "duration_seconds": round(duration, 3),
                "outcome": outcome,
                "team": self._team,
                "metadata": self._metadata,
                "event_count": len(self._events),
                "events": self._events,
            }
            self._active = False
            self._close_active_file()
            try:
                path = self.save(payload)
            except OSError as error:
                # Logging must never interrupt combat or task cleanup.
                logger.warning(f"failed to save battle log: {error}")
                path = None
            if path is not None:
                self._discard_active_file()
            self._saved_path = path
            self._events = []
            return path

    def save(self, payload: dict[str, Any]) -> Path:
        root = self.root or default_battle_log_root()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{self._session_id}.json"
        temp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        temp.write_text(
            json.dumps(_json_safe(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, path)
        return path

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session_id": self._session_id,
                "active": self._active,
                "team": list(self._team),
                "events": list(self._events),
                "active_path": str(self._active_path) if self._active_path else None,
            }


class BattleLogStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else default_battle_log_root()
        self._cache: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}

    def list_sessions(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        sessions = []
        for path in self.root.glob("*.json"):
            try:
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                cached = self._cache.get(path)
                if cached and cached[0] == signature:
                    data = dict(cached[1])
                else:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    self._cache[path] = (signature, data)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if data.get("schema_version") != SCHEMA_VERSION:
                continue
            item = dict(data)
            item["_path"] = str(path)
            sessions.append(item)
        completed_ids = {item.get("session_id") for item in sessions}
        for path in self.root.glob("*.active.jsonl"):
            try:
                header = None
                events = []
                with path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if header is None:
                            if record.get("record_type") == "session":
                                header = record
                            continue
                        events.append(record)
            except OSError:
                continue
            if not header or header.get("schema_version") != SCHEMA_VERSION:
                continue
            session_id = header.get("session_id")
            if session_id in completed_ids:
                continue
            last_event = events[-1] if events else {}
            sessions.append({
                **header,
                "ended_at": last_event.get("wall_time", header.get("started_at")),
                "duration_seconds": last_event.get("elapsed_seconds", 0),
                "outcome": "interrupted",
                "event_count": len(events),
                "events": events,
                "_path": str(path),
            })
        sessions.sort(key=lambda item: item.get("started_at", ""), reverse=True)
        return sessions

    def delete_session(self, path: Path | str) -> None:
        """Delete one log file, rejecting paths outside this store's root."""
        root = self.root.resolve()
        target = Path(path).resolve()
        if target.parent != root:
            raise ValueError("Battle log is outside the configured log directory")
        target.unlink(missing_ok=True)
        self._cache.pop(target, None)

