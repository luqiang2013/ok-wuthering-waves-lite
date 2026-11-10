import json
import logging
import tempfile
import unittest
from pathlib import Path

from src.char.BaseChar import BaseChar
from src.task.BattleLog import BattleLogRecorder, BattleLogStore
from src.task.BaseCombatTask import BaseCombatTask


class FakeChar:
    def __init__(self, name, char_name):
        self.name = name
        self.char_name = char_name


class FakeTask:
    def __init__(self, recorder, chars):
        self.battle_log = recorder
        self.chars = chars


class NoopChar(BaseChar):
    def do_perform(self):
        pass


class TestBattleLog(unittest.TestCase):
    def test_battle_writes_one_complete_json_session(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = BattleLogRecorder("TestCombat", root=folder)
            chars = [FakeChar("Hiyuki", "char_hiyuki"), FakeChar("Chisa", "char_chisa")]

            self.assertTrue(recorder.ensure_started(chars, {"mode": "reference"}))
            self.assertFalse(recorder.ensure_started(chars))
            recorder.record_decision(
                chars[0],
                "共鸣解放可用",
                True,
                "长按解放",
                detail={"count": 4},
            )
            recorder.record_action(chars[1], "普通攻击", detail={"duration": 0.8})
            path = recorder.finish("completed")

            self.assertIsNotNone(path)
            self.assertTrue(path.is_file())
            self.assertFalse(recorder.active)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["task"], "TestCombat")
            self.assertEqual(data["team"], ["Hiyuki (char_hiyuki)", "Chisa (char_chisa)"])
            self.assertEqual(data["outcome"], "completed")
            self.assertEqual(data["events"][0]["condition"], "进入战斗")
            self.assertTrue(any(event["condition"] == "共鸣解放可用" for event in data["events"]))
            self.assertTrue(any(event["action"] == "普通攻击" for event in data["events"]))
            self.assertEqual(data["events"][-1]["condition"], "结束战斗")
            self.assertEqual(data["event_count"], len(data["events"]))
            self.assertEqual(list(Path(folder).glob("*.active.jsonl")), [])

            sessions = BattleLogStore(folder).list_sessions()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["session_id"], data["session_id"])

    def test_unfinished_battle_is_recovered_as_interrupted(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = BattleLogRecorder("TestCombat", root=folder)
            recorder.ensure_started([FakeChar("Hiyuki", "char_hiyuki")])
            recorder.record_decision("Hiyuki", "四居合计数", 3, "继续输出")

            sessions = BattleLogStore(folder).list_sessions()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["outcome"], "interrupted")
            self.assertEqual(sessions[0]["event_count"], 2)
            self.assertIn("Hiyuki", sessions[0]["team"][0])
            recorder.set_enabled(False)

    def test_store_deletes_only_the_selected_log(self):
        with tempfile.TemporaryDirectory() as folder:
            first = BattleLogRecorder("First", root=folder)
            first.ensure_started([])
            first_path = first.finish()
            second = BattleLogRecorder("Second", root=folder)
            second.ensure_started([])
            second_path = second.finish()

            store = BattleLogStore(folder)
            self.assertEqual(len(store.list_sessions()), 2)
            store.delete_session(first_path)
            sessions = store.list_sessions()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["session_id"], second_path.stem)
            self.assertFalse(Path(first_path).exists())

    def test_duplicate_decisions_inside_window_are_collapsed(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = BattleLogRecorder("TestCombat", root=folder)
            recorder.ensure_started([FakeChar("Lucilla", "char_lucilla")])
            recorder.record_decision("Lucilla", "解放可用", False, "继续蓄能")
            recorder.record_decision("Lucilla", "解放可用", False, "继续蓄能")
            path = recorder.finish()
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            matches = [
                event for event in data["events"]
                if event["condition"] == "解放可用"
            ]
            self.assertEqual(len(matches), 1)

    def test_finish_without_active_battle_is_a_noop(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = BattleLogRecorder("TestCombat", root=folder)
            self.assertIsNone(recorder.finish())
            self.assertEqual(BattleLogStore(folder).list_sessions(), [])

    def test_first_character_perform_starts_the_session(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = BattleLogRecorder("TestCombat", root=folder)
            task = FakeTask(recorder, [])
            char = object.__new__(NoopChar)
            char.task = task
            char.index = 0
            char.has_intro = False
            char.current_con = 0
            char.logger = logging.getLogger("test-battle-log")

            char.perform()

            self.assertTrue(recorder.active)
            events = recorder.snapshot()["events"]
            self.assertTrue(any(event["condition"] == "角色轮转开始" for event in events))
            recorder.finish()

    def test_team_is_filled_when_it_becomes_available(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = BattleLogRecorder("TestCombat", root=folder)
            recorder.ensure_started([])
            recorder.ensure_started([FakeChar("Chisa", "char_chisa")])
            snapshot = recorder.snapshot()
            self.assertEqual(snapshot["team"], ["Chisa (char_chisa)"])
            self.assertEqual(snapshot["events"][0]["detail"], "Chisa (char_chisa)")
            recorder.set_enabled(False)

    def test_combat_end_finishes_the_active_session(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = BattleLogRecorder("TestCombat", root=folder)
            recorder.ensure_started([])
            task = object.__new__(BaseCombatTask)
            task.battle_log = recorder
            task._battle_outcome = "left_combat"
            task._battle_outcome_detail = "test"
            task.chars = []
            task.get_current_char = lambda raise_exception=False: None
            task.combat_end()
            self.assertFalse(recorder.active)
            self.assertEqual(len(BattleLogStore(folder).list_sessions()), 1)

    def test_disabled_recorder_writes_nothing_and_can_be_reenabled(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = BattleLogRecorder("TestCombat", root=folder)
            recorder.ensure_started([])
            recorder.record_action("Hiyuki", "普通攻击")
            recorder.set_enabled(False)
            self.assertFalse(recorder.active)
            self.assertIsNone(recorder.finish())
            self.assertEqual(BattleLogStore(folder).list_sessions(), [])

            recorder.set_enabled(True)
            self.assertTrue(recorder.ensure_started([]))
            recorder.record_action("Hiyuki", "普通攻击")
            self.assertIsNotNone(recorder.finish())
            self.assertEqual(len(BattleLogStore(folder).list_sessions()), 1)

    def test_combat_setting_disables_an_already_active_recorder(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = BattleLogRecorder("TestCombat", root=folder)
            recorder.ensure_started([])
            task = object.__new__(BaseCombatTask)
            task.battle_log = recorder
            task.chars = []
            task.config = {"启用战斗日志": False}
            self.assertFalse(task.sync_battle_log_enabled())
            self.assertFalse(recorder.active)
            self.assertEqual(BattleLogStore(folder).list_sessions(), [])


if __name__ == "__main__":
    unittest.main()
