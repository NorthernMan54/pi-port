# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Video push for third-party (ONVIF) cameras assigned to the AI Port.

When an ONVIF camera is assigned, Protect stops ingesting it itself and hands
the job to the AI Port: OnvifStreamControl carries the camera's RTSP URL, and
ChangeVideoSettings asks for each channel (video1/2/3) as an "extendedFlv"
stream pushed to tcp://<console>:7550 under a given streamName. Without that
push the camera shows no video and records nothing.

OnvifStreamControl names only the camera's main profile. video3, the low channel
on a UniFi camera, gets the camera's substream instead when its ONVIF Media service
lists a smaller H.264 profile (onvif_media.py); video1/video2 stay on the main one.

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
import queue
import socket
import struct
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

import onvif_media
from procutil import start_stderr_drain

log = logging.getLogger("aiport-onvif")

# The channel that carries the camera's substream when it has one.
SUB_CHANNEL = "video3"

CLOCK_SYNC_INTERVAL_S = 5.0
_TAG_AUDIO = 8
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


def timing_trailer(is_video, elapsed_s, scale=None):
    """16 bytes: 0, the 24-bit timescale, 4 zero bytes, then elapsed time in
    that timescale as 64-bit big-endian (identical to the 32-bit layout below
    2**32 ticks, ~13 h of video; no wrap after that)."""
    scale = scale or (_TIMESCALE_VIDEO if is_video else _TIMESCALE_OTHER)
    return (b"\x00" + scale.to_bytes(3, "big") + b"\x00" * 4
            + struct.pack(">Q", int(elapsed_s * scale)))


_MPMA = {
    "cs": {"cur": 1500000, "max": 1500000, "min": 1500000},
    "m": {"cur": 1500000, "max": 1500000, "min": 1500000},
    "r": 0,
    "sp": {"cur": 1500000, "max": 1500000, "min": 1500000},
    "t": 75000.0,
}


class UnsupportedCodec(ValueError):
    """The source's FLV video codec is not H.264.

    Protect's extendedFlv ingest (ms) only takes AVC here. ffmpeg's FLV muxer
    writes HEVC as Enhanced RTMP: byte0 0x90/0x91/0xA1 with an `hvc1` FourCC
    after it and an hvcC record, which ms does not ingest at all -- it reports
    videoCodec=VUNK, resets live view and closes the push. UniFi's own HEVC
    flavour uses FLV codec id 8 plus a `0x68 0x01` VPS/SPS/PPS config tag, which
    this module does not emit.
    """

    def __init__(self, codec):
        self.codec = codec
        super().__init__(
            f"unsupported video codec ({codec}): Protect only ingests H.264 "
            f"here -- an H.265/HEVC camera must be set to H.264")


def video_codec(payload):
    """Codec label for a FLV video-tag payload; 'h264' is the one we can push.

    Classic tags carry the codec id in the low nibble (7 = AVC). FFmpeg muxes
    HEVC/AV1/VP9 as Enhanced RTMP instead: byte0 has 0x80 set and a FourCC
    follows ('hvc1', ...), so there is no codec id nibble at all.
    """
    if not payload:
        return "empty"
    if payload[0] & 0x80:
        fourcc = bytes(payload[1:5]).decode("latin-1", "replace")
        return "hevc (enhanced flv)" if fourcc in ("hvc1", "hev1") else fourcc
    codec_id = payload[0] & 0x0F
    return "h264" if codec_id == 7 else f"FLV codec id {codec_id}"


def _read_exact(read, n):
    buf = b""
    while len(buf) < n:
        chunk = read(n - len(buf))
        if not chunk:
            return buf
        buf += chunk
    return buf


def _tag_timestamp(tag_header):
    return struct.unpack(">i", tag_header[7:8] + tag_header[4:7])[0]


def _retime(tag_header, timestamp):
    """The tag header with its timestamp (24 bits + extended byte) replaced."""
    ts = struct.pack(">i", max(0, int(timestamp)))
    return tag_header[:4] + ts[1:] + ts[:1] + tag_header[8:]


def read_flv_header(read):
    """FLV header plus PreviousTagSize0; ValueError on a non-FLV source."""
    header = _read_exact(read, 9)
    if len(header) < 9 or header[:3] != b"FLV":
        raise ValueError(f"not an FLV stream: {header[:3]!r}")
    return header + _read_exact(read, 4)


