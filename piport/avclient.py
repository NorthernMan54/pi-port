#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Second WebSocket connection AI Port needs alongside ucp4: the classic camera
"avclient" protocol.

The controller's pairing/stream pool only includes connections that do NOT
negotiate "ucp4" and that carry camera-mac plus camera-model = literal hex
sysid "0xa5f1". Wire protocol: wss://<host>:7442/camera/1.0/ws[?token=],
subprotocol secure_transfer over mTLS, headers camera-mac/camera-ip/
camera-model/camera-firmware/device-id/x-guid/adopted plus
Origin: http://ws_camera_proto_secure_transfer, and plain JSON in BINARY
frames starting with "ubnt_avclient_hello".
"""
import argparse
import collections
import itertools
import json
import logging
import math
import os
import shutil
import ssl
import statistics
import subprocess
import tempfile
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit

import requests
import urllib3
from websockets.sync.client import connect

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("aiport-avclient")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import enrich

HERE = os.path.dirname(os.path.abspath(__file__))
ADOPT_STATE_FILE = os.path.join(HERE, "adopt_state.json")
CERT_PATH = os.path.join(HERE, "server.crt")
KEY_PATH = os.path.join(HERE, "server.key")
_PROCESS_START_MS = int(time.time() * 1000)

_msg_id_counter = itertools.count(1)

# deviceID -> subprocess.Popen pulling its RTSP stream from Protect's relay (not the camera).
_active_streams = {}
# tmpfs dir ffmpeg writes each camera's snapshot JPEG into (never persistent).
_STREAM_DIR = config.stream_dir()
# --state-dir (or None for the shared piport/ path); the firmware pin lives here.
STATE_DIR = None
# deviceID -> consecutive unexpected-death count since the last UiStreamControl start.
_stream_restart_count = {}
# deviceID -> time.monotonic()*1000 when its RTSP pull started; the origin for
# clockMonotonic/clockStream on that device's EventSmartDetect payloads.
_stream_started_monotonic_ms = {}

# Hardware video decode (AIPORT_HWDEC=auto|rkmpp|v4l2m2m|videotoolbox|none): rkmpp on the
# RK3588, v4l2m2m on a Pi 4 (/dev/video10 + /dev/vchiq), videotoolbox on macOS; else software.
FFMPEG_BIN = ("/usr/local/ffmpeg-rkmpp/bin/ffmpeg"
              if os.path.exists("/usr/local/ffmpeg-rkmpp/bin/ffmpeg") else "ffmpeg")
# Backend -> codec -> ffmpeg input args. VideoToolbox is a hwaccel on ffmpeg's own decoder
# (frames come back to system memory for the filters): ~4x less CPU than software HEVC.
# HEVC only: on a Protect-relayed H.264 stream it failed ~14 pictures in 20 s (software: 0).
_HWDEC_BACKENDS = {
    "rkmpp": {"h264": ["-c:v", "h264_rkmpp"], "hevc": ["-c:v", "hevc_rkmpp"]},
    "v4l2m2m": {"h264": ["-c:v", "h264_v4l2m2m"], "hevc": ["-c:v", "hevc_v4l2m2m"]},
    "videotoolbox": {"hevc": ["-hwaccel", "videotoolbox"]},
}
_FFPROBE_BIN = os.path.join(os.path.dirname(FFMPEG_BIN), "ffprobe") \
    if FFMPEG_BIN != "ffmpeg" and os.path.exists(os.path.join(os.path.dirname(FFMPEG_BIN), "ffprobe")) \
    else "ffprobe"


def _is_pi4(compatible_path="/proc/device-tree/compatible"):
    """True on the Raspberry Pi 4 family (BCM2711). The Pi 5 (BCM2712) has NO
    hardware H.264 decoder, so v4l2m2m must not be selected there."""
    try:
        with open(compatible_path, "rb") as f:
            return b"bcm2711" in f.read()
    except OSError:
        return False


def _detect_hwdec():
    """Active hardware-decode backend name, or None for software."""
    forced = os.environ.get("AIPORT_HWDEC", "auto").strip().lower()
    if forced in ("none", "sw", "software"):
        return None
    if forced in _HWDEC_BACKENDS:
        return forced
    if FFMPEG_BIN != "ffmpeg":
        return "rkmpp"
    # Pi 4 only (flaky under load); the Pi 5 has no H.264 hardware decoder.
    if _is_pi4() and os.path.exists("/dev/video10") and os.path.exists("/dev/vchiq"):
        return "v4l2m2m"
    if sys.platform == "darwin":
        return "videotoolbox"
    return None


def _probe_codec(url, timeout=8):
    """Video codec_name of the RTSP stream (e.g. 'h264', 'hevc'), or None if unknown."""
    try:
        out = subprocess.run(
            [_FFPROBE_BIN, "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", url],
            capture_output=True, timeout=timeout, text=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out.splitlines()[0].strip().lower() if out else None


def _hwdec_args(codec):
    """Input decoder args for the active hardware backend, or empty (ffmpeg's
    auto-selected software decoder). Unknown/failed probe assumes h264, the
    historical default."""
    backend = _detect_hwdec()
    if not backend:
        return []
    return list(_HWDEC_BACKENDS[backend].get(codec or "h264", []))

# Real detection: per-tick YOLOv5s/RKNN classify the latest frame; objects are
# IOU-tracked per objectType with their own trackerID/eventId and enter/move/leave.
_motion_state = {}   # deviceID -> {"tracks": {trackerID: {...}}, "next_tracker_id": int}
_motion_lock = threading.Lock()
_motion_poll_stop = {}  # deviceID -> threading.Event, signals the poll loop to stop
_motion_poll_thread = {}  # deviceID -> threading.Thread, joined on stop
_event_id_counter = itertools.count(int(time.time()))  # monotonic, avoids same-second collisions across tracks
# Seconds between classification ticks (AIPORT_TICK_S). 0.33 s on macOS, where Core ML runs
# a frame in ~8 ms: a G5 Bullet sends a moving update every ~330 ms (median of 44, 249-600 ms)
# with displayTimeoutMSec ~326; at 1.5 s a car crossing a far road was classified once or not at all.
# 1.5 s elsewhere (Pi/RK3588 CPU budget). The detection stream runs at least this fast.
MOTION_POLL_INTERVAL_S = float(os.environ.get("AIPORT_TICK_S")
                               or (0.33 if sys.platform == "darwin" else 1.5))
# How often `moving` updates go to Protect: once per AIPORT_TICK_S.
REPORT_INTERVAL_MS = int(MOTION_POLL_INTERVAL_S * 1000)
# How many frames per second each camera's detector analyses (AIPORT_DETECT_FPS). More frames
# mean objects move less between checks, so tracks match more reliably, and enter/leave
# land closer to the truth; reports still go out once per tick. 10 on macOS (Core ML runs
# a frame in ~8 ms); elsewhere one frame per tick, as before. Never slower than reporting.
DETECT_INTERVAL_S = min(MOTION_POLL_INTERVAL_S,
                        1 / float(os.environ.get("AIPORT_DETECT_FPS")
                                  or (10 if sys.platform == "darwin"
                                      else 1 / MOTION_POLL_INTERVAL_S)))
DETECT_FPS = max(2, math.ceil(1 / DETECT_INTERVAL_S))


def _ticks(seconds):
    """Analysed-frame count for a duration. Track counters advance once per analysed
    frame, so time-based thresholds don't change with the detection rate."""
    return max(1, round(seconds / DETECT_INTERVAL_S))


# How long Protect shows each box: until just past the next update (a G5 Bullet's
# displayTimeoutMSec ~= its update gap). At 1.5 s per update a 200 ms box flashed on and off.
BOX_DISPLAY_MS = int(MOTION_POLL_INTERVAL_S * 1000) + 100
# Seconds with no detection before a track closes (AIPORT_MISS_S, default 3): the event
# ends about this long after the object was last seen. A G5 Bullet sends leave ~2 s after
# the object was last seen; 3 s allows for our detector missing a frame or two. At 30 s a
# car passing in a few seconds made a 30 s event. Needs track state to survive
# UiStreamControl refreshes. Parked tracks flicker in and out of detection, so they keep
# 30 s (dropping one would re-create it as a new arrival).
MOTION_MISS_TOLERANCE_S = float(os.environ.get("AIPORT_MISS_S") or 3)
MOTION_MISS_TOLERANCE_TICKS = _ticks(MOTION_MISS_TOLERANCE_S)
PARKED_MISS_TOLERANCE_TICKS = _ticks(30)
# Reported boxes are smoothed (exponential, this weight on the newest detection) so per-frame
# detector jitter doesn't make them jump; matching and crops use the raw box.
BOX_SMOOTHING = 0.6
# A moving track that no box overlaps takes the nearest same-type detection within this many
# times its size (+ margin, 0-1000 units) of where its last movement predicts it: small, fast
# objects (far-road cars and walkers) moved further than their width between ticks and were
# re-created as a new track every tick. The radius is capped at NN_DIST_MAX and widens only
# by NN_MISS_GROWTH per missed tick, to at most NN_MISS_GROWTH_MAX x: it used to multiply by
# every missed tick, so a track missed for a second took any same-type box in the frame (live
# 09:56 on Driveway: one vehicle track hopped between four cars over 24 s).
NN_DIST_FACTOR = 3.0
NN_DIST_MARGIN = 30
NN_DIST_MAX = 150
NN_MISS_GROWTH = 0.25
NN_MISS_GROWTH_MAX = 1.5
# A moving track never takes a box more than this many times wider or taller (or narrower or
# shorter) than its own, from Norfair or the nearest rule: live 10:20 on Driveway a bicycle's
# track (40x57) took a 49x136 box beside it, then a 107x253 one, and held the event 16 s.
NN_SIZE_RATIO_MAX = 2.0
# Min IOU (same objectType) to match a detection to an existing track.
IOU_MATCH_THRESHOLD = 0.3
# Which detection belongs to which track (AIPORT_TRACKER): "norfair", Frigate's Kalman-filtered
# Norfair tracker (norfair_match.py) with pi-port's parked and fast-mover rules for what it
# leaves, or "builtin", IoU then the nearest detection where a track's motion predicts it.
# Default: norfair when the package is installed (two people crossing kept their IDs where
# the built-in matcher swapped them), else builtin.
TRACKER = (os.environ.get("AIPORT_TRACKER") or "").strip().lower()
if TRACKER in ("", "norfair"):
    try:
        import norfair  # noqa: F401
        TRACKER = "norfair"
    except ImportError:
        if TRACKER == "norfair":
            log.warning("AIPORT_TRACKER=norfair but the norfair package isn't installed; "
                        "using the built-in matcher")
        TRACKER = "builtin"

# A track is a true positive once the median of its last SCORE_HISTORY scores (0.0 for each
# analysed frame it was missed in) reaches AIPORT_TRUE_THRESH -- Frigate's false-positive
# filter (frigate/track/tracked_object.py, MIT). Every detection already needs
# AIPORT_OBJ_THRESH (detector.py). Seen 2026-10-17: a tree stump on Backyard and a pot by
# the doorbell scored "animal" ~0.5-0.6 for a minute at a time; a passing sedan scored
# 0.85 and 0.78 on its only two frames.
SCORE_HISTORY = 10
DEFAULT_TRUE_THRESH = 0.7
# ...and only once it has been detected on at least MIN_HITS frames (Frigate's tracker
# initialises an object after 2+ hits): a car scoring 0.9 on a single region crop became an
# event on its own, confirmed a second later with nothing else seen.
MIN_HITS = 2


