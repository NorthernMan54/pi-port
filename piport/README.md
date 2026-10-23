# Pi Port -- the AI Port emulator (RK3588 · Raspberry Pi · x86 · macOS)

`piport` emulates a **UniFi Protect AI Port**: sysid `0xa5f1`, catalog platform
string `"UVC AI Port"` for adoption, discovery SKU `"UP-AI-Port"`. It runs on the
RK3588 NPU, a Coral Edge TPU, CPU onnxruntime, or Core ML on Apple silicon. It
answers the real discovery, adoption, and camera protocols well enough for a
Protect console to adopt it and pair real cameras to it, and runs real object
detection. It is an interoperability proof of concept, **not a product** and
**not affiliated with or endorsed by Ubiquiti**. See "Scope and legal" below.

## Wire protocol (confirmed)

The AI Port shares firmware middleware with real UniFi cameras (`ubnt_ctlserver`,
`ubnt_avclient`), so its protocol is the known camera/chime protocol plus AI-Port-specific
additions. All of the following was confirmed live against a real console.

1. **UDP/10001 discovery** -- classic UBNT L2 discovery. The discovery platform TLV
   (`0x0C`) advertises the SKU `"UP-AI-Port"` by default (`[identity]
   discovery_platform`); the canonical `"UVC AI Port"` is the adoption/catalog
   type. sysid is `0xa5f1`.