class ExtendedFlvWriter:
    """Protect's extendedFlv to write(bytes): header flags 7, a timing trailer
    after every tag, onClockSync/onMpma every 5 s. One per connection."""

    def __init__(self, write, clock=time.time, early_sync=False):
        self.write = write
        self.clock = clock
        self.now = self.start = self.last_sync = None
        # Sync right after the first video frame, not 5 s in: Protect's ms drops a
        # recording file that closes before its first sync ("no last known clock sync
        # - dropping"), which lost the first ~5 s after every reconnect (live 2026-10-28).
        self.early_sync = early_sync
        self._sync_next = False
        # Audio trailers in the audio's own sample rate, as a UniFi camera writes them
        # (16000 for 16 kHz AAC); 11025 made AAC stall in Protect's live view.
        self.audio_rate = None

    def header(self, header):
        self.write(header[:4] + b"\x07" + header[5:9])
        self.write(header[9:13])  # PreviousTagSize0
        # Never before the first frames, as clock_sync.py has it: Protect closes the
        # connection when an onClockSync arrives ahead of them.
        self.last_sync = self.clock()
        self.start = self.clock()  # read separately, as clock_sync.py does

    def sync_if_due(self, timestamp):
        """Once per tag, before it: the clock sync when it is due."""
        now = self.now = self.clock()
        if self._sync_next or now - self.last_sync >= CLOCK_SYNC_INTERVAL_S:
            self._sync_next = False
            self.last_sync = now
            self.write(script_tag("onClockSync", {"streamClock": int(timestamp),
                                                  "streamClockBase": 0,
                                                  "wallClock": now * 1000}, timestamp))
            self.write(timing_trailer(False, now - self.start))
            self.write(script_tag("onMpma", _MPMA, 0))
            self.write(timing_trailer(False, now - self.start))

    def tag(self, tag_header, body):
        """A whole tag (body = payload + PreviousTagSize) and its trailer."""
        self.write(tag_header + body)
        rate = self.audio_rate if tag_header[0] == _TAG_AUDIO else None
        self.write(timing_trailer(tag_header[0] == _TAG_VIDEO, self.now - self.start, rate))
        if self.early_sync and tag_header[0] == _TAG_VIDEO and len(body) > 5 and body[1] == 1:
            self.early_sync = False  # the first video frame (AVC NALU, not its config)
            self._sync_next = True


def read_tags(read):
    """(tag_header, body) for each whole tag after the header; body is the
    payload plus PreviousTagSize. Ends with the source (a cut-off tag is dropped)."""
    while True:
        tag_header = _read_exact(read, 11)
        if len(tag_header) < 11:
            return
        body = _read_exact(read, int.from_bytes(tag_header[1:4], "big") + 4)
        yield tag_header, body
        if len(body) < int.from_bytes(tag_header[1:4], "big") + 4:
            return


def check_video_codec(tag_header, body):
    if tag_header[0] == _TAG_VIDEO and len(body) > 4:
        codec = video_codec(body)
        if codec != "h264":
            raise UnsupportedCodec(codec)


def rewrite_flv(read, write, clock=time.time):
    """Copy a plain FLV stream from read(n) to write(bytes) as Protect's
    extendedFlv. Returns when the source ends; raises ValueError on a non-FLV
    source. write() errors (socket gone) propagate."""
    writer = ExtendedFlvWriter(write, clock)
    writer.header(read_flv_header(read))
    for tag_header, body in read_tags(read):
        writer.sync_if_due(_tag_timestamp(tag_header))
        if len(body) < int.from_bytes(tag_header[1:4], "big") + 4:
            write(tag_header + body)
            return
        check_video_codec(tag_header, body)
        writer.tag(tag_header, body)


def set_stream_name(tag_header, body, name):
    """ffmpeg's onMetaData tag with its streamName value replaced: one pull
    feeds several pushes, each under the streamName Protect gave its channel."""
    payload = body[:-4]
    key = b"\x00\x0astreamName\x02"
    at = payload.find(key)
    if tag_header[0] != _TAG_SCRIPT or at < 0:
        return tag_header, body
    start = at + len(key)
    end = start + 2 + struct.unpack(">H", payload[start:start + 2])[0]
    payload = payload[:start] + _amf_string(name) + payload[end:]
    tag = _flv_tag(_TAG_SCRIPT, payload, _tag_timestamp(tag_header))
    return tag[:11], tag[11:]


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


