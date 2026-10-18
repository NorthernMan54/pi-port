# Pi Port -- a DIY UniFi Protect "AI Port" on Orange Pi 5 Plus / RK3588

`piport` emulates a **UniFi Protect AI Port** on an RK3588 board (Orange Pi 5 Plus): sysid
`0xa5f1`, catalog platform string `"UVC AI Port"`. It answers the real discovery, adoption, and
camera protocols well enough for a Protect console to adopt it and pair real cameras to it, and
runs real object detection on the RK3588 NPU. It is an interoperability proof of concept, **not a
product** and **not affiliated with or endorsed by Ubiquiti**. See "Scope and legal" below.

## Wire protocol (confirmed)

The AI Port shares firmware middleware with real UniFi cameras (`ubnt_ctlserver`,
`ubnt_avclient`), so its protocol is the known camera/chime protocol plus AI-Port-specific
additions. All of the following was confirmed live against a real console.

1. **UDP/10001 discovery** -- classic UBNT L2 discovery. The `platform` TLV (`0x0C`) must
   equal the controller's catalog string `"UVC AI Port"`; sysid is `0xa5f1`.
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
| `discovery.py` | UDP/10001 discovery responder, `platform="UVC AI Port"`, `sysid=0xa5f1`. |
| `http_api.py` | HTTPS `:443`: `/api/1.2/manage`, `/api/1.2/login`, `/api/1.2/status`, `/api/1.2/snapshot`. |
| `ucp4_client.py` | Generic `ucp4` device-management WebSocket client (adoption, `getInfo`). |
| `avclient.py` | Classic per-camera avclient client: pairing, detection/classification, `EventSmartDetect` / `EventFeatureFlagsUpdated` pushes, snapshot upload. Most real behavior lives here. |
| `detector.py` | YOLOv5 detector with a runtime-selected backend: `RKNNLite` (quantized YOLOv5s on the RK3588 NPU) or `onnxruntime` (CPU, `yolov5n.onnx`; x86_64/arm64). |
| `run_all.py` | Launches all of the above together. |
| `sysinfo_server.py` | Board system monitor / multi-instance web UI (CPU, memory, GPU, NPU); not part of the protocol. |
| `instance_manager.py` | Manages multiple reboot-surviving emulator instances as systemd units (macvlan or physical). |

## Configuration (`aiport.cfg`)

Identity, networking, and the console endpoint live in `aiport.cfg` (INI) next to `run_all.py`,
loaded via `config.py`. Every stage reads the same file, and any CLI flag overrides the cfg
value. The protocol scripts also accept `--config <path>` and `--debug`; `run_all.py` passes both through
to its children. Only full-line `#` comments are supported (`configparser` ignores inline ones).

```ini
[identity]
mac =
hostname = piport
platform = UVC AI Port
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
[logging]
debug = false
```

| Key | Meaning |
|-----|---------|
| `[console] host` | IP of the Protect console **you own**; the avclient dials it when not yet adopted. Blank by default. `port` default 7442. |
| `[identity] mac` | Empty derives a Ubiquiti-OUI MAC `FC:EC:DA:<iface last 3 octets>`. |
| `[identity] platform` | Must match the controller's catalog string. |
| `[identity] device_id` | Must be unique per emulated instance. |
| `[identity] fw_version` / `discovery_fw_version` | Short version over ucp4/avclient/http / longer L2 discovery string. |
| `[network] mode` | `dhcp` reads the live lease; `static` uses `ip`/`netmask`/`gateway`. `create --ip X.Y.Z.W` or `mode=static` makes `instance_manager.py` use `ip addr add` instead of `dhclient`. |
| `[network] iface` / `parent_iface` | Blank = auto-detect (default-route interface). |
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

Options: `--name NAME`, `--no-npu` (protocol stack only), `--rknn PATH`, `--rknn-url URL` /
`--rknn-sha256 HEX`, `--webui-port N`. Run as root. The manual equivalent:

```bash
pip install -r requirements.txt
pip install rknn-toolkit-lite2   # see "Object detection" below first

sudo python3 run_all.py --iface <your-iface> --fallback-host-port <console-ip>:7442
```