2. **HTTPS control API on the device, port 443** (the shared camera firmware's
   `ubnt_ctlserver`, not AI Key's bespoke 8080):

   | Method | Path                | Purpose |
   |--------|---------------------|---------|
   | POST   | `/api/1.2/manage`   | Adopt endpoint. Nested body: `{"username","password","mgmt":{"username","password","hosts","token","protocol","mode","nvr","controller","consoleId","consoleName"}}`. |
   | POST   | `/api/1.2/login`    | Controller logs in first (any `Set-Cookie` accepted) before most `api/1.2/<action>` calls. |
   | GET    | `/api/1.2/status`   | `{fw, board:{hwaddr}, features:{...}}`. A camera capability endpoint; not the AI Port capability mechanism (step 4). |
   | POST   | `/api/1.2/snapshot` | Still image. |

3. **Outbound mTLS WebSocket -- `ucp4` binary envelope**, to `<console-ip>:7442` (8-byte
   header + JSON envelope, as chime/AI Key). Handles adoption bookkeeping and generic RPCs
   (`getInfo`, `networkStatus`, `changeUserPassword`, …). It does **not** carry camera commands:
   `registerDeviceWebSocket` only adds a connection to the camera-command routing pool for
   connections that do **not** negotiate `ucp4`, which is why pairing needs a second
   connection.
4. **Outbound mTLS WebSocket -- the classic pre-`ucp4` avclient protocol**, same URL/port,
   headers `camera-mac`/`camera-model` (`camera-model` is the literal sysid string `"0xa5f1"`,
   not `"UVC AI Port"`), subprotocol `secure_transfer`. **Wire format is plain JSON in BINARY
   WebSocket frames** (not text). This connection carries:
   - `UiStreamControl` -- pairing/streaming control. Reply must be
     `{"status":"started"|"stopped","usedPoints":N}`; `usedPoints` MUST be on every reply
     (start **and** stop) or the controller's capacity bookkeeping breaks after repeated
     pair/unpair cycles.
   - `EventSmartDetect` -- device-to-controller detection push. Top-level `descriptors` array
     (not `tracks`); each has `objectType`, `coord: [x,y,w,h]` (flat array), `trackerID`,
     `confidenceLevel`. Top-level `edgeType` is a per-track state machine: `"enter"` opens a
     dashboard event, `"moving"` updates it, `"leave"` closes it and triggers a thumbnail
     upload. **`"none"` alone opens nothing.**
   - `EventFeatureFlagsUpdated` -- device-to-controller capability push; the real mechanism
     behind `featureFlags.smartDetectTypes` / `hasMotionDetection` (see "Object detection").
   - `GetRequest` (`what: "snapshot"` or `"smartDetectZoneSnapshot"`) -- asks for a JPEG,
     uploaded as `multipart/form-data` to the request's `uri`.

## Components

| File | Role |
|------|------|
| `config.py` + `aiport.cfg` | Shared configuration and device-identity loader. All stages read the same file; CLI flags override cfg. |
| `discovery.py` | UDP/10001 discovery responder, discovery platform `"UP-AI-Port"` (TLV `0x0C`), `sysid=0xa5f1`. |
| `http_api.py` | HTTPS `:443`: `/api/1.2/manage`, `/api/1.2/login`, `/api/1.2/status`, `/api/1.2/snapshot`. |
| `ucp4_client.py` | Generic `ucp4` device-management WebSocket client (adoption, `getInfo`). |
| `avclient.py` | Classic per-camera avclient client: pairing, detection/classification, `EventSmartDetect` / `EventFeatureFlagsUpdated` pushes, snapshot upload. Most real behavior lives here. |
| `detectors/` | Pluggable detection backends selected by `[hardware] detector`: `rknn` (RK3588 NPU), `edgetpu` (Coral USB/PCIe), `cpu` (onnxruntime; Core ML on Apple-silicon macOS). `postprocess.py` holds the shared YOLOv5 decode/NMS and COCO → person/vehicle/animal mapping. |
| `run_all.py` | Launches all of the above together. |
| `sysinfo_server.py` | Board system monitor / multi-instance web UI (CPU, memory, GPU, NPU); not part of the protocol. |
| `instance_manager.py` | Manages multiple reboot-surviving emulator instances as systemd units (macvlan or physical). |

## Configuration (`aiport.cfg`)

Identity, networking, and the console endpoint live in `aiport.cfg` (INI) next to `run_all.py`,
loaded via `config.py`. Every stage reads the same file, and any CLI flag overrides the cfg
value. The protocol scripts also accept `--config <path>` and `--debug`; `run_all.py` passes both through
to its children. Inline comments are **not** stripped by `configparser`: an
inline `#` becomes part of the value, so keep comments on their own lines.

```ini
[identity]
mac =
name = AI Port
hostname = piport
platform = UVC AI Port
discovery_platform = UP-AI-Port
sysid = 0xa5f1
fw_version = 5.1.12.67
discovery_fw_version = aiport4G.mt8390.v5.1.12.67.emu.260904.0000
device_id = fe6488e7-7042-5bcb-ab86-6f0ad1a5baed
guid = 5c2659d4-48f3-16c4-ed83-2fefbad37066
[network]
iface =
parent_iface =
mode = dhcp
ip =
netmask = 255.255.255.0
gateway =
dns =
[console]
host =
port = 7442
[hardware]
detector = auto
detector_model =
[logging]
debug = false
```

| Key | Meaning |
|-----|---------|
| `[console] host` | IP of the Protect console **you own**; the avclient dials it when not yet adopted. Blank by default. `port` default 7442. |
| `[identity] mac` | Empty derives a Ubiquiti-OUI MAC `FC:EC:DA:<iface last 3 octets>`. |
| `[identity] platform` | Must match the controller's catalog string. |
| `[identity] discovery_platform` | Discovery TLV `0x0C` SKU (`UP-AI-Port`); blank falls back to `platform`. |
| `[identity] device_id` | Must be unique per emulated instance (the shipped default is shared; `instance_manager.py`/the container entrypoint generate unique ones). |
| `[identity] name` | Advertised name (`AIPORT_NAME`); Protect's rename is persisted separately. |
| `[identity] guid` | Fixed AI Port catalog GUID; must not be random. |
| `[identity] fw_version` / `discovery_fw_version` | Short version over ucp4/avclient/http / longer L2 discovery string. |
| `[network] mode` | `dhcp` reads the live lease; `static` uses `ip`/`netmask`/`gateway`. `create --ip X.Y.Z.W` or `mode=static` makes `instance_manager.py` use `ip addr add` instead of `dhclient`. |
| `[network] iface` / `parent_iface` | Blank = auto-detect (default-route interface). |
| `[hardware] detector` | `auto` (default), `rknn`, `edgetpu`, `cpu` or `none`; `AIPORT_DETECTOR` overrides. |
| `[hardware] detector_model` | Model file for that backend; blank = its default in `models/` (`AIPORT_MODEL_PATH` overrides). |
| `[logging] debug` | Debug logs; any `--debug` overrides. |

Env vars override the same keys after the file is read (`config.ENV_OVERRIDES`) --
handy in containers: `AIPORT_CONSOLE_HOST`/`AIPORT_CONSOLE_PORT`, `AIPORT_MAC`,
`AIPORT_HOSTNAME`, `AIPORT_NAME`, `AIPORT_DEVICE_ID`, `AIPORT_IFACE`,
`AIPORT_PARENT_IFACE`, `AIPORT_NETWORK_MODE`/`IP`/`GATEWAY`, `AIPORT_DEBUG`. A blank
env var is ignored (the file value survives).

## Install and run

One-shot installer (packages, venv, deps, model assets, `librknnrt` upgrade, systemd unit, web UI):

```bash
sudo scripts/install.sh --console <UNVR-IP> [--parent-iface <iface>]
```

Options: `--name NAME`, `--accel auto|rknn|edgetpu|cpu|none` (default `auto`; `--no-npu`
is an alias for `none`), `--rknn PATH`, `--rknn-url URL` / `--rknn-sha256 HEX`,
`--webui-port N`. Run as root. The manual equivalent:

```bash
pip install -r requirements.txt
pip install rknn-toolkit-lite2   # or ai-edge-litert (Coral) / onnxruntime (CPU)
sudo python3 run_all.py --iface <your-iface> --fallback-host-port <console-ip>:7442
```

Identity and the console endpoint default from `aiport.cfg` -- edit it (or pass `--config`)
before the first run. Then adopt "AI Port" from the Protect UI like a normal camera, and pair a
real camera to it from the camera's own settings.

## Object detection (NPU / CPU / Core ML / Edge TPU)

The pipeline runs a real COCO detector through `piport/detectors/`, chosen at runtime
by `[hardware] detector` / `AIPORT_DETECTOR` (`auto|rknn|edgetpu|cpu|none`; `auto`
takes the first backend whose runtime, hardware and model are present):

- `rknn` — RK3588 NPU via `rknnlite` (quantized YOLOv5s `.rknn`).
- `edgetpu` — Coral USB/PCIe via `ai-edge-litert` (SSDLite MobileDet, or a YOLOv5
  Edge TPU export); see below.
- `cpu` — `onnxruntime` (Ultralytics `yolov5n.onnx`; also accepts Rockchip's
  `yolov5s_relu.onnx`), on x86_64/arm64 or Core ML on Apple-silicon macOS.

`AIPORT_MODEL_PATH` / `[hardware] detector_model` overrides the model file; output
layouts are auto-detected. Model binaries are not committed; fetch the assets and
optionally build `.rknn`:

```bash
python3 scripts/fetch_models.py                     # rknn assets (also downloads yolov5n.onnx)
python3 scripts/fetch_models.py --no-cpu-model      # rknn assets without yolov5n.onnx
python3 scripts/fetch_models.py --backend cpu       # CPU/Core ML model
python3 scripts/fetch_models.py --backend edgetpu   # Coral MobileDet model
python3 scripts/fetch_models.py --convert           # build .rknn (x86 host)
```

For a containerized CPU install (x86 servers / Raspberry Pi 4/5) see `docker/README.md`.
macOS is bare metal only — Docker Desktop's VM can't do the L2 discovery the console
needs; see the root README.

**Video decode** picks a hardware backend automatically, or by `AIPORT_HWDEC`
(`auto`|`rkmpp`|`v4l2m2m`|`none`): `rkmpp` on the RK3588 (ffmpeg-rockchip), `v4l2m2m`
on a **Raspberry Pi 4 only** (BCM2711; the bcm2835-codec decoder at `/dev/video10`,
which also needs `/dev/vchiq`), else software. The **Pi 5 has no H.264 hardware
decoder** (HEVC only), so it always software-decodes H.264 -- and its CPU does that
faster than the Pi 4's hardware block. See `docker/README.md` and the root README
for the measured numbers and the known Pi 4 decoder limits.

The CPU detector disables onnxruntime's thread **spin-wait** (it burned ~3 extra cores
alongside ffmpeg on a Pi 4) and caps threads with `AIPORT_ORT_INTRA_THREADS` (e.g. `2`
on a Pi 4; unset = onnxruntime's default). `AIPORT_OBJ_THRESH` overrides the detection
confidence gate.

On macOS (Apple silicon, bare metal) the onnx backend uses onnxruntime's **Core ML** provider
(`MLProgram` on the Neural Engine): ~6.5 ms vs ~37 ms per frame on CPU (M1 Pro, `yolov5n`),
with the same detections. `AIPORT_ORT_PROVIDER=cpu` forces CPU; `auto` (default) picks Core ML
when onnxruntime offers it (never on Linux) and falls back to CPU if the model won't compile.

Classification is also **gated by a cheap frame-diff**: a tick with no open track and no
meaningful frame change skips inference entirely (on a static scene this is the difference
between a busied core and ~1% CPU). `AIPORT_MOTION_GATE=0` disables it and
`AIPORT_MOTION_GATE_THRESH` tunes the threshold (default `0.02`, on an 80x80 grayscale
diff). Tracks that are already open always classify, so a stationary object keeps its
track and can close normally.

**Conversion must run on a separate x86_64 build host.** `rknn-toolkit-lite2` is the on-device,
inference-only API (aarch64-only wheels for Python <= 3.12); it can only run an already-converted
`.rknn`. Conversion needs full `rknn-toolkit2`, matching the on-Pi version. Two pins avoid known
environment bugs (`setuptools>=81` dropped `pkg_resources`; `onnx` 1.22.0 dropped `onnx.mapping`,
still used by `rknn-toolkit2`):

```bash
pip install "setuptools<81" "onnx==1.16.1"
pip install rknn-toolkit2==2.3.2
pip install onnx==1.16.1   # repin; rknn-toolkit2 may upgrade onnx
```

`fetch_models.py` places the conversion assets (`anchors_yolov5.txt`,
`coco_80_labels_list.txt`) in `models/`; `--convert` builds
`yolov5s_relu.rknn` there on the x86 host.

- **System `librknnrt.so` upgrade (required).** The stock OS ships runtime 1.4.0 (2022), which
  cannot load models built by `rknn-toolkit2==2.3.2` (`Invalid RKNN model version 6`). Replace
  `/usr/lib/librknnrt.so` with the v2.3.2 aarch64 runtime (back up the original, then `ldconfig`);
  `scripts/install.sh` does this. `RKNNLite().init_runtime()` should then log version 2.3.2.
- **Explicit batch dimension.** `RKNNLite.inference()` needs `frame[np.newaxis, ...]` (shape
  `(1,H,W,3)`) or it fails with `"need 4dims input, but 3dims input buffer feed"`. Handled in
  `detectors/rknn.py`.

## Object detection (Coral Edge TPU, e.g. Raspberry Pi 5)

`detectors/edgetpu.py` runs Google's SSDLite MobileDet COCO model (320x320 uint8, decode and NMS
in-graph, the model Frigate ships) on a Coral via `ai-edge-litert` and the `libedgetpu` delegate;
pycoral is not used (it has no wheels past Python 3.9). `scripts/install.sh --accel edgetpu` does
the setup below.

