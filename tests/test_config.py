"""M1 Gate: config 集中配置与校验。"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("dispatch_config", ROOT / "config.py")
config = importlib.util.module_from_spec(_spec)
sys.modules["dispatch_config"] = config
_spec.loader.exec_module(config)


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        c = config.load({})
        self.assertEqual(c.host, "127.0.0.1")
        self.assertEqual(c.port, 8765)
        self.assertEqual(c.lease_seconds, 120.0)
        self.assertEqual(c.pair_ttl, 600.0)
        self.assertEqual(c.token, "")

    def test_env_override(self):
        c = config.load({"DISPATCH_PORT": "9000", "DISPATCH_LEASE": "5.5",
                         "DISPATCH_TOKEN": "tk", "DISPATCH_DB": "/tmp/x.db"})
        self.assertEqual(c.port, 9000)
        self.assertEqual(c.lease_seconds, 5.5)
        self.assertEqual(c.token, "tk")
        self.assertEqual(c.db_path, Path("/tmp/x.db"))

    def test_bad_number_fails_fast(self):
        with self.assertRaises(ValueError):
            config.load({"DISPATCH_PORT": "abc"})
        with self.assertRaises(ValueError):
            config.load({"DISPATCH_LEASE": "nope"})

    def test_validate_rejects_invalid(self):
        for env in ({"DISPATCH_LEASE": "0"}, {"DISPATCH_PAIR_TTL": "-1"},
                    {"DISPATCH_PORT": "99999"}, {"DISPATCH_ONLINE_WINDOW": "0"}):
            with self.assertRaises(ValueError):
                config.load(env)

    def test_frozen(self):
        c = config.load({})
        with self.assertRaises(Exception):
            c.port = 1234


if __name__ == "__main__":
    unittest.main(verbosity=2)