Identity and the console endpoint default from `aiport.cfg` -- edit it (or pass `--config`)
before the first run. Then adopt "AI Port" from the Protect UI like a normal camera, and pair a
real camera to it from the camera's own settings.

On macOS, `sudo scripts/piport_service.sh` runs it from settings in `~/.piport.env`
(`scripts/piport.env.example`) and restarts it on `touch ~/.piport-restart`, so a code or
settings change needs neither a new terminal nor the password.

## Object detection (NPU / CPU)

The pipeline runs a real YOLOv5 model through `detector.py`, which picks a backend at
runtime (`AIPORT_BACKEND=auto|rknn|onnx`, or infer from `AIPORT_MODEL_PATH`): the RK3588
NPU via `rknnlite` (quantized YOLOv5s `.rknn`), or CPU via `onnxruntime` (Ultralytics
`yolov5n.onnx`, x86_64/arm64). Model binaries are not committed; fetch the assets (ONNX,
anchors, COCO labels, calibration subset) and optionally build `.rknn`:

```bash
python3 scripts/fetch_models.py            # CPU model + conversion assets
python3 scripts/fetch_models.py --convert  # build .rknn (x86 host)
```

For a containerized CPU install (x86 servers / Raspberry Pi 4/5) see `docker/README.md`.

**Video decode** picks a hardware backend automatically, or by `AIPORT_HWDEC`
(`auto`|`rkmpp`|`v4l2m2m`|`videotoolbox`|`none`): `rkmpp` on the RK3588 (ffmpeg-rockchip), `v4l2m2m`
on a **Raspberry Pi 4 only** (BCM2711; the bcm2835-codec decoder at `/dev/video10`,
which also needs `/dev/vchiq`), `videotoolbox` on **macOS** for HEVC (`-hwaccel videotoolbox`; on an
M2 a 2688x1512 HEVC camera took ~5% of a core instead of ~21%; H.264 stays software, as
VideoToolbox dropped pictures on a Protect-relayed H.264 stream), else software. The **Pi 5 has no H.264 hardware
decoder** (HEVC only), so it always software-decodes H.264 -- and its CPU does that
faster than the Pi 4's hardware block. See `docker/README.md` and `implementation.md`
for the measured numbers and the known Pi 4 decoder limits.

The CPU detector disables onnxruntime's thread **spin-wait** (it burned ~3 extra cores
alongside ffmpeg on a Pi 4) and caps threads with `AIPORT_ORT_INTRA_THREADS` (e.g. `2`
on a Pi 4; unset = onnxruntime's default). `AIPORT_OBJ_THRESH` (default 0.5) is the score every
detection needs (Frigate's `min_score`); whether a track is real is decided by its median
score (see **Confirmation**).

On macOS (Apple silicon, bare metal) the onnx backend uses onnxruntime's **Core ML** provider:
~6.5 ms vs ~37 ms per frame on CPU (M1 Pro, `yolov5n`), with the same detections. The Core ML
format follows the model's input dtype: `MLProgram` for the fp16 Ultralytics YOLOv5 (the default
NeuralNetwork format runs it at ~95 ms), NeuralNetwork for float32 graphs such as YOLOv9
(3 ms vs 22 ms under MLProgram, M2). `AIPORT_ORT_PROVIDER=cpu` forces CPU; `auto` (default) picks Core ML
when onnxruntime offers it (never on Linux) and falls back to CPU if the model won't compile.

**YOLOv8/v9 models.** The onnx backend also reads anchor-free exports (one `[1,4+classes,N]`
output: xywh and class scores, no objectness), e.g. Frigate's `yolov9-s-320.onnx`. The input
size comes from the graph: frames (640x640) are resized to it and boxes scaled back, so nothing
else changes. Select one with `AIPORT_MODEL_PATH`; the startup log shows `layout=anchor_free,
input=320x320`. The score is the class confidence (no objectness to multiply), gated by the same
`AIPORT_OBJ_THRESH`. On the M2's doorbell frame YOLOv9-s at 320 left out two flower pots YOLOv5s
called birds (0.55/0.54) and named an RV a truck where YOLOv5s said train; it is 3.4 ms per
frame vs ~8 ms. Small, distant objects at 320 are not yet compared.

`scripts/fetch_models.py` does not install it: there is no published YOLOv9 ONNX. Frigate
exports it locally from the YOLOv9 authors' weights (<https://github.com/WongKinYiu/yolov9>,
GPL-3.0; `yolov9-s-converted.pt`, release `v0.1`) with the Docker recipe in its object-detector
docs (`MODEL_SIZE=s`, `IMG_SIZE=320`). Copy that export into `piport/models/` (gitignored like
the other models); the one in use is sha256
`ae31147e082ac2026b67f930f92dcbe99328deda19a51e493ecaf26b495e081f`:

```bash
cp <frigate config>/model_cache/yolov9-s-320.onnx piport/models/
AIPORT_MODEL_PATH=$PWD/piport/models/yolov9-s-320.onnx ...   # on the run_all.py command
```

### Enrichment: vehicle attributes, license plates, faces (`enrich.py`, best-guess wire format)

On top of person/vehicle/animal, `enrich.py` adds three optional stages. The field names
come from Protect's backend (7.1.75); **none is confirmed against a real AI Port capture,
and the vehicle-attribute values follow Protect's label vocabulary:**

| Stage | Models | Emitted as |
|-------|--------|------------|
| Vehicle type + colour | YOLO class + HSV colour vote (no extra model) | `attributes: {vehicleType: {val, confidence}, color: {val, confidence}}` on vehicle descriptors (shape from the Protect backend; value vocabulary unconfirmed) |
| License plates | `fast-alpr` (YOLOv9 plate detector + CCT OCR, MIT) on the full-res snapshot inside each vehicle box | plate text in the vehicle descriptor's `name` (Protect's `parseSmartDetectTrackPayload` reads it there; `recognizeTracks` recognises moving vehicles with a `name`); per track, the most frequent reading of the largest group of similar reads (see below) |
| Faces | YuNet (MIT) inside each person box; SFace (Apache-2.0) embedding | its own `face` track carrying `faceEmbed` (128 floats, best-scoring face kept), which `recognizeTracks` passes to Protect's face service; an optional local-gallery match also sets `name`/`tag` on the face and parent person |

A vehicle that is still, or already has a confident plate (score >= 0.85), isn't
re-read: its plate can't have changed. A parked one is read for 5 s after it parks, as in
Frigate, then not again; a box mostly inside a parked car's box counts as that car. As in
Frigate, a track keeps its last 25 reads and groups those whose average Jaro-Winkler
similarity is >= 0.85; the largest group wins, and within it the reading seen most often
(Frigate takes the most confident), so one misread can't replace the plate. The plate is
logged at INFO only when a track's plate first appears or changes; each raw read is DEBUG. Plates and faces are read from the full-res snapshot (refreshed every second while they are on),
not the 640x640 detection buffer. The `smartDetect` capabilities gain `licensePlate`,
`lprEnhancedByAiKey`, `face`, `faceEnhancedByAiKey` only when the matching stage is available.
Knobs: `AIPORT_VEHICLE_ATTRS` / `AIPORT_LPR` / `AIPORT_FACE` (`auto`|`0`|`1`),
`AIPORT_LPR_THRESH`, `AIPORT_FACE_THRESH`, `AIPORT_FACE_MATCH_THRESH`, and
`AIPORT_FACES_DIR` (default `<state-dir>/faces`): one sub-directory per person, named after
them, holding a few clear face photos; edits are picked up without a restart.

Protect also sends `UpdateFaceDBRequest` with a URL for its known-faces database (named
groups, for on-device matching). `avclient.py` downloads it to `<state-dir>/facedb/known.db`
and logs its contents (Protect 7.3 sends a tar of `known/uiface.json`, the subject/name maps,
and `known/uiface.db`, a binary store), so the embedding layout can
be compared with SFace's; matching against it (`matchedId`) is not implemented yet. Whether
Protect's face service accepts SFace embeddings is also unconfirmed.