def _parse_true_thresh(spec):
    """AIPORT_TRUE_THRESH: "<default>[,<objectType>=<value>...]" -> (default, {type: value})."""
    default, per = DEFAULT_TRUE_THRESH, {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "=" in part:
                name, value = part.split("=", 1)
                per[name.strip()] = float(value)
            else:
                default = float(part)
        except ValueError:
            log.warning("ignoring bad AIPORT_TRUE_THRESH entry %r", part)
    return default, per


_TRUE_THRESH_DEFAULT, _TRUE_THRESH = _parse_true_thresh(os.environ.get("AIPORT_TRUE_THRESH"))


def _true_thresh(object_type):
    return _TRUE_THRESH.get(object_type, _TRUE_THRESH_DEFAULT)


def _record_score(tr, score):
    """Add this frame's score (0.0 if missed) and latch true_positive once the median of the
    last SCORE_HISTORY passes the type's threshold."""
    history = tr.setdefault("score_history", [])
    history.append(score)
    del history[:-SCORE_HISTORY]
    if score > 0:
        tr["hits"] = tr.get("hits", 0) + 1
    if not tr.get("true_positive") and tr.get("hits", 0) >= MIN_HITS:
        tr["true_positive"] = statistics.median(history) >= _true_thresh(tr["objectType"])


# A same-type detection where a track closed within RESUME_S (AIPORT_RESUME_S) resumes it
# (IoU >= RESUME_IOU with its last or first box): back into its event while that is still
# open, or quietly as parked for a vehicle that never moved -- the unloading car came back
# as a "new" vehicle three times. Moved = centre shifted > RESUME_MOVE_FRACTION of its size.
RESUME_S = float(os.environ.get("AIPORT_RESUME_S") or 60)
RESUME_IOU = 0.4
RESUME_MOVE_FRACTION = 0.25
RESUME_MAX = 32
# A face or plate is only looked for inside a person or vehicle box, so it confirms with
# that parent (it's seen once per snapshot, too rarely to confirm on its own).
CONFIRM_WITH = {"face": "person", "licensePlate": "vehicle"}
INSIDE_PARKED_FRACTION = 0.7
# Motion/scene gate: skip the classifier on a near-static frame with no open track.
# The gate asks motion.py (Frigate's motion detector) for motion boxes against a running
# background, so a person walking through Backyard's background (~0.5% of the frame) counts;
# the old whole-frame average diff missed them. AIPORT_MOTION_GATE=0 disables the gate.
# Protect's motion sensitivity and motion zones set its pixel threshold and mask;
# AIPORT_MOTION_THRESHOLD (1-255) overrides the threshold. Without OpenCV the old average
# diff is the fallback (AIPORT_MOTION_GATE_THRESH, default 0.02).
MOTION_GATE = os.environ.get("AIPORT_MOTION_GATE", "1").strip().lower() not in (
    "0", "false", "no", "off")
MOTION_GATE_THRESH = float(os.environ.get("AIPORT_MOTION_GATE_THRESH", "0.02"))
_GATE_STRIDE = 8  # sample every Nth pixel -> 80x80 grayscale for the diff
# Paired camera's smartDetectZones row id -- zonesStatus's key must match it (not always "1").
SMART_DETECT_ZONE_ID = "1"

# RK3588 NPU YOLOv5s (detector.py); ffmpeg decodes to 640x640 raw RGB, latest frame only.
_latest_frame = {}   # deviceID -> HxWx3 uint8 numpy array (640x640, RGB)
_latest_frame_seq = {}  # deviceID -> number of the frame in _latest_frame (1 = first of the pull)
# deviceID -> recent (seq, full-res JPEG) of the same frames, numbered alike: a track's best
# frame is the one it was detected in, not the next snapshot (a passing bus had moved on).
_frame_jpegs = {}
# deviceID -> (wall ms, monotonic ms) when _latest_frame was read from ffmpeg.
_latest_frame_time = {}
# deviceID -> (wall ms, monotonic ms) of the frame the detector last analysed; every
# event time describes that frame, not the moment the event is sent.
_analysed_frame_time = {}
_frame_lock = threading.Lock()
_frame_cond = threading.Condition(_frame_lock)  # notified when a frame JPEG arrives
FRAME_JPEG_KEEP_S = 2.0
FRAME_JPEG_WAIT_S = 0.5  # how long a detection waits for its frame's JPEG
_stream_dims = {}    # deviceID -> (width, height) of the real camera stream
# deviceID -> capacity points of each stream UiStreamControl started. Kept apart from
# _active_streams, which briefly drops a device while its ffmpeg restarts.
_stream_points = {}
# deviceID -> exclusion polygons in the normalized 0-1000 space.
_exclude_zones = {}
# Prototype app exposes `zones` (not excludeZones): when non-default, a detection must be inside one.
_detect_zones = {}
# deviceID -> each detection zone's objectTypes (None = any), parallel to _detect_zones.
_detect_zone_types = {}
# deviceID -> Line Crossing segments parsed from ChangeSmartDetectSettings.lines.
_lines = {}
_snapshot_seq_by_device = {}  # deviceID -> monotonic counter for fullfov snapshot filenames
# Filename -> {"device_id", "kind", "coord"} so an upload maps back to its camera; bounded
# against leaks.
_snapshot_filename_to_device = collections.OrderedDict()
_SNAPSHOT_FILENAME_MAP_MAX = 500
# (monotonic_time, deviceID) per announced snapshot; fallback only when the GetRequest has
# no/unknown filename.
SNAPSHOT_FALLBACK_WINDOW_S = 60
_recent_snapshot_announcements = collections.deque(maxlen=32)
# deviceID -> last announced per-object coord, so a filename-less fallback can still crop.
_last_object_coord_by_device = {}
DETECT_IMG_SIZE = 640  # must match detector.py's IMG_SIZE
# Per-object snapshot: square crop centered on the detection, ffmpeg-scaled to this size.
SMART_SNAPSHOT_SIZE = 512
SMART_SNAPSHOT_MARGIN = 1.6
# descriptors[].coord and derived geometry are normalized 0-1000, not stream pixels (controller clamps to [0,1000]).
CAMERA_COORD_END = 1000
_detector = None
_detector_lock = threading.Lock()
_enricher = None
_snapshot_cache = {}  # deviceID -> (mtime, BGR image) of the last decoded full-res snapshot
_snapshot_bytes = {}  # deviceID -> (mtime, JPEG bytes) of the latest full-res snapshot
# Records still holding a best-frame JPEG; older ones drop their bytes (memory bound).
BEST_FRAME_RECORDS_MAX = 40
_enrich_cache = {}    # deviceID -> (snapshot mtime, plate/face detections read from it)


def _get_detector():
    global _detector
    with _detector_lock:
        if _detector is None:
            from detector import Detector
            _detector = Detector()
        return _detector


def build_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(CERT_PATH, KEY_PATH)
    return ctx


def send_msg(ws, functionName, payload, to="ubnt_avclient", in_response_to=None,
             response_expected=False, from_="UniFiVideo"):
    msg = {
        "from": from_,
        "to": to,
        "functionName": functionName,
        "messageId": next(_msg_id_counter),
        # ds's serde requires `timeStamp` as an RFC3339 string (a unix-ms integer is rejected).
        "timeStamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
        "responseExpected": response_expected,
        # ds requires inResponseTo on every message (it drops EventSmartDetect
        # without it); 0 = "not a response".
        "inResponseTo": in_response_to if in_response_to is not None else 0,
    }
    # Controller sends/expects plain JSON as BINARY frames (opcode 0x2), not text.
    ws.send(json.dumps(msg).encode())
    log.info(">>> sent %s (id=%s) payload=%s", functionName, msg["messageId"], payload)


def _stop_stream(device_id):
    proc = _active_streams.pop(device_id, None)
    if proc and proc.poll() is None:
        log.info("stopping RTSP pull for deviceID=%s (pid=%s)", device_id, proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _stream_started_monotonic_ms.pop(device_id, None)
    with _frame_lock:
        _latest_frame.pop(device_id, None)
        _latest_frame_seq.pop(device_id, None)
        _frame_jpegs.pop(device_id, None)
        _latest_frame_time.pop(device_id, None)
        _analysed_frame_time.pop(device_id, None)
    _motion_detectors.pop(device_id, None)  # a new stream starts a new background
    _startup_scan.pop(device_id, None)
    _last_parked_check.pop(device_id, None)
    # The startup window starts again on the new stream's first frame: live 11:47 on
    # Driveway the RTSP pull dropped 3 s in and the window ran out before frames came back,
    # so the parked car opened an event instead of parking.
    with _motion_lock:
        dev = _motion_state.get(device_id)
        if dev is not None:
            dev["started_ms"] = None


def _jpeg_end(buf):
    """End offset of the JPEG starting at buf[0], or -1 while it is incomplete. Header
    segments are skipped by length (a table can hold FF D9); in the scan data 0xFF is
    byte-stuffed, so there FF D9 is the end marker."""
    pos = 2
    while pos + 4 <= len(buf):
        if buf[pos] != 0xFF:
            raise ValueError("bad JPEG marker at offset %d" % pos)
        marker = buf[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        seg = (buf[pos + 2] << 8) | buf[pos + 3]
        if marker == 0xDA:  # start of scan
            end = buf.find(b"\xff\xd9", pos + 2 + seg)
            return -1 if end < 0 else end + 2
        pos += 2 + seg
    return -1


def _jpeg_frames(stream):
    """Split ffmpeg's image2pipe MJPEG output into one JPEG per frame."""
    buf = bytearray()
    while True:
        chunk = stream.read1(1 << 16)
        if not chunk:
            return
        buf += chunk
        while True:
            start = buf.find(b"\xff\xd8")
            if start < 0:
                del buf[:-1]  # the last byte may be the first half of the SOI
                break
            if start:
                del buf[:start]
            try:
                end = _jpeg_end(buf)
            except ValueError:
                del buf[:2]  # resync on the next SOI
                continue
            if end < 0:
                break
            yield bytes(buf[:end])
            del buf[:end]


def _tmp_dir():
    """This instance's scratch folder, inside its own stream dir (one per --state-dir, so
    AI Ports sharing a host never touch each other's files); emptied at startup. Made on
    use: macOS's periodic /tmp cleanup can remove it while pi-port runs."""
    path = os.path.join(_STREAM_DIR, "tmp")
    os.makedirs(path, exist_ok=True)
    return path


def _write_snapshot(path, data):
    """Replace the camera's snapshot file in one step, so a reader never sees half of it."""
    fd, tmp = tempfile.mkstemp(suffix=".jpg", dir=_tmp_dir())
    try:
        os.fchmod(fd, 0o644)  # mkstemp's 0600; ffmpeg wrote the snapshot world-readable
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        os.remove(tmp)
        raise


def _reset_tmp_dir():
    """Empty this instance's scratch folder: whatever a crash left there (a half-written
    snapshot, a crop input) is never needed again."""
    shutil.rmtree(_tmp_dir(), ignore_errors=True)
    _tmp_dir()


def _frame_jpeg(device_id, seq, timeout=FRAME_JPEG_WAIT_S):
    """Full-res JPEG of detection frame `seq` (the same decoded frame the detector saw),
    waiting briefly for the encoder to catch up; None once it is gone or never comes."""
    deadline = time.monotonic() + timeout
    with _frame_cond:
        while True:
            jpegs = _frame_jpegs.get(device_id) or ()
            for s, data in jpegs:
                if s == seq:
                    return data
            if jpegs and jpegs[-1][0] >= seq:
                return None
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            _frame_cond.wait(left)


# Region detection (Frigate's): the detector frame keeps the camera's aspect ratio, its long
# side AIPORT_DETECT_SIZE, and the model runs on square crops around motion and tracks
# (regions.py). Otherwise the whole frame is squeezed into DETECT_IMG_SIZE x DETECT_IMG_SIZE
# and detected at once (the Pi/RK3588 NPU path). On by default on macOS.
REGIONS = os.environ.get("AIPORT_REGIONS", "1" if sys.platform == "darwin" else "0"
                         ).strip().lower() not in ("0", "false", "no", "off")
DETECT_SIZE = int(os.environ.get("AIPORT_DETECT_SIZE") or 1280)
PARKED_RECHECK_S = 10.0  # a parked object is re-detected this often (Frigate: 10 s of frames)


def _detect_dims(width=None, height=None):
    """(width, height) of the detector frame for a stream of this size."""
    if not REGIONS or not width or not height:
        return DETECT_IMG_SIZE, DETECT_IMG_SIZE
    scale = min(1.0, DETECT_SIZE / float(max(width, height)))
    return (max(2, int(round(width * scale / 2)) * 2), max(2, int(round(height * scale / 2)) * 2))


def _detect_filter_graph(width=DETECT_IMG_SIZE, height=None):
    """ffmpeg filter graph for the detection pipeline: [jpg] full-res frames, [det] the same
    frames scaled for the detector. replay.py runs clips through this same graph."""
    height = height or width
    return f"[0:v]fps={DETECT_FPS},split=2[jpg][detin];[detin]scale={width}:{height}[det]"


# One ffmpeg pulls RTSP and splits post-decode into a full-res JPEG and a 640x640 RGB
# copy of each detection frame; decoding twice was wasteful (CPU-side NAL parsing
# dominates, not the NPU). Both outputs come after one fps filter, so frame n of
# each is the same picture.
def _start_stream(device_id, ip, port, uri, width=None, height=None):
    _stop_stream(device_id)
    os.makedirs(_STREAM_DIR, exist_ok=True)
    _stream_dims[device_id] = (width or DETECT_IMG_SIZE, height or DETECT_IMG_SIZE)
    url = f"rtsp://{ip}:{port}/{uri}"
    out_path = os.path.join(_STREAM_DIR, f"{device_id}.jpg")
    codec = _probe_codec(url)
    hwdec_args = _hwdec_args(codec)
    encode_args = ["-c:v", "mjpeg_rkmpp"] if FFMPEG_BIN != "ffmpeg" else ["-c:v", "mjpeg"]
    det_w, det_h = _detect_dims(width, height)
    # Plate/face reads use the snapshot file, so refresh it every second when they're on.
    snap_interval = 1.0 if enrich.smart_detect_features() else 2.0
    jpeg_r, jpeg_w = os.pipe()
    cmd = [
        FFMPEG_BIN, "-loglevel", "warning", "-rtsp_transport", "tcp",
        # Low-latency decode (no B-frame reorder delay). Not "-fflags nobuffer": with this
        # split/fps pipeline it left the rawvideo detection output empty (ffmpeg 8).
        "-flags", "low_delay",
        *hwdec_args, "-i", url,
        "-filter_complex", _detect_filter_graph(det_w, det_h),
        "-map", "[jpg]", *encode_args, "-f", "image2pipe", f"pipe:{jpeg_w}",
        "-map", "[det]", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ]
    log.info("starting merged RTSP pull for deviceID=%s: %s (codec=%s)", device_id, url, codec)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                pass_fds=(jpeg_w,))
    finally:
        os.close(jpeg_w)
    jpeg_pipe = os.fdopen(jpeg_r, "rb")
    _active_streams[device_id] = proc
    # Origin for this device's clockMonotonic/clockStream (ms since the pull began).
    _stream_started_monotonic_ms[device_id] = int(time.monotonic() * 1000)
    frame_bytes = det_w * det_h * 3
    jpegs = collections.deque(maxlen=max(2, int(DETECT_FPS * FRAME_JPEG_KEEP_S)))
    with _frame_lock:
        _frame_jpegs[device_id] = jpegs
        _latest_frame_seq.pop(device_id, None)

    def _jpeg_reader():
        seq, last_write = 0, None
        try:
            for data in _jpeg_frames(jpeg_pipe):
                seq += 1
                with _frame_cond:
                    jpegs.append((seq, data))
                    _frame_cond.notify_all()
                now = time.monotonic()
                if last_write is None or now - last_write >= snap_interval:
                    _write_snapshot(out_path, data)
                    last_write = now
        except Exception:
            log.exception("JPEG reader crashed for deviceID=%s", device_id)
        finally:
            jpeg_pipe.close()

    def _reader():
        import numpy as np
        seq = 0
        try:
            while True:
                buf = proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                # Stamp the frame as it leaves ffmpeg: events describe this moment, not
                # the later tick that analyses it.
                stamp = _now_ms()
                frame = np.frombuffer(buf, dtype=np.uint8).reshape((det_h, det_w, 3))
                seq += 1
                with _frame_lock:
                    _latest_frame[device_id] = frame
                    _latest_frame_seq[device_id] = seq
                    _latest_frame_time[device_id] = stamp
        except Exception:
            log.exception("frame reader crashed for deviceID=%s", device_id)
        rc = proc.wait()
        stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        log.info("merged RTSP pull for deviceID=%s exited rc=%s stderr_tail=%s",
                  device_id, rc, stderr[-500:])
        # Only restart on an unexpected death: _stop_stream() pops the device first,
        # so finding `proc` here means it died on its own. No retry cap.
        if _active_streams.get(device_id) is proc:
            _active_streams.pop(device_id, None)
            # 0.5s for the first attempts: Protect rebuilds its ingest in ~2-3s and
            # a fixed 2s backoff lost the race. Counter resets on UiStreamControl.
            count = _stream_restart_count.get(device_id, 0) + 1
            _stream_restart_count[device_id] = count
            backoff = 0.5 if count <= 6 else 2.0
            log.warning("deviceID=%s RTSP pull died unexpectedly (attempt %d), restarting in %.1fs",
                        device_id, count, backoff)
            time.sleep(backoff)
            if device_id not in _active_streams:
                _start_stream(device_id, ip, port, uri, width, height)

    threading.Thread(target=_jpeg_reader, daemon=True).start()
    threading.Thread(target=_reader, daemon=True).start()


def _point_in_polygon(x, y, polygon):
    """Ray-casting point-in-polygon test (zone coord is 4 free, non-axis-aligned points)."""
    inside = False
    n = len(polygon)
    x1, y1 = polygon[-1]
    for i in range(n):
        x2, y2 = polygon[i]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1) + x1):
            inside = not inside
        x1, y1 = x2, y2
    return inside


def _zone_center_norm(coord):
    """Center of a [x, y, w, h] box in normalized 0-1000 space."""
    x, y, w, h = coord
    return x + w / 2, y + h / 2


# Protect draws 0-1000 coords (and the user's zones/lines) on a 16:9 canvas the height
# of the video; a camera of another aspect sits centred in it. Seen on a 1920x2560
# doorbell: boxes normalised to the portrait image landed ~2.4x too far from centre.
PROTECT_CANVAS_ASPECT = 16 / 9


def _canvas_scale(device_id):
    """(sx, sy) mapping image-normalised coords onto Protect's 16:9 canvas."""
    w, h = _stream_dims.get(device_id, (0, 0))
    if not w or not h or abs(w / h - PROTECT_CANVAS_ASPECT) < 0.02:
        return 1.0, 1.0
    a = w / h
    if a < PROTECT_CANVAS_ASPECT:  # narrower (portrait, 4:3): pillarboxed
        return a / PROTECT_CANVAS_ASPECT, 1.0
    return 1.0, PROTECT_CANVAS_ASPECT / a  # wider: letterboxed


def _to_protect(device_id, coord):
    """Image-normalised [x, y, w, h] -> Protect canvas [x, y, w, h] (0-1000)."""
    sx, sy = _canvas_scale(device_id)
    x, y, w, h = coord
    half = CAMERA_COORD_END / 2
    return [half + (x - half) * sx, half + (y - half) * sy, w * sx, h * sy]


def _protect_center(device_id, coord):
    """Box center in Protect's canvas space, where zones and lines are drawn."""
    return _zone_center_norm(_to_protect(device_id, coord))


def _protect_foot(device_id, coord):
    """Bottom-centre of the box in Protect's canvas space: where the object stands, which
    zones are tested against (as in Frigate). Line crossing keeps using the centre. Seen
    live: a person walking along Backyard's hedge had their box centre near the edge of
    zone 2 while their feet were well inside it."""
    x, y, w, h = _to_protect(device_id, coord)
    return x + w / 2.0, y + h


def _is_excluded(device_id, coord):
    """True if the detection's bottom-centre falls inside any configured excludeZones."""
    polygons = _exclude_zones.get(device_id)
    if not polygons:
        return False
    cx, cy = _protect_foot(device_id, coord)
    return any(_point_in_polygon(cx, cy, poly) for poly in polygons)


# A detection counts in a zone listing what it confirms with (a plate rides its vehicle).
ZONE_TYPE_ALIASES = {"licensePlate": ("licensePlate", "vehicle")}


def _zone_allows(types, object_type):
    if types is None or object_type is None:
        return True
    return any(t in types for t in ZONE_TYPE_ALIASES.get(object_type, (object_type,)))


def _is_outside_detect_zones(device_id, coord, object_type=None):
    """True if the detection's bottom-centre is outside every configured `zones` polygon
    that lists its objectType (no zones means no restriction): live 16:47 on Driveway two
    vehicles were reported from a zone set to person/animal/face."""
    polygons = _detect_zones.get(device_id)
    if not polygons:
        return False
    types = _detect_zone_types.get(device_id) or [None] * len(polygons)
    cx, cy = _protect_foot(device_id, coord)
    return not any(_zone_allows(t, object_type) and _point_in_polygon(cx, cy, poly)
                   for poly, t in zip(polygons, types))


# deviceID -> {"first", "last"}: monotonic s of the stream's first frame and last full look.
_startup_scan = {}
STARTUP_SCAN_INTERVAL_S = 1.0
_last_parked_check = {}    # deviceID -> monotonic s when parked objects were last re-detected


def _detect_regions(device_id, detector, frame):
    """Detections for this frame from square regions around motion and tracked objects
    (Frigate's process_frames), in detector-frame pixels. Parked objects are re-detected
    every PARKED_RECHECK_S or when motion touches them; otherwise they are passed on as
    their last box ("seeded"). The first frame of a stream gets a full startup look."""
    import regions
    shape = frame.shape
    now = time.monotonic()
    with _motion_lock:
        tracks = list(((_motion_state.get(device_id) or {}).get("tracks") or {}).items())
    det = _motion_detectors.get(device_id)
    motion = [] if det is None or det.is_calibrating() else list(_motion_boxes.get(device_id, []))
    recheck = now - _last_parked_check.get(device_id, 0.0) >= PARKED_RECHECK_S
    if recheck:
        _last_parked_check[device_id] = now
    object_boxes, seeded = [], []
    for tid, tr in tracks:
        box = _coord_px(tr["coord"], shape)
        if tr.get("stationary_coord") and not recheck and not regions.intersects_any(box, motion):
            # Its parked spot, not its last box: repeating a recheck's slightly different box
            # for seconds counted as the object moving.
            seeded.append((tid, tr, _coord_px(tr["stationary_coord"], shape)))
        else:
            object_boxes.append(box)
    found = regions.regions_for(shape, object_boxes)
    found += regions.regions_for(shape, [b for b in motion if not regions.inside_any(b, found)])
    # A full look about once a second through the startup window, not just on the first
    # frame: on one coarse look a parked car at the doorbell scored too low, so it wasn't
    # parked at start and later opened an event as an arrival.
    scan = _startup_scan.setdefault(device_id, {"first": now, "last": None})
    if (now - scan["first"] <= STARTUP_WINDOW_MS / 1000.0
            and (scan["last"] is None or now - scan["last"] >= STARTUP_SCAN_INTERVAL_S)):
        scan["last"] = now
        found += regions.startup_regions(shape)
    dets = []
    for region in found:
        x1, y1, x2, y2 = (int(v) for v in region)
        for r in detector.detect(frame[y1:y2, x1:x2]):
            bx1, by1, bx2, by2 = r["box"]
            dets.append(dict(r, label=r.get("label") or r["objectType"], region=region,
                             box=(bx1 + x1, by1 + y1, bx2 + x1, by2 + y1)))
    out = regions.reduce_detections(shape, dets)
    out.sort(key=lambda r: -r["score"])
    for tid, tr, box in seeded:
        out.append({"objectType": tr["objectType"], "label": None, "score": tr["score"],
                    "box": box, "seeded": tid})
    return out


