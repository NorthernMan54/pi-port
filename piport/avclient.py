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
import os
import ssl
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

# Hardware video decode (AIPORT_HWDEC=auto|rkmpp|v4l2m2m|none): rkmpp on the RK3588,
# v4l2m2m on a Pi 4 (/dev/video10 + /dev/vchiq); else software.
FFMPEG_BIN = ("/usr/local/ffmpeg-rkmpp/bin/ffmpeg"
              if os.path.exists("/usr/local/ffmpeg-rkmpp/bin/ffmpeg") else "ffmpeg")
_HWDEC_BACKENDS = {
    "rkmpp": {"h264": "h264_rkmpp", "hevc": "hevc_rkmpp"},
    "v4l2m2m": {"h264": "h264_v4l2m2m", "hevc": "hevc_v4l2m2m"},
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
    dec = _HWDEC_BACKENDS[backend].get(codec or "h264")
    return ["-c:v", dec] if dec else []

# Real detection: per-tick YOLOv5s/RKNN classify the latest frame; objects are
# IOU-tracked per objectType with their own trackerID/eventId and enter/move/leave.
_motion_state = {}   # deviceID -> {"tracks": {trackerID: {...}}, "next_tracker_id": int}
_motion_lock = threading.Lock()
_motion_poll_stop = {}  # deviceID -> threading.Event, signals the poll loop to stop
_motion_poll_thread = {}  # deviceID -> threading.Thread, joined on stop
_event_id_counter = itertools.count(int(time.time()))  # monotonic, avoids same-second collisions across tracks
MOTION_POLL_INTERVAL_S = 1.5
# Empty ticks before a track closes (~30s at 1.5s polls); a real departure takes
# that long to register. Needs track state to survive UiStreamControl refreshes.
MOTION_MISS_TOLERANCE_TICKS = 20
# Min IOU (same objectType) to match a detection to an existing track.
IOU_MATCH_THRESHOLD = 0.3
# Motion/scene gate: skip the classifier on a near-static frame with no open track.
# AIPORT_MOTION_GATE=0 disables; AIPORT_MOTION_GATE_THRESH tunes (default 0.02).
MOTION_GATE = os.environ.get("AIPORT_MOTION_GATE", "1").strip().lower() not in ("0", "false", "no", "off")
MOTION_GATE_THRESH = float(os.environ.get("AIPORT_MOTION_GATE_THRESH", "0.02"))
_GATE_STRIDE = 8  # sample every Nth pixel -> 80x80 grayscale for the diff
# Paired camera's smartDetectZones row id -- zonesStatus's key must match it (not always "1").
SMART_DETECT_ZONE_ID = "1"

# RK3588 NPU YOLOv5s (detector.py); ffmpeg decodes to 640x640 raw RGB, latest frame only.
_latest_frame = {}   # deviceID -> HxWx3 uint8 numpy array (640x640, RGB)
_frame_lock = threading.Lock()
_stream_dims = {}    # deviceID -> (width, height) of the real camera stream
# deviceID -> exclusion polygons in the normalized 0-1000 space.
_exclude_zones = {}
# Prototype app exposes `zones` (not excludeZones): when non-default, a detection must be inside one.
_detect_zones = {}
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


# One ffmpeg pulls RTSP and splits post-decode into a JPEG snapshot and 640x640
# RGB; decoding twice was wasteful (CPU-side NAL parsing dominates, not the NPU).
def _start_stream(device_id, ip, port, uri, width=None, height=None):
    _stop_stream(device_id)
    os.makedirs(_STREAM_DIR, exist_ok=True)
    _stream_dims[device_id] = (width or DETECT_IMG_SIZE, height or DETECT_IMG_SIZE)
    url = f"rtsp://{ip}:{port}/{uri}"
    out_path = os.path.join(_STREAM_DIR, f"{device_id}.jpg")
    codec = _probe_codec(url)
    hwdec_args = _hwdec_args(codec)
    encode_args = ["-c:v", "mjpeg_rkmpp"] if FFMPEG_BIN != "ffmpeg" else []
    size = DETECT_IMG_SIZE
    # Plate/face reads use this full-res snapshot, so refresh it every second when they're on.
    snap_fps = "1" if enrich.smart_detect_features() else "1/2"
    cmd = [
        FFMPEG_BIN, "-loglevel", "warning", "-rtsp_transport", "tcp",
        *hwdec_args, "-i", url,
        "-filter_complex",
        f"[0:v]split=2[snapin][detin];"
        f"[snapin]fps={snap_fps}[snap];[detin]fps=2,scale={size}:{size}[det]",
        "-map", "[snap]", *encode_args, "-update", "1", "-y", out_path,
        "-map", "[det]", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ]
    log.info("starting merged RTSP pull for deviceID=%s: %s (codec=%s)", device_id, url, codec)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _active_streams[device_id] = proc
    # Origin for this device's clockMonotonic/clockStream (ms since the pull began).
    _stream_started_monotonic_ms[device_id] = int(time.monotonic() * 1000)
    frame_bytes = size * size * 3

    def _reader():
        import numpy as np
        try:
            while True:
                buf = proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                frame = np.frombuffer(buf, dtype=np.uint8).reshape((size, size, 3))
                with _frame_lock:
                    _latest_frame[device_id] = frame
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


def _is_excluded(device_id, coord):
    """True if the detection box center falls inside any configured excludeZones."""
    polygons = _exclude_zones.get(device_id)
    if not polygons:
        return False
    cx, cy = _protect_center(device_id, coord)
    return any(_point_in_polygon(cx, cy, poly) for poly in polygons)


def _is_outside_detect_zones(device_id, coord):
    """True if the detection center is outside all configured `zones` (no zones
    means no restriction)."""
    polygons = _detect_zones.get(device_id)
    if not polygons:
        return False
    cx, cy = _protect_center(device_id, coord)
    return not any(_point_in_polygon(cx, cy, poly) for poly in polygons)


def _classify_frame_detections(device_id):
    """Return all qualifying detections for the latest frame as
    {objectType, score, coord} in normalized 0-1000 space, sorted by score;
    [] if none."""
    with _frame_lock:
        frame = _latest_frame.get(device_id)
    if frame is None:
        return []
    try:
        detector = _get_detector()
        results = detector.detect(frame)
    except Exception:
        log.exception("classification failed for deviceID=%s", device_id)
        return []
    # Detector's 640 buffer is a resolution-independent stretch, so scale straight to 0-1000.
    s = CAMERA_COORD_END / DETECT_IMG_SIZE
    out = []
    for r in results:
        x1, y1, x2, y2 = r["box"]
        coord = [max(0, x1 * s), max(0, y1 * s), max(1, (x2 - x1) * s), max(1, (y2 - y1) * s)]
        if _is_excluded(device_id, coord):
            log.info("real motion tick: %s detection excluded by configured excludeZones for "
                      "deviceID=%s (score=%.2f)", r["objectType"], device_id, r["score"])
            continue
        if _is_outside_detect_zones(device_id, coord):
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
    time. On leave the thumbnails come from it, not from the frame ~30 s after
    the object was last seen (which, for a face, no longer contains it)."""
    best = tr.get("best")
    if best and d["score"] < best["score"]:
        return
    mtime, data = d.get("frame") or _read_snapshot(device_id)
    if data is None:
        return
    tr["best"] = {"jpeg": data, "coord": list(d["coord"]), "score": d["score"],
                  "wall": int(time.time() * 1000)}


def _enrich_detections(device_id, detections):
    """Face detections (plus plate text on vehicles) from this tick's boxes.
    Protect reads a vehicle's plate from its descriptor `name` and recognises a
    face from its `faceEmbed`. Results are reused until a newer snapshot lands, so
    tracks don't flap; plate/name reach the parent only on a fresh read (tracks keep them)."""
    vehicles = [d for d in detections if d["objectType"] == "vehicle"]
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
                log.info("enrichment: plate %r on vehicle for deviceID=%s (score=%.2f)",
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


def _merge_extra(tr, d):
    """Carry enrichment fields onto a track. Scored keys (plate text in a
    vehicle's `name`, a face's `faceEmbed`) keep the best-scoring read; other
    keys (attributes, names) take the newest value and are never cleared by a
    tick that lacks them."""
    out = tr.setdefault("extra", {})
    out.update({k: v for k, v in (d.get("extra") or {}).items() if v not in ("", None)})
    scores = tr.setdefault("extra_scores", {})
    for key, (value, score) in (d.get("scored") or {}).items():
        if score >= scores.get(key, 0):
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


_scene_ref = {}  # deviceID -> last tick's downsampled grayscale frame (motion gate)


def _scene_changed(device_id):
    """True if this tick's frame differs enough from the previous tick's. Always
    advances the reference so a genuinely static scene stays below the gate."""
    import numpy as np  # lazy, like the detector path: avclient imports without numpy
    with _frame_lock:
        frame = _latest_frame.get(device_id)
    if frame is None:
        return False
    gray = frame[::_GATE_STRIDE, ::_GATE_STRIDE].mean(axis=2)
    ref = _scene_ref.get(device_id)
    _scene_ref[device_id] = gray
    if ref is None or ref.shape != gray.shape:
        return True
    return float(np.abs(gray - ref).mean()) / 255.0 >= MOTION_GATE_THRESH


def _motion_tick(ws, device_id):
    # With open tracks we always classify (a stationary object must keep its track
    # and be able to close); otherwise skip the classifier when nothing moved.
    with _motion_lock:
        has_tracks = bool(_motion_state.get(device_id, {}).get("tracks"))
    changed = _scene_changed(device_id)
    gated = MOTION_GATE and not has_tracks and not changed
    if gated:
        log.debug("motion gate: static scene, skipping inference for deviceID=%s", device_id)
        detections = []
    else:
        detections = _dedupe_detections(_classify_frame_detections(device_id))
        detections += _enrich_detections(device_id, detections)
    with _motion_lock:
        dev = _motion_state.setdefault(device_id, {"tracks": {}, "next_tracker_id": 1})
        tracks = dev["tracks"]
        unclaimed = list(range(len(detections)))

        # IOU only disambiguates multiple same-type candidates; with one track and one
        # detection, match unconditionally.
        type_track_count = collections.Counter(tr["objectType"] for tr in tracks.values())
        type_detect_count = collections.Counter(d["objectType"] for d in detections)

        # 1. Match each track to its best remaining same-type detection by IOU.
        for tracker_id, tr in list(tracks.items()):
            unambiguous = (type_track_count[tr["objectType"]] == 1
                            and type_detect_count[tr["objectType"]] == 1)
            best_idx, best_iou = None, (-1.0 if unambiguous else IOU_MATCH_THRESHOLD)
            for idx in unclaimed:
                d = detections[idx]
                if d["objectType"] != tr["objectType"]:
                    continue
                iou = _iou(tr["coord"], d["coord"])
                if iou > best_iou:
                    best_idx, best_iou = idx, iou
            if best_idx is not None:
                d = detections[best_idx]
                unclaimed.remove(best_idx)
                tr["coord"] = d["coord"]
                _merge_extra(tr, d)
                _capture_best(device_id, tr, d)
                tr["score"] = d["score"]
                tr["miss_count"] = 0
                # Line Crossing: check this tick's movement against configured lines.
                new_center = _protect_center(device_id, d["coord"])
                if tr["objectType"] not in ("licensePlate", "face"):
                    _process_line_crossings(ws, device_id, tracker_id, tr, new_center)
                tr["prev_center"] = new_center
                # Push a "moving" update to keep the open event's smartDetectTypes/score populated.
                try:
                    _send_smart_detect_event(ws, device_id, edge_type="moving", event_id=tr["event_id"],
                                              stationary=False, object_type=tr["objectType"],
                                              coord=tr["coord"], confidence=tr["score"],
                                              first_shown_ms=tr["first_shown_ms"], tracker_id=tracker_id,
                                              descriptor_extra=tr.get("extra"))
                except Exception:
                    log.exception("failed to send moving update for tracker_id=%s deviceID=%s",
                                  tracker_id, device_id)
                continue
            # No matching detection this tick for this track.
            tr["miss_count"] += 1
            if tr["miss_count"] < MOTION_MISS_TOLERANCE_TICKS:
                # Within the tolerance window -- send nothing, keep the track.
                log.info("real motion tick: no match (miss %d/%d) for tracker_id=%s deviceID=%s -- "
                          "tolerating brief gap, track still open", tr["miss_count"],
                          MOTION_MISS_TOLERANCE_TICKS, tracker_id, device_id)
                continue
            # Missed enough consecutive ticks -- this track is genuinely gone.
            log.info("real motion tick: no detection for %d consecutive ticks, closing smartDetectZone "
                      "track for deviceID=%s tracker_id=%s event_id=%s (last seen: %s score=%.2f)",
                      MOTION_MISS_TOLERANCE_TICKS, device_id, tracker_id, tr["event_id"],
                      tr["objectType"], tr["score"])
            try:
                _send_smart_detect_event(ws, device_id, edge_type="leave", event_id=tr["event_id"],
                                          stationary=True, object_type=tr["objectType"], coord=tr["coord"],
                                          confidence=tr["score"], first_shown_ms=tr["first_shown_ms"],
                                          tracker_id=tracker_id, descriptor_extra=tr.get("extra"),
                                          best=tr.get("best"))
            except Exception:
                log.exception("failed to send leave for tracker_id=%s deviceID=%s", tracker_id, device_id)
            # Close any line events this track opened, mirroring the zone lifecycle.
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
            del tracks[tracker_id]

        # 2. Any unclaimed detection opens a new track with its own trackerID/eventId.
        for idx in unclaimed:
            d = detections[idx]
            tracker_id = dev["next_tracker_id"]
            dev["next_tracker_id"] += 1
            event_id = next(_event_id_counter)
            first_shown_ms = int(time.time() * 1000)
            tracks[tracker_id] = {"event_id": event_id, "objectType": d["objectType"],
                                    "coord": d["coord"], "score": d["score"],
                                    "first_shown_ms": first_shown_ms, "miss_count": 0,
                                    "prev_center": _protect_center(device_id, d["coord"]),
                                    "crossed_lines": {}}
            _merge_extra(tracks[tracker_id], d)
            _capture_best(device_id, tracks[tracker_id], d)
            log.info("real motion tick: %s detected (score=%.2f), opening smartDetectZone track "
                      "for deviceID=%s tracker_id=%s event_id=%s", d["objectType"], d["score"],
                      device_id, tracker_id, event_id)
            try:
                _send_smart_detect_event(ws, device_id, edge_type="enter", event_id=event_id,
                                          stationary=False, object_type=d["objectType"],
                                          coord=d["coord"], confidence=d["score"],
                                          first_shown_ms=first_shown_ms, tracker_id=tracker_id,
                                          descriptor_extra=tracks[tracker_id]["extra"])
            except Exception:
                log.exception("failed to send enter for tracker_id=%s deviceID=%s", tracker_id, device_id)

        if not detections and not tracks and not gated:
            # Classified and found nothing = non-smart motion (lighting/noise); ignore.
            log.info("real motion tick: scene changed but no person/vehicle/animal found "
                      "for deviceID=%s -- ignoring (not smart-detect-worthy)", device_id)


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
        while not stop_event.wait(MOTION_POLL_INTERVAL_S):
            # Superseded by a newer loop -> this one must not touch shared state.
            if _motion_poll_stop.get(device_id) is not stop_event:
                log.info("motion poll loop superseded for deviceID=%s -- exiting", device_id)
                return
            try:
                _motion_tick(ws, device_id)
            except Exception:
                log.exception("motion poll tick crashed for deviceID=%s", device_id)

    log.info("starting real motion detector (periodic classification, every %.1fs) for deviceID=%s",
              MOTION_POLL_INTERVAL_S, device_id)
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
            fd, best_path = tempfile.mkstemp(suffix=".jpg", dir=_STREAM_DIR)
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
    mono = int(time.monotonic() * 1000) - started
    return mono, mono


def _send_smart_detect_event(ws, device_id, edge_type="none", event_id=1, stationary=False,
                              object_type="person", coord=None, confidence=95,
                              first_shown_ms=None, tracker_id=1, descriptor_extra=None, best=None):
    # Real EventSmartDetect shape; deviceID is our addition (required by AI-Port event routing).
    now_ms = int(time.time() * 1000)
    clock_mono, clock_stream = _stream_clocks(device_id)
    # Placeholder box is only a fallback; tracks open on real detections.
    real_coord = ([int(round(v)) for v in _to_protect(device_id, coord)]
                  if coord is not None else [100, 100, 200, 200])
    # firstShownTimeMs must stay at the track's first-seen time (recomputing looks like a new track).
    first_shown = first_shown_ms if first_shown_ms is not None else now_ms
    confidence_level = int(round(confidence * 100)) if confidence <= 1 else int(round(confidence))
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
            "confidenceLevel": confidence_level,
            "coord": real_coord,
            "coord3d": [-1, -1],
            "firstShownTimeMs": first_shown,
            # Real capture had idleSinceTimeMs == firstShownTimeMs; per-tick semantics unconfirmed.
            "idleSinceTimeMs": first_shown,
            "intelligenceZones": [],
            "lines": [],
            "loiterZones": [],
            "name": "",
            "objectType": object_type,
            "secondLensZones": [],
            "stationary": stationary,
            "tag": "",
            "trackerID": tracker_id,
            # Naming the zone makes ds create a smartDetectZone (vs smartDetectLine) event.
            "zones": [int(SMART_DETECT_ZONE_ID)],
            # Best-guess enrichment fields: attributes, licensePlate, name/tag (enrich.py).
            **(descriptor_extra or {}),
        }],
        "displayTimeoutMSec": 200,
        "edgeType": edge_type,
        "eventId": event_id,
        # Top-level objectTypes; likely what add_smart_detect_types() reads.
        "objectTypes": [object_type] if object_type else [],
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
            str(tracker_id): {"objectType": object_type, "zone": [int(SMART_DETECT_ZONE_ID)]}
        }
        # Full-FoV dims use the real stream resolution, not the square detection buffer.
        seq = _snapshot_seq_by_device.get(device_id, 0) + 1
        _snapshot_seq_by_device[device_id] = seq
        fov_w, fov_h = _stream_dims.get(device_id, (DETECT_IMG_SIZE, DETECT_IMG_SIZE))
        # Thumbnails come from the track's best-scoring moment when we kept one.
        best_jpeg = best["jpeg"] if best else None
        # Crop in image space; report the box in Protect's canvas space.
        crop_coord = list(best["coord"]) if best else (list(coord) if coord is not None else None)
        snap_coord = ([int(round(v)) for v in _to_protect(device_id, best["coord"])]
                      if best else real_coord)
        best_wall = best["wall"] if best else first_shown
        fullfov_filename = f"smartdetectsnap_zone_{seq:08d}_fullfov.jpg"
        snapshot_filename = f"smartdetectsnap_zone_{tracker_id}{best_wall}.jpg"
        _record_snapshot_filename(fullfov_filename, device_id, kind="fullfov", jpeg=best_jpeg)
        _record_snapshot_filename(snapshot_filename, device_id, kind="object", coord=crop_coord,
                                  jpeg=best_jpeg)
        payload["smartDetectSnapshotFullFoV"] = fullfov_filename
        payload["smartDetectSnapshotFullFoVHeight"] = fov_h
        payload["smartDetectSnapshotFullFoVWidth"] = fov_w
        payload["smartDetectSnapshots"] = [{
            "clockBestMonotonic": 0,
            "clockBestWall": best_wall,
            "confidenceLevel": confidence_level,
            "coord": snap_coord,
            "framingRect": snap_coord,
            "reVerifyEligible": False,
            "smartDetectSnapshot": snapshot_filename,
            # Matches the object-crop image, not the full frame.
            "smartDetectSnapshotHeight": SMART_SNAPSHOT_SIZE,
            "smartDetectSnapshotName": "",
            "smartDetectSnapshotType": object_type,
            "smartDetectSnapshotWidth": SMART_SNAPSHOT_SIZE,
            "trackerID": tracker_id,
        }]
    send_msg(ws, "EventSmartDetect", payload, response_expected=False)


