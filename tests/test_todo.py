import importlib.util
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TODO_SCRIPT = PROJECT_ROOT / "scripts" / "todo.py"

spec = importlib.util.spec_from_file_location("chronos_todo", TODO_SCRIPT)
todo_module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(todo_module)


class TodoHelpersTests(unittest.TestCase):
    def test_parse_entry_identifier_accepts_prefixed_ids(self):
        self.assertEqual(todo_module.parse_entry_identifier("ID45"), 45)
        self.assertEqual(todo_module.parse_entry_identifier("45"), 45)

    def test_parse_compact_end_date_supports_yymmdd(self):
        self.assertEqual(todo_module.parse_compact_end_date("260630"), "2026-06-30")
        self.assertEqual(todo_module.parse_compact_end_date("20260630"), "2026-06-30")
        self.assertIsNone(todo_module.parse_compact_end_date("20261340"))

    def test_natural_language_parser_extracts_compact_end_date(self):
        parsed = todo_module.parse_natural_language("添加任务 每周三 10:00 周三抢券 结束日期260630")

        self.assertEqual(parsed["cmd"], "add")
        self.assertEqual(parsed["cycle_type"], "weekly")
        self.assertEqual(parsed["weekday"], 2)
        self.assertEqual(parsed["time_of_day"], "10:00")
        self.assertEqual(parsed["end_date"], "2026-06-30")


if __name__ == "__main__":
    unittest.main()
