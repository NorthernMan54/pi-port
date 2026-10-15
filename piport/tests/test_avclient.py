# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Unit tests for avclient.py's pure logic: geometry, zone/line
parsing + crossing, snapshots. No sockets/device; ffmpeg tests skipped if absent.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import avclient as av  # noqa: E402
import config  # noqa: E402

_FFMPEG = shutil.which(av.FFMPEG_BIN) or (av.FFMPEG_BIN if os.path.exists(av.FFMPEG_BIN) else None)

SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]  # note: normalized 0-1000 in real use


def make_frame(path, w=1920, h=1080):
    subprocess.run([av.FFMPEG_BIN, "-loglevel", "error", "-f", "lavfi",
                    "-i", f"testsrc=size={w}x{h}", "-frames:v", "1", "-y", path],
                   check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        av._lines.clear()
        av._detect_zones.clear()
        av._exclude_zones.clear()
        av._snapshot_filename_to_device.clear()
        av._recent_snapshot_announcements.clear()
        av._last_object_coord_by_device.clear()
        av._snapshot_seq_by_device.clear()
        av._stream_dims.clear()
        av._active_streams.clear()
        self.sent = []
        self._orig_send = av.send_msg
        av.send_msg = lambda ws, fn, payload, **kw: self.sent.append((fn, payload))
        self.addCleanup(lambda: setattr(av, "send_msg", self._orig_send))


class TestGeometry(unittest.TestCase):
    def test_point_in_polygon(self):
        self.assertTrue(av._point_in_polygon(5, 5, SQUARE))
        self.assertFalse(av._point_in_polygon(15, 5, SQUARE))
        self.assertFalse(av._point_in_polygon(-1, 5, SQUARE))
        # concave polygon (an L) -- the ray-cast must not treat the notch as inside
        L = [(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)]
        self.assertTrue(av._point_in_polygon(2, 2, L))
        self.assertFalse(av._point_in_polygon(8, 8, L))

    def test_zone_center_norm(self):
        self.assertEqual(av._zone_center_norm([0, 0, 10, 20]), (5.0, 10.0))
        self.assertEqual(av._zone_center_norm([100, 200, 50, 50]), (125.0, 225.0))

    def test_iou(self):
        self.assertAlmostEqual(av._iou([0, 0, 10, 10], [0, 0, 10, 10]), 1.0)
        self.assertEqual(av._iou([0, 0, 10, 10], [100, 100, 10, 10]), 0.0)
        # half-overlap on x: inter=50, union=150
        self.assertAlmostEqual(av._iou([0, 0, 10, 10], [5, 0, 10, 10]), 50 / 150)

    def test_line_side_sign(self):
        p1, p2 = (0, 500), (1000, 500)
        self.assertLess(av._line_side(p1, p2, (500, 100)), 0)
        self.assertGreater(av._line_side(p1, p2, (500, 900)), 0)
        self.assertEqual(av._line_side(p1, p2, (500, 500)), 0)

    def test_segment_crosses_line(self):
        p1, p2 = (0, 500), (1000, 500)
        self.assertTrue(av._segment_crosses_line((500, 100), (500, 900), p1, p2))
        self.assertTrue(av._segment_crosses_line((500, 900), (500, 100), p1, p2))
        self.assertFalse(av._segment_crosses_line((500, 100), (600, 200), p1, p2))
        self.assertFalse(av._segment_crosses_line((100, 600), (900, 600), p1, p2))
        # movement crosses y=500 but outside the segment's x-span
        self.assertFalse(av._segment_crosses_line((900, 100), (900, 900), (0, 500), (100, 500)))


class TestParsers(Base):
    def test_parse_zones(self):
        field = {"1": {"coord": [0, 0, 10, 0, 10, 10, 0, 10]},
                 "2": {"coord": [1, 2, 3, 4]},          # too few points -> skipped
                 "3": {"coord": [0, 0, 1, 1, 2, 2, 3]}}  # odd -> skipped
        polys = av._parse_zones(field)
        self.assertEqual(len(polys), 1)
        self.assertEqual(polys[0], [(0, 0), (10, 0), (10, 10), (0, 10)])
        self.assertEqual(av._parse_zones(None), [])

    def test_parse_lines_coord_and_points(self):
        lines = av._parse_lines({
            "1": {"coord": [0, 500, 1000, 500], "objectTypes": ["person"]},
            "2": {"points": [[100, 0], [100, 1000]]},
            "3": {"coord": [1, 2]},  # skipped
        })
        self.assertEqual([l["id"] for l in lines], ["1", "2"])
        self.assertEqual((lines[0]["p1"], lines[0]["p2"]), ((0, 500), (1000, 500)))
        self.assertEqual(lines[0]["object_types"], ["person"])
        self.assertEqual(lines[1]["p2"], (100, 1000))

    def test_line_ref(self):
        self.assertEqual(av._line_ref("1"), 1)
        self.assertEqual(av._line_ref("a"), "a")


class TestZoneFiltering(Base):
    def test_exclude_and_detect_zones(self):
        av._exclude_zones["M"] = [SQUARE]
        self.assertTrue(av._is_excluded("M", [4, 4, 2, 2]))    # center (5,5)
        self.assertFalse(av._is_excluded("M", [40, 40, 2, 2]))  # center (41,41)
        self.assertFalse(av._is_excluded("OTHER", [4, 4, 2, 2]))  # no zones -> not excluded

        av._detect_zones["M2"] = [SQUARE]
        self.assertFalse(av._is_outside_detect_zones("M2", [4, 4, 2, 2]))
        self.assertTrue(av._is_outside_detect_zones("M2", [40, 40, 2, 2]))
        self.assertFalse(av._is_outside_detect_zones("M3", [40, 40, 2, 2]))  # unset -> no restriction


class TestLineCrossing(Base):
    LINE = {"id": "1", "p1": (0, 500), "p2": (1000, 500),
            "direction": None, "object_types": None}

    def _track(self, prev):
        return {"event_id": 7, "objectType": "person", "coord": [480, 80, 40, 40],
                "score": 0.9, "first_shown_ms": 1000, "miss_count": 0,
                "prev_center": prev, "crossed_lines": {}}

    def test_enter_then_moving_and_counters(self):
        av._lines["M"] = [self.LINE]
        tr = self._track((500, 100))
        av._process_line_crossings(None, "M", 3, tr, (500, 900))  # neg -> pos
        p = self.sent[-1][1]
        self.assertEqual(self.sent[-1][0], "EventSmartDetect")
        self.assertEqual(p["edgeType"], "enter")
        self.assertIn("linesStatus", p)
        self.assertNotIn("zonesStatus", p)
        self.assertEqual(p["descriptors"][0]["crosslineA2B"], 1)
        self.assertEqual(p["descriptors"][0]["crosslineB2A"], 0)
        self.assertEqual(p["descriptors"][0]["crosslineA2BAdd"], 1)
        ev = p["eventId"]

        # _motion_tick advances prev_center between ticks; do the same here
        tr["prev_center"] = (500, 900)
        av._process_line_crossings(None, "M", 3, tr, (500, 100))  # pos -> neg
        p = self.sent[-1][1]
        self.assertEqual(p["edgeType"], "moving")
        self.assertEqual(p["eventId"], ev)  # same track event
        self.assertEqual(p["linesStatus"]["1"]["direction"], "B2A")

    def test_no_crossing_emits_nothing(self):
        av._lines["M"] = [self.LINE]
        tr = self._track((500, 100))
        av._process_line_crossings(None, "M", 3, tr, (520, 110))
        self.assertEqual(self.sent, [])

    def test_object_type_filter(self):
        av._lines["M"] = [dict(self.LINE, object_types=["vehicle"])]
        tr = self._track((500, 100))
        av._process_line_crossings(None, "M", 3, tr, (500, 900))
        self.assertEqual(self.sent, [])

    def test_leave_payload_and_filenames(self):
        av._stream_dims["M"] = (1920, 1080)
        av._send_line_detect_event(None, "M", self.LINE, "leave", 42, "A2B", 1, 2,
                                   "person", [480, 80, 40, 40], 0.9, 1000, 3)
        _, p = self.sent[-1]
        self.assertEqual(p["edgeType"], "leave")
        self.assertEqual(p["trackerIDAttrMap"]["3"], {"objectType": "person", "line": [1]})
        self.assertTrue(p["smartDetectSnapshotFullFoV"].startswith("smartdetectsnap_line_"))
        self.assertTrue(p["smartDetectSnapshotFullFoV"].endswith("_fullfov.jpg"))
        self.assertEqual(p["smartDetectSnapshots"][0]["smartDetectSnapshotWidth"],
                         av.SMART_SNAPSHOT_SIZE)
        for fname in (p["smartDetectSnapshotFullFoV"],
                      p["smartDetectSnapshots"][0]["smartDetectSnapshot"]):
            self.assertEqual(av._snapshot_filename_to_device[fname]["device_id"], "M")
        json.dumps(p)  # must be JSON-serialisable


class TestSnapshotResolution(Base):
    def test_resolve_by_filename(self):
        av._record_snapshot_filename("f.jpg", "AAA", kind="fullfov")
        rec, how = av._resolve_snapshot_device("f.jpg", None)
        self.assertEqual((rec["device_id"], how), ("AAA", "filename"))

    def test_resolve_sole_active_stream(self):
        av._active_streams["AAA"] = object()
        rec, how = av._resolve_snapshot_device(None, "smartDetectZoneSnapshot")
        self.assertEqual((rec["device_id"], how), ("AAA", "sole active stream"))

    def test_resolve_sole_recent_announcement(self):
        av._active_streams["AAA"] = object()
        av._active_streams["BBB"] = object()
        av._record_snapshot_filename("b.jpg", "BBB", kind="object", coord=[1, 2, 3, 4])
        rec, how = av._resolve_snapshot_device(None, "smartDetectZoneSnapshot")
        self.assertEqual((rec["device_id"], how), ("BBB", "sole recent announcement"))
        self.assertEqual(rec["coord"], [1, 2, 3, 4])  # reuses last object coord

    def test_resolve_ambiguous_refuses(self):
        av._active_streams["AAA"] = object()
        av._active_streams["BBB"] = object()
        av._record_snapshot_filename("a.jpg", "AAA", kind="object", coord=[1, 2, 3, 4])
        av._record_snapshot_filename("b.jpg", "BBB", kind="object", coord=[1, 2, 3, 4])
        rec, how = av._resolve_snapshot_device(None, "smartDetectZoneSnapshot")
        self.assertIsNone(rec)
        self.assertIsNone(how)

    def test_fallback_kind_from_what(self):
        self.assertEqual(av._fallback_record("X", "smartDetectZoneSnapshotFullFoV")["kind"], "fullfov")
        self.assertEqual(av._fallback_record("X", "smartDetectZoneSnapshot")["kind"], "object")

    def test_resolve_by_request_deviceid(self):
        av._active_streams["AA:BB:CC"] = object()
        rec, how = av._resolve_snapshot_device(None, "snapshot", "aa:bb:cc")
        self.assertEqual((rec["device_id"], how), ("AA:BB:CC", "request deviceID"))
        self.assertEqual(rec["kind"], "fullfov")

    def test_general_snapshot_not_cropped_by_stale_coord(self):
        # A general "snapshot" must not reuse a stale per-object coord as a crop.
        av._last_object_coord_by_device["AAA"] = [1, 2, 3, 4]
        rec = av._fallback_record("AAA", "snapshot")
        self.assertEqual(rec["kind"], "fullfov")
        self.assertIsNone(rec["coord"])

    def test_wait_for_frame(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "no.jpg")
            t = time.monotonic()
            self.assertFalse(av._wait_for_frame(missing, timeout=0.4))
            self.assertGreaterEqual(time.monotonic() - t, 0.3)
            late = os.path.join(d, "late.jpg")
            threading.Timer(0.2, lambda: open(late, "wb").write(b"x")).start()
            self.assertTrue(av._wait_for_frame(late, timeout=2.0))


@unittest.skipUnless(_FFMPEG, "ffmpeg not installed")
class TestSnapshotCrop(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._dir, True)
        self.path = os.path.join(self._dir, "f.jpg")
        make_frame(self.path, 1504, 2016)

    def test_jpeg_size(self):
        self.assertEqual(av._jpeg_size(self.path), (1504, 2016))
        bogus = os.path.join(self._dir, "x.jpg")
        with open(bogus, "wb") as f:
            f.write(b"not a jpeg")
        self.assertIsNone(av._jpeg_size(bogus))

    def test_crop_uses_real_frame_size(self):
        # Announced 1920x2560 but delivered 1504x2016 (seen live on a doorbell):
        # the crop must fit the real frame instead of failing.
        out = av._crop_snapshot_jpeg(self.path, [308, 346, 306, 620], 1920, 2560)
        self.assertTrue(out and out.startswith(b"\xff\xd8"))


class TestSnapshotUpload(Base):
    class _Resp:
        status_code = 200
        text = "ok"

    def _with_stream(self, *devices):
        self._dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._dir, True)
        for dev in devices:
            with open(os.path.join(self._dir, f"{dev}.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xd9" + dev.encode())
        self._orig_dir = av._STREAM_DIR
        av._STREAM_DIR = self._dir
        self.addCleanup(lambda: setattr(av, "_STREAM_DIR", self._orig_dir))
        self.posted = []
        self._orig_post = av.requests.post
        av.requests.post = lambda uri, headers=None, files=None, **kw: (
            self.posted.append(files["payload"][1]) or self._Resp())
        self.addCleanup(lambda: setattr(av.requests, "post", self._orig_post))

    INFO = {"mac_nosep": "X", "type": "UVC AI Port", "sysid_hex": "0xa5f1"}

    def test_fullfov_uploads_full_frame(self):
        self._with_stream("AAA")
        av._record_snapshot_filename("full.jpg", "AAA", kind="fullfov")
        av._upload_snapshot("https://c/upload/t", self.INFO, "full.jpg", "smartDetectZoneSnapshotFullFoV")
        self.assertEqual(len(self.posted), 1)
        self.assertTrue(self.posted[0].endswith(b"AAA"))

    def test_unknown_ambiguous_refuses(self):
        self._with_stream("AAA", "BBB")
        av._active_streams["AAA"] = object()
        av._active_streams["BBB"] = object()
        av._upload_snapshot("https://c/upload/t", self.INFO, None, "smartDetectZoneSnapshot")
        self.assertEqual(self.posted, [])

    def test_unknown_sole_stream_falls_back(self):
        self._with_stream("AAA")
        av._active_streams["AAA"] = object()
        av._upload_snapshot("https://c/upload/t", self.INFO, None, "smartDetectZoneSnapshot")
        self.assertEqual(len(self.posted), 1)
        self.assertTrue(self.posted[0].endswith(b"AAA"))

    def test_no_uri_noop(self):
        self._with_stream("AAA")
        av._upload_snapshot(None, self.INFO, "x", "snapshot")
        self.assertEqual(self.posted, [])


class TestCrop(unittest.TestCase):
    @unittest.skipUnless(_FFMPEG, "ffmpeg not available")
    def test_crop_produces_square(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "frame.jpg")
            make_frame(src)
            data = av._crop_snapshot_jpeg(src, [400, 400, 200, 200], 1920, 1080)
            self.assertIsNotNone(data)
            self.assertTrue(data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"))
            # Assert the JPEG is non-trivial and exercise crop math for corner
            # cases rather than re-decoding dimensions.
            for coord in ([0, 0, 100, 150], [950, 950, 50, 50], [0, 0, 1000, 1000], [500, 500, 5, 5]):
                self.assertIsNotNone(av._crop_snapshot_jpeg(src, coord, 1920, 1080), coord)

    @unittest.skipUnless(_FFMPEG, "ffmpeg not available")
    def test_crop_bad_input_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "bad.jpg")
            open(bad, "wb").write(b"not a jpeg")
            self.assertIsNone(av._crop_snapshot_jpeg(bad, [1, 2, 3, 4], 1920, 1080))


class TestConfigHandler(Base):
    INFO = {"mac_nosep": "X", "type": "UVC AI Port", "sysid_hex": "0xa5f1"}

    def test_change_smart_detect_settings(self):
        msg = {"functionName": "ChangeSmartDetectSettings", "messageId": 1,
               "responseExpected": True,
               "payload": {"deviceID": "MAC", "zones": {}, "excludeZones": {},
                           "lines": {"1": {"coord": [0, 250, 1000, 250]},
                                     "2": {"coord": [250, 0, 250, 1000]}}}}
        av.handle_function(None, msg, self.INFO)
        self.assertEqual(len(av._lines["MAC"]), 2)
        self.assertEqual(av._lines["MAC"][0]["p1"], (0, 250))
        self.assertEqual(self.sent[-1][0], "ChangeSmartDetectSettings")


class TestEventDedupe(Base):
    def setUp(self):
        super().setUp()
        av._last_status_sent.clear()
        av._feature_flags_sent.clear()

    def test_status_event_sent_only_on_change(self):
        av._send_status_event(None, "D", True, True, True, True)
        av._send_status_event(None, "D", True, True, True, True)
        self.assertEqual(len(self.sent), 1)
        av._send_status_event(None, "D", True, False, True, True)  # stream stopped
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[-1][0], "EventAIPortStatus")

    def test_feature_flags_sent_once_per_connection(self):
        av._send_feature_flags_event(None, "D")
        av._send_feature_flags_event(None, "D")
        self.assertEqual(len(self.sent), 1)
        av._feature_flags_sent.clear()  # simulates the next controller connection
        av._send_feature_flags_event(None, "D")
        self.assertEqual(len(self.sent), 2)


