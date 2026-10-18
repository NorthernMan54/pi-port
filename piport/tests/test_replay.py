# SPDX-License-Identifier: AGPL-3.0-or-later
"""replay.py on synthetic clips (a white box crossing a black frame) with a fake detector
that finds it, and record_clip.py's log scan. ffmpeg tests are skipped if it's absent."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import avclient as av  # noqa: E402
import replay  # noqa: E402

_FFMPEG = shutil.which(av.FFMPEG_BIN)
_spec = importlib.util.spec_from_file_location(
    "record_clip", os.path.join(os.path.dirname(_ROOT), "scripts", "record_clip.py"))
record_clip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(record_clip)


class BrightBoxDetector:
    """Finds the white pixels in a frame and reports them as one person."""

    def detect(self, frame, min_score=None):
        import numpy as np
        ys, xs = np.nonzero(frame[:, :, 0] > 200)
        if len(xs) < 20:
            return []
        return [{"objectType": "person", "label": "person", "score": 0.9,
                 "box": (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))}]


def make_clip(path, enable="between(t,0.5,2.5)", duration=3):
    """A 320x180 black clip with a 40x60 white box moving right while `enable` holds."""
    subprocess.run(
        [_FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         f"color=black:s=320x180:r=25:d={duration}[bg];color=white:s=40x60:r=25:d={duration}[fg];"
         f"[bg][fg]overlay=x='20+t*60':y=60:enable='{enable}'",
         "-c:v", "mjpeg", "-q:v", "3", path], check=True)


@unittest.skipUnless(_FFMPEG, "ffmpeg not installed")
class TestReplay(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.clip = os.path.join(self.dir, "walk.avi")
        make_clip(self.clip)
        patcher = mock.patch.object(av, "_detector", BrightBoxDetector())
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_replay(self, settings=None):
        return replay.replay(av, replay.clip_frames(av, self.clip), settings or {"width": 320,
                                                                                 "height": 180})

    def test_crossing_box_is_one_event(self):
        sent = self.run_replay()
        edges = [p["edgeType"] for _, p in sent]
        self.assertEqual(edges[0], "enter")
        self.assertEqual(edges[-1], "leave")
        self.assertEqual(set(edges[1:-1]), {"moving"})
        events = replay.summarize(sent)
        self.assertEqual(len(events), 1)
        self.assertEqual([o["type"] for o in events[0]["objects"]], ["person"])
        # it appears at 0.5 s and is sent once it has initialised (MIN_INITIALIZED frames
        # later), its enter dated back to 0.5 s; its track closes AIPORT_MISS_S after it goes
        # at 2.5 s
        init_s = (av.MIN_INITIALIZED + 1) / av.DETECT_FPS
        self.assertAlmostEqual(events[0]["start_s"], 0.5 + init_s, delta=0.3)
        walls = [p["clockWall"] for _, p in sent]
        self.assertAlmostEqual((walls[1] - walls[0]) / 1000.0, av.REPORT_INTERVAL_MS / 1000.0,
                               delta=0.15)   # back-filled from the first frame
        self.assertAlmostEqual(events[0]["end_s"], 2.5 + av.MOTION_MISS_TOLERANCE_S, delta=0.5)

    def test_boxes_move_right(self):
        sent = self.run_replay()
        xs = [p["descriptors"][0]["coord"][0] for _, p in sent if p["edgeType"] == "moving"]
        self.assertGreaterEqual(len(xs), 3)  # one update per AIPORT_TICK_S
        self.assertGreater(xs[-1], xs[0])

    def test_detection_zone_elsewhere_means_no_event(self):
        settings = {"width": 320, "height": 180, "smartDetect": {
            "zones": {"1": {"coord": [0, 900, 100, 900, 100, 1000, 0, 1000]}}}}
        self.assertEqual(self.run_replay(settings), [])

    def test_event_start_msec_from_sidecar(self):
        settings = {"width": 320, "height": 180, "smartDetect": {"eventStartMSec": 1000}}
        events = replay.summarize(self.run_replay(settings))
        self.assertAlmostEqual(events[0]["start_s"], 1.5, delta=0.3)

    def test_avclient_left_as_it_was(self):
        before = (av.send_msg, av.LEAVE_FALLBACK_S, av.STARTUP_WINDOW_MS, av._STREAM_DIR)
        self.run_replay()
        self.assertEqual((av.send_msg, av.LEAVE_FALLBACK_S, av.STARTUP_WINDOW_MS,
                          av._STREAM_DIR), before)
        self.assertNotIn(replay.DEVICE, av._motion_state)

    def test_cli_json_with_sidecar(self):
        with open(os.path.join(self.dir, "walk.json"), "w") as f:
            json.dump({"width": 320, "height": 180, "start": "2026-10-17T13:54:00-04:00"}, f)
        out = os.path.join(self.dir, "out.json")
        with open(out, "w") as f, mock.patch("sys.stdout", f):
            replay.main([self.clip, "--json"])
        with open(out) as f:
            summary = json.load(f)
        self.assertEqual(len(summary["events"]), 1)
        self.assertGreater(summary["messages"], 2)


class TestRecordClipLogScan(unittest.TestCase):
    LOG = [
        "2026-10-17 15:09:59,455 INFO starting merged RTSP pull for deviceID=8CEDE172C456: "
        "rtsp://192.168.1.1:7447/old (codec=hevc)\n",
        "2026-10-17 15:10:01,607 INFO <<< received functionName='UiStreamControl' "
        "messageId=1 from='UniFiVideo' to='ubnt_avclient' payload={'streaming': True, "
        "'ip': '192.168.1.1', 'port': '7447', 'uri': 'x', 'deviceID': '8CEDE172C456', "
        "'width': 1920, 'height': 2560, 'fps': 24}\n",
        "2026-10-17 15:10:02,000 INFO <<< received functionName='ChangeSmartDetectSettings' "
        "messageId=2 from='UniFiVideo' to='ubnt_avclient' payload={'enableSmartDetect': "
        "['person'], 'eventStartMSec': 1000, 'zones': {'1': {'coord': [0, 0, 1000, 0, 1000, "
        "1000, 0, 1000]}}, 'excludeZones': {}, 'deviceID': '8CEDE172C456'}\n",
        "2026-10-17 15:10:02,100 INFO <<< received functionName='ChangeSmartDetectSettings' "
        "messageId=3 from='UniFiVideo' to='ubnt_avclient' payload={'isLprCamera': 0, "
        "'deviceID': '8CEDE172C456'}\n",
        "2026-10-17 15:10:02,200 INFO <<< received functionName='ChangeSmartMotionSettings' "
        "messageId=4 from='UniFiVideo' to='ubnt_avclient' payload={'eventMaxDurationMSec': "
        "300000, 'deviceID': '8CEDE172C456'}\n",
        "2026-10-17 15:20:00,000 INFO starting merged RTSP pull for deviceID=8CEDE172C456: "
        "rtsp://192.168.1.1:7447/new (codec=hevc)\n",
        "2026-10-17 15:20:00,000 INFO starting merged RTSP pull for deviceID=F4E2C60D4B4C: "
        "rtsp://192.168.1.1:7447/other (codec=hevc)\n",
    ]

    def test_latest_values_for_the_camera(self):
        info = record_clip.scan_log(self.LOG, "8cede172c456")
        self.assertEqual(info["url"], "rtsp://192.168.1.1:7447/new")
        self.assertEqual((info["width"], info["height"], info["fps"]), (1920, 2560, 24))
        self.assertEqual(info["smartDetect"]["eventStartMSec"], 1000)  # not the partial one
        self.assertEqual(info["smartMotion"]["eventMaxDurationMSec"], 300000)

    def test_unknown_camera(self):
        self.assertEqual(record_clip.scan_log(self.LOG, "000000000000"), {})


if __name__ == "__main__":
    unittest.main()