def _classify_frame_detections(device_id):
    """Return all qualifying detections for the latest frame as
    {objectType, score, coord} in normalized 0-1000 space, sorted by score;
    [] if none."""
    with _frame_lock:
        frame = _latest_frame.get(device_id)
        seq = _latest_frame_seq.get(device_id)
        if frame is not None:
            _analysed_frame_time[device_id] = _latest_frame_time.get(device_id) or _now_ms()
    if frame is None:
        return []
    try:
        detector = _get_detector()
        results = (_detect_regions(device_id, detector, frame) if REGIONS
                   else detector.detect(frame))
    except Exception:
        log.exception("classification failed for deviceID=%s", device_id)
        return []
    # Boxes are in detector-frame pixels; coords are 0-1000 on each axis of the image.
    sx = CAMERA_COORD_END / frame.shape[1]
    sy = CAMERA_COORD_END / frame.shape[0]
    out = []
    for r in results:
        x1, y1, x2, y2 = r["box"]
        coord = [max(0, x1 * sx), max(0, y1 * sy), max(1, (x2 - x1) * sx), max(1, (y2 - y1) * sy)]
        if r.get("seeded"):
            # A parked object not re-detected this frame keeps its last box (Frigate).
            out.append({"objectType": r["objectType"], "score": r["score"], "coord": coord,
                        "seeded": r["seeded"]})
            continue
        if _is_excluded(device_id, coord):
            log.info("real motion tick: %s detection excluded by configured excludeZones for "
                      "deviceID=%s (score=%.2f)", r["objectType"], device_id, r["score"])
            continue
        if _is_outside_detect_zones(device_id, coord, r["objectType"]):
            log.info("real motion tick: %s detection outside all configured detection zones for "
                      "deviceID=%s (score=%.2f)", r["objectType"], device_id, r["score"])
            continue
        d = {"objectType": r["objectType"], "score": r["score"], "coord": coord}
        if r["objectType"] == "vehicle" and enrich.vehicle_attrs_enabled():
            try:
                attrs = enrich.vehicle_attributes(r.get("label"), frame, coord)
            except Exception:
                log.exception("vehicle attributes failed for deviceID=%s", device_id)
                attrs = None
            if attrs:
                d["extra"] = {"attributes": attrs}
        out.append(d)
    # The full-res JPEG of this same frame: a track's best frame (and its thumbnail) comes
    # from it. Without one, _capture_best falls back to the latest snapshot.
    jpeg = _frame_jpeg(device_id, seq) if out and seq is not None else None
    if jpeg is not None:
        for d in out:
            d["frame"] = (seq, jpeg)
    return out


def _get_enricher():
    global _enricher
    with _detector_lock:
        if _enricher is None:
            faces_dir = os.environ.get("AIPORT_FACES_DIR") or \
                os.path.join(STATE_DIR or HERE, "faces")
            _enricher = enrich.Enricher(faces_dir)
        return _enricher


def _read_snapshot(device_id):
    """(mtime, JPEG bytes) of the latest full-res snapshot, re-read only when the
    file changed; (None, None) if unavailable. Face reads and best-frame capture
    share it, so a face box always refers to the exact bytes it was found in."""
    path = os.path.join(_STREAM_DIR, f"{device_id}.jpg")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, None
    cached = _snapshot_bytes.get(device_id)
    if cached and cached[0] == mtime:
        return cached
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None, None
    if not data.endswith(b"\xff\xd9"):  # torn write; the next tick retries
        return None, None
    _snapshot_bytes[device_id] = (mtime, data)
    return mtime, data


def _load_snapshot_bgr(device_id):
    """Latest full-res snapshot (BGR) for plate/face reads, decoded once per
    snapshot; (None, None) if unavailable."""
    mtime, data = _read_snapshot(device_id)
    if data is None:
        return None, None
    cached = _snapshot_cache.get(device_id)
    if cached and cached[0] == mtime:
        return cached[1], mtime
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is not None:
        _snapshot_cache[device_id] = (mtime, img)
    return img, mtime


def _capture_best(device_id, tr, d):
    """Keep the track's best-scoring moment: the full-res JPEG, the box and the
    time. On leave the thumbnails come from it, not from the frame several seconds after
    the object was last seen (which, for a face, no longer contains it)."""
    best = tr.get("best")
    if d.get("seeded") or (best and d["score"] < best["score"]):
        return
    mtime, data = d.get("frame") or _read_snapshot(device_id)
    if data is None:
        return
    tr["best"] = {"jpeg": data, "coord": list(d["coord"]), "score": d["score"],
                  "wall": _frame_wall_ms(device_id)}


# A plate can't change on a vehicle that isn't moving, and a confident read won't improve.
# As Frigate, a parked vehicle is still read for PLATE_PARKED_SCAN_MS after it parks (a car
# that has just pulled in often shows its plate only once it stops).
PLATE_CONFIDENT = 0.85
PLATE_PARKED_SCAN_MS = 5000
SETTLED_MATCH_IOU = 0.5


def _parked_scan_over(tr):
    """True once a parked track's plate-reading window has passed (or its start is unknown)."""
    parked, seen = tr.get("parked_at_ms"), tr.get("seen_ms")
    return parked is None or seen is None or seen - parked > PLATE_PARKED_SCAN_MS


def _plate_settled(device_id, coord):
    """True if this vehicle box belongs to a track that is parked, still, or already has a
    confident plate -- re-reading it is wasted work. A partial box mostly inside a parked
    car's box is that car (live 10:14 on Driveway: a parked car's plate was re-read and logged
    on every snapshot from boxes on part of it)."""
    with _motion_lock:
        by_id = dict(_motion_state.get(device_id, {}).get("tracks", {}))
    inside = _inside_parked(by_id, {"objectType": "vehicle", "coord": coord})
    if inside is not None and _parked_scan_over(by_id[inside]):
        return True
    tracks = list(by_id.values())
    for tr in tracks:
        if tr["objectType"] != "vehicle" or _iou(tr["coord"], coord) < SETTLED_MATCH_IOU:
            continue
        if ((tr.get("stationary_coord") and _parked_scan_over(tr)) or tr.get("still")
                or tr.get("extra_scores", {}).get("name", 0) >= PLATE_CONFIDENT):
            return True
    return False


def _enrich_detections(device_id, detections):
    """Face detections (plus plate text on vehicles) from this tick's boxes.
    Protect reads a vehicle's plate from its descriptor `name` and recognises a
    face from its `faceEmbed`. Results are reused until a newer snapshot lands, so
    tracks don't flap; plate/name reach the parent only on a fresh read (tracks keep them)."""
    vehicles = [d for d in detections if d["objectType"] == "vehicle"
                and not _plate_settled(device_id, d["coord"])]
    people = [d for d in detections if d["objectType"] == "person"]
    if not vehicles and not people:
        return []
    enricher = _get_enricher()
    if not (enricher.lpr and vehicles) and not (enricher.face and people):
        return []
    img, mtime = _load_snapshot_bgr(device_id)
    if img is None:
        return []
    cached = _enrich_cache.get(device_id)
    if cached and cached[0] == mtime:
        extra = cached[1]
    else:
        extra = []
        try:
            for p in enricher.plates(img, [v["coord"] for v in vehicles]):
                vehicles[p["parent"]].setdefault("scored", {})["name"] = (p["text"], p["confidence"])
                log.debug("enrichment: plate %r on vehicle for deviceID=%s (score=%.2f)",
                         p["text"], device_id, p["confidence"])
            for f in enricher.faces(img, [p["coord"] for p in people]):
                face = {"objectType": "face", "score": f["score"], "coord": f["coord"],
                        "extra": {"name": f["name"], "tag": f["name"]},
                        "frame": _snapshot_bytes.get(device_id)}
                if f["embedding"] is not None:
                    face["scored"] = {"faceEmbed": (f["embedding"], f["score"])}
                extra.append(face)
                if f["name"]:
                    people[f["parent"]].setdefault("extra", {}).update(
                        {"name": f["name"], "tag": f["name"]})
        except Exception:
            log.exception("plate/face enrichment failed for deviceID=%s", device_id)
        _enrich_cache[device_id] = (mtime, extra)
        for e in extra:
            log.info("enrichment: %s for deviceID=%s (score=%.2f, name=%r, embedding=%s)",
                     e["objectType"], device_id, e["score"], e["extra"]["name"],
                     "faceEmbed" in e.get("scored", {}))
    return [dict(e, extra=dict(e["extra"])) for e in extra]


def _size_jump(a, b):
    """True if box b ([x, y, w, h]) is over NN_SIZE_RATIO_MAX x wider or taller than a, or
    under 1/NN_SIZE_RATIO_MAX x: a different object, not this one moving."""
    for i in (2, 3):
        lo, hi = sorted((max(a[i], 1e-6), max(b[i], 1e-6)))
        if hi / lo > NN_SIZE_RATIO_MAX:
            return True
    return False


def _nearest_detection(tr, detections, unclaimed):
    """Index of the nearest same-type unclaimed detection to where tr's motion predicts it,
    within NN_DIST_FACTOR x its size (+ margin), at most NN_DIST_MAX, widening a little
    while it's missed; None if none."""
    cx, cy = _zone_center_norm(tr["coord"])
    vx, vy = tr.get("velocity", (0.0, 0.0))
    steps = 1 + tr["miss_count"]
    px, py = cx + vx * steps, cy + vy * steps
    limit = (min(NN_DIST_FACTOR * max(tr["coord"][2], tr["coord"][3]) + NN_DIST_MARGIN, NN_DIST_MAX)
             * min(1 + NN_MISS_GROWTH * tr["miss_count"], NN_MISS_GROWTH_MAX))
    best, best_dist = None, limit
    for idx in unclaimed:
        d = detections[idx]
        if d["objectType"] != tr["objectType"] or _size_jump(tr["coord"], d["coord"]):
            continue
        dx, dy = _zone_center_norm(d["coord"])
        dist = ((dx - px) ** 2 + (dy - py) ** 2) ** 0.5
        if dist <= best_dist:
            best, best_dist = idx, dist
    return best


def _follow(tr, coord):
    """Move a track to a new raw box: velocity per tick and the smoothed reporting box."""
    (ox, oy), (nx, ny) = _zone_center_norm(tr["coord"]), _zone_center_norm(coord)
    steps = 1 + tr.get("miss_count", 0)
    tr["velocity"] = ((nx - ox) / steps, (ny - oy) / steps)
    prev = tr.get("smooth") or tr["coord"]
    tr["smooth"] = [BOX_SMOOTHING * c + (1 - BOX_SMOOTHING) * p for c, p in zip(coord, prev)]
    tr["coord"] = coord


def _jaro_winkler(a, b):
    """Jaro-Winkler similarity of two strings, 0-1 (as rapidfuzz's, which Frigate uses)."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    reach = max(0, max(len(a), len(b)) // 2 - 1)
    a_hit, b_hit = [False] * len(a), [False] * len(b)
    matches = 0
    for i, ch in enumerate(a):
        for j in range(max(0, i - reach), min(len(b), i + reach + 1)):
            if not b_hit[j] and b[j] == ch:
                a_hit[i] = b_hit[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    a_m = [ch for ch, hit in zip(a, a_hit) if hit]
    b_m = [ch for ch, hit in zip(b, b_hit) if hit]
    half_transpositions = sum(x != y for x, y in zip(a_m, b_m))
    jaro = (matches / len(a) + matches / len(b) + (matches - half_transpositions / 2) / matches) / 3
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


# Plate reads: as Frigate, a track keeps its recent reads, groups those whose average
# similarity is >= PLATE_CLUSTER_SIMILARITY and takes the largest group (ties: the group with
# the most confident read). Unlike Frigate, which reports that group's most confident read,
# the reading seen most often in it wins (ties: the most confident), so one confident misread
# ('DEBM782' among 'DEPM782's) can't replace the plate.
PLATE_READS_KEEP = 25
PLATE_CLUSTER_SIMILARITY = 0.85


def _plate_rep(reads):
    """(text, confidence) representing a track's [(text, confidence), ...] plate reads."""
    clusters = []
    for read in reads:
        for cluster in clusters:
            if sum(_jaro_winkler(read[0], r[0]) for r in cluster) / len(cluster) >= PLATE_CLUSTER_SIMILARITY:
                cluster.append(read)
                break
        else:
            clusters.append([read])
    best = max(clusters, key=lambda c: (len(c), max(r[1] for r in c)))
    counts = collections.Counter(r[0] for r in best)
    text = max(counts, key=lambda t: (counts[t], max(r[1] for r in best if r[0] == t)))
    return text, max(r[1] for r in best if r[0] == text)


def _merge_extra(tr, d, device_id=None, tracker_id=None):
    """Carry enrichment fields onto a track. A vehicle's plate (`name`) is the
    representative of its recent reads (_plate_rep), logged when it first appears or
    changes; other scored keys (a face's `faceEmbed`) keep the best-scoring read; other
    keys (attributes, names) take the newest value and are never cleared by a
    tick that lacks them."""
    out = tr.setdefault("extra", {})
    out.update({k: v for k, v in (d.get("extra") or {}).items() if v not in ("", None)})
    scores = tr.setdefault("extra_scores", {})
    for key, (value, score) in (d.get("scored") or {}).items():
        if key == "name" and tr.get("objectType") == "vehicle":
            reads = tr.setdefault("plate_reads", [])
            reads.append((value, score))
            del reads[:-PLATE_READS_KEEP]
            text, conf = _plate_rep(reads)
            if text != tr.get("plate_logged"):
                log.info("plate %r for vehicle tracker_id=%s deviceID=%s (score=%.2f, %d read(s))",
                         text, tracker_id, device_id, conf, len(reads))
                tr["plate_logged"] = text
            out[key], scores[key] = text, conf
        elif score >= scores.get(key, 0):
            out[key] = value
            scores[key] = score


def _iou(box_a, box_b):
    """Intersection-over-union of two [x, y, w, h] boxes."""
    ax1, ay1, aw, ah = box_a
    bx1, by1, bw, bh = box_b
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _parse_zone_types(zones_field):
    """Each zone's objectTypes (None when it doesn't say), in _parse_zones' order."""
    out = []
    for zone in (zones_field or {}).values():
        coord = zone.get("coord") or []
        if len(coord) >= 6 and len(coord) % 2 == 0:
            types = zone.get("objectTypes")
            out.append(list(types) if isinstance(types, list) else None)
    return out


def _parse_zones(zones_field):
    """Parse ChangeSmartDetectSettings zones/excludeZones into polygons
    (even-length coord, >= 3 points)."""
    polygons = []
    for zone in (zones_field or {}).values():
        coord = zone.get("coord") or []
        if len(coord) >= 6 and len(coord) % 2 == 0:
            polygons.append(list(zip(coord[0::2], coord[1::2])))
    return polygons


def _parse_lines(lines_field):
    """Parse ChangeSmartDetectSettings.lines into 2-point segments (normalized
    0-1000; a `points` fallback is accepted)."""
    out = []
    for line_id, line in (lines_field or {}).items():
        if not isinstance(line, dict):
            continue
        pts = []
        coord = line.get("coord") or []
        if len(coord) >= 4 and len(coord) % 2 == 0:
            pts = list(zip(coord[0::2], coord[1::2]))
        else:
            for pt in (line.get("points") or []):
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    pts.append((pt[0], pt[1]))
        if len(pts) < 2:
            continue
        out.append({
            "id": str(line_id),
            "p1": pts[0],
            "p2": pts[-1],
            "direction": line.get("direction") or line.get("crosslineDirection"),
            "object_types": line.get("objectTypes") or line.get("object_types"),
        })
    return out


def _line_ref(line_id):
    """Return a numeric line id when numeric, else the string (controller keys
    numeric ids as numbers)."""
    s = str(line_id)
    return int(s) if s.isdigit() else s


def _line_side(p1, p2, pt):
    """Sign of the cross product: which side of directed line p1->p2 `pt` is on."""
    return (p2[0] - p1[0]) * (pt[1] - p1[1]) - (p2[1] - p1[1]) * (pt[0] - p1[0])


def _segment_crosses_line(prev, cur, p1, p2):
    """True if segment prev->cur crosses segment p1->p2."""
    d1 = _line_side(p1, p2, prev)
    d2 = _line_side(p1, p2, cur)
    d3 = _line_side(prev, cur, p1)
    d4 = _line_side(prev, cur, p2)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _process_line_crossings(ws, device_id, tracker_id, tr, new_center):
    """Emit a smartDetectLine event for each configured line this movement
    crosses (first crossing = enter, later = moving)."""
    lines = _lines.get(device_id)
    prev_center = tr.get("prev_center")
    if not lines or prev_center is None:
        return
    for line in lines:
        types = line.get("object_types")
        if types and tr["objectType"] not in types:
            continue
        if not _segment_crosses_line(prev_center, new_center, line["p1"], line["p2"]):
            continue
        direction = "A2B" if _line_side(line["p1"], line["p2"], prev_center) < 0 else "B2A"
        entry = tr.setdefault("crossed_lines", {}).setdefault(line["id"], {
            "a2b": 0, "b2a": 0, "direction": direction, "event_id": None,
        })
        entry["a2b" if direction == "A2B" else "b2a"] += 1
        entry["direction"] = direction
        edge_type = "moving" if entry["event_id"] is not None else "enter"
        if entry["event_id"] is None:
            entry["event_id"] = next(_event_id_counter)
        log.info("line crossing: %s crossed line %s (%s, a2b=%d b2a=%d) for deviceID=%s "
                  "tracker_id=%s -- sending %s", tr["objectType"], line["id"], direction,
                  entry["a2b"], entry["b2a"], device_id, tracker_id, edge_type)
        try:
            _send_line_detect_event(ws, device_id, line, edge_type, entry["event_id"],
                                    direction, entry["a2b"], entry["b2a"],
                                    tr["objectType"], tr["coord"], tr["score"],
                                    tr["first_shown_ms"], tracker_id)
        except Exception:
            log.exception("failed to send line %s event for tracker_id=%s deviceID=%s",
                          line["id"], tracker_id, device_id)


def _dedupe_detections(detections, iou_thresh=0.4):
    """Collapse same-type detections that heavily overlap into one (highest
    score wins). Per-class NMS in detector.py uses NMS_THRESH 0.45, but two
    boxes for ONE object can still survive it (e.g. a full-body box plus a
    partial/torso box with IoU just under the gate) -- observed live as two
    `person detected` ticks 1ms apart at scores 0.67/0.51 for the same person.
    Left alone, each unmatched detection opens its own track, so one object
    produces two tracks, two event streams, and no clean matched pair -- which
    is fatal for line crossing (it needs a matched pair to get a movement
    segment)."""
    out = []
    for d in sorted(detections, key=lambda d: -d["score"]):
        if any(d["objectType"] == k["objectType"] and _iou(d["coord"], k["coord"]) >= iou_thresh
               for k in out):
            continue
        out.append(d)
    return out


_scene_ref = {}  # deviceID -> (downsampled grayscale reference frame, monotonic ms taken)
_motion_detectors = {}  # deviceID -> motion.MotionDetector (rebuilt when settings change)
_motion_settings = {}   # deviceID -> {"level", "zones"} from ChangeSmartMotionSettings
_motion_boxes = {}      # deviceID -> this frame's motion boxes (detector-frame pixels)