class TestStreamClocks(Base):
    def test_stream_clocks_zero_before_start(self):
        self.assertEqual(av._stream_clocks("nope"), (0, 0))

    def test_stream_clocks_advance_after_start(self):
        av._stream_started_monotonic_ms["D"] = int(time.monotonic() * 1000) - 1500
        mono, stream = av._stream_clocks("D")
        self.assertGreaterEqual(mono, 1400)
        self.assertEqual(mono, stream)  # same timebase, like a real camera

    def test_stop_stream_clears_clock_origin(self):
        av._active_streams["D"] = None
        av._stream_started_monotonic_ms["D"] = int(time.monotonic() * 1000)
        av._stop_stream("D")
        self.assertNotIn("D", av._stream_started_monotonic_ms)


class TestDetectionDedupe(Base):
    def test_overlapping_same_type_collapse_to_one(self):
        dets = [
            {"objectType": "person", "score": 0.67, "coord": [100, 100, 200, 400]},
            {"objectType": "person", "score": 0.51, "coord": [110, 110, 195, 390]},
        ]
        out = av._dedupe_detections(dets)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["score"], 0.67)

    def test_distinct_objects_survive(self):
        dets = [
            {"objectType": "person", "score": 0.67, "coord": [0, 0, 100, 100]},
            {"objectType": "person", "score": 0.60, "coord": [500, 500, 100, 100]},
        ]
        self.assertEqual(len(av._dedupe_detections(dets)), 2)

    def test_different_types_not_merged(self):
        # A person box overlapping an animal box is genuinely two objects.
        dets = [
            {"objectType": "person", "score": 0.67, "coord": [100, 100, 200, 400]},
            {"objectType": "animal", "score": 0.51, "coord": [110, 110, 195, 390]},
        ]
        self.assertEqual(len(av._dedupe_detections(dets)), 2)


