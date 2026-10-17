# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replay a recorded clip through pi-port's detector and tracker, offline.

Runs the clip through the same ffmpeg filter graph as a live camera, feeds each frame to
avclient's motion tick with a clock taken from the frame timestamps (so it runs as fast as
the detector allows), and prints the EventSmartDetect messages pi-port would have sent:
which events open, which objects join them, and when they leave.

    venv/bin/python piport/replay.py ~/piport-clips/backyard-walk.mp4
    venv/bin/python piport/replay.py CLIP --moves          # every moving update too
    venv/bin/python piport/replay.py CLIP --json           # machine-readable summary

A sidecar CLIP.json (written by scripts/record_clip.py) supplies the camera's Protect
settings: stream width/height (for Protect's coordinate canvas), detection and exclusion
zones, lines, eventStartMSec and the event time limit. Without one the camera has no zones.
The detector is the live one: AIPORT_MODEL_PATH etc. apply. Thumbnails aren't produced.
"""
import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time as _time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DEVICE = "REPLAY"


def load_sidecar(clip_path, sidecar_path=None):
    """The clip's settings JSON ({} if there is none)."""
    path = sidecar_path or os.path.splitext(clip_path)[0] + ".json"
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def probe_size(path):
    """(width, height) of the clip's video, or None."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        w, h = out.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except ValueError:
        return None


def clip_frames(av, path, width=None, height=None):
    """Detector frames of the clip, exactly as a live camera produces them (width/height:
    the stream's size, which sets the detector frame's shape when regions are on)."""
    if not (width and height):
        width, height = probe_size(path) or (None, None)
    det_w, det_h = av._detect_dims(width, height)
    cmd = [av.FFMPEG_BIN, "-loglevel", "error", "-i", path,
           "-filter_complex", av._detect_filter_graph(det_w, det_h),
           "-map", "[jpg]", "-f", "null", os.devnull,
           "-map", "[det]", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    import numpy as np
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    frame_bytes = det_w * det_h * 3
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape((det_h, det_w, 3))
    finally:
        proc.stdout.close()
        proc.wait()


def replay(av, frames, settings=None, start_ms=0, settle_s=5.0, startup=False):
    """Run frames (at av.DETECT_FPS) through the tracker. Returns [(t_s, payload)] for every
    EventSmartDetect sent. After the clip, `settle_s` of empty frames let open tracks close.
    startup=False skips the live 15 s startup window, in which objects already in view are
    parked silently; True keeps it (a clip that starts with a parked car). avclient's
    globals are restored afterwards."""
    settings = settings or {}
    sent, now = [], [0.0]
    class _ClipTime:
        """avclient's `time`, with the monotonic clock following the clip (the motion
        gate's reference frame advances on it)."""
        def __getattr__(self, name):
            return getattr(_time, name)

        def monotonic(self):
            return now[0]

    patches = {
        "time": _ClipTime(),
        "send_msg": lambda ws, fn, payload, **kw: (
            sent.append((now[0], payload)) if fn == "EventSmartDetect" else None),
        "LEAVE_FALLBACK_S": 10 ** 9,             # no snapshot requests will come
        "_frame_jpeg": lambda *a, **k: None,     # no JPEG pipe offline
        "_BOX_DELAY_DEFAULT_MS": 0,
        "_BOX_DELAY_MS": {},
        "_STREAM_DIR": tempfile.mkdtemp(prefix="piport-replay-"),  # never the live one
    }
    if not startup:
        patches["STARTUP_WINDOW_MS"] = -1
    saved = {name: getattr(av, name) for name in patches}
    for name, value in patches.items():
        setattr(av, name, value)
    try:
        _run(av, frames, settings, start_ms, settle_s, now)
    finally:
        for name, value in saved.items():
            setattr(av, name, value)
        shutil.rmtree(patches["_STREAM_DIR"], ignore_errors=True)
        for table in (av._motion_state, av._stream_dims, av._latest_frame,
                      av._latest_frame_seq, av._latest_frame_time, av._analysed_frame_time,
                      av._detect_zones, av._exclude_zones, av._lines, av._event_start_ms,
                      av._event_max_ms, av._stream_started_monotonic_ms, av._scene_ref,
                      av._motion_detectors, av._motion_settings, av._motion_boxes,
                      av._last_parked_check, av._empty_motion):
            table.pop(DEVICE, None)
        av._startup_scan.pop(DEVICE, None)
    return sent


def _run(av, frames, settings, start_ms, settle_s, now):
    import numpy as np
    for table in (av._motion_state, av._detect_zones, av._exclude_zones, av._lines,
                  av._event_start_ms, av._event_max_ms, av._motion_detectors,
                  av._motion_settings, av._motion_boxes):
        table.pop(DEVICE, None)
    if settings.get("width") and settings.get("height"):
        av._stream_dims[DEVICE] = (int(settings["width"]), int(settings["height"]))
    detect = settings.get("smartDetect") or {}
    av._detect_zones[DEVICE] = av._parse_zones(detect.get("zones"))
    av._exclude_zones[DEVICE] = av._parse_zones(detect.get("excludeZones"))
    av._lines[DEVICE] = av._parse_lines(detect.get("lines"))
    if isinstance(detect.get("eventStartMSec"), (int, float)):
        av._event_start_ms[DEVICE] = int(detect["eventStartMSec"])
    motion = settings.get("smartMotion") or {}
    if isinstance(motion.get("eventMaxDurationMSec"), (int, float)):
        av._event_max_ms[DEVICE] = int(motion["eventMaxDurationMSec"])
    av._motion_detectors.pop(DEVICE, None)
    av._motion_settings.pop(DEVICE, None)
    av._startup_scan.pop(DEVICE, None)
    av._last_parked_check.pop(DEVICE, None)
    av._apply_motion_settings(dict(motion, deviceID=DEVICE))
    av._stream_started_monotonic_ms[DEVICE] = 0

    step_ms = 1000.0 / av.DETECT_FPS
    n, last = 0, None

    def tick(frame):
        t_ms = int(n * step_ms)
        now[0] = t_ms / 1000.0
        with av._frame_lock:
            av._latest_frame[DEVICE] = frame
            av._latest_frame_seq[DEVICE] = n + 1
            av._latest_frame_time[DEVICE] = (start_ms + t_ms, t_ms)
        av._motion_tick(None, DEVICE)

    for frame in frames:
        tick(frame)
        last, n = frame, n + 1
    if last is not None:
        blank = np.zeros_like(last)
        for _ in range(int(settle_s * av.DETECT_FPS)):
            tick(blank)
            n += 1


def summarize(sent):
    """Events in order: id, start/end (s), and each object's type and best confidence."""
    events = collections.OrderedDict()
    for t, p in sent:
        ev = events.setdefault(p["eventId"], {"id": p["eventId"], "start_s": t, "end_s": None,
                                              "objects": {}})
        for d in p.get("descriptors") or []:
            o = ev["objects"].setdefault(d["trackerID"], {"tracker": d["trackerID"],
                                                         "type": d["objectType"], "max_conf": 0})
            o["max_conf"] = max(o["max_conf"], d.get("confidenceLevel", 0))
        for snap in p.get("smartDetectSnapshots") or []:
            o = ev["objects"].setdefault(snap["trackerID"], {
                "tracker": snap["trackerID"], "type": snap.get("smartDetectSnapshotType"),
                "max_conf": 0})
            o["max_conf"] = max(o["max_conf"], snap.get("confidenceLevel", 0))
        if p["edgeType"] == "leave":
            ev["end_s"] = t
    return [dict(ev, objects=list(ev["objects"].values())) for ev in events.values()]


def _fmt_desc(d):
    return "#%s %s %s %s" % (d["trackerID"], d["objectType"], d.get("confidenceLevel"),
                             d.get("coord"))


def print_report(sent, moves=False, out=sys.stdout):
    for t, p in sent:
        if p["edgeType"] == "moving" and not moves:
            continue
        descs = ", ".join(_fmt_desc(d) for d in p.get("descriptors") or [])
        if p["edgeType"] == "leave" and not descs:
            descs = ", ".join("#%s %s" % (s["trackerID"], s.get("smartDetectSnapshotType"))
                              for s in p.get("smartDetectSnapshots") or [])
        print("t=%7.2fs  %-6s event %s  %s" % (t, p["edgeType"], p["eventId"], descs), file=out)
    events = summarize(sent)
    print("\n%d event(s)" % len(events), file=out)
    for ev in events:
        span = ("%.1fs-%.1fs" % (ev["start_s"], ev["end_s"]) if ev["end_s"] is not None
                else "%.1fs-(still open)" % ev["start_s"])
        objs = ", ".join("%s#%s (max %s)" % (o["type"], o["tracker"], o["max_conf"])
                         for o in ev["objects"])
        print("  event %s: %s  %s" % (ev["id"], span, objs), file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("clip")
    ap.add_argument("--sidecar", help="settings JSON (default: CLIP with .json)")
    ap.add_argument("--fps", type=float, help="detection rate (AIPORT_DETECT_FPS)")
    ap.add_argument("--settle", type=float, default=5.0,
                    help="seconds of empty frames after the clip so open tracks close")
    ap.add_argument("--startup", action="store_true",
                    help="keep the live startup window (objects present at once are parked)")
    ap.add_argument("--moves", action="store_true", help="print every moving update")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    ap.add_argument("-v", "--verbose", action="store_true", help="avclient's INFO log on stderr")
    args = ap.parse_args(argv)

    if args.fps:
        os.environ["AIPORT_DETECT_FPS"] = str(args.fps)
    import logging
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    import avclient as av  # after the env is set: rates are read at import

    settings = load_sidecar(args.clip, args.sidecar)
    if not settings.get("width"):
        size = probe_size(args.clip)
        if size:
            settings["width"], settings["height"] = size
    start_ms = 0
    if settings.get("start"):
        start_ms = int(datetime.fromisoformat(settings["start"]).timestamp() * 1000)
    sent = replay(av, clip_frames(av, args.clip, settings.get("width"), settings.get("height")),
                  settings, start_ms=start_ms,
                  settle_s=args.settle, startup=args.startup)
    if args.json:
        json.dump({"events": summarize(sent), "messages": len(sent)}, sys.stdout, indent=1)
        print()
    else:
        print_report(sent, moves=args.moves)


if __name__ == "__main__":
    main()