def _canvas_to_image(device_id, x, y):
    """Protect canvas point (0-1000) -> image-normalised point (0-1000), clamped."""
    sx, sy = _canvas_scale(device_id)
    half = CAMERA_COORD_END / 2
    clamp = lambda v: min(CAMERA_COORD_END, max(0.0, v))  # noqa: E731
    return clamp(half + (x - half) / sx), clamp(half + (y - half) / sy)


def _motion_contour_area(device_id):
    """AIPORT_MOTION_CONTOUR_AREA ("<default>[,<deviceID>=<area>...]"): the smallest motion
    blob, in pixels of the 100-row motion frame (Frigate's motion.contour_area; 10 by
    default, as in Frigate)."""
    import motion
    default, per = _parse_box_delay(os.environ.get("AIPORT_MOTION_CONTOUR_AREA"),
                                    "AIPORT_MOTION_CONTOUR_AREA", motion.DEFAULT_CONTOUR_AREA)
    return per.get(str(device_id).upper(), default)


def _motion_detector(device_id, shape):
    det = _motion_detectors.get(device_id)
    if det is None or det.frame_shape != tuple(shape[:2]):
        import motion
        settings = _motion_settings.get(device_id) or {}
        override = os.environ.get("AIPORT_MOTION_THRESHOLD")
        threshold = (int(override) if override and override.isdigit()
                     else motion.threshold_for_level(settings.get("level", 50)))
        mask = motion.zones_mask(settings.get("zones"), shape,
                                 lambda x, y: _canvas_to_image(device_id, x, y))
        contour_area = _motion_contour_area(device_id)
        det = _motion_detectors[device_id] = motion.MotionDetector(
            shape[:2], threshold=threshold, contour_area=contour_area, mask=mask)
        log.info("motion detector for deviceID=%s: threshold %d, contour area %d, %s", device_id,
                 threshold, contour_area,
                 "masked to Protect's motion zones" if mask is not None else "whole frame")
    return det


def _scene_changed(device_id):
    """True if this frame has motion boxes (Frigate's motion detector, which must see
    every frame to keep its background current). While it calibrates -- at start, or after
    lightning or IR switching changed most of the picture -- nothing counts as motion."""
    with _frame_lock:
        frame = _latest_frame.get(device_id)
    if frame is None:
        return False
    try:
        import cv2
    except ImportError:
        return _scene_changed_mean(device_id, frame)
    det = _motion_detector(device_id, frame.shape)
    boxes = det.detect(cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY))
    _motion_boxes[device_id] = boxes
    return bool(boxes) and not det.is_calibrating()


def _scene_changed_mean(device_id, frame):
    """Fallback without OpenCV: true if this frame differs enough from the reference. The reference
    advances at most once per report interval: frames analysed faster than that are only
    a little apart, and slow movement would otherwise stay under the gate. A static scene
    still stays below it."""
    import numpy as np  # lazy, like the detector path: avclient imports without numpy
    gray = frame[::_GATE_STRIDE, ::_GATE_STRIDE].mean(axis=2)
    now = int(time.monotonic() * 1000)
    ref, ref_ms = _scene_ref.get(device_id) or (None, None)
    if ref is None or now - ref_ms >= REPORT_INTERVAL_MS:
        _scene_ref[device_id] = (gray, now)
    if ref is None or ref.shape != gray.shape:
        return True
    return float(np.abs(gray - ref).mean()) / 255.0 >= MOTION_GATE_THRESH


# Protect keeps ONE smart-detect event per camera (not per track): the first leave
# closes it, and its scheduled check force-ends one open > ~5.5 min with no snapshots.
# So tracks share a camera-wide event: enter when the first track appears, a batched
# moving per tick, and one leave (every participant's snapshot) when the last active
# track goes or eventMaxDurationMSec (sent in ChangeSmartMotionSettings) runs out.
DEFAULT_EVENT_MAX_MS = 300000
_event_max_ms = {}  # deviceID -> eventMaxDurationMSec from ChangeSmartMotionSettings
# A new track joins the camera event only once it has been seen for this long, so a
# detection that lasts a frame or two never opens a dashboard event. Protect sends it as
# eventStartMSec in ChangeSmartDetectSettings; AIPORT_CONFIRM_MS overrides it. 0 = at once.
_event_start_ms = {}  # deviceID -> eventStartMSec from ChangeSmartDetectSettings


def _confirm_ms(device_id):
    override = os.environ.get("AIPORT_CONFIRM_MS")
    if override:
        try:
            return max(0, int(override))
        except ValueError:
            log.warning("ignoring bad AIPORT_CONFIRM_MS %r", override)
    return _event_start_ms.get(device_id, 0)
# A track still present when its event hit the time limit goes quiet (stationary, e.g. a
# parked car) until it really moves: its box mostly leaves the parked spot, or its centre
# stays shifted (0-1000 image units) for several ticks at about the same size. Someone
# walking past shrinks the box for a moment without moving it off its spot.
STATIONARY_LEFT_IOU = 0.3
STATIONARY_RESUME_DISTANCE = 50
STATIONARY_RESUME_TICKS = _ticks(6)
STATIONARY_SIZE_RATIO = (0.7, 1.4)
# A track whose box keeps overlapping its anchor (IoU >= STILL_IOU) for STILL_TICKS ticks
# (~30 s) is still: its descriptors say stationary, and a still vehicle is parked (its
# snapshot stays in the event's leave). Objects found within STARTUP_WINDOW_MS of a camera's
# tracker or stream starting (cars already parked there) are parked at once instead of opening
# an event; a stream restart starts the window again.
STILL_IOU = 0.7
STILL_TICKS = _ticks(30)
STARTUP_WINDOW_MS = 15000
PARK_WHEN_STILL = ("vehicle",)


def _center_moved(a, b, frac=RESUME_MOVE_FRACTION):
    """True if box b's centre is more than `frac` of box a's size from a's centre."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return (abs((bx + bw / 2) - (ax + aw / 2)) > frac * aw
            or abs((by + bh / 2) - (ay + ah / 2)) > frac * ah)


def _inside_parked(tracks, d, frac=INSIDE_PARKED_FRACTION):
    """tracker_id of a parked same-type track whose box holds >= frac of d's box, or None:
    a second box on part of a parked car (the doorbell saw one on its lower half) is the
    same car, not a new vehicle."""
    x, y, w, h = d["coord"]
    area = max(1.0, w * h)
    for tid, tr in tracks.items():
        if tr["objectType"] != d["objectType"] or not tr.get("stationary_coord"):
            continue
        px, py, pw, ph = tr["stationary_coord"]
        iw = min(x + w, px + pw) - max(x, px)
        ih = min(y + h, py + ph) - max(y, py)
        if iw > 0 and ih > 0 and iw * ih / area >= frac:
            return tid
    return None


def _resume_recent(dev, d, event):
    """(tracker_id, track, how) of a recently closed track detection d continues, taken out
    of dev["recent"]; how is "event" (its event is still open) or "parked" (a vehicle that
    never moved, or was parked). None if d is a new object."""
    found, best_iou = None, RESUME_IOU
    for i, (tid, tr, _closed_ms) in enumerate(dev.get("recent", ())):
        if tr["objectType"] != d["objectType"]:
            continue
        iou = max(_iou(tr["coord"], d["coord"]), _iou(tr.get("first_coord") or tr["coord"], d["coord"]))
        if iou < best_iou:
            continue
        if tr.get("stationary_coord"):
            how = "parked"  # stays in its event's finished list, if it is in one
        elif event is not None and tr.get("event_id") == event["id"]:
            how = "event"
        elif tr["objectType"] in PARK_WHEN_STILL and not tr.get("moved"):
            how = "parked"
        else:
            continue
        found, best_iou = (i, tid, tr, how), iou
    if found is None:
        return None
    i, tid, tr, how = found
    del dev["recent"][i]
    return tid, tr, how


def _update_still(tr, now_ms):
    """Advance a track's stillness against its anchor box (detector jitter tolerated)."""
    anchor = tr.get("anchor")
    if anchor is None or _iou(tr["coord"], anchor) < STILL_IOU:
        tr["anchor"] = list(tr["coord"])
        tr["still_ticks"] = 0
        tr["still_since_ms"] = now_ms
    else:
        tr["still_ticks"] = tr.get("still_ticks", 0) + 1
    tr["still"] = tr["still_ticks"] >= STILL_TICKS


def _park(tr):
    tr["stationary_coord"] = list(tr["coord"])
    tr["parked_at_ms"] = tr.get("seen_ms")
    tr["moved_ticks"] = 0


def _track_args(tracker_id, tr):
    still = bool(tr.get("still"))
    return {"tracker_id": tracker_id, "object_type": tr["objectType"],
            "coord": tr.get("smooth") or tr["coord"],
            "confidence": tr["score"], "first_shown_ms": tr["first_shown_ms"],
            "descriptor_extra": tr.get("extra"), "best": tr.get("best"),
            "still": still, "idle_since_ms": tr.get("still_since_ms") if still else None}


def _send_event_message(ws, device_id, edge_type, event_id, members, stationary=False):
    """One EventSmartDetect for a camera event carrying every member track
    (faces last, so a leave's Full-FoV comes from a person/vehicle best frame)."""
    if not members:
        return
    members = sorted(members, key=lambda m: m[1]["objectType"] == "face")
    args = [_track_args(tid, tr) for tid, tr in members]
    try:
        _send_smart_detect_event(ws, device_id, edge_type=edge_type, event_id=event_id,
                                  stationary=stationary, also_leaving=args[1:], **args[0])
    except Exception:
        log.exception("failed to send %s for tracker_ids=%s deviceID=%s", edge_type,
                      [tid for tid, _ in members], device_id)


# Protect fetched snapshots for single-tracker leaves but, seen live, requested nothing
# for a leave carrying two trackers (and logged nothing). So an event closes with one
# tracker: the most useful participant's best frame.
LEAVE_PRIORITY = {"face": 0, "person": 1, "vehicle": 2, "animal": 3}


# A multi-participant event closes the way Protect's backend reads it (onObjectsLeft,
# getAssociatedFaceTrackerIds, publishFace, fillAndCorrectTrackInfo): empty descriptors,
# a snapshot per participant, and per-tracker attributes in trackerIDAttrMap (faceEmbed,
# associatedFaceTrackerID, vehicleType/color, plate as matchedName). If Protect requests
# none of its snapshots within LEAVE_FALLBACK_S, the proven single-tracker leave is resent.
LEAVE_FALLBACK_S = 4.0
_pending_leaves = {}  # snapshot filename -> shared {"seen": bool} for a multi-tracker leave


def _center_inside(inner, outer):
    cx, cy = _zone_center_norm(inner)
    x, y, w, h = outer
    return x <= cx <= x + w and y <= cy <= y + h


def _associate_faces(members):
    """{person tracker_id: face tracker_id} for faces inside a person's box."""
    def box(tr):
        return (tr.get("best") or {}).get("coord") or tr["coord"]
    people = [(tid, tr) for tid, tr in members if tr["objectType"] == "person"]
    out = {}
    for fid, face in members:
        if face["objectType"] != "face":
            continue
        holders = [(tid, tr) for tid, tr in people
                   if tid not in out and _center_inside(box(face), box(tr))]
        if holders:
            tid, _ = min(holders, key=lambda m: box(m[1])[2] * box(m[1])[3])
            out[tid] = fid
    return out


def _tracker_attrs(tracker_id, tr, faces_by_person):
    """trackerIDAttrMap entry for one participant (attributeSchema keys)."""
    entry = {"objectType": tr["objectType"], "zone": [int(SMART_DETECT_ZONE_ID)]}
    extra = tr.get("extra") or {}
    if tr["objectType"] == "face" and extra.get("faceEmbed"):
        entry["faceEmbed"] = extra["faceEmbed"]
    if tr["objectType"] == "person" and tracker_id in faces_by_person:
        entry["associatedFaceTrackerID"] = faces_by_person[tracker_id]
    if tr["objectType"] == "vehicle":
        for key in ("vehicleType", "color"):
            if key in (extra.get("attributes") or {}):
                entry[key] = extra["attributes"][key]
        if extra.get("name"):
            entry["matchedName"] = extra["name"]
    return entry


def _send_multi_leave(ws, device_id, event_id, members, fallback):
    """Close a multi-participant event in one leave; run fallback() if Protect then
    requests none of its snapshots within LEAVE_FALLBACK_S."""
    members = sorted(members, key=lambda m: (m[1]["objectType"] == "face",
                                              LEAVE_PRIORITY.get(m[1]["objectType"], 9)))
    now_ms = _frame_wall_ms(device_id)
    clock_mono, clock_stream = _stream_clocks(device_id)
    seq = _snapshot_seq_by_device.get(device_id, 0) + 1
    _snapshot_seq_by_device[device_id] = seq
    fov_w, fov_h = _stream_dims.get(device_id, (DETECT_IMG_SIZE, DETECT_IMG_SIZE))
    fullfov = f"smartdetectsnap_zone_{seq:08d}_fullfov.jpg"
    first_best = members[0][1].get("best")
    _record_snapshot_filename(fullfov, device_id, kind="fullfov",
                              jpeg=first_best["jpeg"] if first_best else None)
    faces_by_person = _associate_faces(members)
    snapshots, attr_map = [], {}
    for tid, tr in members:
        b = tr.get("best")
        crop = list(b["coord"] if b else tr["coord"])
        box = [int(round(v)) for v in _to_protect(device_id, crop)]
        wall = b["wall"] if b else tr["first_shown_ms"]
        name = f"smartdetectsnap_zone_{tid}{wall}.jpg"
        _record_snapshot_filename(name, device_id, kind="object", coord=crop,
                                  jpeg=b["jpeg"] if b else None)
        level = int(round(tr["score"] * 100)) if tr["score"] <= 1 else int(round(tr["score"]))
        snapshots.append({
            "clockBestMonotonic": 0, "clockBestWall": wall, "confidenceLevel": level,
            "coord": box, "framingRect": box, "reVerifyEligible": False,
            "smartDetectSnapshot": name, "smartDetectSnapshotHeight": SMART_SNAPSHOT_SIZE,
            "smartDetectSnapshotName": "", "smartDetectSnapshotType": tr["objectType"],
            "smartDetectSnapshotWidth": SMART_SNAPSHOT_SIZE, "trackerID": tid,
        })
        attr_map[str(tid)] = _tracker_attrs(tid, tr, faces_by_person)
    level = max(s_["confidenceLevel"] for s_ in snapshots)
    payload = {
        "deviceID": device_id, "clockMonotonic": clock_mono, "clockStream": clock_stream,
        "clockStreamRate": 1000, "clockWall": now_ms,
        # Departing trackers aren't repeated in descriptors (real captures, notes s7).
        "descriptors": [], "displayTimeoutMSec": 200, "edgeType": "leave",
        "eventId": event_id, "objectTypes": [],
        "smartDetectSnapshotFullFoV": fullfov,
        "smartDetectSnapshotFullFoVHeight": fov_h, "smartDetectSnapshotFullFoVWidth": fov_w,
        "smartDetectSnapshots": snapshots, "trackerIDAttrMap": attr_map,
        "zonesStatus": {SMART_DETECT_ZONE_ID: {"level": level, "status": "leave"}},
    }
    pending = {"seen": False}
    names = [fullfov] + [x["smartDetectSnapshot"] for x in snapshots]
    for n in names:
        _pending_leaves[n] = pending

    def _check():
        for n in names:
            _pending_leaves.pop(n, None)
        if pending["seen"]:
            log.info("multi-tracker leave for event %s deviceID=%s: snapshots requested",
                     event_id, device_id)
            return
        log.warning("multi-tracker leave for event %s deviceID=%s got no snapshot request in "
                    "%.0fs; resending as a single-tracker leave", event_id, device_id,
                    LEAVE_FALLBACK_S)
        try:
            fallback()
        except Exception:
            log.exception("single-tracker fallback leave failed for event %s", event_id)

    send_msg(ws, "EventSmartDetect", payload, response_expected=False)
    timer = threading.Timer(LEAVE_FALLBACK_S, _check)
    timer.daemon = True
    timer.start()
    return timer


def _leave_track(members):
    """(tracker_id, track) whose snapshot closes the event: face > person > vehicle >
    animal, then the best-scoring frame."""
    return min(members, key=lambda m: (LEAVE_PRIORITY.get(m[1]["objectType"], 9),
                                       -(m[1].get("best") or {}).get("score", m[1]["score"])))


def _close_line_events(ws, device_id, tracker_id, tr):
    """Close any line events this track opened (line events stay per track)."""
    for line_id, entry in tr.get("crossed_lines", {}).items():
        if entry["event_id"] is None:
            continue
        line = next((l for l in _lines.get(device_id, []) if l["id"] == line_id), None)
        if line is None:
            continue
        try:
            _send_line_detect_event(ws, device_id, line, "leave", entry["event_id"],
                                    entry["direction"], entry["a2b"], entry["b2a"],
                                    tr["objectType"], tr["coord"], tr["score"],
                                    tr["first_shown_ms"], tracker_id)
        except Exception:
            log.exception("failed to send line leave for line=%s tracker_id=%s deviceID=%s",
                          line_id, tracker_id, device_id)


def _moved_since(tr):
    """True once a stationary track has really moved off its parked spot (occlusion-proof)."""
    cur, parked = tr["coord"], tr["stationary_coord"]
    if _iou(cur, parked) < STATIONARY_LEFT_IOU:
        return True
    cx, cy = _zone_center_norm(cur)
    sx, sy = _zone_center_norm(parked)
    shifted = ((cx - sx) ** 2 + (cy - sy) ** 2) ** 0.5 > STATIONARY_RESUME_DISTANCE
    ratio = (cur[2] * cur[3]) / max(1.0, parked[2] * parked[3])
    same_size = STATIONARY_SIZE_RATIO[0] <= ratio <= STATIONARY_SIZE_RATIO[1]
    tr["moved_ticks"] = tr.get("moved_ticks", 0) + 1 if shifted and same_size else 0
    return tr["moved_ticks"] >= STATIONARY_RESUME_TICKS