**Detection and report rates.** Detecting and reporting run at separate rates. Each camera's
detector analyses up to `AIPORT_DETECT_FPS` frames per second (10 on macOS, where Core ML runs
a frame in ~8 ms; elsewhere one frame per tick), each frame once, and tracking runs on every
analysed frame. `moving` updates go to Protect once per `AIPORT_TICK_S` (0.33 s on macOS,
matching a G5 Bullet's `moving` every ~330 ms, median 249-600 ms, with `displayTimeoutMSec`
~326; 1.5 s elsewhere), batching every track seen since the last update; `enter` and
`leave` go out as they happen. Boxes are displayed for one tick + 100 ms. Time-based
thresholds keep their durations at any rate: a track closes after `AIPORT_MISS_S` (default
3 s; the G5 sends `leave` ~2 s after the object was last seen), a parked track after 30 s,
30 s still counts as parked, 6 s to resume. The motion gate compares against a reference
frame held for one tick, so slow movement still registers at higher detection rates. The
RTSP pull uses `-flags low_delay`.

**Confirmation.** A new track joins the camera event only after it has been seen for
Protect's `eventStartMSec` (from `ChangeSmartDetectSettings`; 0 until Protect sends it), so
a detection lasting a frame or two never opens a dashboard event. A track lost before then
is dropped without any event. `AIPORT_CONFIRM_MS` overrides Protect's value.

It must also be a **true positive**, Frigate's rule (`frigate/track/tracked_object.py`): the
median of its last 10 scores, counting 0.0 for each analysed frame it was missed in, must
reach `AIPORT_TRUE_THRESH` (default 0.7; per type as `0.7,animal=0.75`). Once reached it
stays reached, and the track confirms as soon as it is also old enough, even on a frame it
was missed in. A tree stump on Backyard (median ~0.55) and a pot by the doorbell never pass;
a sedan seen on only two frames (0.85, 0.78) passes on the second and still gets its event
after the 1 s wait. A track that never passes is dropped silently; the debug log gives its
median, for tuning the threshold per type. A face or plate confirms as soon as a confirmed
person or vehicle is there, since it's only looked for inside that box and only once per
snapshot.

**Resuming tracks.** A track that closed within `AIPORT_RESUME_S` (default 60 s) is picked
up again by a same-type detection overlapping its last or first box (IoU >= 0.4): it rejoins
its event if that is still open, and a vehicle that never moved (centre within a quarter of
its size) or was parked comes back quietly as parked. Seen live: a car backed in with its
hatch open and two people unloading it was re-detected as a "new" vehicle in three
successive events. A person or a car that moved and left is still new next time. A box mostly (70%) inside a
parked car's box is part of that car, not a new vehicle: on the doorbell a second box on a
parked car's lower half opened "vehicle" events while nothing moved.

**Parked objects move by their pixels, not their boxes** (`stationary.py`, adapted from
Frigate's `stationary_classifier.py`, MIT). When a track parks, a crop of its spot becomes an
anchor. When its boxes later say it moved, the same spot is compared with the anchor
(normalised cross-correlation and phase-correlation shift): only if the picture there has
changed on two checks in a row, or drifted, does it rejoin as moving. Otherwise its box snaps
back to where it parked. On the doorbell, boxes on a parked car shrinking onto its lower half
had counted as the car moving.

**Matching with Norfair** (`norfair_match.py`, after Frigate's `norfair_tracker.py`, MIT;
`AIPORT_TRACKER`, default `norfair` when the package is installed, else `builtin`). One
Norfair tracker (BSD-3-Clause, Kalman-filtered) per object type, with Frigate's distance
(bottom-centre offset relative to size, plus width and height change) and limits, decides
which detection belongs to which track; pi-port's own track state (confirmation, parking,
events, miss tolerance) is unchanged. On top: a parked object's repeated box is its own; a
parked track only takes a box overlapping its spot; and what Norfair leaves, parked tracks
take by overlap and moving tracks by the nearest detection where their motion predicts them
(the built-in rules below). Two people crossing kept their IDs where the built-in matcher
swapped them. Install it without its dependency pins, which would downgrade numpy below 2
(OpenCV 5 and onnxruntime need 2): `venv/bin/pip install scipy` then
`venv/bin/pip install --no-deps norfair==2.3.0 "filterpy>=1.4.5,<2"`.