- **Runtime pairing.** Google's own `libedgetpu` apt repo stopped in 2021. feranick's maintained
  `libedgetpu1-std` 16.0 (built against TF 2.19.1) works with `ai-edge-litert` 2.3.0 on Pi OS
  trixie / Python 3.13 (verified on a Pi 5). `tflite-runtime` has no Python 3.13 wheels.
- **`std`, not `max`.** The max-clock build runs the USB stick very hot.
- **USB ids.** The Coral enumerates as `1a6e:089a` until libedgetpu uploads its firmware on first
  open, then as `18d1:9302`.
- **"Failed to load delegate" with no message** (verbose libedgetpu logs show `USB error -7
  [GetDeviceDescriptor]`): the stick stopped answering on USB, typically after an earlier failed
  open. Unplug and replug it; no driver install fixes this.
- **USB 2 vs 3.** The Pi 5 has only two USB 3 ports. On USB 2 MobileDet takes ~33 ms per frame
  (measured); USB 3 is faster (not measured here). Either is far below the 1.5 s detection tick.
  The web UI shows the Coral's link speed.
- **PCIe/M.2 Coral** additionally needs the `gasket`/`apex` kernel driver (and, on a Pi 5, the
  4K-page kernel); not handled by the installer.
- **No H.264 hardware decode on the Pi 5** (only HEVC), so camera streams are decoded on the CPU.