def _builtin_match(tracks, detections):
    """(matches {tracker_id: detection index}, unclaimed indices): each track takes its
    best remaining same-type detection by IoU, then still-unmatched moving tracks take the
    nearest detection where their motion predicts them (parked tracks don't, so a passing
    car can't wake one)."""
    unclaimed = list(range(len(detections)))
    # A lone track and a lone detection of a type no longer match unconditionally: a
    # missed track then took a car anywhere in the frame. The nearest rule below covers a
    # fast mover that no box overlaps.
    matches = {}
    for tracker_id, tr in tracks.items():
        best_idx, best_iou = None, IOU_MATCH_THRESHOLD
        for idx in unclaimed:
            d = detections[idx]
            if d["objectType"] != tr["objectType"]:
                continue
            iou = _iou(tr["coord"], d["coord"])
            if iou > best_iou:
                best_idx, best_iou = idx, iou
        if best_idx is not None:
            matches[tracker_id] = best_idx
            unclaimed.remove(best_idx)
    for tracker_id, tr in sorted(tracks.items(), key=lambda m: m[1]["miss_count"]):
        if tracker_id in matches or tr.get("stationary_coord"):
            continue
        idx = _nearest_detection(tr, detections, unclaimed)
        if idx is not None:
            matches[tracker_id] = idx
            unclaimed.remove(idx)

    return matches, unclaimed


def _norfair_match(dev, tracks, detections):
    """(matches, unclaimed, pending) with Norfair first: a detection associated with an
    object that maps to a live track matches it, except that a parked track only takes a
    box overlapping its parked spot (a passing car mustn't take over a parked one). Then
    pi-port's own rules for what Norfair left: parked tracks by IoU, moving tracks by the
    nearest detection where their motion predicts them (a small person moving 3x their width
    between frames is past Norfair's distance limit). pending {index: Norfair id} binds each
    Norfair object to the track its detection ends up in."""
    import norfair_match
    nf = dev.get("norfair")
    if nf is None:
        nf = dev["norfair"] = norfair_match.Matcher(PARKED_MISS_TOLERANCE_TICKS)
    ids = dev.setdefault("norfair_ids", {})
    for gid in [g for g, tid in ids.items() if tid not in tracks]:
        del ids[gid]
    associated = nf.match(detections)
    matches = {}
    # A parked object's repeated box is its own (two overlapping parked boxes on one camper
    # mustn't trade places through Norfair).
    for idx, d in enumerate(detections):
        tid = d.get("seeded")
        if tid in tracks and tid not in matches:
            matches[tid] = idx
            if idx in associated:
                ids[associated[idx]] = tid
    for idx, gid in associated.items():
        if idx in matches.values():
            continue
        tid = ids.get(gid)
        tr = tracks.get(tid)
        if tr is None or tid in matches:
            continue
        if (tr.get("stationary_coord")
                and _iou(tr["stationary_coord"], detections[idx]["coord"]) < IOU_MATCH_THRESHOLD):
            del ids[gid]  # Norfair moved this object onto another one: the parked track lets go
            continue
        if not tr.get("stationary_coord") and _size_jump(tr["coord"], detections[idx]["coord"]):
            del ids[gid]  # a box of another size beside it is another object
            continue
        matches[tid] = idx
    unclaimed = [i for i in range(len(detections)) if i not in set(matches.values())]
    for tid, tr in tracks.items():
        if tid in matches:
            continue
        idx = None
        if tr.get("stationary_coord"):
            best = IOU_MATCH_THRESHOLD
            for i in unclaimed:
                d = detections[i]
                if d["objectType"] == tr["objectType"]:
                    iou = _iou(tr["stationary_coord"], d["coord"])
                    if iou >= best:
                        idx, best = i, iou
        else:
            idx = _nearest_detection(tr, detections, unclaimed)
        if idx is not None:
            matches[tid] = idx
            unclaimed.remove(idx)
            if idx in associated:
                ids[associated[idx]] = tid
    pending = {i: associated[i] for i in unclaimed if i in associated}
    return matches, unclaimed, pending


def _frame_gray(device_id):
    """This frame in grayscale (detector-frame pixels), or None without one or OpenCV."""
    with _frame_lock:
        frame = _latest_frame.get(device_id)
    if frame is None:
        return None
    try:
        import cv2
    except ImportError:
        return None
    return cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)


def _coord_px(coord, shape):
    """Image-normalised [x, y, w, h] -> pixel box (x1, y1, x2, y2) in a frame of shape."""
    h, w = shape[:2]
    x, y, bw, bh = coord
    s = CAMERA_COORD_END
    return (x * w / s, y * h / s, (x + bw) * w / s, (y + bh) * h / s)


def _anchor_parked(device_id, tracks, now_ms=0):
    """Give each parked track an anchor crop of its spot, for _parked_spot_unchanged."""
    todo = [tr for tr in tracks.values() if tr.get("stationary_coord") and "pixel_anchor" not in tr]
    if not todo:
        return
    gray = _frame_gray(device_id)
    if gray is None:
        return
    import stationary
    for tr in todo:
        tr["pixel_anchor"] = stationary.anchor(gray, _coord_px(tr["stationary_coord"], gray.shape))
        tr["anchor_ms"] = now_ms


def _parked_spot_unchanged(device_id, tracker_id, tr):
    """True if the parked track's spot still looks as it did when it parked (Frigate's
    stationary classifier), though its boxes say it moved. Without an anchor or a frame,
    False: the boxes decide, as before."""
    state = tr.get("pixel_anchor")
    gray = _frame_gray(device_id) if state else None
    if gray is None:
        return False
    import stationary
    keep = stationary.still_there(state, gray)
    ncc, shift = state["last"]
    log.log(logging.DEBUG if keep else logging.INFO,
            "real motion tick: parked %s tracker_id=%s boxes moved; its spot %s (ncc %.2f, "
            "shift %.3f) for deviceID=%s", tr["objectType"], tracker_id,
            "looks the same, staying parked" if keep else "changed", ncc, shift, device_id)
    return keep


EMPTY_MOTION_LOG_S = 60.0
_empty_motion = {}  # deviceID -> [monotonic s of the last log line, frames since]


def _log_empty_motion(device_id):
    """"Scene changed but nothing found", at most once a minute per camera with the count:
    leaves and shadows on Backyard moved on ~40 frames a minute."""
    now = time.monotonic()
    entry = _empty_motion.setdefault(device_id, [None, 0])
    entry[1] += 1
    if entry[0] is None or now - entry[0] >= EMPTY_MOTION_LOG_S:
        log.info("real motion tick: scene changed but no person/vehicle/animal found for "
                 "deviceID=%s -- ignoring (not smart-detect-worthy; %d such frame(s) since "
                 "the last note)", device_id, entry[1])
        entry[0], entry[1] = now, 0


def _motion_tick(ws, device_id):
    # With open tracks we always classify (a stationary object must keep its track
    # and be able to close); otherwise skip the classifier when nothing moved.
    with _motion_lock:
        dev_state = _motion_state.get(device_id, {})
        has_tracks = bool(dev_state.get("tracks"))
        started = dev_state.get("started_ms")
    with _frame_lock:  # this tick describes this frame, whether or not the gate skips it
        if device_id in _latest_frame_time:
            _analysed_frame_time[device_id] = _latest_frame_time[device_id]
    changed = _scene_changed(device_id)
    # The startup look runs on a still scene too: live 17:05 the doorbell saw no motion for
    # 27 s at dusk, so its parked cars were first detected after the window and opened an event.
    in_startup = started is None or _frame_wall_ms(device_id) - started <= STARTUP_WINDOW_MS
    gated = MOTION_GATE and not has_tracks and not changed and not in_startup
    if gated:
        log.debug("motion gate: static scene, skipping inference for deviceID=%s", device_id)
        detections = []
    else:
        detections = _dedupe_detections(_classify_frame_detections(device_id))
        detections += _enrich_detections(
            device_id, detections)
    with _motion_lock:
        now_ms = _frame_wall_ms(device_id)
        confirm_ms = _confirm_ms(device_id)
        dev = _motion_state.setdefault(device_id, {"tracks": {}, "next_tracker_id": 1,
                                                   "started_ms": None})
        tracks = dev["tracks"]
        event = dev.get("event")
        dev["recent"] = [r for r in dev.get("recent", ())
                         if now_ms - r[2] <= RESUME_S * 1000][-RESUME_MAX:]
        # The startup window starts on the stream's first analysed frame, not on a tick with
        # no frame yet (live 17:02 on Driveway ticks ran through a 10 s reconnect and used it up).
        if dev.get("started_ms") is None and device_id in _analysed_frame_time:
            dev["started_ms"] = now_ms
        starting = dev.get("started_ms") is None or now_ms - dev["started_ms"] <= STARTUP_WINDOW_MS  # {"id", "started_ms", "finished": [(tid, tr)]} or None
        unclaimed = list(range(len(detections)))
        joining = []  # tracks that start (or resume) taking part in the event this tick

        # 1. Which detection belongs to which track: Norfair (AIPORT_TRACKER=norfair) or
        #    the built-in IoU + nearest-neighbour matcher.
        if TRACKER == "norfair":
            matches, unclaimed, pending_ids = _norfair_match(dev, tracks, detections)
        else:
            matches, unclaimed = _builtin_match(tracks, detections)
            pending_ids = {}

        for tracker_id, tr in list(tracks.items()):
            if tracker_id in matches:
                d = detections[matches[tracker_id]]
                _follow(tr, d["coord"])
                tr["score"] = d["score"]
                tr["miss_count"] = 0
                if _center_moved(tr.get("first_coord") or d["coord"], d["coord"]):
                    tr["moved"] = True
                _record_score(tr, d["score"])
                _update_still(tr, now_ms)
                if (tr.get("stationary_coord") and not d.get("seeded")
                        and _iou(d["coord"], tr["stationary_coord"]) >= STILL_IOU
                        and now_ms - tr.get("anchor_ms", now_ms) >= PARKED_RECHECK_S * 1000):
                    # Re-detected right where it parked: take a fresh anchor, so slow light
                    # and shadow changes don't add up to "its spot changed" (a parked camper's
                    # spot fell to NCC 0.82 over 4 minutes of moving sun, with no shift).
                    tr.pop("pixel_anchor", None)
                if tr.get("stationary_coord") and _moved_since(tr):
                    if _parked_spot_unchanged(device_id, tracker_id, tr):
                        # Its boxes moved but its pixels didn't (jitter, a partial box,
                        # someone walking past): it stays where it parked.
                        tr["coord"] = list(tr["stationary_coord"])
                        tr["moved_ticks"] = 0
                    else:
                        # A stationary object moved again: it rejoins as a fresh participant.
                        log.info("real motion tick: stationary %s tracker_id=%s moved, rejoining "
                                 "for deviceID=%s", tr["objectType"], tracker_id, device_id)
                        tr.pop("stationary_coord")
                        tr.pop("moved_ticks", None)
                        tr.pop("pixel_anchor", None)
                        tr["first_shown_ms"] = now_ms
                        tr.pop("best", None)
                        tr.pop("anchor", None)
                        _update_still(tr, now_ms)
                        if event is not None:  # parked earlier in this same event
                            event["finished"] = [m for m in event["finished"]
                                                 if m[0] != tracker_id]
                        joining.append((tracker_id, tr))
                _merge_extra(tr, d, device_id, tracker_id)
                _capture_best(device_id, tr, d)
                tr["seen_ms"] = now_ms
                # Line Crossing: check this frame's movement against configured lines.
                new_center = _protect_center(device_id, d["coord"])
                if tr["objectType"] not in ("licensePlate", "face") and tr.get("confirmed", True):
                    _process_line_crossings(ws, device_id, tracker_id, tr, new_center)
                tr["prev_center"] = new_center
                continue
            # No matching detection this frame for this track.
            tr["miss_count"] += 1
            _record_score(tr, 0.0)
            tolerance = (PARKED_MISS_TOLERANCE_TICKS if tr.get("stationary_coord")
                         else MOTION_MISS_TOLERANCE_TICKS)
            if not tr.get("confirmed", True) and tr["miss_count"] >= tolerance:
                # Gone before it was confirmed: a passing false detection, no event.
                log.debug("real motion tick: unconfirmed %s tracker_id=%s lost (median score "
                          "%.2f), dropped for deviceID=%s", tr["objectType"], tracker_id,
                          statistics.median(tr.get("score_history") or [0.0]), device_id)
                del tracks[tracker_id]
                continue
            if tr["miss_count"] < tolerance:
                # Within the tolerance window -- send nothing, keep the track.
                log.debug("real motion tick: no match (miss %d/%d) for tracker_id=%s deviceID=%s -- "
                          "tolerating brief gap, track still open", tr["miss_count"],
                          tolerance, tracker_id, device_id)
                continue
            # Missed enough consecutive ticks -- this track is genuinely gone.
            log.info("real motion tick: no detection for %d consecutive ticks, closing smartDetectZone "
                      "track for deviceID=%s tracker_id=%s (last seen: %s score=%.2f)",
                      tolerance, device_id, tracker_id,
                      tr["objectType"], tr["score"])
            if event is not None and not tr.get("stationary_coord"):
                event["finished"].append((tracker_id, tr))  # its snapshot goes in the leave
            _close_line_events(ws, device_id, tracker_id, tr)
            del tracks[tracker_id]
            dev["recent"].append((tracker_id, tr, now_ms))

        # 2. Any unclaimed detection resumes a track that just closed, or opens a new track
        #    (which joins the camera event once confirmed).
        for idx in unclaimed:
            d = detections[idx]
            part_of = _inside_parked(tracks, d)
            if part_of is not None:
                log.debug("real motion tick: %s (score %.2f) lies inside parked tracker_id=%s for "
                          "deviceID=%s -- part of it, ignored", d["objectType"], d["score"],
                          part_of, device_id)
                continue
            resumed = _resume_recent(dev, d, event)
            if resumed:
                tracker_id, tr, how = resumed
                tracks[tracker_id] = tr
                if idx in pending_ids:
                    dev["norfair_ids"][pending_ids[idx]] = tracker_id
                _follow(tr, d["coord"])
                tr["score"] = d["score"]
                tr["miss_count"] = 0
                tr["seen_ms"] = now_ms
                tr["prev_center"] = _protect_center(device_id, d["coord"])
                _update_still(tr, now_ms)
                _merge_extra(tr, d, device_id, tracker_id)
                _capture_best(device_id, tr, d)
                if how == "event":
                    event["finished"] = [m for m in event["finished"] if m[0] != tracker_id]
                    joining.append((tracker_id, tr))
                else:
                    _park(tr)
                log.info("real motion tick: %s resumed tracker_id=%s (%s) for deviceID=%s "
                         "(score=%.2f)", d["objectType"], tracker_id,
                         "back in its event" if how == "event" else "never moved, parked",
                         device_id, d["score"])
                continue
            tracker_id = dev["next_tracker_id"]
            dev["next_tracker_id"] += 1
            if idx in pending_ids:
                dev["norfair_ids"][pending_ids[idx]] = tracker_id
            tracks[tracker_id] = {"objectType": d["objectType"],
                                  "coord": d["coord"], "score": d["score"],
                                  "first_shown_ms": now_ms, "seen_ms": now_ms, "miss_count": 0,
                                  "first_coord": list(d["coord"]),
                                  "prev_center": _protect_center(device_id, d["coord"]),
                                  "crossed_lines": {}}
            _record_score(tracks[tracker_id], d["score"])
            tracks[tracker_id]["confirmed"] = starting or (
                confirm_ms <= 0 and tracks[tracker_id].get("true_positive")
                and d["objectType"] not in CONFIRM_WITH)
            _update_still(tracks[tracker_id], now_ms)
            _merge_extra(tracks[tracker_id], d, device_id, tracker_id)
            _capture_best(device_id, tracks[tracker_id], d)
            if starting:
                # Already there when the tracker started (a parked car): no event until it moves.
                _park(tracks[tracker_id])
                log.info("real motion tick: %s already present at start, parked for deviceID=%s "
                         "tracker_id=%s", d["objectType"], device_id, tracker_id)
                continue
            if not tracks[tracker_id]["confirmed"]:
                log.debug("real motion tick: %s detected (score=%.2f), tracker_id=%s waiting to "
                          "confirm for deviceID=%s", d["objectType"], d["score"], tracker_id,
                          device_id)
                continue
            log.info("real motion tick: %s detected (score=%.2f), opening smartDetectZone track "
                      "for deviceID=%s tracker_id=%s", d["objectType"], d["score"],
                      device_id, tracker_id)
            joining.append((tracker_id, tracks[tracker_id]))

        # 2b. A track confirms once it's a true positive (median score) and has been around
        #     for Protect's eventStartMSec -- even on a frame it was missed in, so an object
        #     seen on only its first few frames still makes its event.
        for tid, tr in tracks.items():
            if (not tr.get("confirmed", True) and tr.get("true_positive")
                    and tr["objectType"] not in CONFIRM_WITH
                    and now_ms - tr["first_shown_ms"] >= confirm_ms):
                tr["confirmed"] = True
                log.info("real motion tick: %s tracker_id=%s confirmed after %d ms (median score "
                         "%.2f), joining for deviceID=%s", tr["objectType"], tid,
                         now_ms - tr["first_shown_ms"], statistics.median(tr["score_history"]),
                         device_id)
                joining.append((tid, tr))

        # 3. A vehicle that has stopped (still ~30 s) parks; its snapshot stays in the leave.
        for tid, tr in tracks.items():
            if (not tr.get("stationary_coord") and tr.get("still")
                    and tr["objectType"] in PARK_WHEN_STILL and tr.get("confirmed", True)):
                log.info("real motion tick: %s tracker_id=%s still for %d ticks, parked for "
                         "deviceID=%s", tr["objectType"], tid, tr["still_ticks"], device_id)
                _park(tr)
                if event is not None and tr.get("event_id") == event["id"]:
                    event["finished"].append((tid, tr))

        # 3b. A face or plate confirms as soon as a confirmed, active track of its parent
        #     type is there (it was found inside that track's box).
        for tid, tr in tracks.items():
            parent = CONFIRM_WITH.get(tr["objectType"])
            if parent and not tr.get("confirmed", True) and tr["miss_count"] == 0 and any(
                    p["objectType"] == parent and p.get("confirmed", True)
                    and not p.get("stationary_coord") for p in tracks.values()):
                tr["confirmed"] = True
                log.info("real motion tick: %s tracker_id=%s confirmed with its %s, joining for "
                         "deviceID=%s", tr["objectType"], tid, parent, device_id)
                joining.append((tid, tr))

        # 4. Camera event lifecycle. Unconfirmed tracks aren't part of it yet.
        active = [(tid, tr) for tid, tr in tracks.items()
                  if not tr.get("stationary_coord") and tr.get("confirmed", True)]
        # A track can join and park in one tick (still since before it was confirmed):
        # that's a parked car, not an event.
        if event is None and joining and active:
            event = dev["event"] = {"id": next(_event_id_counter), "started_ms": now_ms,
                                    "finished": []}
            for _, tr in active:
                tr["event_id"] = event["id"]
            log.info("smartDetectZone event %s opened for deviceID=%s", event["id"], device_id)
            _send_event_message(ws, device_id, "enter", event["id"], active)
            dev["last_report_ms"] = now_ms
        elif event is not None:
            for _, tr in joining:
                tr["event_id"] = event["id"]
            max_ms = _event_max_ms.get(device_id, DEFAULT_EVENT_MAX_MS)
            timed_out = now_ms - event["started_ms"] >= max_ms
            if not active or timed_out:
                members = event["finished"] + active
            if (not active or timed_out) and not members:
                # Nothing to report (shouldn't happen): close it quietly rather than
                # failing every frame with the event stuck open.
                log.warning("smartDetectZone event %s for deviceID=%s has no tracks left; "
                            "closing it without a leave", event["id"], device_id)
                dev["event"] = None
            elif not active or timed_out:
                lead = _leave_track(members)
                log.info("smartDetectZone event %s closed for deviceID=%s (%s; %d track(s), "
                         "snapshot from %s tracker_id=%s)", event["id"], device_id,
                         "time limit" if timed_out else "all gone", len(members),
                         lead[1]["objectType"], lead[0])
                def single(ws=ws, event_id=event["id"], lead=lead):
                    _send_event_message(ws, device_id, "leave", event_id, [lead], stationary=True)
                if len(members) > 1:
                    try:
                        _send_multi_leave(ws, device_id, event["id"], members, single)
                    except Exception:
                        log.exception("multi-tracker leave failed for event %s", event["id"])
                        single()
                else:
                    single()
                for _, tr in active:  # still in view: quiet until it moves again
                    _park(tr)
                dev["event"] = None
            elif now_ms - dev.get("last_report_ms", now_ms - REPORT_INTERVAL_MS) >= REPORT_INTERVAL_MS:
                # One batched update per report interval, with only the tracks seen since the
                # last one: a track inside its miss tolerance stays in the event (and its
                # leave) but isn't drawn where it was last seen.
                last = dev.get("last_report_ms")
                seen = [m for m in active
                        if last is None or m[1].get("seen_ms", now_ms) > last]
                if seen:
                    _send_event_message(ws, device_id, "moving", event["id"], seen)
                    dev["last_report_ms"] = now_ms

        # 5. A parked track without a pixel anchor gets one from this frame.
        _anchor_parked(device_id, tracks, now_ms)

        # Logged only when the frame really changed: with the gate off every frame is
        # classified, and an empty one isn't news.
        if not detections and not tracks and not gated and changed:
            # Classified and found nothing = non-smart motion (lighting/noise); ignore.
            _log_empty_motion(device_id)