**Tracking.** Detections match tracks by box overlap; a moving track no box overlaps takes
the nearest same-type detection near where its last movement predicts it (within 3x its size,
at most 150 of 1000, widening by a quarter per missed tick up to 1.5x), so small, fast objects
no longer split into a new track every tick, and a missed track can't take a car across the
frame. A lone track and a lone detection no longer match regardless of distance. A moving
track never takes a box over 2x wider or taller (or narrower or shorter) than its own, from
Norfair or the nearest rule: a bicycle's track mustn't move onto a bigger object beside it. Parked tracks never take a
detection that doesn't overlap them (a passing car can't wake one). Reported boxes are
smoothed (0.6 weight on the newest detection); matching and crops use the raw box.

**Box timing.** Protect places boxes in playback by `clockWall`. Each frame is stamped
(wall and monotonic time) as it is read from ffmpeg, and every frame-describing time
(`clockWall`, the stream clocks, first-shown/idle times, `clockBestWall`) comes from the
analysed frame's stamp, so the tick wait and inference time don't delay boxes. What's left is
Protect's relay and our decode. `AIPORT_BOX_DELAY_MS` back-dates every such time by that
residual: `"500"` for all cameras, or with per-camera overrides, e.g.
`"300,AABBCC3344CC=800"` (a proxied camera has an extra hop). Default 0. Tune it by comparing a
box's time with the video in playback.

**Events.** Protect keeps one smart-detect event per camera, not per track: the first
`leave` closes it, and a scheduled check force-ends one left open past ~5.5 minutes without
fetching any snapshots. So tracks share a camera event: `enter` when the first track
appears, one batched `moving` per tick, and a single `leave` with one participant's
snapshot when the last active track goes or `eventMaxDurationMSec` (from
`ChangeSmartMotionSettings`, 5 min by default) runs out. A single-participant event closes with that
tracker as the leave's descriptor and snapshot (proven live). A multi-participant event closes
the way Protect's backend reads a leave: empty `descriptors`, one `smartDetectSnapshots` entry
per participant, and `trackerIDAttrMap` carrying `faceEmbed` (face), `associatedFaceTrackerID`
(the person whose box holds the face), and `vehicleType`/`color`/`matchedName` (plate) for
vehicles. If Protect requests none of its snapshots within 4 s, the single-tracker leave (face >
person > vehicle > animal) is resent. Seen live: Protect recorded faces in its faces section from
pi-port's face events, so its face service accepts SFace embeddings. A track still in view at the time
limit, or a vehicle still for ~30 s (box overlapping its anchor, so detector jitter doesn't
count), parks: it goes quiet, its snapshot stays in the event's leave, and the event closes
once nothing else is active. Objects already present in a camera's first 15 s (cars parked
there when pi-port starts) are parked at once and open no event; the 15 s start again
whenever a camera's stream restarts. Still tracks report
`stationary: true` with `idleSinceTimeMs` set to when they stopped. A parked object resumes only when it really moves: its box mostly leaves the parked spot,
or shifts at about the same size for ~6 s (someone walking past only shrinks it for a moment). Line-crossing events stay per track.

