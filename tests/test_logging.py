"""M1 Gate: 结构化 JSON 日志 + request_id + 敏感字段脱敏。"""
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("dispatch_logging", ROOT / "logging_util.py")
logging_util = importlib.util.module_from_spec(_spec)
sys.modules["dispatch_logging"] = logging_util
_spec.loader.exec_module(logging_util)


class LoggingTest(unittest.TestCase):
    def test_request_id_unique(self):
        ids = {logging_util.new_request_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(len(x) == 12 for x in ids))

    def test_structured_output(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            logging_util.info("task_claimed", "rid123", task_id=7)
        rec = json.loads(buf.getvalue().strip())
        self.assertEqual(rec["event"], "task_claimed")
        self.assertEqual(rec["request_id"], "rid123")
        self.assertEqual(rec["task_id"], 7)
        self.assertEqual(rec["level"], "info")
        self.assertIn("ts", rec)

    def test_sensitive_fields_masked(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            logging_util.info("pair", "r", token="SECRET123", code="ABCD-EFGH", name="a")
        rec = json.loads(buf.getvalue().strip())
        self.assertEqual(rec["token"], "***")
        self.assertEqual(rec["code"], "***")
        self.assertEqual(rec["name"], "a")


if __name__ == "__main__":
    unittest.main(verbosity=2)