def _stop_motion_detector(device_id, preserve_tracks=False):
    """Stop this device's poll loop.

    preserve_tracks=True keeps `_motion_state` (open tracks, prev_center) so a
    UiStreamControl refresh does not silently abandon an in-flight track. Only a
    genuine teardown (stream stop / un-adopt) should discard track state -- and
    when a track IS discarded with an open event, emit a `leave` first so the
    controller closes it instead of leaving a dangling event.
    """
    if not preserve_tracks:
        with _motion_lock:
            dev = _motion_state.get(device_id)
            open_tracks = list((dev or {}).get("tracks", {}).items())
            _motion_state.pop(device_id, None)
        # Emit leave for anything still open (a discarded track with no leave
        # leaves an orphaned event on the controller).
        for tracker_id, tr in open_tracks:
            try:
                _send_smart_detect_event(None, device_id, edge_type="leave",
                                          event_id=tr["event_id"], stationary=True,
                                          object_type=tr["objectType"], coord=tr["coord"],
                                          confidence=tr["score"],
                                          first_shown_ms=tr["first_shown_ms"],
                                          tracker_id=tracker_id)
                log.info("emitted leave for discarded track tracker_id=%s deviceID=%s "
                          "(detector stopping)", tracker_id, device_id)
            except Exception:
                log.exception("failed to emit leave for discarded tracker_id=%s deviceID=%s",
                              tracker_id, device_id)
    ev = _motion_poll_stop.pop(device_id, None)
    if ev:
        ev.set()
    # Join the loop: stop_event is checked only between ticks, so an old thread
    # could still be inside _motion_tick() and mutate the same track state.
    t = _motion_poll_thread.pop(device_id, None)
    if t and t.is_alive():
        t.join(timeout=MOTION_POLL_INTERVAL_S + 5.0)


def _start_motion_detector(ws, device_id, ip, port, uri, preserve_tracks=False):
    # Runs the classifier periodically on the latest frame; no scene-diff pre-filter
    # (unreliable, and NPU inference is cheap).
    _stop_motion_detector(device_id, preserve_tracks=preserve_tracks)
    stop_event = threading.Event()
    _motion_poll_stop[device_id] = stop_event

    def _poll_loop():
        last_stamp = None
        while not stop_event.wait(DETECT_INTERVAL_S):
            # Superseded by a newer loop -> this one must not touch shared state.
            if _motion_poll_stop.get(device_id) is not stop_event:
                log.info("motion poll loop superseded for deviceID=%s -- exiting", device_id)
                return
            # Analyse each frame once: with no new frame since the last pass, wait.
            with _frame_lock:
                stamp = _latest_frame_time.get(device_id)
            if stamp is not None and stamp == last_stamp:
                continue
            last_stamp = stamp
            try:
                _motion_tick(ws, device_id)
            except Exception:
                log.exception("motion poll tick crashed for deviceID=%s", device_id)

    log.info("starting real motion detector (up to %.0f frames/s, reports every %.2fs) for "
             "deviceID=%s", 1 / DETECT_INTERVAL_S, MOTION_POLL_INTERVAL_S, device_id)
    t = threading.Thread(target=_poll_loop, daemon=True)
    _motion_poll_thread[device_id] = t
    t.start()


def _record_snapshot_filename(filename, device_id, kind, coord=None, jpeg=None):
    _snapshot_filename_to_device[filename] = {
        "device_id": device_id, "kind": kind, "coord": coord, "jpeg": jpeg,
    }
    while len(_snapshot_filename_to_device) > _SNAPSHOT_FILENAME_MAP_MAX:
        _snapshot_filename_to_device.popitem(last=False)
    # Bound memory: only the newest records keep their best-frame JPEG.
    holding = [r for r in _snapshot_filename_to_device.values() if r.get("jpeg")]
    for r in holding[:max(0, len(holding) - BEST_FRAME_RECORDS_MAX)]:
        r["jpeg"] = None
    # Record for the fallback; collapse consecutive repeats from one leave (two filenames).
    if not _recent_snapshot_announcements or _recent_snapshot_announcements[-1][1] != device_id:
        _recent_snapshot_announcements.append((time.monotonic(), device_id))
    if kind == "object" and coord:
        _last_object_coord_by_device[device_id] = coord


def _find_device_by_id(requested_device_id):
    """Match a controller-supplied deviceID (a paired camera MAC, upper or lower
    case, with or without separators) to a known active/known camera id, or None."""
    if not requested_device_id:
        return None
    want = str(requested_device_id).lower().replace(":", "").replace("-", "")
    if not want:
        return None
    known = list(_active_streams) + [d for d in _stream_dims if d not in _active_streams]
    for device_id in known:
        if str(device_id).lower().replace(":", "").replace("-", "") == want:
            return device_id
    return None


def _resolve_snapshot_device(requested_filename, what, requested_device_id=None):
    """Resolve a snapshot GetRequest to a camera; returns (record, how), record
    None if ambiguous. The controller's general snapshot request (camera
    overview/timeline thumbnail) carries no `filename` but DOES carry a
    `deviceID` naming the paired camera -- use it. Otherwise fall back only to a
    sole active stream or sole recent announcement."""
    record = _snapshot_filename_to_device.get(requested_filename)
    if record is not None:
        return record, "filename"
    device_id = _find_device_by_id(requested_device_id)
    if device_id is not None:
        return _fallback_record(device_id, what), "request deviceID"
    now = time.monotonic()
    recent = [dev for t, dev in _recent_snapshot_announcements
              if now - t <= SNAPSHOT_FALLBACK_WINDOW_S]
    distinct = list(dict.fromkeys(recent))
    active = list(_active_streams)
    if len(active) == 1:
        return _fallback_record(active[0], what), "sole active stream"
    if len(distinct) == 1:
        return _fallback_record(distinct[0], what), "sole recent announcement"
    log.warning("snapshot upload requested for unknown filename=%r (what=%r, deviceID=%r): "
                "%d active stream(s), %d distinct camera(s) announced in the last %ds -- "
                "ambiguous, refusing to guess",
                requested_filename, what, requested_device_id, len(active), len(distinct),
                SNAPSHOT_FALLBACK_WINDOW_S)
    return None, None


def _fallback_record(device_id, what):
    # Only smart-detect object snapshots are crops; a general/FullFoV snapshot is
    # the full frame even if a stale coord is on file.
    w = str(what or "").lower()
    kind = "object" if ("smartdetect" in w and "fullfov" not in w) else "fullfov"
    coord = _last_object_coord_by_device.get(device_id) if kind == "object" else None
    return {"device_id": device_id, "kind": kind, "coord": coord}


def _jpeg_size(path):
    """(width, height) from a JPEG's SOF header, or None."""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None
            while True:
                marker = f.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                seg_len = int.from_bytes(f.read(2), "big")
                # SOF0-SOF15, excluding DHT (C4), JPG (C8) and DAC (CC)
                if 0xC0 <= marker[1] <= 0xCF and marker[1] not in (0xC4, 0xC8, 0xCC):
                    sof = f.read(5)
                    return int.from_bytes(sof[3:5], "big"), int.from_bytes(sof[1:3], "big")
                f.seek(seg_len - 2, 1)
    except OSError:
        return None