**Coordinates and thumbnails.** Protect draws descriptor coords (and the user's zones and
lines) on a 16:9 canvas the height of the video; a camera of another aspect sits centred
in it (a 1920x2560 doorbell fills the middle 42%). Boxes are converted to that canvas when
sent, and zone/line checks run in it; crops and plate/face reads stay in image space.
A detection is in a zone (or an exclusion zone) when its box's **bottom-centre** is, where the
object stands, as in Frigate; line crossing still follows the box centre. A person walking
along Backyard's hedge had their box centre on the edge of zone 2 while their feet were well
inside it.
Each track keeps its best-scoring frame (full-res JPEG, box, time); on `leave` the
object crop, the Full-FoV image and `clockBestWall` come from it, not from the frame
seconds after the object was last seen. The JPEG is the very frame the detector scored:
ffmpeg encodes every detection frame at full resolution, numbered like the raw frames
(a once-a-second snapshot was up to a second off, enough for a passing bus to leave
the crop). Seen live: Protect shows vehicle attributes
(`Truck (70%), White (70%)`, `Red (42%)`). Attributes become labels verbatim, so only
Protect's values show: `vehicleType` car/suv/truck/van/bus/motorcycle/bike (COCO "car" is
sent as `car`; `sedan` displayed nothing) and `color`
red/orange/yellow/green/blue/purple/pink/brown/black/white/gray (silver is sent as `gray`).