class TestMotionLoopLifecycle(Base):
    def setUp(self):
        super().setUp()
        av._motion_poll_stop.clear()
        av._motion_poll_thread.clear()
        # Make ticks observable without running the detector.
        self.ticks = []
        self._orig_tick = av._motion_tick
        av._motion_tick = lambda ws, dev: self.ticks.append(dev)
        self.addCleanup(lambda: setattr(av, "_motion_tick", self._orig_tick))
        self._orig_interval = av.MOTION_POLL_INTERVAL_S
        av.MOTION_POLL_INTERVAL_S = 0.05
        self.addCleanup(lambda: setattr(av, "MOTION_POLL_INTERVAL_S", self._orig_interval))
        self.addCleanup(lambda: av._stop_motion_detector("D"))

    def test_restart_does_not_leave_overlapping_loops(self):
        # A stream restart calls _start_motion_detector again; the previous loop
        # must be stopped AND joined, or both mutate the same track state.
        av._start_motion_detector(None, "D", "ip", 1, "uri")
        av._start_motion_detector(None, "D", "ip", 1, "uri")  # simulate restart
        self.assertEqual(len(av._motion_poll_stop), 1)
        self.assertEqual(len(av._motion_poll_thread), 1)
        live = av._motion_poll_thread["D"]
        self.assertTrue(live.is_alive())
        av._stop_motion_detector("D")
        # _stop pops the handle as part of joining it, so assert on the captured
        # thread, not on the (now-absent) dict entry.
        self.assertFalse(live.is_alive())
        self.assertNotIn("D", av._motion_poll_stop)
        self.assertNotIn("D", av._motion_poll_thread)

    def test_superseded_loop_exits(self):
        av._start_motion_detector(None, "D", "ip", 1, "uri")
        first = av._motion_poll_thread["D"]
        av._start_motion_detector(None, "D", "ip", 1, "uri")
        time.sleep(0.2)
        self.assertFalse(first.is_alive())  # old loop bailed out, not still ticking
        av._stop_motion_detector("D")

    def test_preserve_tracks_keeps_open_tracks(self):
        # A UiStreamControl refresh must not abandon an in-flight track, or a
        # line crossing can never accumulate a movement segment.
        av._motion_state["D"] = {"tracks": {1: {"event_id": 1, "objectType": "person",
                                                 "coord": [0, 0, 10, 10], "score": 0.9,
                                                 "first_shown_ms": 1, "miss_count": 0,
                                                 "prev_center": (5, 5), "crossed_lines": {}}},
                                  "next_tracker_id": 2}
        av._start_motion_detector(None, "D", "ip", 1, "uri", preserve_tracks=True)
        self.assertIn("D", av._motion_state)
        self.assertIn(1, av._motion_state["D"]["tracks"])
        av._stop_motion_detector("D")

    def test_discard_emits_leave_for_open_tracks(self):
        sent = []
        self._orig_send = av._send_smart_detect_event
        av._send_smart_detect_event = lambda ws, dev, **kw: sent.append((dev, kw["edge_type"]))
        self.addCleanup(lambda: setattr(av, "_send_smart_detect_event", self._orig_send))
        av._motion_state["D"] = {"tracks": {7: {"event_id": 42, "objectType": "person",
                                                 "coord": [0, 0, 10, 10], "score": 0.9,
                                                 "first_shown_ms": 1, "miss_count": 0,
                                                 "prev_center": (5, 5), "crossed_lines": {}}},
                                  "next_tracker_id": 8}
        av._stop_motion_detector("D")  # preserve_tracks defaults to False
        self.assertEqual(sent, [("D", "leave")])
        self.assertNotIn("D", av._motion_state)