def _send_line_detect_event(ws, device_id, line, edge_type, event_id, direction,
                            a2b, b2a, object_type, coord, confidence,
                            first_shown_ms, tracker_id):
    """Line Crossing event: same envelope as the zone path but with
    linesStatus, so the controller selects smartDetectLine."""
    now_ms = int(time.time() * 1000)
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
        "displayTimeoutMSec": 200,
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
                reply_status = "started"
            except Exception:
                log.exception("failed to start RTSP pull for deviceID=%s", device_id)
                reply_status = "stopped"
        else:
            if device_id:
                _stop_stream(device_id)
                _stop_motion_detector(device_id)
            reply_status = "stopped"
        # usedPoints MUST be on every reply (start and stop); omitting it breaks re-pairing
        # with estimated_capacity_exceeded.
        active_count = len(_active_streams)
        reply = {"status": reply_status, "usedPoints": active_count * 2}
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
        # aiportUpdateHandler requires this before re-issuing UiStreamControl; reply needs `list`.
        send_msg(ws, "GetStreamList", {"list": []}, in_response_to=msg_id)
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
        send_msg(ws, "GetRequest", {}, in_response_to=msg_id)
        threading.Thread(target=_upload_snapshot,
                          args=(payload.get("uri"), device_info, payload.get("filename"),
                                payload.get("what"), payload.get("deviceID")),
                          daemon=True).start()
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
        if device_id:
            _exclude_zones[device_id] = _parse_zones(payload.get("excludeZones"))
            _detect_zones[device_id] = _parse_zones(payload.get("zones"))
            _lines[device_id] = _parse_lines(payload.get("lines"))
            log.info("ChangeSmartDetectSettings: deviceID=%s now has %d excludeZones "
                      "polygon(s), %d detection-zone polygon(s), %d Line Crossing segment(s)",
                      device_id, len(_exclude_zones[device_id]), len(_detect_zones[device_id]),
                      len(_lines[device_id]))
        if msg.get("responseExpected"):
            send_msg(ws, fn, {}, in_response_to=msg_id)
        return

    if fn == "ResetToDefaults":
        # Un-adopt signal: the controller doesn't close the WS afterward, so tear down here.
        log.info("ResetToDefaults received -- treating as un-adopt: clearing "
                  "adopt state and tearing down streams")
        for device_id in list(_active_streams):
            _stop_stream(device_id)
            _stop_motion_detector(device_id)
        _exclude_zones.clear()
        _detect_zones.clear()
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
