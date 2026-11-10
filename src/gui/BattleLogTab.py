import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ok.ui.qt.kit.buttons import Button
from ok.ui.qt.kit.dialogs import Dialog
from ok.ui.qt.kit.feedback import show_toast
from ok.ui.qt.kit.icons import icon
from ok.ui.qt.widget.CustomTab import CustomTab
from src.task.BattleLog import BattleLogStore


PAGE_SIZE = 500


def format_duration(seconds):
    seconds = max(0, float(seconds or 0))
    minutes, remainder = divmod(seconds, 60)
    if minutes:
        return f"{int(minutes)}分{remainder:04.1f}秒"
    return f"{remainder:.1f}秒"


def format_result(value):
    if value is True:
        return "是"
    if value is False:
        return "否"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class BattleLogTab(CustomTab):
    def __init__(self):
        super().__init__()
        self.store = BattleLogStore()
        self.sessions = []
        self.current_session_id = None
        self.current_session = None
        self.current_page = 0

        header = QHBoxLayout()
        title = QLabel("战斗日志")
        title.setProperty("role", "subtitle")
        header.addWidget(title)
        header.addStretch(1)
        self.open_folder_button = Button("打开目录", icon="folder")
        self.open_folder_button.clicked.connect(self.open_log_folder)
        header.addWidget(self.open_folder_button)
        self.addLayout(header)

        self.hint_label = QLabel(
            "每场战斗结束后自动生成一条日志；可在自动战斗设置中关闭“启用战斗日志”。"
        )
        self.hint_label.setWordWrap(True)
        self.add_widget(self.hint_label)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        records_bar = QHBoxLayout()
        records_bar.addWidget(QLabel("战斗记录"))
        records_bar.addStretch(1)
        self.add_log_button = Button(icon="refresh", variant="subtle", size="small")
        self.add_log_button.setFixedSize(30, 30)
        self.add_log_button.setToolTip("重新扫描战斗日志")
        self.add_log_button.clicked.connect(self.reload_logs)
        self.delete_log_button = Button(icon="minus", variant="subtle", size="small")
        self.delete_log_button.setFixedSize(30, 30)
        self.delete_log_button.setToolTip("删除选中的战斗日志")
        self.delete_log_button.clicked.connect(self.delete_current_log)
        records_bar.addWidget(self.add_log_button)
        records_bar.addWidget(self.delete_log_button)
        left_layout.addLayout(records_bar)
        self.log_list = QListWidget(left)
        self.log_list.setMinimumWidth(290)
        self.log_list.setMaximumWidth(420)
        self.log_list.currentItemChanged.connect(self.session_selected)
        left_layout.addWidget(self.log_list, 1)

        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        self.summary_label = QLabel("请选择一场战斗")
        self.summary_label.setWordWrap(True)
        right_layout.addWidget(self.summary_label)
        self.team_label = QLabel("")
        self.team_label.setWordWrap(True)
        right_layout.addWidget(self.team_label)

        self.event_table = QTableWidget(right)
        self.event_table.setColumnCount(6)
        self.event_table.setHorizontalHeaderLabels(
            ["时间", "角色", "类型", "条件判定", "判定结果", "操作 / 详情"]
        )
        self.event_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.event_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.event_table.setAlternatingRowColors(True)
        self.event_table.verticalHeader().hide()
        header_view = self.event_table.horizontalHeader()
        for column, width in enumerate((75, 160, 95, 210, 85)):
            header_view.setSectionResizeMode(column, QHeaderView.Interactive)
            self.event_table.setColumnWidth(column, width)
        header_view.setSectionResizeMode(5, QHeaderView.Stretch)
        self.event_table.setWordWrap(False)
        self.event_table.setTextElideMode(Qt.ElideRight)
        right_layout.addWidget(self.event_table, 1)
        pager = QHBoxLayout()
        self.previous_page_button = Button("上一页", icon="chevron-left")
        self.next_page_button = Button("下一页", icon="chevron-right")
        self.page_label = QLabel("")
        self.previous_page_button.clicked.connect(lambda: self.change_page(-1))
        self.next_page_button.clicked.connect(lambda: self.change_page(1))
        pager.addWidget(self.previous_page_button)
        pager.addWidget(self.next_page_button)
        pager.addWidget(self.page_label)
        pager.addStretch(1)
        right_layout.addLayout(pager)
        self._update_pager()

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, 1100])
        self.add_widget(splitter, stretch=1)

        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.refresh_logs)
        self.refresh_logs()

    @property
    def name(self):
        return "战斗日志"

    @property
    def icon(self):
        return icon("history")

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_logs()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def refresh_logs(self, force=False):
        sessions = self.store.list_sessions()
        signature = tuple(
            (session.get("_path"), session.get("event_count"), session.get("ended_at"))
            for session in sessions
        )
        if not force and signature == getattr(self, "_session_signature", None):
            return
        self._session_signature = signature
        self.sessions = sessions
        previous = self.current_session_id
        self.log_list.blockSignals(True)
        self.log_list.clear()
        selected_row = -1
        for row, session in enumerate(sessions):
            started = self._parse_time(session.get("started_at"))
            started_text = started.strftime("%Y-%m-%d %H:%M:%S") if started else session.get("session_id", "")
            team = ", ".join(session.get("team") or ())
            duration = format_duration(session.get("duration_seconds"))
            outcome = self._outcome_text(session.get("outcome"))
            item = QListWidgetItem(f"{started_text}  ·  {duration}  ·  {outcome}\n{team}")
            item.setData(Qt.UserRole, session.get("session_id"))
            self.log_list.addItem(item)
            if session.get("session_id") == previous:
                selected_row = row
        self.log_list.blockSignals(False)
        if selected_row < 0 and sessions:
            selected_row = 0
        if selected_row >= 0:
            self.log_list.setCurrentRow(selected_row)
            self._show_session(sessions[selected_row])
        else:
            self.current_session_id = None
            self.current_session = None
            self.summary_label.setText("暂无战斗日志")
            self.team_label.clear()
            self.event_table.setRowCount(0)
            self._update_pager()

    def reload_logs(self):
        self.refresh_logs(force=True)

    def delete_current_log(self):
        if not self.current_session:
            show_toast(self, "请先选择一条战斗日志", tone="warning", duration=2000)
            return
        started = self._parse_time(self.current_session.get("started_at"))
        started_text = started.strftime("%Y-%m-%d %H:%M:%S") if started else self.current_session_id
        box = Dialog(self.window(), "删除战斗日志", f"确定删除 {started_text} 的战斗日志吗？")
        box.ok_button.setText("删除")
        box.cancel_button.setText("取消")
        try:
            if not box.exec():
                return
        finally:
            box.deleteLater()
        try:
            self.store.delete_session(self.current_session["_path"])
        except (OSError, ValueError, KeyError) as error:
            show_toast(self, f"删除失败：{error}", title="战斗日志", tone="danger", duration=4000)
            return
        show_toast(self, "战斗日志已删除", title="战斗日志", tone="success", duration=2000)
        self.current_session_id = None
        self.current_session = None
        self._session_signature = None
        self.refresh_logs(force=True)

    @staticmethod
    def _parse_time(value):
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _outcome_text(value):
        return {
            "completed": "正常结束",
            "left_combat": "离开战斗",
            "char_dead": "角色死亡",
            "aborted": "中途停止",
            "interrupted": "异常中断（已恢复现场）",
        }.get(str(value or ""), str(value or "未知"))

    def session_selected(self, current, _previous=None):
        if current is None:
            return
        session_id = current.data(Qt.UserRole)
        session = next(
            (item for item in self.sessions if item.get("session_id") == session_id),
            None,
        )
        if session:
            self._show_session(session)

    def _show_session(self, session):
        if session.get("session_id") != self.current_session_id:
            self.current_page = 0
        self.current_session_id = session.get("session_id")
        self.current_session = session
        started = self._parse_time(session.get("started_at"))
        ended = self._parse_time(session.get("ended_at"))
        started_text = started.strftime("%Y-%m-%d %H:%M:%S") if started else "未知"
        ended_text = ended.strftime("%H:%M:%S") if ended else "未知"
        self.summary_label.setText(
            f"{started_text} → {ended_text}  |  "
            f"用时 {format_duration(session.get('duration_seconds'))}  |  "
            f"{self._outcome_text(session.get('outcome'))}  |  "
            f"{session.get('event_count', 0)} 条记录"
        )
        self.team_label.setText("队伍：" + (", ".join(session.get("team") or ()) or "未记录"))
        self._render_page()

    def _render_page(self):
        if self.current_session is None:
            self.event_table.setRowCount(0)
            self._update_pager()
            return
        events = self.current_session.get("events") or []
        page_count = max(1, (len(events) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.current_page = min(max(0, self.current_page), page_count - 1)
        start = self.current_page * PAGE_SIZE
        page_events = events[start:start + PAGE_SIZE]
        self.event_table.setUpdatesEnabled(False)
        self.event_table.setRowCount(len(page_events))
        for row, event in enumerate(page_events):
            detail = event.get("detail")
            if event.get("data"):
                data_text = json.dumps(event["data"], ensure_ascii=False)
                detail = f"{detail} {data_text}".strip() if detail not in (None, "") else data_text
            action = event.get("action") or ""
            if detail not in (None, ""):
                action = f"{action}  |  {format_result(detail)}"
            values = [
                f"{float(event.get('elapsed_seconds', 0)):.3f}s",
                event.get("character") or "",
                self._kind_text(event.get("kind")),
                event.get("condition") or "",
                format_result(event.get("result")),
                action,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column in (0, 2, 4):
                    item.setTextAlignment(Qt.AlignCenter)
                self.event_table.setItem(row, column, item)
        self.event_table.setUpdatesEnabled(True)
        self._update_pager()

    def change_page(self, offset):
        events = (self.current_session or {}).get("events") or []
        page_count = max(1, (len(events) + PAGE_SIZE - 1) // PAGE_SIZE)
        next_page = min(max(0, self.current_page + offset), page_count - 1)
        if next_page == self.current_page:
            return
        self.current_page = next_page
        self._render_page()

    def _update_pager(self):
        events = (self.current_session or {}).get("events") or []
        page_count = max(1, (len(events) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.current_page = min(max(0, self.current_page), page_count - 1)
        self.page_label.setText(
            f"第 {self.current_page + 1} / {page_count} 页，共 {len(events)} 条"
        )
        self.previous_page_button.setEnabled(bool(events) and self.current_page > 0)
        self.next_page_button.setEnabled(
            bool(events) and self.current_page + 1 < page_count
        )

    @staticmethod
    def _kind_text(kind):
        return {
            "decision": "条件判定",
            "action": "操作",
            "lifecycle": "战斗生命周期",
            "note": "备注",
        }.get(str(kind or ""), str(kind or ""))

    def open_log_folder(self):
        folder = Path(self.store.root)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))