class TestFirmwareUpdate(unittest.TestCase):
    """UpdateFirmwareRequest is acked and its target version pinned, never flashed."""

    def setUp(self):
        self._orig_state = av.STATE_DIR
        self._tmp = tempfile.mkdtemp()
        av.STATE_DIR = self._tmp
        self.addCleanup(lambda: setattr(av, "STATE_DIR", self._orig_state))
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._orig_sleep = av.time.sleep
        av.time.sleep = lambda *_: None
        self.addCleanup(lambda: setattr(av.time, "sleep", self._orig_sleep))

    def test_parse_path_form(self):
        self.assertEqual(
            av._version_from_update_uri(
                "https://10.0.0.1:7444/internal/update/aiport/uvc/firmware/5.2.0/FCECDA000001"),
            "5.2.0")

    def test_parse_query_form(self):
        self.assertEqual(
            av._version_from_update_uri(
                "https://10.0.0.1:7444/internal/update?platform=aiport&product=uvc"
                "&updateType=firmware&version=5.2.0&mac=FCECDA000001"),
            "5.2.0")

    def test_parse_rejects_custom_uri(self):
        self.assertIsNone(av._version_from_update_uri("https://example.com/fw.bin"))
        self.assertIsNone(av._version_from_update_uri(""))

    def test_pin_round_trip(self):
        self.assertIsNone(config.pinned_fw_version(av.STATE_DIR))
        config.pin_fw_version(av.STATE_DIR, "9.9.9")
        self.assertEqual(config.pinned_fw_version(av.STATE_DIR), "9.9.9")

    def test_handler_pins_and_reboots(self):
        ws = _FakeWS()
        av.handle_function(ws, {
            "functionName": "UpdateFirmwareRequest",
            "messageId": "m1",
            "payload": {"uri": "https://c:7444/internal/update/aiport/uvc/firmware/6.0.0/AA",
                        "timeoutMs": 30000},
        }, {})
        self.assertEqual(config.pinned_fw_version(av.STATE_DIR), "6.0.0")
        names = [json.loads(m)["functionName"] for m in ws.sent]
        self.assertEqual(names, ["EventUpdateFirmwareStatus", "EventUpdateFirmwareStatus"])
        statuses = [json.loads(m)["payload"]["status"] for m in ws.sent]
        self.assertEqual(statuses, ["FW_DOWNLOADING", "FW_UPDATING"])
        self.assertTrue(ws.closed)

    def test_handler_with_unparsable_uri_acks_without_pinning(self):
        ws = _FakeWS()
        av.handle_function(ws, {
            "functionName": "UpdateFirmwareRequest",
            "messageId": "m2",
            "responseExpected": True,
            "payload": {"uri": "https://example.com/fw.bin"},
        }, {})
        self.assertIsNone(config.pinned_fw_version(av.STATE_DIR))
        self.assertTrue(ws.closed)
        acks = [json.loads(m) for m in ws.sent
                if json.loads(m)["functionName"] == "UpdateFirmwareRequest"]
        self.assertEqual(len(acks), 1)
        self.assertEqual(acks[0]["inResponseTo"], "m2")

    def test_change_device_settings_persists_name(self):
        ws = _FakeWS()
        av.handle_function(ws, {
            "functionName": "ChangeDeviceSettings",
            "messageId": "m3",
            "responseExpected": True,
            "payload": {"name": "Front Door", "timezone": "Europe/Berlin"},
        }, {})
        self.assertEqual(config.device_name(av.STATE_DIR), "Front Door")
        reply = json.loads(ws.sent[-1])
        self.assertEqual(reply["functionName"], "ChangeDeviceSettings")
        self.assertEqual(reply["payload"]["name"], "Front Door")