**Region detection** (`regions.py`, adapted from Frigate's region code, MIT; on by default on
macOS, `AIPORT_REGIONS=0` turns it off and is the default on Linux, where the RK3588 NPU model
is fixed at 640x640). The detector frame keeps the camera's aspect ratio, its long side
`AIPORT_DETECT_SIZE` (default 1280: 1280x720 for a 16:9 camera, 954x1280 for the portrait
doorbell), instead of being squeezed into 640x640. The model then runs on square crops, each at
least its input size (320) and at most the frame's short side: around every tracked object and
around motion boxes not already covered, clustered as Frigate does. Detections from
overlapping crops are merged (per-label NMS, a box cut by its crop's edge competes at 0.6, and
a box mostly inside a bigger same-label box is dropped). The first frame of a stream gets one
full look (a few crops the frame's height) so parked cars are found without motion. A parked
object is re-detected every 10 s, or when motion touches it; in between it keeps its parked
box, and each re-detection that finds it in place refreshes its pixel anchor. A track needs 2
detections before it can be a true positive, as Frigate's tracker needs 2 hits. A small or
distant object now reaches the model at close to its native size.

Classification is also **gated by motion** (`motion.py`, adapted from Frigate's
`improved_motion.py`, MIT): each frame is shrunk to 100 rows of grayscale, contrast-stretched,
blurred and compared with a running-average background; pixels differing by more than the
threshold form motion boxes, and a frame where most of the picture changes (lightning, IR
switching) recalibrates instead. A frame with no motion box and no open track skips
inference. Unlike the old whole-frame average diff, this finds a person walking through
Backyard's background (~0.5% of the frame). Protect's motion sensitivity (`level`, 50 by
default) sets the pixel threshold (50 -> 30, Frigate's default; 100 -> 5) and its motion zones
mask the rest of the frame; `AIPORT_MOTION_THRESHOLD` overrides the threshold and
`AIPORT_MOTION_GATE=0` disables the gate. `AIPORT_MOTION_CONTOUR_AREA` sets the smallest
motion blob, in pixels of the 100-row motion frame (Frigate's `motion.contour_area`), per
camera like the box delay: `10,AABBCC3344CC=100` (10 by default, as in Frigate). It costs ~0.4 ms a frame on the M2. Without OpenCV
the old average diff (`AIPORT_MOTION_GATE_THRESH`) is the fallback. Tracks that are already
open always classify, so a stationary object keeps its track and can close normally.

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

`fetch_models.py` places `yolov5s_relu.rknn`, `anchors_yolov5.txt`, and `coco_80_labels_list.txt`
in `models/` next to `detector.py`.

- **System `librknnrt.so` upgrade (required).** The stock OS ships runtime 1.4.0 (2022), which
  cannot load models built by `rknn-toolkit2==2.3.2` (`Invalid RKNN model version 6`). Replace
  `/usr/lib/librknnrt.so` with the v2.3.2 aarch64 runtime (back up the original, then `ldconfig`);
  `scripts/install.sh` does this. `RKNNLite().init_runtime()` should then log version 2.3.2.
- **Explicit batch dimension.** `RKNNLite.inference()` needs `frame[np.newaxis, ...]` (shape
  `(1,H,W,3)`) or it fails with `"need 4dims input, but 3dims input buffer feed"`. Handled in
  `detector.py`.

`avclient.py` runs one ffmpeg process per paired camera that pulls the RTSP stream and uses
`filter_complex fps,split=2` for both outputs from a single decode: a full-res JPEG of every
detection frame (an `image2pipe` on a second pipe; best frames come from it, and every 1-2 s it
becomes the snapshot file) and the same frames as 640x640 raw RGB on stdout for classification
(no image-decode library needed). The JPEGs cost ~8% of a core per 2560x1440 camera at 5 fps
on an M2; they're made at the detection rate (`AIPORT_DETECT_FPS`), so expect about twice
that at macOS's default 10. Files live in a RAM/temp stream dir unique to each `--state-dir`
(`<tmp>/aiport-streams/<hash>/`), so several AI Ports can share a host; its `tmp/` folder
holds the in-flight files (snapshot rewrites, crop inputs) and is emptied at startup, so a
crash leaves nothing behind. A background loop classifies the latest frame every 1.5s (`MOTION_POLL_INTERVAL_S`): a
detection opens (`"enter"`) or refreshes (`"moving"`) a track, and a track closes (`"leave"`)
only after `AIPORT_MISS_S` (default 3 s) of **consecutive** empty ticks. Simultaneous
objects are tracked independently and matched frame-to-frame by bounding-box IOU within each
type, with a nearest-neighbour fallback for moving tracks (see **Tracking** above).

Capability declaration is separate. For an event to be filterable by object type in the UI, the
camera's own `featureFlags.smartDetectTypes` / `hasMotionDetection` must be populated. This does
**not** come from `/api/1.2/status`; the real mechanism is the `EventFeatureFlagsUpdated` push
(above). The controller writes these into the AI Port's `featureFlagsMap` for the paired camera's
MAC and propagates the overridable subset on the next reconnect pass.

## Replaying clips (`replay.py`, `scripts/record_clip.py`)

Tracking changes are checked offline on recorded clips instead of waiting for live events.
Record what a paired camera sends while pi-port is running (it finds the stream URL and the
camera's Protect settings in `~/piport.log`):

```bash
scripts/record_clip.py F4E2C60D4B4C 60 backyard-walk
```

That writes `~/piport-clips/backyard-walk.mp4` (video copied, not re-encoded) and
`backyard-walk.json` (stream size, zones, exclusion zones, lines, `eventStartMSec`, event
time limit, start time). Clips stay outside git. Then replay it through the live detector and
tracker, with the same env vars as the live command:

```bash
AIPORT_TICK_S=0.2 AIPORT_MODEL_PATH=$HOME/Code/pi-port/piport/models/yolov9-s-320.onnx \
  venv/bin/python piport/replay.py ~/piport-clips/backyard-walk.mp4 [--moves] [--json] [-v]
```

It prints each `enter`/`leave` (with `--moves`, every update) at clip time, then one line per
event with its objects and their best confidence; `-v` adds avclient's log. The clip runs
through the same ffmpeg filter graph as a live camera at `AIPORT_DETECT_FPS`, with the clock
taken from frame timestamps (20 s of Backyard replays in ~5 s on the M2); 5 s of empty frames
after the clip let open tracks close (`--settle`). The live 15 s startup window, in which
objects already in view are parked, is off unless `--startup`. Not covered: thumbnails,
plates and faces (they read the live snapshot file).

## Known limitations and next steps

- Live verification of multi-object tracking in a genuinely simultaneous different-type scene is
  still pending; only the single-object path has been re-confirmed live.
- Line Crossing support is implemented, but its event and capability shapes have not been tested
  against a live controller.
- The classifier is stock YOLOv5s (person/vehicle/animal), not Ubiquiti's second-pass classifier;
  real Ubiquiti weights are not portable to this NPU.

## Scope and legal

This only talks to hardware and a Protect console **you own**, using protocol details
reverse-engineered from **your own** firmware, purely to interoperate with your own UniFi Protect
install. Nothing here targets third-party systems; no Ubiquiti binaries are distributed. Licensed
AGPL-3.0-or-later (see the repository root).