## ONVIF cameras (`onvif_push.py`)

For an assigned third-party camera Protect sends `OnvifStreamControl` (the camera's
RTSP URL, `streaming` true/false) and, per channel, `ChangeVideoSettings` with an
`extendedFlv` `avSerializer`: a `streamName` and `tcp://<console>:7550` as destination.
Protect's own ingest for that camera stops; without the push the camera has no video.

Each channel is `ffmpeg -c:v copy` (plus AAC audio) into FLV, rewritten by
`rewrite_flv()` and sent over TCP. **H.264 only:** an HEVC source is muxed by
ffmpeg as Enhanced RTMP (an `hvc1` FourCC), which `ms` refuses (live view resets,
the push is closed after a few hundred bytes); `rewrite_flv()` raises
`UnsupportedCodec` for it and the push logs that once. Set the camera to H.264.

- FLV header flags byte `7`; after every tag a 16-byte trailer: `0`, a 24-bit
  timescale (90000 video, 11025 otherwise), four reserved zero bytes, and the
  elapsed time **in that timescale** (64-bit).
- `onClockSync` (`streamClock`, `wallClock`) and `onMpma` script tags every 5 s,
  the first only after 5 s -- Protect closes a push whose clock sync precedes the
  first frames.

The format follows unifi-cam-proxy's `clock_sync.py` (MIT), except the trailer units:
it writes 10-microsecond units regardless of timescale, which Protect 7.2's media
server reads as video 11% and audio 9x too fast and drops ("Video frame wc=... diff
exceeds threshold" in `/srv/ms/logs/ms.err.00.log`); the browser live view then never
starts. The `ChangeVideoSettings` reply reports the stream's probed resolution:
Protect echoes back whatever was answered before, and the web player sizes its
decoder from it.

## Stream pipeline

`avclient.py` runs one ffmpeg process per paired camera that pulls the RTSP stream and uses
`filter_complex split=2` for both outputs from a single decode: a 1/2-fps JPEG still (snapshots)
and 2-fps raw RGB frames at the detector's input size (640x640 RKNN/CPU, 320x320 Edge TPU) piped
to Python for classification (no image-decode library needed). A background loop classifies the latest frame every 1.5s (`MOTION_POLL_INTERVAL_S`): a
detection opens (`"enter"`) or refreshes (`"moving"`) a track, and a track closes (`"leave"`)
only after `MOTION_MISS_TOLERANCE_TICKS` (20, ~30s) of **consecutive** empty ticks. Simultaneous
objects are tracked independently and matched frame-to-frame by bounding-box IOU within each type.

Capability declaration is separate. For an event to be filterable by object type in the UI, the
camera's own `featureFlags.smartDetectTypes` / `hasMotionDetection` must be populated. This does
**not** come from `/api/1.2/status`; the real mechanism is the `EventFeatureFlagsUpdated` push
(above). The controller writes these into the AI Port's `featureFlagsMap` for the paired camera's
MAC and propagates the overridable subset on the next reconnect pass.

## Known limitations and next steps

- Live verification of multi-object tracking in a genuinely simultaneous different-type scene is
  still pending; only the single-object path has been re-confirmed live.
- Line Crossing support is implemented, but its event and capability shapes have not been tested
  against a live controller.
- The classifier is a stock COCO detector (YOLOv5 on NPU/CPU/Core ML, SSDLite
  MobileDet on Coral), not Ubiquiti's second-pass classifier; real Ubiquiti
  weights are not portable to this NPU.

## Scope and legal

This only talks to hardware and a Protect console **you own**, using protocol details
reverse-engineered from **your own** firmware, purely to interoperate with your own UniFi Protect
install. Nothing here targets third-party systems; no Ubiquiti binaries are distributed. Licensed
AGPL-3.0-or-later (see the repository root).