class _FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(data)

    def close(self, *a, **k):
        self.closed = True


class HwDecoderSelection(unittest.TestCase):
    """AIPORT_HWDEC pins the backend so this is device-independent."""

    def setUp(self):
        self._saved = os.environ.get("AIPORT_HWDEC")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("AIPORT_HWDEC", None)
        else:
            os.environ["AIPORT_HWDEC"] = self._saved

    def test_rkmpp(self):
        os.environ["AIPORT_HWDEC"] = "rkmpp"
        self.assertEqual(av._hwdec_args("hevc"), ["-c:v", "hevc_rkmpp"])
        self.assertEqual(av._hwdec_args("h264"), ["-c:v", "h264_rkmpp"])
        self.assertEqual(av._hwdec_args(None), ["-c:v", "h264_rkmpp"])
        self.assertEqual(av._hwdec_args("mjpeg"), [])

    def test_v4l2m2m(self):
        os.environ["AIPORT_HWDEC"] = "v4l2m2m"
        self.assertEqual(av._hwdec_args("h264"), ["-c:v", "h264_v4l2m2m"])
        self.assertEqual(av._hwdec_args("hevc"), ["-c:v", "hevc_v4l2m2m"])
        self.assertEqual(av._hwdec_args("mjpeg"), [])

    def test_none_is_software(self):
        os.environ["AIPORT_HWDEC"] = "none"
        self.assertEqual(av._hwdec_args("hevc"), [])
        self.assertEqual(av._hwdec_args("h264"), [])

    def test_auto_prefers_ffmpeg_rkmpp(self):
        os.environ.pop("AIPORT_HWDEC", None)
        old = av.FFMPEG_BIN
        av.FFMPEG_BIN = "/usr/local/ffmpeg-rkmpp/bin/ffmpeg"
        try:
            self.assertEqual(av._hwdec_args("h264"), ["-c:v", "h264_rkmpp"])
        finally:
            av.FFMPEG_BIN = old


