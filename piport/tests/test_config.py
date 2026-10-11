# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Unit tests for config.py's pure logic (no hardware, no network)."""
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402


class TestNetmask(unittest.TestCase):
    def test_common_masks(self):
        self.assertEqual(config.netmask_to_prefix("255.255.255.0"), 24)
        self.assertEqual(config.netmask_to_prefix("255.255.0.0"), 16)
        self.assertEqual(config.netmask_to_prefix("255.0.0.0"), 8)
        self.assertEqual(config.netmask_to_prefix("255.255.255.255"), 32)

    def test_invalid(self):
        self.assertIsNone(config.netmask_to_prefix(""))
        self.assertIsNone(config.netmask_to_prefix("not-a-mask"))
        self.assertIsNone(config.netmask_to_prefix("255.255.255"))


class TestMisc(unittest.TestCase):
    def test_get_bool(self):
        for v in ("1", "true", "TRUE", "yes", "on", " On "):
            self.assertTrue(config.get_bool({"x": v}, "x"), v)
        for v in ("0", "false", "no", "off", ""):
            self.assertFalse(config.get_bool({"x": v}, "x"), v)
        self.assertFalse(config.get_bool({}, "missing"))

    def test_sysid_int(self):
        self.assertEqual(config.sysid_int({"sysid": "0xa5f1"}), 0xA5F1)
        self.assertEqual(config.sysid_int({"sysid": "42481"}), 42481)