def _crop_snapshot_jpeg(src_path, coord, fov_w, fov_h):
    """Square object-crop JPEG centered on the 0-1000 coord, scaled to
    SMART_SNAPSHOT_SIZE (ffmpeg); None on failure."""
    # The relay can deliver a different size than UiStreamControl announced
    # (a 1920x2560 doorbell arrived as 1504x2016); crop in the JPEG's real pixels.
    fov_w, fov_h = _jpeg_size(src_path) or (fov_w, fov_h)
    x, y, w, h = coord
    cx = (x + w / 2) / CAMERA_COORD_END * fov_w
    cy = (y + h / 2) / CAMERA_COORD_END * fov_h
    box_w = w / CAMERA_COORD_END * fov_w
    box_h = h / CAMERA_COORD_END * fov_h
    side = max(box_w, box_h) * SMART_SNAPSHOT_MARGIN
    side = max(float(SMART_SNAPSHOT_SIZE), min(side, float(min(fov_w, fov_h))))
    side = int(side) // 2 * 2  # even: required for yuvj420p JPEG dimensions
    crop_x = max(0, min(int(round(cx - side / 2)), fov_w - side))
    crop_y = max(0, min(int(round(cy - side / 2)), fov_h - side))
    vf = f"crop={side}:{side}:{crop_x}:{crop_y},scale={SMART_SNAPSHOT_SIZE}:{SMART_SNAPSHOT_SIZE}"
    try:
        out = subprocess.run(
            [FFMPEG_BIN, "-loglevel", "error", "-i", src_path, "-vf", vf,
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("snapshot crop failed for %s (coord=%s): %r", src_path, coord, e)
        return None
    if out.returncode != 0 or not out.stdout:
        log.warning("snapshot crop produced no image for %s (coord=%s) rc=%s: %s",
                    src_path, coord, out.returncode, out.stderr.decode(errors="replace")[-300:])
        return None
    return out.stdout


def _wait_for_frame(path, timeout=2.0):
    """Wait briefly for the snapshot JPEG; a missing thumbnail leaves the
    event's detectedAreas empty."""
    deadline = time.monotonic() + timeout
    while not os.path.isfile(path):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    return True


def _upload_snapshot(upload_uri, device_info, requested_filename, what=None,
                     requested_device_id=None):
    if not upload_uri:
        log.warning("snapshot GetRequest had no uri, nothing to upload")
        return
    # Resolve filename -> camera and read that camera's frame (never guess from directory contents).
    record, resolved_by = _resolve_snapshot_device(requested_filename, what,
                                                   requested_device_id)
    if record is None:
        return
    device_id = record["device_id"]
    if resolved_by not in ("filename",):
        log.info("snapshot upload for filename=%r (what=%r, deviceID=%r) resolved to "
                  "deviceID=%s via %s", requested_filename, what, requested_device_id,
                  device_id, resolved_by)
    path = os.path.join(_STREAM_DIR, f"{device_id}.jpg")
    best_path = None
    if record.get("jpeg"):
        # The track's best-scoring frame, kept at detection time.
        data = record["jpeg"]
        if record["kind"] == "object" and record["coord"]:
            fd, best_path = tempfile.mkstemp(suffix=".jpg", dir=_tmp_dir())
            with os.fdopen(fd, "wb") as f:
                f.write(data)
    else:
        if not _wait_for_frame(path):
            log.warning("snapshot upload requested for deviceID=%s (filename=%s) but no captured "
                        "frame available yet at %s", device_id, requested_filename, path)
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            log.warning("failed to read %s for snapshot upload: %s", path, e)
            return
    if record["kind"] == "object" and record["coord"]:
        fov_w, fov_h = _stream_dims.get(device_id, (DETECT_IMG_SIZE, DETECT_IMG_SIZE))
        try:
            cropped = _crop_snapshot_jpeg(best_path or path, record["coord"], fov_w, fov_h)
        finally:
            if best_path:
                os.remove(best_path)
        if cropped is not None:
            log.info("snapshot crop for deviceID=%s coord=%s -> %d bytes (from %d-byte full frame)",
                      device_id, record["coord"], len(cropped), len(data))
            data = cropped
        else:
            log.warning("snapshot crop failed for deviceID=%s, uploading full frame instead",
                        device_id)
    headers = {
        "x-ident": device_info["mac_nosep"].upper(),
        "x-type": device_info["type"],
        "x-sysid": device_info["sysid_hex"],
    }
    files_form = {"payload": ("snapshot.jpg", data, "image/jpeg")}
    try:
        resp = requests.post(upload_uri, headers=headers, files=files_form,
                              cert=(CERT_PATH, KEY_PATH), verify=False, timeout=15)
        log.info("uploaded snapshot (%d bytes) from %s to %s -> %s: %s",
                  len(data), path, upload_uri, resp.status_code, resp.text[:300])
    except requests.RequestException as e:
        log.warning("snapshot upload to %s failed: %r", upload_uri, e)


def _describe_face_db(path):
    """One-line summary of a downloaded face DB. Protect 7.3 sends a tar of
    known/uiface.json (subject/name maps) + known/uiface.db (binary store): list
    members with sizes, JSON keys with entry counts, and non-zero bytes per binary.
    SQLite gets each table's row count and columns (blob length = embedding size)."""
    import tarfile
    try:
        is_tar = tarfile.is_tarfile(path)
    except Exception:  # e.g. a truncated gzip raises instead of returning False
        is_tar = False
    if is_tar:
        parts = []
        with tarfile.open(path) as tar:
            for m in tar.getmembers():
                if not m.isfile():
                    continue
                data = tar.extractfile(m).read()
                desc = f"{m.name} ({m.size} bytes"
                try:
                    doc = json.loads(data)
                    desc += "; " + ", ".join(f"{k}={len(v) if isinstance(v, (list, dict)) else v!r}"
                                             for k, v in doc.items())
                except ValueError:
                    desc += f"; {sum(1 for x in data if x)} non-zero bytes"
                parts.append(desc + ")")
        return "tar: " + "; ".join(parts)
    with open(path, "rb") as f:
        head = f.read(16)
    if not head.startswith(b"SQLite format 3"):
        kind = {b"\x1f\x8b": "gzip", b"PK": "zip", b"{": "json", b"[": "json"}
        return next((k for m, k in kind.items() if head.startswith(m)), f"unknown ({head!r})")
    import sqlite3
    parts = []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            rows = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            sample = con.execute(f'SELECT * FROM "{table}" LIMIT 1').fetchone() or ()
            blobs = {c: len(v) for c, v in zip(cols, sample) if isinstance(v, bytes)}
            parts.append(f"{table}({rows} rows; cols={cols}; blob bytes={blobs})")
    finally:
        con.close()
    return "sqlite: " + "; ".join(parts)


def _download_face_db(uri, device_info):
    """Fetch the known-faces DB Protect offers (UpdateFaceDBRequest) into
    <state-dir>/facedb/ and log what it contains."""
    if not uri:
        log.warning("UpdateFaceDBRequest had no uri")
        return
    headers = {"x-ident": device_info["mac_nosep"].upper(), "x-type": device_info["type"],
               "x-sysid": device_info["sysid_hex"]}
    try:
        resp = requests.get(uri, headers=headers, cert=(CERT_PATH, KEY_PATH), verify=False,
                            timeout=30)
    except requests.RequestException as e:
        log.warning("face DB download from %s failed: %r", uri, e)
        return
    if resp.status_code != 200:
        log.info("face DB download from %s -> %s: %s", uri, resp.status_code, resp.text[:200])
        return
    out_dir = os.path.join(STATE_DIR or HERE, "facedb")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "known.db")
    with open(path + ".tmp", "wb") as f:
        f.write(resp.content)
    os.replace(path + ".tmp", path)
    try:
        summary = _describe_face_db(path)
    except Exception as e:  # unknown format: the raw file is still saved
        summary = f"unreadable ({e!r})"
    log.info("face DB saved to %s (%d bytes, content-type=%s): %s", path, len(resp.content),
             resp.headers.get("content-type"), summary)


def _parse_box_delay(spec, name="AIPORT_BOX_DELAY_MS", default=0):
    """A per-camera integer setting such as AIPORT_BOX_DELAY_MS: "<default>[,<deviceID>=<value>
    ...]" -> (default, {deviceID: value})."""
    per = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "=" in part:
                dev, ms = part.split("=", 1)
                per[dev.strip().upper()] = int(ms)
            else:
                default = int(part)
        except ValueError:
            log.warning("ignoring bad %s entry %r", name, part)
    return default, per


# Protect places boxes in playback by clockWall. Event times come from when the analysed
# frame left ffmpeg, so the tick wait and inference no longer delay them; what's left is
# Protect's relay and our decode. This per-camera delay back-dates every frame-describing
# time by that remainder (default 0 = unchanged).
_BOX_DELAY_DEFAULT_MS, _BOX_DELAY_MS = _parse_box_delay(os.environ.get("AIPORT_BOX_DELAY_MS"))


def _box_delay_ms(device_id):
    return _BOX_DELAY_MS.get(str(device_id).upper(), _BOX_DELAY_DEFAULT_MS)


def _now_ms():
    """(wall ms, monotonic ms) right now."""
    return int(time.time() * 1000), int(time.monotonic() * 1000)


def _frame_time(device_id):
    """(wall ms, monotonic ms) when the analysed frame was read from ffmpeg; now if no
    frame has been analysed yet."""
    with _frame_lock:
        stamp = _analysed_frame_time.get(device_id)
    return stamp or _now_ms()


def _frame_wall_ms(device_id):
    """Wall-clock ms of the analysed frame, minus the camera's residual delay."""
    return _frame_time(device_id)[0] - _box_delay_ms(device_id)


def _stream_clocks(device_id):
    """(clockMonotonic, clockStream) in ms for a device: the device's monotonic
    clock (ms since this process started the stream) and the media-timeline
    position. Real cameras report the two within tens of ms of each other
    (confirmed from a native camera's EventSmartDetect: clockStream 69938862 vs
    clockMonotonic 69964728, rate 1000 = ms). The console joins tracks to the
    media timeline with these, so zeros -- what we used to send -- leave the
    console unable to place a track (no live overlay).
    """
    started = _stream_started_monotonic_ms.get(device_id)
    if started is None:
        return 0, 0
    mono = max(0, _frame_time(device_id)[1] - started - _box_delay_ms(device_id))
    return mono, mono


def _send_smart_detect_event(ws, device_id, edge_type="none", event_id=1, stationary=False,
                              object_type="person", coord=None, confidence=95,
                              first_shown_ms=None, tracker_id=1, descriptor_extra=None, best=None,
                              also_leaving=None, still=False, idle_since_ms=None):
    # Real EventSmartDetect shape; deviceID is our addition (required by AI-Port event routing).
    # also_leaving: further tracks closing on the same tick (dicts with this function's
    # per-track arguments). Real devices batch them into one leave with one snapshot
    # entry per tracker; one leave per track got only the first track's images fetched.
    now_ms = _frame_wall_ms(device_id)
    clock_mono, clock_stream = _stream_clocks(device_id)
    tracks = [{"tracker_id": tracker_id, "object_type": object_type, "coord": coord,
               "confidence": confidence, "first_shown_ms": first_shown_ms,
               "descriptor_extra": descriptor_extra, "best": best, "still": still,
               "idle_since_ms": idle_since_ms}, *(also_leaving or [])]
    for t in tracks:
        # Placeholder box is only a fallback; tracks open on real detections.
        t["real_coord"] = ([int(round(v)) for v in _to_protect(device_id, t["coord"])]
                           if t["coord"] is not None else [100, 100, 200, 200])
        # firstShownTimeMs must stay at the track's first-seen time (recomputing looks like a new track).
        t["first_shown"] = t["first_shown_ms"] if t["first_shown_ms"] is not None else now_ms
        c = t["confidence"]
        t["level"] = int(round(c * 100)) if c <= 1 else int(round(c))
    confidence_level = tracks[0]["level"]
    # zonesStatus mirrors edgeType with a non-zero level; an empty one blocks add_smart_detect_types().
    zone_status = "none" if edge_type == "none" else edge_type
    zone_level = 0 if edge_type == "none" else confidence_level
    payload = {
        "deviceID": device_id,
        "clockMonotonic": clock_mono,
        "clockStream": clock_stream,
        "clockStreamRate": 1000,
        "clockWall": now_ms,
        "descriptors": [{
            "attributes": None,
            "boxColor": "cyan",
            "confidenceLevel": t["level"],
            "coord": t["real_coord"],
            "coord3d": [-1, -1],
            "firstShownTimeMs": t["first_shown"],
            # Real capture had idleSinceTimeMs == firstShownTimeMs; a still track reports
            # when it stopped moving (Protect's idle tracker reads it with stationary).
            "idleSinceTimeMs": t.get("idle_since_ms") or t["first_shown"],
            "intelligenceZones": [],
            "lines": [],
            "loiterZones": [],
            "name": "",
            "objectType": t["object_type"],
            "secondLensZones": [],
            "stationary": stationary or bool(t.get("still")),
            "tag": "",
            "trackerID": t["tracker_id"],
            # Naming the zone makes ds create a smartDetectZone (vs smartDetectLine) event.
            "zones": [int(SMART_DETECT_ZONE_ID)],
            # Enrichment fields: attributes, plate text in name, faceEmbed, name/tag (enrich.py).
            **(t["descriptor_extra"] or {}),
        } for t in tracks],
        "displayTimeoutMSec": BOX_DISPLAY_MS,
        "edgeType": edge_type,
        "eventId": event_id,
        # Top-level objectTypes; likely what add_smart_detect_types() reads.
        "objectTypes": list(dict.fromkeys(t["object_type"] for t in tracks if t["object_type"])),
        "smartDetectSnapshotFullFoV": "",
        "smartDetectSnapshotFullFoVHeight": 0,
        "smartDetectSnapshotFullFoVWidth": 0,
        "smartDetectSnapshots": [],
        "zonesStatus": {SMART_DETECT_ZONE_ID: {"level": zone_level, "status": zone_status}},
    }
    # Other *Status fields omitted: the real capture had only zonesStatus, and those detectors aren't implemented.
    if edge_type == "leave":
        # "leave" carries the final classification in trackerIDAttrMap (likely read by add_smart_detect_types()).
        payload["trackerIDAttrMap"] = {
            str(t["tracker_id"]): {"objectType": t["object_type"], "zone": [int(SMART_DETECT_ZONE_ID)]}
            for t in tracks
        }
        # Full-FoV dims use the real stream resolution, not the square detection buffer.
        seq = _snapshot_seq_by_device.get(device_id, 0) + 1
        _snapshot_seq_by_device[device_id] = seq
        fov_w, fov_h = _stream_dims.get(device_id, (DETECT_IMG_SIZE, DETECT_IMG_SIZE))
        fullfov_filename = f"smartdetectsnap_zone_{seq:08d}_fullfov.jpg"
        # One Full-FoV per message, from the first track's best-scoring moment if kept.
        first_best = tracks[0]["best"]
        _record_snapshot_filename(fullfov_filename, device_id, kind="fullfov",
                                  jpeg=first_best["jpeg"] if first_best else None)
        payload["smartDetectSnapshotFullFoV"] = fullfov_filename
        payload["smartDetectSnapshotFullFoVHeight"] = fov_h
        payload["smartDetectSnapshotFullFoVWidth"] = fov_w
        for t in tracks:
            b = t["best"]
            # Thumbnails come from the track's best-scoring moment when we kept one.
            # Crop in image space; report the box in Protect's canvas space.
            crop_coord = list(b["coord"]) if b else (list(t["coord"]) if t["coord"] is not None else None)
            snap_coord = ([int(round(v)) for v in _to_protect(device_id, b["coord"])]
                          if b else t["real_coord"])
            best_wall = b["wall"] if b else t["first_shown"]
            snapshot_filename = f"smartdetectsnap_zone_{t['tracker_id']}{best_wall}.jpg"
            _record_snapshot_filename(snapshot_filename, device_id, kind="object", coord=crop_coord,
                                      jpeg=b["jpeg"] if b else None)
            payload["smartDetectSnapshots"].append({
                "clockBestMonotonic": 0,
                "clockBestWall": best_wall,
                "confidenceLevel": t["level"],
                "coord": snap_coord,
                "framingRect": snap_coord,
                "reVerifyEligible": False,
                "smartDetectSnapshot": snapshot_filename,
                # Matches the object-crop image, not the full frame.
                "smartDetectSnapshotHeight": SMART_SNAPSHOT_SIZE,
                "smartDetectSnapshotName": "",
                "smartDetectSnapshotType": t["object_type"],
                "smartDetectSnapshotWidth": SMART_SNAPSHOT_SIZE,
                "trackerID": t["tracker_id"],
            })
    send_msg(ws, "EventSmartDetect", payload, response_expected=False)


def _send_line_detect_event(ws, device_id, line, edge_type, event_id, direction,
                            a2b, b2a, object_type, coord, confidence,
                            first_shown_ms, tracker_id):
    """Line Crossing event: same envelope as the zone path but with
    linesStatus, so the controller selects smartDetectLine."""
    now_ms = _frame_wall_ms(device_id)
    clock_mono, clock_stream = _stream_clocks(device_id)
    real_coord = ([int(round(v)) for v in _to_protect(device_id, coord)]
                  if coord is not None else [100, 100, 200, 200])
    first_shown = first_shown_ms if first_shown_ms is not None else now_ms
    confidence_level = int(round(confidence * 100)) if confidence <= 1 else int(round(confidence))
    line_ref = _line_ref(line["id"])
    stationary = edge_type == "leave"
    zone_status = "none" if edge_type == "none" else edge_type
    zone_level = 0 if edge_type == "none" else confidence_level
    payload = {
        "deviceID": device_id,
        "clockMonotonic": clock_mono,
        "clockStream": clock_stream,
        "clockStreamRate": 1000,
        "clockWall": now_ms,
        "descriptors": [{
            "attributes": None,
            "boxColor": "cyan",
            "confidenceLevel": confidence_level,
            "coord": real_coord,
            "coord3d": [-1, -1],
            "firstShownTimeMs": first_shown,
            "idleSinceTimeMs": first_shown,
            "intelligenceZones": [],
            "lines": [line_ref],
            "loiterZones": [],
            "name": "",
            "objectType": object_type,
            "secondLensZones": [],
            "stationary": stationary,
            "tag": "",
            "trackerID": tracker_id,
            "zones": [],
            # Cumulative crossings; the real descriptor also carries per-crossing Add deltas.
            "crosslineA2B": a2b,
            "crosslineB2A": b2a,
            "crosslineA2BAdd": 1 if direction == "A2B" else 0,
            "crosslineB2AAdd": 1 if direction == "B2A" else 0,
        }],
        "displayTimeoutMSec": BOX_DISPLAY_MS,
        "edgeType": edge_type,
        "eventId": event_id,
        "objectTypes": [object_type] if object_type else [],
        "smartDetectSnapshotFullFoV": "",
        "smartDetectSnapshotFullFoVHeight": 0,
        "smartDetectSnapshotFullFoVWidth": 0,
        "smartDetectSnapshots": [],
        "linesStatus": {str(line_ref): {
            "level": zone_level,
            "status": zone_status,
            "direction": direction,
            "crosslineA2B": a2b,
            "crosslineB2A": b2a,
        }},
    }
    if edge_type == "leave":
        # Line event closes with trackerIDAttrMap naming the line (zone path uses "zone").
        payload["trackerIDAttrMap"] = {
            str(tracker_id): {"objectType": object_type, "line": [line_ref]},
        }
        seq = _snapshot_seq_by_device.get(device_id, 0) + 1
        _snapshot_seq_by_device[device_id] = seq
        fov_w, fov_h = _stream_dims.get(device_id, (DETECT_IMG_SIZE, DETECT_IMG_SIZE))
        fullfov_filename = f"smartdetectsnap_line_{seq:08d}_fullfov.jpg"
        snapshot_filename = f"smartdetectsnap_line_{tracker_id}{now_ms}.jpg"
        _record_snapshot_filename(fullfov_filename, device_id, kind="fullfov")
        # Crop in image space; the payload's coord/framingRect stay in canvas space.
        _record_snapshot_filename(snapshot_filename, device_id, kind="object",
                                  coord=list(coord) if coord is not None else real_coord)
        payload["smartDetectSnapshotFullFoV"] = fullfov_filename
        payload["smartDetectSnapshotFullFoVHeight"] = fov_h
        payload["smartDetectSnapshotFullFoVWidth"] = fov_w
        payload["smartDetectSnapshots"] = [{
            "clockBestMonotonic": 0,
            "clockBestWall": first_shown,
            "confidenceLevel": confidence_level,
            "coord": real_coord,
            "framingRect": real_coord,
            "reVerifyEligible": False,
            "smartDetectSnapshot": snapshot_filename,
            "smartDetectSnapshotHeight": SMART_SNAPSHOT_SIZE,
            "smartDetectSnapshotName": "",
            "smartDetectSnapshotType": object_type,
            "smartDetectSnapshotWidth": SMART_SNAPSHOT_SIZE,
            "trackerID": tracker_id,
        }]
    send_msg(ws, "EventSmartDetect", payload, response_expected=False)


# Dedupe the idempotent capability/status events: Protect re-applies smart-detect
# settings on every resend, and a G3 camera's "Service Unavailable" reply loops.
_last_status_sent = {}
_feature_flags_sent = set()


def _send_status_event(ws, device_id, plug, streaming, smart_ready, audio_ready):
    # Field names from the real ubnt_av_aiport string table; isSmartDetectReady gates AI-task dispatch.
    state = (bool(plug), bool(streaming), bool(smart_ready), bool(audio_ready))
    if _last_status_sent.get(device_id) == state:
        log.info("EventAIPortStatus unchanged for deviceID=%s %s -- not resending",
                  device_id, state)
        return
    _last_status_sent[device_id] = state
    send_msg(ws, "EventAIPortStatus", {
        "deviceID": device_id,
        "isPlug": state[0],
        "isStreaming": state[1],
        "isSmartDetectReady": state[2],
        "isAudioEventReady": state[3],
    }, response_expected=False)


def _send_feature_flags_event(ws, device_id):
    # Must mirror the AI Port's real key names/values: the controller WHOLESALE
    # replaces the paired camera's flags, so an omitted key wipes the real value.
    if device_id in _feature_flags_sent:
        return
    _feature_flags_sent.add(device_id)
    send_msg(ws, "EventFeatureFlagsUpdated", {
        "deviceID": device_id,
        "smartDetect": _smart_detect_features(),
        # Real key names + genuine hardware values (echoed, not owned).
        "motionDetect": ["enhanced"],
        "privacyMask": True,
        "privacyMasks": {"maxZones": 16, "rectangleOnly": False},
        "squareEventThumbnail": True,
        "mic": True,
        "speaker": True,
        "ledStatus": True,
    }, response_expected=False)


def _smart_detect_features():
    # Plate/face types and the *EnhancedByAiKey flags (seen on a real doorbell's
    # hello) are only advertised when enrich.py can actually produce them.
    return ["person", "vehicle", "animal", "lineCrossing", "liveviewTracking",
            *enrich.smart_detect_features()]


# Capacity points per stream, measured from the real AI Port's ubnt_onvifd
# (GetUiStreamPoints, firmware 5.1.12): tiered on width x height x fps, the same
# thresholds Protect uses for its own estimate (it divides usedPoints by 10 and
# allows 1.0 in total). Orientation doesn't matter.
STREAM_POINT_TIERS = ((62208000, 2), (121927680, 2.5), (248832000, 5))
STREAM_POINTS_MAX = 10


def _points_value(points):
    """2.0 -> 2, 2.5 stays: the real device sends whole points as integers."""
    points = round(points, 1)
    return int(points) if points == int(points) else points


def _stream_points_for(width, height, fps):
    """Capacity points for one stream, or None if width/height/fps aren't positive numbers."""
    try:
        rate = int(width) * int(height) * int(fps)
    except (TypeError, ValueError):
        return None
    if rate <= 0:
        return None
    for limit, points in STREAM_POINT_TIERS:
        if rate <= limit:
            return points
    return STREAM_POINTS_MAX


def _used_points():
    return _points_value(sum(_stream_points.values()))


def _apply_motion_settings(payload):
    """Protect's motion zones and sensitivity (each zone's `level`, 0-100) for the motion
    gate; the detector is rebuilt on its next frame when they change."""
    device_id = payload.get("deviceID")
    zones = payload.get("zones")
    if not device_id or not isinstance(zones, dict):
        return
    levels = [z.get("level") for z in zones.values()
              if isinstance(z, dict) and isinstance(z.get("level"), (int, float))]
    settings = {"zones": zones, "level": max(levels) if levels else 50}
    if _motion_settings.get(device_id) != settings:
        _motion_settings[device_id] = settings
        _motion_detectors.pop(device_id, None)


def _stop_all_streams(preserve_tracks=False):
    for device_id in list(set(_active_streams) | set(_stream_points)):
        _stop_stream(device_id)
        _stop_motion_detector(device_id, preserve_tracks=preserve_tracks)
    _stream_points.clear()


def _version_from_update_uri(uri):
    """Target version out of the controller's update URL, or None.

    service.js `internalUpdatePath` builds
    `/internal/update/<platform>/<product>/<firmware|software>/<version>/<mac>`;
    the non-camera `getUpdateUrl` form is the same fields as a query string.
    """
    if not uri:
        return None
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) >= 3 and segments[-3] in ("firmware", "software"):
        return segments[-2]
    return dict(parse_qsl(parts.query)).get("version") or None