class Pi4Detection(unittest.TestCase):
    """v4l2m2m is Pi 4 (BCM2711) only -- the Pi 5 has no H.264 hardware decoder."""

    def _compat(self, data):
        f = tempfile.NamedTemporaryFile(delete=False)
        f.write(data)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_bcm2711_is_pi4(self):
        self.assertTrue(av._is_pi4(self._compat(b"brcm,bcm2711\x00raspberrypi,4-model-b")))

    def test_bcm2712_is_not_pi4(self):
        self.assertFalse(av._is_pi4(self._compat(b"brcm,bcm2712\x00raspberrypi,5-model-b")))

    def test_missing_compatible_is_false(self):
        self.assertFalse(av._is_pi4("/nonexistent/compatible"))


try:
    import numpy as _np
except Exception:  # numpy is a project dep but keep the suite runnable without it
    _np = None


@unittest.skipIf(_np is None, "numpy unavailable")
class MotionGate(unittest.TestCase):
    """_scene_changed drives the frame-diff gate that skips static-scene inference."""

    def setUp(self):
        self.addCleanup(av._latest_frame.clear)
        self.addCleanup(av._scene_ref.clear)
        av._latest_frame.clear()
        av._scene_ref.clear()

    def test_first_frame_triggers(self):
        av._latest_frame["d"] = _np.zeros((640, 640, 3), _np.uint8)
        self.assertTrue(av._scene_changed("d"))

    def test_static_scene_does_not_trigger(self):
        av._latest_frame["d"] = _np.full((640, 640, 3), 100, _np.uint8)
        av._scene_changed("d")                    # first tick sets the reference
        self.assertFalse(av._scene_changed("d"))  # identical frame -> below threshold

    def test_large_change_triggers(self):
        av._latest_frame["d"] = _np.zeros((640, 640, 3), _np.uint8)
        av._scene_changed("d")
        av._latest_frame["d"] = _np.full((640, 640, 3), 200, _np.uint8)
        self.assertTrue(av._scene_changed("d"))

    def test_no_frame_is_no_change(self):
        self.assertFalse(av._scene_changed("absent"))


if __name__ == "__main__":
    unittest.main()
