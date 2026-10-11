# pi-port — a DIY UniFi Protect "AI Port" on an Orange Pi (RK3588)

<div align="center">

<a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0--or--later-blue" alt="License: AGPL-3.0-or-later"></a>
<a href="https://github.com/hutchx86/pi-port/actions/workflows/ci.yml"><img src="https://github.com/hutchx86/pi-port/actions/workflows/ci.yml/badge.svg" alt="CI"></a>

**A reverse-engineering / interoperability proof of concept that makes a stock
Orange Pi 5 Plus (RK3588) present itself to a real UniFi Protect console as
genuine AI Port hardware (catalog model `UVC AI Port`, sysid `0xa5f1`).**
**It speaks the vendor wire protocol only — discovery, adoption, camera pairing,
video relay, NPU object detection and detection events with thumbnails — with no
firmware modification, no controller database writes and no Ubiquiti code or
binaries.**

</div>

> [!WARNING]
> Proof of concept, not production software. It binds privileged ports
> (UDP/10001, TCP/443) and manages macvlan interfaces and DHCP leases;
> misconfiguration can disrupt your network or device. Use it only on hardware
> and consoles you own. See [Disclaimer](#disclaimer).

> [!NOTE]
> Not affiliated with, endorsed by, or connected to Ubiquiti Inc. "UniFi",
> "UniFi Protect" and "AI Port" are trademarks of Ubiquiti Inc. No Ubiquiti
> firmware or binaries are distributed here. Built with AI assistance under
> human review — verify anything you rely on. See [Legal](#legal).

## Quick start

```bash
git clone https://github.com/hutchx86/pi-port && cd pi-port
sudo scripts/install.sh --console <console-ip> [--parent-iface <iface>]
# then adopt "AI Port" in Protect and pair a camera to it
```

## At a glance

|  |  |
| --- | --- |
| **What** | Emulates the UniFi Protect AI Port device role on an RK3588 board |
| **Hardware** | Orange Pi 5 Plus (RK3588 NPU); Raspberry Pi 5 + Coral USB Edge TPU |
| **Protocol / interface** | UBNT discovery UDP/10001; mTLS WSS :7442 (`ucp4` + classic avclient); RTSP relay :7447 |
| **Language** | Python 3 |
| **Status** | Working against a real Protect console — 4 cameras paired and streamed concurrently |
| **License** | AGPL-3.0-or-later |

## How it compares

|  | Real AI Port | Pi Port |
| --- | --- | --- |
| **What it is** | Ubiquiti AI Port appliance (Ambarella) | This repo's Python, on an Orange Pi 5 Plus (RK3588) |
| **Firmware** | Ubiquiti, signed | None — the emulator itself |
| **Detection model** | Ubiquiti's own classifier | Stock COCO detector mapped to person/vehicle/animal: YOLOv5s on the RK3588 NPU, or SSDLite MobileDet on a Coral |
| **Output** | Full Ubiquiti product behaviour | The subset this project implements |

It is a protocol-compatible stand-in built on the same shared camera/chime
middleware, not a clone of the device.

## How it works

The board answers the console's L2 discovery, then adopts like a classic camera
(`POST /api/1.2/manage` on HTTPS :443). It keeps two mTLS WebSocket connections
to the console — the generic `ucp4` channel for management RPCs, and the classic
avclient channel for camera pairing and detection events. When a camera is
paired, the console hands the board an RTSP URL on its own internal relay
(`:7447`); the board pulls that stream, runs YOLOv5 on the NPU, and pushes
`EventSmartDetect` events and snapshot uploads back to the console.

<img src="docs/images/architecture.svg" width="760" alt="Pi Port data path: cameras → Protect console → emulated AI Port on the Orange Pi">

| Component | Language | Role |
| --- | --- | --- |
| `discovery.py` | Python | UDP/10001 UBNT discovery responder |
| `http_api.py` | Python | HTTPS :443 adopt / control / snapshot API |
| `ucp4_client.py` | Python | Generic `ucp4` device-management WebSocket |
| `avclient.py` | Python | Pairing, streaming and smart-detect WebSocket + detection loop |
| `detectors/` | Python | Pluggable YOLOv5 backends: RKNN (NPU), Edge TPU (Coral), CPU |
| `sysinfo_server.py` | Python | Dev-box CPU/mem/GPU/NPU status page (`webui.html`) |
| `instance_manager.py`, `run_all.py` | Python | Multi-instance orchestration |

## Supported hardware

| Model | SoC / variant | Status | Notes |
| --- | --- | --- | --- |
| Orange Pi 5 Plus | RK3588 | Verified | The target board; 4 real cameras paired and streamed concurrently |
| Other RK3588 boards | RK3588 | Untested | Should work; the NPU and ffmpeg paths are RK3588-specific |
| Raspberry Pi 5 + Coral USB | BCM2712 + Edge TPU | Partly verified | Detector verified on Pi OS trixie (MobileDet, ~33 ms/frame with the Coral on USB 2); adopt/pair against a console not yet run. No H.264 hardware decode on the Pi 5, so camera streams decode on the CPU |
| Any Linux box | CPU (`onnxruntime`) | Untested | `--accel cpu`; fine for a camera or two |

The detector is pluggable (`piport/detectors/`, `[hardware] detector` in
`aiport.cfg`): `rknn`, `edgetpu`, `cpu`, or `auto`, which takes the first one
whose runtime, hardware and model are present. A new accelerator is one
`base.Detector` subclass.

## Features

- **Emulated AI Port device role** — L2 discovery, adoption, and both WebSocket
  connections a real console expects.
- **Camera pairing and video relay** — pairs real cameras and pulls their stream
  from Protect's internal RTSP relay.
- **Third-party (ONVIF) cameras** — when one is assigned, pushes its video to
  Protect as `extendedFlv` (live view and recording) and runs detection on it.
- **Real object detection on an accelerator** — YOLOv5 → RKNN on the RK3588
  NPU, or SSDLite MobileDet on a Coral Edge TPU (person/vehicle/animal), not a
  placeholder.
- **Detection events with thumbnails** — `EventSmartDetect` with per-object
  snapshots and Full-FoV images, plus the overview/timeline thumbnails.
- **Multi-instance** — one reboot-surviving systemd unit and macvlan interface
  per instance, each a distinct L2 device, with a small web UI.
- **Honest state handling** — reflects real adopted state and clears local state
  on the console's reset.

## Requirements

- Orange Pi 5 Plus (or another RK3588 board), or a Raspberry Pi 5 with a Coral
  USB Edge TPU, running Linux, with root — the emulator binds UDP/10001 and
  TCP/443.
- Python 3 (3.13 on Pi OS trixie works), plus the backend's runtime, which the
  installer adds: `rknn-toolkit-lite2` (NPU), or `ai-edge-litert` +
  `libedgetpu` (Coral).
- A UniFi Protect console you own and control, on the same LAN.
- For model conversion: an x86_64 Linux host with `rknn-toolkit2` (the RKNN
  converter does not run on the board).

## Repository layout

```
piport/            the emulator (RK3588 / Orange Pi)
  discovery.py       UDP/10001 UBNT discovery responder
  http_api.py        HTTPS :443 adopt/control API
  ucp4_client.py     generic ucp4 device-management websocket
  avclient.py        pairing/streaming/smart-detect websocket + detection
  detectors/         pluggable YOLOv5 backends (rknn / edgetpu / cpu)
  sysinfo_server.py  dev-box CPU/mem/GPU/NPU status page (+ webui.html)
  models/            anchors + COCO labels (model binaries fetched, not stored)
  instance_manager.py / run_all.py  multi-instance orchestration
scripts/           install.sh (one-shot installer), fetch_models.py, helpers
rknn_convert/      YOLOv5s -> RKNN conversion recipe (x86 build host)
docs/images/       README diagram
```

Model binaries (`.onnx`/`.rknn`) and all Ubiquiti firmware are **not** stored
here; see [Model assets](#model-assets-not-tracked). `piport/README.md` has the
protocol detail and per-component notes. The x86/no-NPU Docker variant lives on
the `experimental` branch.

## Install / Usage

`scripts/install.sh` does the whole stand-up on the board: system packages, a
venv + dependencies, model assets, the required `librknnrt.so` upgrade, a
reboot-surviving systemd instance (via `instance_manager.py`), and the web UI.

```bash
sudo scripts/install.sh --console <UNVR-IP> [--parent-iface <iface>]
```

- Run from the checkout root, as root. `--parent-iface` defaults to the
  auto-detected default-route NIC.
- `--accel auto|rknn|edgetpu|cpu|none` picks the detector backend (default
  `auto`: rknn on an RK3588, edgetpu when a Coral is plugged in, else cpu) and
  installs only that backend's runtime and model. For `edgetpu` that is
  feranick's `libedgetpu1-std` build for the distro (sha256-pinned) and
  Google's MobileDet model; a PCIe/M.2 Coral additionally needs the `gasket`
  kernel driver, which the installer does not set up.
- The NPU model cannot be built on the Pi; build it on an x86 host
  (`scripts/fetch_models.py --convert`) or let the installer fetch it from
  `--rknn-url` / `PIPORT_RKNN_URL` / its default URL. `--rknn <path>` takes a
  local build, and a local `models-rknn/` submodule is also detected. Use
  `--accel none` (or `--no-npu`) for a protocol-stack-only install.
- Opens the web UI on `:8090` (`--webui-port` to change) for board stats and
  instance management.

Then, in Protect:

1. Adopt "AI Port" from the console UI like any camera.
2. Pair a real camera to it from the camera's own settings page.
3. Detections appear as dashboard events with thumbnails, and the console's own
   person/vehicle/animal filters work.

`run_all.py` needs root (binds UDP/10001 and TCP/443) and must be able to send L2
broadcast/multicast on the LAN, so run it on the host, not in a NAT'd container.
It resolves its own interface's IP by default, so multiple instances on one box
don't collide on `:443`.

### Manual install

The installer automates the steps below; run them by hand if you prefer:

```bash
# 1. Fetch the model assets into models/.
python3 scripts/fetch_models.py                     # RKNN: ONNX + labels
python3 scripts/fetch_models.py --convert           # optional, x86 build host
python3 scripts/fetch_models.py --backend edgetpu   # Coral: MobileDet .tflite

# 2. Install runtime dependencies (one backend's).
pip install -r piport/requirements.txt
pip install rknn-toolkit-lite2      # RK3588 NPU
pip install ai-edge-litert          # Coral (plus libedgetpu1-std, see install.sh)

# 3. Set device identity, your console IP and [hardware] detector.
$EDITOR piport/aiport.cfg

# 4. Run (needs root).
sudo python3 piport/run_all.py --iface <your-iface>
```

### Single instance (systemd)

A minimal unit for reboot survival:

```ini
[Unit]
Description=UniFi AI Port emulator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/pi-port/piport
ExecStart=/usr/bin/python3 /opt/pi-port/piport/run_all.py --iface <iface>
Restart=on-failure
KillMode=control-group
[Install]
WantedBy=multi-user.target
```

`KillMode=control-group` matters: `run_all.py` spawns four child processes and
only reaps them on `KeyboardInterrupt`, so a plain SIGTERM would orphan them.

### Multiple instances

`instance_manager.py` creates one systemd unit per instance, each with its own
macvlan sub-interface, MAC, and DHCP lease — a distinct L2 device, so the console
sees independent AI Ports:

```bash
sudo python3 piport/instance_manager.py create lab2 --parent-iface <iface>
python3 piport/instance_manager.py list
sudo python3 piport/instance_manager.py destroy lab2
```

Identity (MAC/device-id) is generated once at creation and baked into the unit,
so it is stable across reboots. `create --ip X.Y.Z.W` uses a static address
instead of DHCP. The web UI (`sysinfo_server.py`) can also create/destroy
instances.

### Docker (console on the same host)

When the Protect console itself runs on the board (e.g. the
`dciancu/unifi-protect-unvr-docker-arm64` container), `install.sh` cannot work:
the console already holds `:443`, and a macvlan interface cannot talk to its
own host. `docker/` runs the emulator in a container on a private bridge
network instead (Coral or CPU detector):

```bash
cd docker && echo "AIPORT_CONSOLE=<console-ip>" > .env && docker compose up -d --build
```

See [`docker/README.md`](docker/README.md).

### ONVIF cameras

Assigning a third-party (ONVIF) camera to the AI Port hands that camera's video
ingest to the AI Port: Protect sends the camera's RTSP URL (`OnvifStreamControl`)
and asks for each channel to be pushed to `tcp://<console>:7550` in Ubiquiti's
`extendedFlv` format (`ChangeVideoSettings`). `piport/onvif_push.py` does that
with one `ffmpeg -c:v copy` per channel (no re-encode), and the AI Port runs
detection on the camera's stream like on a UniFi camera's.

Verified on a Raspberry Pi 5 (Docker, Coral) with a Hikvision 4K camera and
Protect 7.2: live view in the app and browser, recording, and smart-detect
events.

Limits:

- **If the AI Port stops, the camera stops recording.** Protect no longer pulls
  it; unassign the camera (or remove the AI Port) to hand ingest back.
- Every channel carries the camera's main stream (no lower-resolution
  channels); the resolution reported to Protect is probed from the stream.
- Audio is AAC 32 kHz mono (silent if the camera has none). Protect asks for
  Opus, which plain FLV cannot carry.
- Each channel and the detector open their own RTSP session to the camera; some
  cameras limit concurrent logins (Hikvision answers `401` past its limit).
- On a Pi 5 the camera's H.264 is decoded in software for detection (~half a
  core for 4K at 12 fps).

The older x86 Docker variant (`piport/x86/`) lives on the `experimental`
branch.

### Model assets (not tracked)

Each backend needs an open model. Because the binaries are large and
reproducible, they are not committed; `scripts/fetch_models.py` downloads them
(`--backend rknn|edgetpu|cpu`), sha256-pinned where a single file:

| Asset | Used by | Source |
| --- | --- | --- |
| `ssdlite_mobiledet_coco_qat_postprocess_edgetpu.tflite` (5.1 MiB) | `edgetpu` backend | Google Coral test data (Apache-2.0), the model Frigate ships: <https://github.com/google-coral/test_data> (`release-frogfish`) |
| `yolov5n.onnx` (3.8 MiB) | `cpu` backend | Ultralytics YOLOv5 `v7.0` release (AGPL-3.0) |
| `yolov5s_relu.onnx` (27.6 MiB) | RKNN conversion (`.rknn`) | Rockchip model zoo delivery: <https://ftrg.zbox.filez.com/v2/delivery/data/95f00b0fc900458ba134f8b180b3f7a1/examples/yolov5/yolov5s_relu.onnx> |
| anchors, COCO labels, bus.jpg, calibration subset | RKNN conversion | <https://github.com/airockchip/rknn_model_zoo> (tag `v2.3.2`) |

The `cpu` backend also accepts the Rockchip `yolov5s_relu.onnx`, and the
`edgetpu` backend an Ultralytics YOLOv5 Edge TPU export, via `[hardware]
detector_model` / `AIPORT_MODEL_PATH`; output layouts are auto-detected.
`piport/models/coco_90_labels.txt` (the MobileDet class ids) is Google's
`coco_labels.txt`, Apache-2.0.

`yolov5s_relu.rknn` is produced on an x86_64 machine with `rknn-toolkit2`
(install from PyPI or the Rockchip GitHub release — see `rknn_convert/convert.py`
and `piport/README.md`):

```bash
python3 scripts/fetch_models.py --convert
```

`scripts/install.sh` resolves the prebuilt `.rknn` in this order: `--rknn
<path>`, a local copy at `models-rknn/` (e.g. a git submodule of a separate
model repo) or `rknn_convert/`, then a download from `--rknn-url` /
`PIPORT_RKNN_URL` / its `DEFAULT_RKNN_URL`. It verifies the pinned sha256 where
one applies (always for the default URL). The default source is the separate
AGPL-3.0 model repo
[`hutchx86/pi-port-models`](https://github.com/hutchx86/pi-port-models) (tag
`v1`); the model is never committed to this repo.

**Model license.** The model derives from Ultralytics YOLOv5 (AGPL-3.0) via
Rockchip's model zoo (Apache-2.0), so any redistributed `.rknn` is an AGPL-3.0
work, the same license as this project. If you redistribute it, include the
AGPL-3.0 text and attribution; the corresponding source is this repo's
conversion recipe plus the ONNX in the table above. Check those licenses
yourself before redistributing.

## Configuration

Device identity, network mode and the console address live in
`piport/aiport.cfg`; CLI flags on each script override it.

| Key | Default | Meaning |
| --- | --- | --- |
| `[identity] mac` | derived | Device MAC (12 hex chars). Blank derives one from the interface's OUI |
| `[identity] platform` | `UVC AI Port` | Catalog model string the console matches (must be exact) |
| `[identity] sysid` | `0xa5f1` | Hardware/model id |
| `[identity] device_id` | per-instance | Persistent device UUID (must be unique per instance) |
| `[network] iface` / `mode` / `ip` | auto / `dhcp` / — | Interface to bind, and `dhcp` or `static` addressing |
| `[console] host` / `port` | — / `7442` | The Protect console to dial when not yet adopted |
| `[hardware] detector` | `auto` | Detection backend: `auto`, `rknn`, `edgetpu`, `cpu` (env `AIPORT_DETECTOR` overrides) |
| `[hardware] detector_model` | — | Model file for that backend; blank = its default in `models/` (env `AIPORT_MODEL_PATH`) |

## Verification

- Unit suite (no hardware required):

  ```bash
  cd piport && python3 -m unittest discover -s tests
  ```

- On-device: run the stack on the Orange Pi and watch it discover, adopt and pair
  (see `piport/README.md` and `scripts/live_watch.py`).

## Roadmap / known limitations

- **Known:** detection is a stock COCO model (person/vehicle/animal), not
  Ubiquiti's classifier; face recognition, license-plate recognition and
  vehicle classification are not implemented.
- **Known:** an assigned ONVIF camera depends on the AI Port for its video
  (no recording while the AI Port is down), gets the main stream on every
  channel, and AAC instead of Opus audio.
- **Known:** in the Docker setup the AI Port is only reachable from its own
  host, so it only suits a console running on that host.
- **Planned:** stress-testing multiple cameras beyond the four-camera live setup.

## Changelog

- **Unreleased** — pluggable detector backends (`piport/detectors/`): RK3588
  NPU, Coral Edge TPU (Raspberry Pi 5) and CPU, chosen by `[hardware] detector`
  / `install.sh --accel`; a Docker setup for a console on the same host;
  credentials masked in logs; third-party (ONVIF) cameras (video push to
  Protect plus detection); real stream resolution reported to Protect; `leave`
  events for open tracks are sent when a camera is stopped.
- **Unreleased** — the console's firmware-update push (`UpdateFirmwareRequest`)
  is answered by pinning the target version and faking the reboot instead of
  flashing; every interface reports the pinned version.
- **2026-09** — Line Crossing; paired-camera snapshot retrieval over UCP4; the
  `connectionHost` upload fix; event/feature-flag dedupe; multi-instance systemd
  units and the web UI; one-shot `install.sh`.
- **2026-08** — initial protocol stack: discovery, adoption, both WebSocket
  connections, NPU detection and `EventSmartDetect` with thumbnails.

## Troubleshooting

<details>
<summary><b>Troubleshooting / operational notes</b></summary>

**A partial restart leaves stale connections.** Restart the full four-process
stack (discovery, adopt HTTP, ucp4, avclient) — a partial restart leaves
untouched connections in stale state. `run_all.py` starts all four together.

**`pkill -f 'run_all.py'` kills itself.** The pattern matches the killing command
line. Use the bracket trick and also kill orphaned ffmpeg children:

```bash
pkill -9 -f '[r]un_all.py'
pkill -9 -f '[f]fmpeg.*rtsp://<console-ip>:7447'
```

**Starting detached over SSH hangs.** A backgrounded `nohup` can hold the SSH
channel open; use `setsid -f <command> >log 2>&1 </dev/null`.

**Multiple instances collide on `:443`.** Bind each instance to its own
interface's real IP (the default) rather than `0.0.0.0` — a wildcard bind claims
the port on every address on the box.

</details>

## Credits

Special thanks to
[dciancu/unifi-protect-unvr-docker-arm64](https://github.com/dciancu/unifi-protect-unvr-docker-arm64)
— the inspiration for getting into UniFi tinkering in general, and whose methods
helped in learning how parts of it work.

Protocol work was informed by the open-source UniFi community; the detection
model and conversion recipe come from Rockchip's model zoo. Third-party
components and their licenses are listed in [CREDITS.md](CREDITS.md).

<details>
<summary><b>Legal</b></summary>

- **Not affiliated with, or endorsed by, Ubiquiti Inc.** "UniFi", "UniFi
  Protect" and "AI Port" are trademarks of Ubiquiti Inc.
- **No Ubiquiti firmware or binaries are distributed here.** You supply your own
  console, cameras and (for reverse engineering) firmware images.
- Intended for interoperability and personal use on hardware and consoles you
  own. Reverse-engineering may be restricted in your jurisdiction, and you are
  responsible for how you use this. Nothing here targets third-party systems.

</details>

<details>
<summary><b>Disclaimer</b></summary>

**This is a proof-of-concept project, not a production-ready system.** Large
parts were produced with LLMs under human supervision,
review, and real-hardware testing — review and verify everything yourself.
**This software is provided "as is", without warranty of any kind.** It binds
privileged ports, manipulates network interfaces (macvlan/DHCP), and runs
detection hardware; misconfiguration can disrupt your network or device. By using
it you accept full responsibility for any damage, data loss, or other
consequences. **The authors and contributors are not responsible or liable for
any loss or damage arising from its use.**

</details>

## Security

Report vulnerabilities privately via GitHub Security Advisories. Runtime state —
the adoption token and the mTLS client certificate/key — is written to the
instance state directory (`--state-dir`) and excluded by `.gitignore`; never
commit it. No secrets are stored in this repository.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE). Because this is a network service,
anyone who runs a modified version for others to interact with over a network
must offer them the corresponding source.
