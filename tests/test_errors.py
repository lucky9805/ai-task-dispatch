"""M1 Gate: 类型化错误与 HTTP 映射。"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("dispatch_errors", ROOT / "errors.py")
errors = importlib.util.module_from_spec(_spec)
sys.modules["dispatch_errors"] = errors
_spec.loader.exec_module(errors)


class ErrorsTest(unittest.TestCase):
    def test_hierarchy_and_status(self):
        self.assertEqual(errors.ValidationError("x").http_status, 400)
        self.assertEqual(errors.AuthError("x").http_status, 401)
        self.assertEqual(errors.PermissionDeniedError("x").http_status, 403)
        self.assertEqual(errors.NotFoundError("x").http_status, 404)
        self.assertEqual(errors.ConflictError("x").http_status, 409)

    def test_as_dict(self):
        e = errors.ValidationError("名字不能为空")
        self.assertEqual(e.as_dict(), {"error": "名字不能为空", "code": "invalid_argument"})

    def test_override_code_and_status(self):
        e = errors.DispatchError("x", code="custom", http_status=418)
        self.assertEqual(e.as_dict()["code"], "custom")
        self.assertEqual(e.http_status, 418)

    def test_to_http_known(self):
        status, body = errors.to_http(errors.NotFoundError("没找到"))
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")

    def test_to_http_unknown_no_leak(self):
        status, body = errors.to_http(RuntimeError("secret internal detail"))
        self.assertEqual(status, 500)
        self.assertNotIn("secret", str(body))
        self.assertEqual(body["code"], "internal_error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