def ffmpeg_push_cmd(ffmpeg_bin, source_url, stream_name, camera_audio=True, copy_audio=True):
    """Camera video copied (no re-encode) plus an audio track into FLV on stdout.
    Protect's ingest only marks a stream ready (live view, recording) with both
    tracks present -- the extendedFlv header always flags audio+video -- so a
    camera without audio gets a silent AAC track.

    The camera's audio is copied (FLV carries G.711 and AAC as they are), as
    unifi-cam-proxy runs here with -c:a copy. Re-encoding G.711 to AAC made
    Protect's live view stop and start every second (live 2026-10-28);
    `copy_audio=False` re-encodes, for audio FLV can't hold (e.g. Opus)."""
    cmd = [
        ffmpeg_bin, "-nostdin", "-loglevel", "error",
        "-avoid_negative_ts", "make_zero", "-fflags", "+genpts+discardcorrupt",
        "-timeout", "15000000",  # camera timestamps kept: no -use_wallclock_as_timestamps (test)
        "-rtsp_transport", "tcp", "-i", source_url,
    ]
    aac = ["-c:a", "aac", "-ar", "32000", "-ac", "1", "-b:a", "32k"]
    if camera_audio:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"] + (["-c:a", "copy"] if copy_audio else aac)
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=32000:cl=mono",
                "-map", "0:v:0", "-map", "1:a:0", "-shortest"] + aac
    return cmd + [
        "-c:v", "copy",
        "-metadata", f"streamName={stream_name}", "-f", "flv", "-",
    ]


# FLV sound formats (high nibble of an audio tag) Protect takes copied: MP3, G.711 A-law and
# mu-law, AAC. ffmpeg 8 copies e.g. Opus too, as Enhanced FLV (9), like the HEVC ms refuses.
_COPYABLE_AUDIO = (2, 7, 8, 10)


class _AudioNotCopyable(ValueError):
    """The camera's audio came out of a copy in a format Protect won't take."""


def _metadata_numbers(payload):
    """{key: number} of an onMetaData payload's AMF0 numbers (framerate, width, ...)."""
    out = {}
    for key in (b"width", b"height", b"framerate", b"videodatarate", b"videocodecid",
                b"audiocodecid", b"audiosamplerate", b"audiodatarate"):
        at = payload.find(struct.pack(">H", len(key)) + key + b"\x00")
        if at >= 0:
            start = at + 2 + len(key) + 1
            out[key.decode()] = round(struct.unpack(">d", payload[start:start + 8])[0], 2)
    return out


def _describe(url):
    """A source for logs: host and path, never the login."""
    parts = urlsplit(url)
    return f"{parts.hostname}{parts.path}"


def _is_config_tag(tag_header, payload):
    """AVC sequence header or AAC AudioSpecificConfig: needed before any frame."""
    if tag_header[0] == _TAG_VIDEO:
        return len(payload) > 1 and payload[1] == 0
    if tag_header[0] == _TAG_AUDIO:
        return len(payload) > 1 and payload[0] >> 4 == 10 and payload[1] == 0
    return False


def _is_keyframe(tag_header, payload):
    return tag_header[0] == _TAG_VIDEO and len(payload) > 1 and payload[0] >> 4 == 1