class TestResolveIface(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(config.resolve_iface("ap-main", {"iface": "eth0"}), "ap-main")

    def test_cfg_wins(self):
        self.assertEqual(config.resolve_iface(None, {"iface": "enP4p65s0"}), "enP4p65s0")

    def test_blank_auto_detects(self):
        # auto-detect must return a string, never None (falls back to "eth0")
        self.assertIsInstance(config.resolve_iface(None, {"iface": ""}), str)
        self.assertIsInstance(config.resolve_iface(), str)

    def test_default_iface_is_str(self):
        self.assertIsInstance(config._default_iface(), str)

    def test_parent_precedence(self):
        self.assertEqual(config.resolve_parent_iface(
            {"parent_iface": "enP4p65s0", "iface": "eth0"}), "enP4p65s0")
        self.assertEqual(config.resolve_parent_iface(
            {"parent_iface": "", "iface": "enP4p65s0"}), "enP4p65s0")
        self.assertIsInstance(config.resolve_parent_iface({"parent_iface": "", "iface": ""}), str)


class TestStreamDir(unittest.TestCase):
    def test_default_is_ram_backed(self):
        d = config.stream_dir()
        # falls back to "default" key; must be an absolute path, never the state dir
        self.assertTrue(os.path.isabs(d))
        self.assertEqual(config.stream_dir(), d)

    def test_per_state_dir_is_unique_and_stable(self):
        a = config.stream_dir("/srv/x/instances/main")
        b = config.stream_dir("/srv/x/instances/two")
        self.assertNotEqual(a, b)
        self.assertEqual(config.stream_dir("/srv/x/instances/main"), a)
        # relative vs absolute of the same state dir must agree
        os.chdir("/tmp")
        self.assertEqual(config.stream_dir("instances/main"),
                         config.stream_dir("/tmp/instances/main"))

    def test_tmpfs_base_writable(self):
        self.assertTrue(os.path.isdir(config._tmpfs_base()))


class TestLoadConfig(unittest.TestCase):
    def test_defaults_and_override(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as f:
            f.write("[identity]\nhostname = lab2\n[network]\niface = enP4p65s0\n")
            path = f.name
        try:
            cfg = config.load_config(path)
            self.assertEqual(cfg["hostname"], "lab2")
            self.assertEqual(cfg["iface"], "enP4p65s0")
            self.assertEqual(cfg["platform"], config.DEFAULTS["platform"])  # untouched key
        finally:
            os.unlink(path)

    def test_missing_file_falls_back(self):
        cfg = config.load_config("/nonexistent/path/aiport.cfg")
        self.assertEqual(cfg["platform"], "UVC AI Port")
        self.assertEqual(cfg["sysid"], "0xa5f1")

    def test_discovery_platform_is_the_sku(self):
        # Network's fingerprint catalogue knows the AI Port as "UP-AI-Port", not
        # the canonical Protect type "UVC AI Port".
        self.assertEqual(config.DEFAULTS["discovery_platform"], "UP-AI-Port")
        self.assertEqual(config.DEFAULTS["platform"], "UVC AI Port")


class TestFirmwarePin(unittest.TestCase):
    """A simulated update pins a version; every stage reports it over the configured one."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.pin = os.path.join(self.d, "fw_version.json")

    def test_missing_falls_back_to_configured(self):
        self.assertIsNone(config.pinned_fw_version(self.d))
        self.assertEqual(config.effective_fw_version("5.1.12.67", self.d), "5.1.12.67")

    def test_round_trip(self):
        config.atomic_write_json(self.pin, {"version": "5.9.9.9"})
        self.assertEqual(config.pinned_fw_version(self.d), "5.9.9.9")
        self.assertEqual(config.effective_fw_version("5.1.12.67", self.d), "5.9.9.9")

    def test_malformed_or_empty_is_unpinned(self):
        with open(self.pin, "w") as f:
            f.write("{not json")
        self.assertIsNone(config.pinned_fw_version(self.d))
        config.atomic_write_json(self.pin, {})
        self.assertIsNone(config.pinned_fw_version(self.d))

    def test_device_name_default_and_round_trip(self):
        self.assertEqual(config.device_name(self.d), "AI Port")
        config.set_device_name(self.d, "Front Door")
        self.assertEqual(config.device_name(self.d), "Front Door")


class TestRedact(unittest.TestCase):
    def test_rtsp_credentials(self):
        line = ("payload={'streamUrl': 'rtsp://onvif:s3cret@192.168.1.22/Streaming/Channels/101"
                "?transportmode=unicast', 'width': 3840}")
        out = config.redact(line)
        self.assertNotIn("s3cret", out)
        self.assertNotIn("onvif:", out)
        self.assertIn("rtsp://<redacted>@192.168.1.22/Streaming/Channels/101", out)

    def test_password_and_token_fields(self):
        out = config.redact('body={"username": "ui", "password": "FAKEPASS", "token": "FAKE"}')
        self.assertEqual(out, 'body={"username": "ui", "password": "<redacted>", '
                              '"token": "<redacted>"}')
        self.assertEqual(config.redact("headers={'X-Guid': 'g', 'X-Token': 'FAKE'}"),
                         "headers={'X-Guid': 'g', 'X-Token': '<redacted>'}")
        out = config.redact("payload={'mac': 'fcecda', 'adoptionCode': 'FAKE', 'ip': '1.2.3.4'}")
        self.assertNotIn("FAKE", out)
        self.assertIn("'ip': '1.2.3.4'", out)

    def test_token_query_and_label(self):
        self.assertEqual(config.redact("wss://h:7442/camera/1.0/ws?token=abc123 headers"),
                         "wss://h:7442/camera/1.0/ws?token=<redacted> headers")
        self.assertEqual(config.redact("  token:      FAKETOKEN0123456"), "  token:      <redacted>")
        self.assertEqual(config.redact("token FAKETOKEN0123456789 already used on a prior"),
                         "token <redacted> already used on a prior")
        self.assertEqual(config.redact("adopt token changed (FAKETOKEN012 -> Ab12Cd34Ef56Gh)"),
                         "adopt token changed (<redacted>)")
        self.assertEqual(config.redact("reconnecting tokenless (fingerprint pinned)"),
                         "reconnecting tokenless (fingerprint pinned)")

    def test_leaves_ordinary_text(self):
        for line in ("connecting to wss://192.168.1.2:7442/ with headers={'X-Ident': 'fc'}",
                     "starting merged RTSP pull for deviceID=D: rtsp://10.0.0.1:7447/abc"):
            self.assertEqual(config.redact(line), line)

    def test_filter_rewrites_record(self):
        import logging
        rec = logging.LogRecord("t", logging.INFO, __file__, 1, "url=%s n=%d",
                                ("rtsp://u:p@cam/1", 3), None)
        self.assertTrue(config.RedactFilter().filter(rec))
        self.assertEqual(rec.getMessage(), "url=rtsp://<redacted>@cam/1 n=3")


if __name__ == "__main__":
    unittest.main()
