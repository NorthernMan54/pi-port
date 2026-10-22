# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Video push for third-party (ONVIF) cameras assigned to the AI Port.

When an ONVIF camera is assigned, Protect stops ingesting it itself and hands
the job to the AI Port: OnvifStreamControl carries the camera's RTSP URL, and
ChangeVideoSettings asks for each channel (video1/2/3) as an "extendedFlv"
stream pushed to tcp://<console>:7550 under a given streamName. Without that
push the camera shows no video and records nothing.

Each push is `ffmpeg -c:v copy -f flv -` (no re-encode) piped through
rewrite_flv() into a TCP socket. The rewrite mirrors unifi-cam-proxy's
clock_sync.py (MIT, keshavdv/unifi-cam-proxy): header flags byte 7, a 16-byte
timing trailer after every tag, and onClockSync/onMpma script tags every 5s.
One deliberate difference: the trailer's elapsed time is written in the
timescale the trailer itself declares (90 kHz video, 11025 otherwise).
clock_sync.py writes 10-microsecond units for both, which Protect 7.x reads as
video running 11% and audio 9x too fast; its media server then drops the frames
as "from the future" and the web player never starts.
"""
import logging
import socket
import struct
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

from procutil import start_stderr_drain

log = logging.getLogger("aiport-onvif")

CLOCK_SYNC_INTERVAL_S = 5.0
_TAG_VIDEO = 9
_TAG_SCRIPT = 18
# Trailer timescales as captured from real cameras by unifi-cam-proxy:
# 0x015F90 = 90000 (video clock), 0x2B11 = 11025 for everything else.
_TIMESCALE_VIDEO = 90000
_TIMESCALE_OTHER = 11025


# --- AMF0 / FLV encoding (just what the two script tags need) ----------------

def _amf_string(s):
    raw = s.encode()
    return struct.pack(">H", len(raw)) + raw


def _amf_value(value):
    if isinstance(value, dict):  # AMF0 anonymous object
        body = b"".join(_amf_string(k) + _amf_value(v) for k, v in value.items())
        return b"\x03" + body + b"\x00\x00\x09"
    return b"\x00" + struct.pack(">d", float(value))  # AMF0 number


def _flv_tag(tag_type, payload, timestamp):
    """One FLV tag plus its trailing PreviousTagSize."""
    ts = struct.pack(">i", int(timestamp))
    return (bytes([tag_type]) + struct.pack(">I", len(payload))[1:]
            + ts[1:] + ts[:1] + b"\x00\x00\x00"
            + payload + struct.pack(">I", len(payload) + 11))


def script_tag(name, data, timestamp=0):
    return _flv_tag(_TAG_SCRIPT, b"\x02" + _amf_string(name) + _amf_value(data), timestamp)


def timing_trailer(is_video, elapsed_s):
    """16 bytes: 0, the 24-bit timescale, 4 zero bytes, then elapsed time in
    that timescale as 64-bit big-endian (identical to the 32-bit layout below
    2**32 ticks, ~13 h of video; no wrap after that)."""
    scale = _TIMESCALE_VIDEO if is_video else _TIMESCALE_OTHER
    return (b"\x00" + scale.to_bytes(3, "big") + b"\x00" * 4
            + struct.pack(">Q", int(elapsed_s * scale)))


_MPMA = {
    "cs": {"cur": 1500000, "max": 1500000, "min": 1500000},
    "m": {"cur": 1500000, "max": 1500000, "min": 1500000},
    "r": 0,
    "sp": {"cur": 1500000, "max": 1500000, "min": 1500000},
    "t": 75000.0,
}


def _read_exact(read, n):
    buf = b""
    while len(buf) < n:
        chunk = read(n - len(buf))
        if not chunk:
            return buf
        buf += chunk
    return buf


def rewrite_flv(read, write, clock=time.time):
    """Copy a plain FLV stream from read(n) to write(bytes) as Protect's
    extendedFlv. Returns when the source ends; raises ValueError on a non-FLV
    source. write() errors (socket gone) propagate."""
    header = _read_exact(read, 9)
    if len(header) < 9 or header[:3] != b"FLV":
        raise ValueError(f"not an FLV stream: {header[:3]!r}")
    write(header[:4] + b"\x07" + header[5:9])
    write(_read_exact(read, 4))  # PreviousTagSize0

    # First sync only after 5 s, as clock_sync.py does: Protect closes the
    # connection when an onClockSync arrives ahead of the first frames.
    last_sync = clock()
    start = clock()  # read separately, as clock_sync.py does
    while True:
        tag_header = _read_exact(read, 11)
        if len(tag_header) < 11:
            return
        tag_type = tag_header[0]
        size = int.from_bytes(tag_header[1:4], "big")
        timestamp = struct.unpack(">i", tag_header[7:8] + tag_header[4:7])[0]
        now = clock()
        if now - last_sync >= CLOCK_SYNC_INTERVAL_S:
            last_sync = now
            write(script_tag("onClockSync", {"streamClock": int(timestamp),
                                             "streamClockBase": 0,
                                             "wallClock": now * 1000}, timestamp))
            write(timing_trailer(False, now - start))
            write(script_tag("onMpma", _MPMA, 0))
            write(timing_trailer(False, now - start))
        body = _read_exact(read, size + 4)  # payload + PreviousTagSize
        write(tag_header + body)
        if len(body) < size + 4:
            return
        write(timing_trailer(tag_type == _TAG_VIDEO, now - start))


# --- push processes -----------------------------------------------------------

def parse_destination(dest):
    """tcp://host:port?retryInterval=1&connectTimeout=5 -> (host, port, retry_s, timeout_s)."""
    parts = urlsplit(dest)
    if parts.scheme != "tcp" or not parts.hostname or not parts.port:
        raise ValueError(f"unsupported destination {dest!r}")
    query = parse_qs(parts.query)
    retry = float(query.get("retryInterval", ["1"])[0])
    timeout = float(query.get("connectTimeout", ["5"])[0])
    return parts.hostname, parts.port, retry, timeout