def handle_function(ws, msg, device_info):
    fn = msg.get("functionName")
    payload = msg.get("payload") or {}
    msg_id = msg.get("messageId")
    log.info("<<< received functionName=%r messageId=%s from=%r to=%r payload=%s",
             fn, msg_id, msg.get("from"), msg.get("to"), payload)

    if fn == "UiStreamControl":
        device_id = payload.get("deviceID")
        streaming = bool(payload.get("streaming"))
        if streaming and payload.get("ip") and payload.get("port") and payload.get("uri"):
            try:
                # Fresh viewing session: reset the death-retry counter.
                _stream_restart_count.pop(device_id, None)
                _start_stream(device_id, payload["ip"], payload["port"], payload["uri"],
                              payload.get("width", DETECT_IMG_SIZE),
                              payload.get("height", DETECT_IMG_SIZE))
                # Preserve tracks across UiStreamControl re-issues, else an open
                # event is abandoned and prev_center resets (line crossing breaks).
                _start_motion_detector(ws, device_id, payload["ip"], payload["port"],
                                       payload["uri"], preserve_tracks=True)
                points = _stream_points_for(payload.get("width"), payload.get("height"),
                                            payload.get("fps"))
                # Protect always sends width/height/fps; without them, assume the lowest tier.
                _stream_points[device_id] = points if points is not None else STREAM_POINT_TIERS[0][1]
                reply_status = "started"
            except Exception:
                log.exception("failed to start RTSP pull for deviceID=%s", device_id)
                _stream_points.pop(device_id, None)
                reply_status = "stopped"
        else:
            if device_id:
                _stop_stream(device_id)
                _stop_motion_detector(device_id)
                _stream_points.pop(device_id, None)
            reply_status = "stopped"
        # usedPoints MUST be on every reply (start and stop): without it Protect sets
        # utilization to the maximum and re-pairing fails with estimated_capacity_exceeded.
        reply = {"status": reply_status, "usedPoints": _used_points()}
        send_msg(ws, "UiStreamControl", reply, in_response_to=msg_id)
        if device_id:
            # smart_ready is the likely gate for AI-task dispatch.
            _send_status_event(ws, device_id, plug=True,
                                streaming=(reply_status == "started"),
                                smart_ready=True, audio_ready=True)
            if reply_status == "started":
                _send_feature_flags_event(ws, device_id)
        return
    if fn == "OnvifStreamControl":
        reply_status = "started" if payload.get("streaming") else "stopped"
        send_msg(ws, "OnvifStreamControl", {"status": reply_status}, in_response_to=msg_id)
        return
    if fn == "GetStreamList":
        # Protect asks on every connect; a non-empty list makes it send ResetAIPortStreams
        # before re-issuing UiStreamControl for each paired camera.
        send_msg(ws, "GetStreamList", {"list": [
            {"deviceID": d, "points": _points_value(p)} for d, p in _stream_points.items()
        ]}, in_response_to=msg_id)
        return
    if fn == "GetUiStreamPoints":
        # Cost of each proposed stream; entries without a positive width/height/fps are skipped.
        result = []
        for item in payload.get("list") or []:
            if not isinstance(item, dict):
                continue
            points = _stream_points_for(item.get("width"), item.get("height"), item.get("fps"))
            if points is not None:
                result.append({"deviceID": item.get("deviceID"), "points": points})
        send_msg(ws, fn, {"list": result}, in_response_to=msg_id)
        return
    if fn == "ResetAIPortStreams":
        # The real device stops every stream. Keep track state: Protect re-issues
        # UiStreamControl right after, and open events should carry on.
        log.info("ResetAIPortStreams: stopping %d stream(s)", len(_stream_points))
        _stop_all_streams(preserve_tracks=True)
        if msg.get("responseExpected"):
            send_msg(ws, fn, {}, in_response_to=msg_id)
        return
    if fn == "ChangeVideoSettings":
        # A query, not a push: the controller builds its channel list from `.video`/`.audio`,
        # so `{}` blocks provisioning.
        send_msg(ws, "ChangeVideoSettings", {
            "video": {
                "video1": {"enabled": True, "width": 1920, "height": 1080,
                           "fps": 25, "bitrate": 4000000},
                "video2": {"enabled": False},
                "video3": {"enabled": False},
            },
            "audio": {"enabled": False, "volume": 100},
        }, in_response_to=msg_id)
        return
    if fn == "ChangeIspSettings":
        # Same query-not-push pattern as ChangeVideoSettings; minimal best-effort shape.
        send_msg(ws, "ChangeIspSettings", {
            "irLedMode": "auto",
            "wdr": 1,
        }, in_response_to=msg_id)
        return
    if fn == "UpdateUsernamePassword":
        # Password rotation request; just ack (not persisted) so the controller stops retrying.
        send_msg(ws, "UpdateUsernamePassword", {}, in_response_to=msg_id)
        return
    if fn == "GetRequest" and "snapshot" in str(payload.get("what", "")).lower():
        # Snapshot upload request (what may be snapshot/smartDetectZoneSnapshot/...FullFoV);
        # ack and upload in the background.
        pending = _pending_leaves.pop(payload.get("filename") or "", None)
        if pending is not None:
            pending["seen"] = True  # Protect accepted a multi-tracker leave
        send_msg(ws, "GetRequest", {}, in_response_to=msg_id)
        threading.Thread(target=_upload_snapshot,
                          args=(payload.get("uri"), device_info, payload.get("filename"),
                                payload.get("what"), payload.get("deviceID")),
                          daemon=True).start()
        return

    if fn == "ChangeSmartMotionSettings":
        # Real devices end a smart-detect event after eventMaxDurationMSec themselves.
        max_ms = payload.get("eventMaxDurationMSec")
        if payload.get("deviceID") and isinstance(max_ms, (int, float)) and max_ms > 0:
            _event_max_ms[payload["deviceID"]] = int(max_ms)
        _apply_motion_settings(payload)
        if msg.get("responseExpected"):
            send_msg(ws, fn, {}, in_response_to=msg_id)
        return

    if fn == "UpdateFaceDBRequest":
        # Protect pushes its known-faces DB (named face groups) for on-device matching.
        # Download and describe it; matching against it isn't implemented yet.
        if msg.get("responseExpected"):
            send_msg(ws, fn, {}, in_response_to=msg_id)
        threading.Thread(target=_download_face_db, args=(payload.get("uri"), device_info),
                         daemon=True).start()
        return

    if fn == "ChangeSmartDetectSettings":
        # Parses zones/excludeZones/lines (normalized 0-1000, arbitrary vertices);
        # per-zone sensitivity isn't applied.
        device_id = payload.get("deviceID")
        start_ms = payload.get("eventStartMSec")
        if device_id and isinstance(start_ms, (int, float)) and start_ms >= 0:
            _event_start_ms[device_id] = int(start_ms)
        if device_id:
            # Protect also sends partial updates ({"isLprCamera": 0}): keep what they omit.
            if "excludeZones" in payload:
                _exclude_zones[device_id] = _parse_zones(payload.get("excludeZones"))
            if "zones" in payload:
                _detect_zones[device_id] = _parse_zones(payload.get("zones"))
                _detect_zone_types[device_id] = _parse_zone_types(payload.get("zones"))
            if "lines" in payload:
                _lines[device_id] = _parse_lines(payload.get("lines"))
            log.info("ChangeSmartDetectSettings: deviceID=%s now has %d excludeZones "
                      "polygon(s), %d detection-zone polygon(s), %d Line Crossing segment(s)",
                      device_id, len(_exclude_zones.get(device_id, ())),
                      len(_detect_zones.get(device_id, ())), len(_lines.get(device_id, ())))
        if msg.get("responseExpected"):
            send_msg(ws, fn, {}, in_response_to=msg_id)
        return

    if fn == "ResetToDefaults":
        # Un-adopt signal: the controller doesn't close the WS afterward, so tear down here.
        log.info("ResetToDefaults received -- treating as un-adopt: clearing "
                  "adopt state and tearing down streams")
        _stop_all_streams()
        _exclude_zones.clear()
        _detect_zones.clear()
        _detect_zone_types.clear()
        _lines.clear()
        _clear_adopt_state()
        if msg.get("responseExpected"):
            send_msg(ws, fn, {}, in_response_to=msg_id)
        log.info("closing this connection to force a fresh, honestly-unadopted reconnect")
        ws.close()
        return

    if fn == "UpdateFirmwareRequest":
        # Pin the version from uri and fake a reboot; never fetch/flash (the vendor
        # does the same via custom.skipFirmwareUpdate). Applied on the reconnect.
        uri = str(payload.get("uri") or "")
        target = _version_from_update_uri(uri)
        log.info("UpdateFirmwareRequest uri=%s timeoutMs=%s -> pinning version %s "
                  "(no flash)",
                  uri, payload.get("timeoutMs"), target or "<unparsed>")
        # Status the controller's handleEventUpdateFirmwareStatus consumes:
        # FW_DOWNLOADING then FW_UPDATING -> isDownloadingFW/isUpdating.
        send_msg(ws, "EventUpdateFirmwareStatus", {"status": "FW_DOWNLOADING"},
                 response_expected=False)
        send_msg(ws, "EventUpdateFirmwareStatus", {"status": "FW_UPDATING"},
                 response_expected=False)
        if msg.get("responseExpected"):
            send_msg(ws, fn, {}, in_response_to=msg_id)
        if target:
            config.pin_fw_version(STATE_DIR, target)
        else:
            log.warning("could not parse a version from update uri %r -- acking "
                          "without pinning", uri)
        # Let the status frames flush, then drop the socket: main()'s reconnect
        # loop re-reads the pin and reports the new version.
        time.sleep(1.0)
        log.info("simulated post-update reboot: closing avclient connection")
        ws.close()
        return

    if fn == "ChangeDeviceSettings":
        # Protect pushes the user's rename here; persist it (discovery is a separate
        # process) so the discovery TLV 0x0B reports the same name Network shows.
        name = payload.get("name")
        if isinstance(name, str) and name:
            try:
                config.set_device_name(STATE_DIR, name)
                log.info("ChangeDeviceSettings: device renamed to %r", name)
            except OSError:
                log.warning("failed to persist device name %r", name, exc_info=True)
        if msg.get("responseExpected"):
            send_msg(ws, fn, {"name": config.device_name(STATE_DIR),
                              "timezone": payload.get("timezone") or ""},
                     in_response_to=msg_id)
        return

    # Unknown function: ack if a response is expected.
    if msg.get("responseExpected"):
        send_msg(ws, fn, {}, in_response_to=msg_id)


def _is_adopted():
    try:
        with open(ADOPT_STATE_FILE) as f:
            state = json.load(f)
        return bool(state.get("token")) or bool(state.get("hosts"))
    except (OSError, json.JSONDecodeError):
        return False


def _clear_adopt_state():
    # Overwrite (not delete) adopt_state.json so concurrent readers never hit ENOENT.
    try:
        config.atomic_write_json(ADOPT_STATE_FILE, {})
    except OSError:
        log.warning("failed to clear adopt state file %s", ADOPT_STATE_FILE, exc_info=True)


def _adopt_token():
    try:
        with open(ADOPT_STATE_FILE) as f:
            return json.load(f).get("token")
    except (OSError, json.JSONDecodeError):
        return None


# No wait-for-adopt gate: the unsolicited pre-adopt connection is load-bearing for candidate identity.


def run(host, port, device_info, token=None):
    # connectionHost must be the controller's (Protect builds camera-upload URLs
    # from it), not ours -- self-IP left paired cameras without thumbnails.
    device_info = {**device_info, "console_host": host}
    # New controller connection = fresh state: forget what this session already
    # reported so the controller gets one full status/capability snapshot.
    _last_status_sent.clear()
    _feature_flags_sent.clear()
    url = f"wss://{host}:{port}/camera/1.0/ws"
    if token:
        url += f"?token={token}"
    # All headers are load-bearing: ds proxies the connection and closes incomplete sets.
    headers = {
        "camera-mac": device_info["mac_nosep"],
        "camera-ip": device_info["ip"],
        "camera-model": device_info["sysid_hex"],
        "camera-firmware": device_info["version"],
        "device-id": device_info["device_id"],
        "x-guid": device_info["guid"],
        "adopted": "true" if _is_adopted() else "false",
        "Origin": "http://ws_camera_proto_secure_transfer",
    }
    log.info("connecting to %s headers=%s", url, headers)
    ctx = build_ssl_context()
    with connect(url, subprotocols=["secure_transfer"], additional_headers=headers,
                 ssl=ctx, open_timeout=15) as ws:
        log.info("CLASSIC AVCLIENT WSS CONNECTED (subprotocol=%s)", ws.subprotocol)
        # hwrev must be non-null or hardwareRevision stays empty; 1 is a placeholder.
        now_ms = int(time.time() * 1000)
        send_msg(ws, "ubnt_avclient_hello", {
            "mac": device_info["mac_nosep"],
            "model": device_info["type"],
            "name": config.device_name(STATE_DIR),
            "fwVersion": device_info["version"],
            "connectionHost": device_info.get("console_host") or device_info["ip"],
            "connectionSecurePort": port,
            "protocolVersion": 67,
            "adoptionCode": _adopt_token(),
            "ip": device_info["ip"],
            "hwrev": 1,
            "idleTime": 0,
            "rebootTimeoutSec": 30,
            "upgradeTimeoutSec": 150,
            "semver": f"v{device_info['version']}",
            "totalLoad": 0.1,
            "uptime": (now_ms - _PROCESS_START_MS) // 1000,
            "features": {
                "smartDetect": _smart_detect_features(),
                "motionDetect": ["enhanced"],
                "privacyMask": True,
                "privacyMasks": {"maxZones": 16, "rectangleOnly": False},
                "squareEventThumbnail": True,
                "mic": True,
                "speaker": True,
                "ledStatus": True,
            },
        }, response_expected=True)

        for raw in ws:
            # Controller sends JSON as BINARY frames; decode bytes as UTF-8 JSON.
            text = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                log.warning("non-JSON frame (%d bytes): %r", len(text), text[:200])
                continue
            if msg.get("inResponseTo"):
                log.info("<<< response to our msg id=%s payload=%s",
                          msg.get("inResponseTo"), msg.get("payload"))
                continue
            handle_function(ws, msg, device_info)


def main():
    cfg = config.load_config_and_logging(sys.argv[1:])
    ap = argparse.ArgumentParser(description=__doc__)
    config.add_common_flags(ap)
    ap.add_argument("--iface", default=None,
                     help="interface to derive identity from (never bound); defaults to cfg "
                          "[network] iface, else the default-route interface (auto-detected)")
    ap.add_argument("--mac", default=None)
    ap.add_argument("--fallback-port", type=int, default=int(cfg["port"]),
                     help="port to use when there's no real adopt state yet (dialing --host) "
                          "or when adopt_state.json's hosts[0] entry doesn't include one of its "
                          "own")
    ap.add_argument("--host", default=cfg["host"],
                     help="dial this unconditionally when there's no adopt state yet: the "
                          "unsolicited pre-adopt handshake is required for the app to show the "
                          "device as 'AI Port' rather than a generic/'Unknown' device (the L2 "
                          "discovery beacon alone is not enough). It does NOT cause an un-adopted "
                          "device to auto-re-adopt. See the note above run() before re-adding a "
                          "wait-for-adopt gate.")
    ap.add_argument("--type", default=cfg["platform"],
                     help="catalog model string (see discovery.py's --platform help)")
    ap.add_argument("--sysid-hex", default=cfg["sysid"])
    ap.add_argument("--version", default=cfg["fw_version"])
    ap.add_argument("--bind-ip", default="0.0.0.0")
    ap.add_argument("--hostname", default=cfg["hostname"])
    ap.add_argument("--device-id", default=cfg["device_id"],
                     help="must match ucp4_client.py's --device-id -- same physical device")
    # Fixed Ubiquiti catalog GUID for the "AI Port" SKU, not a per-device random ID.
    ap.add_argument("--guid", default=cfg["guid"],
                     help="must match ucp4_client.py's --guid -- same physical device")
    ap.add_argument("--state-dir", default=None,
                     help="where adopt_state.json/server.crt/server.key live -- "
                          "defaults to the shared piport/ path (single-instance, "
                          "unchanged behavior); set this to isolate a second/third AI "
                          "Port instance running on the same box (see "
                          "instance_manager.py) -- must match the other 3 processes' "
                          "--state-dir for the same instance. Snapshot JPEGs always "
                          "live in a tmpfs-backed dir (config.stream_dir), never on "
                          "persistent storage")
    args = ap.parse_args()
    args.iface = config.resolve_iface(args.iface, cfg)

    if args.state_dir:
        global ADOPT_STATE_FILE, CERT_PATH, KEY_PATH, _STREAM_DIR, STATE_DIR
        os.makedirs(args.state_dir, exist_ok=True)
        ADOPT_STATE_FILE = os.path.join(args.state_dir, "adopt_state.json")
        CERT_PATH = os.path.join(args.state_dir, "server.crt")
        KEY_PATH = os.path.join(args.state_dir, "server.key")
        # RAM-only handoff; http_api.py derives the same dir from --state-dir.
        _STREAM_DIR = config.stream_dir(args.state_dir)
        STATE_DIR = args.state_dir
    _reset_tmp_dir()

    mac_nosep = config.resolve_mac(args.iface, args.mac, cfg).lower()
    ip = config.resolve_ip(args.iface, cfg, args.bind_ip)

    device_info = {
        "mac_nosep": mac_nosep,
        "type": args.type,
        "sysid_hex": args.sysid_hex,
        "version": args.version,
        "ip": ip,
        "hostname": args.hostname,
        "device_id": args.device_id,
        "guid": args.guid,
    }

    # Single-use token: present on first connect after adopt, omitted on later reconnects.
    used_tokens = set()

    pinned = config.pinned_fw_version(STATE_DIR)
    if pinned:
        log.info("using pinned firmware version %s; delete the state fw_version.json to unpin",
                  pinned)

    while True:
        # A simulated update pins a version for the next (fake-reboot) reconnect;
        # otherwise report the configured --version.
        device_info["version"] = config.effective_fw_version(args.version, STATE_DIR)
        # Read adopt state if present but don't block; dial args.host pre-adopt.
        try:
            with open(ADOPT_STATE_FILE) as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            state = {}
        hosts = state.get("hosts") or []
        if hosts:
            host_port = hosts[0]
            if ":" in host_port:
                host, port_s = host_port.rsplit(":", 1)
                port = int(port_s)
            else:
                host, port = host_port, args.fallback_port
        else:
            host, port = args.host, args.fallback_port

        token = state.get("token")
        send_token = token is not None and token not in used_tokens
        if token is not None:
            used_tokens.add(token)

        start = time.time()
        try:
            run(host, port, device_info, token=token if send_token else None)
            log.warning("classic avclient connection closed cleanly by peer")
        except Exception as e:
            log.warning("classic avclient connection ended: %r", e)
        # Always pace reconnects; a clean close must not skip the sleep (rate limiter is 60 msgs/60s).
        elapsed = time.time() - start
        time.sleep(max(0.0, 5.0 - elapsed))


if __name__ == "__main__":
    main()
