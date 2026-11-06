import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clutch.service import ConversionService
from clutch.updater import normalize_channel


def _make_service(db_path, **kwargs):
    service = ConversionService(db_path, **kwargs)
    service.stop()
    service.wait()
    return service


class NormalizeChannelTests(unittest.TestCase):
    def test_only_dev_maps_to_dev(self):
        self.assertEqual(normalize_channel("dev"), "dev")
        self.assertEqual(normalize_channel("DEV"), "dev")
        self.assertEqual(normalize_channel(" dev "), "dev")

    def test_anything_else_maps_to_stable(self):
        for value in ("", None, "stable", "nightly", "latest"):
            self.assertEqual(normalize_channel(value), "stable")


class ServiceUpdateChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "service.db")

    def test_defaults_to_stable(self):
        service = _make_service(self.db)
        self.assertEqual(service.get_update_channel(), "stable")

    def test_explicit_channel_is_applied_and_persisted(self):
        service = _make_service(self.db, update_channel="dev")
        self.assertEqual(service.get_update_channel(), "dev")

        # Restarting without an explicit channel keeps the persisted value.
        restarted = _make_service(self.db)
        self.assertEqual(restarted.get_update_channel(), "dev")

    def test_explicit_channel_overrides_persisted_value(self):
        _make_service(self.db, update_channel="dev")
        service = _make_service(self.db, update_channel="stable")
        self.assertEqual(service.get_update_channel(), "stable")

    def test_invalid_channel_falls_back_to_stable(self):
        service = _make_service(self.db, update_channel="nightly")
        self.assertEqual(service.get_update_channel(), "stable")


if __name__ == "__main__":
    unittest.main()