def ffmpeg_push_cmd(ffmpeg_bin, source_url, stream_name, camera_audio=True):
    """Camera video copied (no re-encode) plus an AAC audio track into FLV on
    stdout. Protect's ingest only marks a stream ready (live view, recording)
    with both tracks present -- the extendedFlv header always flags audio+video
    -- so a camera without audio gets a silent track. AAC 32 kHz mono as in
    unifi-cam-proxy; Protect asks for Opus, which plain FLV cannot carry."""
    cmd = [
        ffmpeg_bin, "-nostdin", "-loglevel", "error",
        "-avoid_negative_ts", "make_zero", "-fflags", "+genpts+discardcorrupt",
        "-use_wallclock_as_timestamps", "1", "-timeout", "15000000",
        "-rtsp_transport", "tcp", "-i", source_url,
    ]
    if camera_audio:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=32000:cl=mono",
                "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    return cmd + [
        "-c:v", "copy", "-c:a", "aac", "-ar", "32000", "-ac", "1", "-b:a", "32k",
        "-metadata", f"streamName={stream_name}", "-f", "flv", "-",
    ]


class FlvPush:
    """Keeps one channel pushed: ffmpeg -> rewrite_flv -> TCP, restarted on any
    failure every retry interval until stop()."""

    def __init__(self, device_id, channel, source_url, stream_name, destination,
                 ffmpeg_bin="ffmpeg", camera_audio=True):
        self.device_id = device_id
        self.camera_audio = camera_audio
        self.channel = channel
        self.source_url = source_url
        self.stream_name = stream_name
        self.destination = destination
        self.host, self.port, self.retry_s, self.timeout_s = parse_destination(destination)
        self.ffmpeg_bin = ffmpeg_bin
        self._stop = threading.Event()
        self._proc = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"push-{device_id}-{channel}")

    def matches(self, source_url, stream_name, destination):
        return (self.source_url, self.stream_name, self.destination) == \
               (source_url, stream_name, destination)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            self._push_once(attempt)
            if self._stop.wait(max(self.retry_s, 1.0)):
                break
        log.info("push %s/%s stopped", self.device_id, self.channel)

    def _push_once(self, attempt):
        sock = proc = None
        stderr_tail = lambda: ""
        sent = 0
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
            sock.settimeout(30)
            proc = self._proc = subprocess.Popen(
                ffmpeg_push_cmd(self.ffmpeg_bin, self.source_url, self.stream_name,
                                self.camera_audio),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            stderr_tail = start_stderr_drain(proc)
            log.info("push %s/%s -> %s:%d streamName=%s started (attempt %d, pid %d)",
                     self.device_id, self.channel, self.host, self.port, self.stream_name,
                     attempt, proc.pid)

            def write(data):
                nonlocal sent
                sock.sendall(data)
                sent += len(data)

            rewrite_flv(proc.stdout.read, write)
            reason = "ffmpeg output ended"
        except (OSError, ValueError) as exc:
            reason = repr(exc)
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if sock:
                sock.close()
        if not self._stop.is_set():
            stderr = stderr_tail()[-300:]
            log.warning("push %s/%s ended after %d bytes: %s%s", self.device_id, self.channel,
                        sent, reason, f" ffmpeg: {stderr.strip()}" if stderr.strip() else "")


class PushManager:
    """Per-device ONVIF source URL plus the channel pushes Protect asked for."""

    def __init__(self, ffmpeg_bin="ffmpeg"):
        self.ffmpeg_bin = ffmpeg_bin
        self._lock = threading.Lock()
        self._sources = {}   # device_id -> {"url", "audio", "width", "height", "fps"}
        self._wanted = {}    # device_id -> {channel: (stream_name, destination)}
        self._pushes = {}    # (device_id, channel) -> FlvPush

    def set_source(self, device_id, url, audio=True, width=None, height=None, fps=None):
        with self._lock:
            self._sources[device_id] = {"url": url, "audio": bool(audio), "width": width,
                                        "height": height, "fps": fps}
            self._sync(device_id)

    def source(self, device_id):
        with self._lock:
            src = self._sources.get(device_id)
            return dict(src) if src else None

    def clear_device(self, device_id):
        with self._lock:
            self._sources.pop(device_id, None)
            self._wanted.pop(device_id, None)
            self._sync(device_id)

    def has_source(self, device_id):
        with self._lock:
            return device_id in self._sources

    def configure(self, device_id, video_settings):
        """Apply a ChangeVideoSettings `video` dict: channels with an extendedFlv
        avSerializer and a tcp destination are pushed; /dev/null stops one."""
        with self._lock:
            wanted = self._wanted.setdefault(device_id, {})
            for channel, settings in (video_settings or {}).items():
                serializer = (settings or {}).get("avSerializer") or {}
                dests = serializer.get("destinations") or []
                name = (serializer.get("parameters") or {}).get("streamName")
                if not dests or any("/dev/null" in d for d in dests):
                    wanted.pop(channel, None)
                elif name and dests[0].startswith("tcp://"):
                    wanted[channel] = (name, dests[0])
            self._sync(device_id)

    def stop_all(self):
        with self._lock:
            for push in self._pushes.values():
                push.stop()
            self._pushes.clear()

    def _sync(self, device_id):
        src = self._sources.get(device_id)
        url = src["url"] if src else None
        wanted = self._wanted.get(device_id, {}) if url else {}
        for key in [k for k in self._pushes if k[0] == device_id]:
            channel = key[1]
            want = wanted.get(channel)
            if not want or not self._pushes[key].matches(url, *want):
                self._pushes.pop(key).stop()
        for channel, (name, dest) in wanted.items():
            if (device_id, channel) not in self._pushes:
                try:
                    self._pushes[(device_id, channel)] = FlvPush(
                        device_id, channel, url, name, dest, self.ffmpeg_bin,
                        camera_audio=src["audio"]).start()
                except ValueError:
                    log.warning("push %s/%s: bad destination %r", device_id, channel, dest)