class _DeliveryStats:
    """How evenly ffmpeg hands over video frames, logged once a minute: Protect's
    player stutters and lags when frames arrive in clumps or late against their
    own timestamps (ffmpeg stamps them on arrival: -use_wallclock_as_timestamps)."""

    INTERVAL_S = 60.0

    def __init__(self, name, clock=time.monotonic):
        self.name = name
        self.clock = clock
        self.started = clock()
        self.first = None
        self._reset(self.started)

    def _reset(self, now):
        self.since = now
        self.delays = []      # wall - stream time, per frame (s)
        self.gaps = []        # wall time between frames (s)
        self.last = None

    def frame(self, timestamp_ms):
        now = self.clock()
        if self.first is None:
            self.first = now
            log.info("pull %s: first frame %.2f s after ffmpeg started", self.name,
                     now - self.started)
        self.delays.append(now - timestamp_ms / 1000.0)
        if self.last is not None:
            self.gaps.append(now - self.last)
        self.last = now
        if now - self.since >= self.INTERVAL_S and len(self.delays) > 2:
            base = min(self.delays)
            late = sorted(d - base for d in self.delays)
            clumped = sum(g < 0.005 for g in self.gaps)
            log.info("pull %s over %.0f s: %d frames (%.1f/s), late vs own timestamp "
                     "p50 %.0f ms p95 %.0f ms max %.0f ms, longest gap %.0f ms, %d%% clumped",
                     self.name, now - self.since, len(self.delays),
                     len(self.delays) / (now - self.since), late[len(late) // 2] * 1000,
                     late[int(len(late) * 0.95)] * 1000, late[-1] * 1000,
                     max(self.gaps) * 1000, 100 * clumped // max(1, len(self.gaps)))
            self._reset(now)


class SharedPull:
    """One ffmpeg reading a camera stream, fanned out to every push of it.

    A camera allows few RTSP sessions (a Hikvision 2560x1440 answered 500 past
    three: video1, video3 and detection), so channels carrying the same stream
    share one session. A push joins at the next key frame: it gets ffmpeg's
    onMetaData and the AVC/AAC config tags first, then frames.
    """

    def __init__(self, source_url, camera_audio=True, ffmpeg_bin="ffmpeg"):
        self.source_url = source_url
        self.camera_audio = camera_audio
        self.ffmpeg_bin = ffmpeg_bin
        self._lock = threading.Lock()
        self._sinks = {}       # FlvPush -> waiting for a key frame to (re)join
        self._header = None
        self._config = {}      # "meta" / "video" / "audio" -> (tag_header, body)
        self._stop = threading.Event()
        self._proc = None
        self._codec_error_logged = None
        self._copy_audio = True  # until ffmpeg says FLV can't hold the camera's audio
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"pull-{_describe(source_url)}")

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

    def subscribe(self, sink):
        with self._lock:
            self._sinks[sink] = True

    def unsubscribe(self, sink):
        with self._lock:
            self._sinks.pop(sink, None)

    def resync(self, sink):
        """Send `sink` a fresh start at the next key frame."""
        with self._lock:
            if sink in self._sinks:
                self._sinks[sink] = True

    def _run(self):
        attempt = failed = 0
        while not self._stop.is_set():
            attempt += 1
            failed = 0 if self._pull_once(attempt) else failed + 1
            if self._stop.wait(retry_delay(1.0, failed)):
                break
        log.info("pull %s stopped", _describe(self.source_url))

    def _pull_once(self, attempt):
        proc = None
        stderr_tail = lambda: ""
        frames = 0
        codec_error = None
        try:
            # The streamName is set per push (set_stream_name), not here.
            proc = self._proc = subprocess.Popen(
                ffmpeg_push_cmd(self.ffmpeg_bin, self.source_url, "piport", self.camera_audio,
                                self._copy_audio),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            stderr_tail = start_stderr_drain(proc)
            with self._lock:
                names = ", ".join(sorted(f"{s.device_id}/{s.channel}" for s in self._sinks))
            log.info("pull %s started for %s (attempt %d, pid %d)",
                     _describe(self.source_url), names or "no push", attempt, proc.pid)
            stats = _DeliveryStats(_describe(self.source_url))
            self._header = read_flv_header(proc.stdout.read)
            self._config = {}
            for tag_header, body in read_tags(proc.stdout.read):
                if self._stop.is_set():
                    break
                payload = body[:-4]
                if len(body) < int.from_bytes(tag_header[1:4], "big") + 4:
                    break
                check_video_codec(tag_header, body)
                if (self._copy_audio and tag_header[0] == _TAG_AUDIO and payload
                        and payload[0] >> 4 not in _COPYABLE_AUDIO):
                    raise _AudioNotCopyable(f"FLV sound format {payload[0] >> 4}")
                if tag_header[0] == _TAG_SCRIPT and b"onMetaData" in payload[:16]:
                    self._config["meta"] = (tag_header, body)
                    log.info("pull %s onMetaData: %s", _describe(self.source_url),
                             _metadata_numbers(payload))
                elif _is_config_tag(tag_header, payload):
                    self._config["video" if tag_header[0] == _TAG_VIDEO else "audio"] = \
                        (tag_header, body)
                else:
                    frames += 1
                    if tag_header[0] == _TAG_VIDEO:
                        stats.frame(_tag_timestamp(tag_header))
                    self._deliver(tag_header, body, _is_keyframe(tag_header, payload))
            reason = "ffmpeg output ended"
        except _AudioNotCopyable as exc:
            self._copy_audio = False
            reason = f"camera audio not copyable ({exc}): re-encoding it to AAC from now on"
        except UnsupportedCodec as exc:
            codec_error = exc.codec
            reason = str(exc)
        except (OSError, ValueError) as exc:
            reason = repr(exc)
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if proc:
                proc.stdout.close()
            with self._lock:
                for sink in self._sinks:
                    self._sinks[sink] = True
                    sink.put(("end", "camera stream ended"))
        if not self._stop.is_set():
            stderr = stderr_tail()[-300:]
            msg = ("pull %s ended after %d frames: %s%s", _describe(self.source_url), frames,
                   reason, f" ffmpeg: {stderr.strip()}" if stderr.strip() else "")
            if codec_error is None:
                self._codec_error_logged = None
                log.warning(*msg)
            elif self._codec_error_logged != codec_error:
                # Deterministic and retried every retryInterval: say it once at
                # error level, not once a second (a camera-side codec change
                # still recovers, since each attempt re-reads the stream).
                self._codec_error_logged = codec_error
                log.error(*msg)
            else:
                log.debug(*msg)
        return frames > 0

    def _deliver(self, tag_header, body, keyframe):
        with self._lock:
            for sink, waiting in list(self._sinks.items()):
                if waiting:
                    if not keyframe or "video" not in self._config:
                        continue
                    config = [self._config[k] for k in ("meta", "video", "audio")
                              if k in self._config]
                    if not sink.put(("start", self._header, config,
                                     _tag_timestamp(tag_header))):
                        continue
                    self._sinks[sink] = False
                if not sink.put(("tag", tag_header, body)):
                    self._sinks[sink] = True  # fell behind: it rejoins at a key frame


class PullPool:
    """The SharedPull per (source url, audio), alive while a push uses it."""

    def __init__(self, ffmpeg_bin="ffmpeg"):
        self.ffmpeg_bin = ffmpeg_bin
        self._lock = threading.Lock()
        self._pulls = {}  # (url, audio) -> [SharedPull, users]

    def acquire(self, url, camera_audio):
        with self._lock:
            entry = self._pulls.get((url, camera_audio))
            if entry is None:
                entry = self._pulls[(url, camera_audio)] = [
                    SharedPull(url, camera_audio, self.ffmpeg_bin).start(), 0]
            entry[1] += 1
            return entry[0]

    def release(self, pull):
        with self._lock:
            key = (pull.source_url, pull.camera_audio)
            entry = self._pulls.get(key)
            if entry and entry[0] is pull:
                entry[1] -= 1
                if entry[1] <= 0:
                    del self._pulls[key]
                    pull.stop()


class FlvPush:
    """Keeps one channel pushed: its SharedPull's tags -> extendedFlv -> TCP,
    reconnected on any failure every retry interval until stop()."""

    QUEUE_TAGS = 600       # ~10 s of 20 fps video plus audio
    STALL_S = 30.0         # no tag for this long ends the connection

    def __init__(self, device_id, channel, source_url, stream_name, destination,
                 ffmpeg_bin="ffmpeg", camera_audio=True, pool=None):
        self.device_id = device_id
        self.camera_audio = camera_audio
        self.channel = channel
        self.source_url = source_url
        self.stream_name = stream_name
        self.destination = destination
        self.host, self.port, self.retry_s, self.timeout_s = parse_destination(destination)
        self._pool = pool or PullPool(ffmpeg_bin)
        self._pull = None
        self._queue = queue.Queue(maxsize=self.QUEUE_TAGS)
        self._stop = threading.Event()
        self._sock = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"push-{device_id}-{channel}")

    def matches(self, source_url, stream_name, destination):
        return (self.source_url, self.stream_name, self.destination) == \
               (source_url, stream_name, destination)

    def start(self):
        self._pull = self._pool.acquire(self.source_url, self.camera_audio)
        self._pull.subscribe(self)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._pull:
            self._pull.unsubscribe(self)
            self._pool.release(self._pull)
        sock = self._sock
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def put(self, item):
        """From the pull: False (and the queue emptied) when this push fell behind."""
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            with self._queue.mutex:
                self._queue.queue.clear()
                self._queue.queue.append(("end", "fell behind the camera stream"))
                self._queue.not_empty.notify()
            return False

    def _get(self, timeout):
        """Next item, or None on stop or after `timeout` seconds without one."""
        deadline = time.monotonic() + timeout
        while not self._stop.is_set():
            try:
                return self._queue.get(timeout=min(1.0, max(0.0, deadline - time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= deadline:
                    return None
        return None

    def _run(self):
        attempt = failed = 0
        while not self._stop.is_set():
            item = self._get(1.0)
            if not item or item[0] != "start":
                continue  # waiting for the pull to hand over a key frame
            attempt += 1
            failed = 0 if self._push_once(item, attempt) else failed + 1
            if self._stop.wait(retry_delay(self.retry_s, failed)):
                break
            with self._queue.mutex:
                self._queue.queue.clear()
            self._pull.resync(self)
        log.info("push %s/%s stopped", self.device_id, self.channel)

    def _push_once(self, start, attempt):
        _, header, config, base_ts = start
        sent = 0
        reason = "stopped"
        try:
            sock = self._sock = socket.create_connection((self.host, self.port),
                                                         timeout=self.timeout_s)
            sock.settimeout(30)
            log.info("push %s/%s -> %s:%d streamName=%s started (attempt %d, %s)",
                     self.device_id, self.channel, self.host, self.port, self.stream_name,
                     attempt, _describe(self.source_url))

            def write(data):
                nonlocal sent
                sock.sendall(data)
                sent += len(data)

            writer = ExtendedFlvWriter(write, early_sync=True)
            meta = next((b for h, b in config if h[0] == _TAG_SCRIPT), None)
            rate = _metadata_numbers(meta[:-4]).get("audiosamplerate") if meta else None
            writer.audio_rate = int(rate) if rate else None
            writer.header(header)
            for tag_header, body in config:
                tag_header, body = set_stream_name(tag_header, body, self.stream_name)
                writer.sync_if_due(0)
                writer.tag(_retime(tag_header, 0), body)
            while not self._stop.is_set():
                item = self._get(self.STALL_S)
                if item is None:
                    if not self._stop.is_set():
                        reason = f"no frames for {self.STALL_S:.0f} s"
                    break
                if item[0] != "tag":
                    reason = item[1] if item[0] == "end" else "restarted"
                    break
                tag_header = _retime(item[1], _tag_timestamp(item[1]) - base_ts)
                writer.sync_if_due(_tag_timestamp(tag_header))
                writer.tag(tag_header, item[2])
        except OSError as exc:
            reason = repr(exc)
        finally:
            if self._sock:
                self._sock.close()
                self._sock = None
        if not self._stop.is_set():
            log.warning("push %s/%s ended after %d bytes: %s", self.device_id, self.channel,
                        sent, reason)
        return sent > 0


PUSH_RETRY_MAX_S = 30.0


def retry_delay(retry_s, failed):
    """Seconds before the next push: Protect's retryInterval, doubling after the third push in
    a row that sent nothing, up to PUSH_RETRY_MAX_S. An unreachable source otherwise started an
    ffmpeg every second (live 2026-10-28)."""
    base = max(retry_s, 1.0)
    return min(PUSH_RETRY_MAX_S, base * 2 ** max(0, failed - 3)) if failed else base


class PushManager:
    """Per-device ONVIF source URL plus the channel pushes Protect asked for."""

    def __init__(self, ffmpeg_bin="ffmpeg", fetch_profiles=None, background=True):
        self.ffmpeg_bin = ffmpeg_bin
        self._fetch_profiles = fetch_profiles or onvif_media.fetch_profiles
        self._background = background  # False runs the profile lookup inline (tests)
        self._lock = threading.Lock()
        self._sources = {}   # device_id -> {"url", "audio", "width", "height", "fps"}
        self._wanted = {}    # device_id -> {channel: (stream_name, destination)}
        self._pushes = {}    # (device_id, channel) -> FlvPush
        self._pool = PullPool(ffmpeg_bin)  # channels on the same stream share one pull
        self._subs = {}      # main url -> substream profile dict, or None (none/failed)
        self._looking_up = set()  # main urls whose profiles are being fetched

    def set_source(self, device_id, url, audio=True, width=None, height=None, fps=None):
        with self._lock:
            self._sources[device_id] = {"url": url, "audio": bool(audio), "width": width,
                                        "height": height, "fps": fps}
            lookup = url not in self._subs and url not in self._looking_up
            if lookup:
                self._looking_up.add(url)
            self._sync(device_id)
        if lookup:
            if self._background:
                threading.Thread(target=self._find_substream, args=(url,), daemon=True,
                                 name=f"onvif-profiles-{device_id}").start()
            else:
                self._find_substream(url)

    def _find_substream(self, url):
        """Look up the camera's profiles once per main url, then start the waiting
        SUB_CHANNEL pushes (on the substream, or the main stream if there is none)."""
        host = urlsplit(url).hostname
        try:
            sub = onvif_media.pick_substream(self._fetch_profiles(url))
        except Exception as e:
            log.warning("ONVIF profiles of %s unavailable (%s): %s carries the main stream",
                        host, e, SUB_CHANNEL)
            sub = None
        else:
            if sub:
                log.info("ONVIF camera %s: %s carries substream %s (%sx%s @%s fps)", host,
                         SUB_CHANNEL, sub["token"], sub["width"], sub["height"], sub["fps"])
            else:
                log.info("ONVIF camera %s has no smaller H.264 profile: %s carries the main "
                         "stream", host, SUB_CHANNEL)
        with self._lock:
            self._subs[url] = sub
            self._looking_up.discard(url)
            for device_id, src in list(self._sources.items()):
                if src["url"] == url:
                    self._sync(device_id)

    def substream(self, device_id):
        """{width, height, fps} of the substream SUB_CHANNEL carries, or None if it
        carries the main stream."""
        with self._lock:
            src = self._sources.get(device_id)
            sub = self._subs.get(src["url"]) if src else None
            return {k: sub[k] for k in ("width", "height", "fps")} if sub else None

    def _channel_url(self, src, channel):
        """The url `channel` pushes; None while SUB_CHANNEL waits for the profiles."""
        url = src["url"]
        if channel != SUB_CHANNEL:
            return url
        if url in self._looking_up:
            return None
        sub = self._subs.get(url)
        return sub["url"] if sub else url

    def source(self, device_id):
        with self._lock:
            src = self._sources.get(device_id)
            return dict(src) if src else None

    def stop_device(self, device_id):
        """Streaming stopped: stop the pushes but remember the destinations.

        OnvifStreamControl streaming:false is a stop, not an unassign. A real AI
        Port keeps its channel config, so a later streaming:true (e.g. after a
        console restart, which does not necessarily re-send ChangeVideoSettings)
        resumes the same streamNames instead of leaving the camera with no video.
        """
        with self._lock:
            self._sources.pop(device_id, None)
            self._sync(device_id)

    def clear_device(self, device_id):
        """Forget the device entirely: stop pushes and drop its destinations."""
        with self._lock:
            self._sources.pop(device_id, None)
            self._wanted.pop(device_id, None)
            self._sync(device_id)

    def has_source(self, device_id):
        with self._lock:
            return device_id in self._sources

    def configure(self, device_id, video_settings):
        """Apply a ChangeVideoSettings `video` dict: channels with an extendedFlv
        avSerializer and a tcp destination are pushed; /dev/null stops one.
        A channel without an avSerializer is left as it is: Protect also sends
        encoder-only settings (fps, bitrate mode), which stopped the push."""
        with self._lock:
            wanted = self._wanted.setdefault(device_id, {})
            for channel, settings in (video_settings or {}).items():
                serializer = (settings or {}).get("avSerializer")
                if serializer is None:
                    continue
                serializer = serializer or {}
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
            self._sources.clear()
            self._wanted.clear()

    def _sync(self, device_id):
        src = self._sources.get(device_id)
        wanted = self._wanted.get(device_id, {}) if src else {}
        urls = {channel: self._channel_url(src, channel) for channel in wanted}
        for key in [k for k in self._pushes if k[0] == device_id]:
            want, url = wanted.get(key[1]), urls.get(key[1])
            if not want or not url or not self._pushes[key].matches(url, *want):
                self._pushes.pop(key).stop()
        for channel, (name, dest) in wanted.items():
            url = urls[channel]
            if url and (device_id, channel) not in self._pushes:
                try:
                    self._pushes[(device_id, channel)] = FlvPush(
                        device_id, channel, url, name, dest, self.ffmpeg_bin,
                        camera_audio=src["audio"], pool=self._pool).start()
                except ValueError:
                    log.warning("push %s/%s: bad destination %r", device_id, channel, dest)
