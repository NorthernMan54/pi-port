# Pi Port in Docker

One image serves every Docker mode: `linux/amd64` and `linux/arm64` (Pi 4/5,
x86_64) with a detector backend baked in at build time:

| `ACCEL` | Detector | Notes |
| --- | --- | --- |
| `cpu` (default) | `onnxruntime` (YOLOv5n) | No accelerator; works on any x86_64/arm64 host |
| `edgetpu` | Coral USB/PCIe (`ai-edge-litert` + `libedgetpu`) | SSDLite MobileDet, sha256-pinned; USB Coral verified on a Pi 5 |
| `none` | detection off | Protocol stack only |

The RK3588 NPU backend is not offered in the image — use `scripts/install.sh`
on that board.

There are two deployment shapes, for two different console locations:

| Console location | Use | Network |
| --- | --- | --- |
| Another machine on the LAN | repo-root `compose.yaml` (or `compose.macvlan.yaml`) | host networking or macvlan |
| **The same host as Docker** | `docker/docker-compose.yml` | private bridge, reachable only from that host |

## Quick start: console elsewhere on the LAN

```bash
# from the repo root
docker build -f docker/Dockerfile -t piport-cpu .
docker run --rm --network host --cap-add NET_ADMIN \
    -e AIPORT_CONSOLE_HOST=<console-ip> piport-cpu
```

Or the Compose stack:

```bash
AIPORT_CONSOLE_HOST=<console-ip> docker compose up -d --build
```

For a dedicated LAN IP per container (or several instances on one host), use
`compose.macvlan.yaml` instead — see [Networking](#networking).

Then adopt "AI Port" in Protect and pair a camera to it, exactly as on a board.

`run-smoke-test.sh` builds the CPU image (`ACCEL=cpu`), starts it on host
networking, and checks all four components are alive — no console or camera
required:

```bash
./docker/run-smoke-test.sh
```

It broadcasts discovery on the host LAN and binds `:443`/`:10001`, so run it only
on a box and network you control.

## Quick start: console on the same host

If the Protect console runs on this host (for example
[dciancu/unifi-protect-unvr-docker-arm64](https://github.com/dciancu/unifi-protect-unvr-docker-arm64)
with `network_mode: host`), host networking clashes on `:443`, and a macvlan
interface cannot talk to its own host. Use the bridge stack instead:

```bash
cd docker
echo "AIPORT_CONSOLE=<console-ip>" > .env     # e.g. the host's own LAN IP
docker compose up -d --build
docker compose logs -f
```

Then adopt "AI Port" in Protect and pair a UniFi camera to it. Verified on a
Raspberry Pi 5 (Pi OS trixie) with a USB Coral, next to the UNVR container:
discovery, adoption, restart, a UniFi camera via Protect's relay, and a
Hikvision 4K ONVIF camera (video push, live view, recording, detection).

| Variable | Default | Meaning |
| --- | --- | --- |
| `AIPORT_CONSOLE` | required | Console `host[:port]` (port defaults to 7442) |
| `AIPORT_ACCEL` | `edgetpu` | Detector baked into the image: `edgetpu`, `cpu` or `none` |
| `AIPORT_IP` / `AIPORT_SUBNET` | `172.30.10.10` / `172.30.10.0/24` | Container address; keep it fixed, the console stores it |
| `AIPORT_HOSTNAME` | `piport` | Advertised hostname |
| `AIPORT_DEBUG` | `false` | Debug logging |

The trade-off: the AI Port lives on the Docker subnet, so only that host can
reach it. A console on another machine cannot — use `scripts/install.sh` or the
LAN Compose stack there.

## Status web UI (optional)

The repo-root `compose.yaml` includes a `webui` service that runs
`sysinfo_server.py` on `:8090` (`http://<host>:8090`): CPU per-core, memory,
GPU/NPU when present, and the Coral's USB link speed. It has **no auth** — LAN
use only. The instance create/destroy buttons are `instance_manager` (board)
features; ignore them off-board. Remove the `webui` service if you do not want
it. The bridge stack does not include the web UI.

## Importing into Komodo

The repo-root `compose.yaml` is the Komodo Stack file (Komodo clones the repo and
runs compose from the clone root, which is exactly the build context). Create a
**Stack** with source **Git Repo** = `hutchx86/pi-port`, branch `main`, file
`compose.yaml` (or `compose.macvlan.yaml` for a dedicated IP per container), then
set the stack environment and deploy:

| Variable | Meaning |
| --- | --- |
| `AIPORT_CONSOLE_HOST` | The Protect console IP to dial before adoption |
| `AIPORT_CONSOLE_PORT` | Console port (default `7442`) |
| `AIPORT_MAC` | Optional fixed MAC; blank derives a Ubiquiti-OUI MAC |
| `AIPORT_DEVICE_ID` | Device identity (one instance per host; see Networking) |
| `AIPORT_HOSTNAME` / `AIPORT_NAME` | DHCP hostname / Protect device name |
| `AIPORT_IFACE` | Pin an interface; blank auto-detects the default-route NIC |

Set `extra_args = "--build"` (Komodo stack config) so each deploy rebuilds the
image after a code change — otherwise compose reuses the existing `piport-cpu`
image. Komodo builds the image from the repo, so the stack needs no pre-built
image and no manual model fetch — the detector model is downloaded during build.

For the full identity/network surface (static IP, parent interface, secret-free
identity), mount a cfg instead — copy `piport/aiport.cfg` and add:

```yaml
    volumes:
      - ./aiport.cfg:/app/piport/aiport.cfg:ro
```

Every `config.ENV_OVERRIDES` key can be set as an environment variable; env wins
over the mounted file.

## Networking (LAN stack)

The L2 discovery beacon (`UDP/10001`) must be broadcast-visible on the console's
LAN, and the console must reach the container's `:443` at a real, stable LAN IP.
Docker's default bridge breaks both (it source-NATs traffic and serves DNS
through a stub at `127.0.0.11`), so the LAN stack has two supported modes:

**Host networking — `compose.yaml` (simplest).** The container shares the host's
IP, DNS and interfaces and is **not** a distinct LAN device. The host must be on
the same L2 LAN as the console, and the host's `:443`/`UDP-10001` must be free (a
Traefik/Caddy/nginx on `:443` will clash). One instance per host — a second
container would collide.

**macvlan — `compose.macvlan.yaml` (dedicated IP per container).** Each container
gets its own LAN IP **and** MAC, its own `:443`, and can coexist with the host
and with other instances. This is the Docker equivalent of the board's
`instance_manager.py` macvlan, and the way to run several AI Ports on one host.
Requirements and caveats:

- Docker's macvlan driver has **no DHCP** — assign a static, free
  `ipv4_address` (and a matching `AIPORT_MAC`/`mac_address`); the file shows the
  pattern.
- The **parent NIC must be wired**; Wi-Fi APs drop the extra MAC.
- The **host cannot reach the container IP** without an extra macvlan shim
  interface on the host (the console on the LAN can).
- Give every instance a unique IP, MAC and `AIPORT_DEVICE_ID`, or the console
  sees one device instead of several.

Both modes require the target host to be on the console's LAN. Choose host mode
for a quick single-instance test, macvlan when the host already uses `:443` or
you want more than one instance.

## Model

The detector model is gitignored, so the Dockerfile downloads it during build
(`scripts/fetch_models.py --backend <ACCEL>`) and verifies the pinned sha256. No
pre-fetch step is needed for a container build or a Komodo deploy. A local
`python3 scripts/fetch_models.py --backend cpu` also places `yolov5n.onnx` in
`piport/models/` for non-container runs.

## Multi-arch build

```bash
docker buildx build \
    --platform linux/amd64,linux/arm64 \
    --build-arg ACCEL=cpu \
    -f docker/Dockerfile -t <registry>/piport-cpu:latest --push .
```

On a Pi, a plain `docker build` is native arm64. Cross-building arm64 elsewhere
needs QEMU/binfmt (`docker run --privileged --rm tonistiigi/binfmt --install arm64`).
For Coral, use `--build-arg ACCEL=edgetpu` (the libedgetpu package is
sha256-pinned per arch).

## State and identity

The bridge stack keeps everything persistent in the `aiport-data` volume
(`/data`):

- `identity.json`: the device_id and Ubiquiti-OUI MAC, generated on first start.
  Docker gives each container start a new random interface MAC, so the MAC is
  pinned here instead of being derived from the interface.
- `aiport.cfg`: seeded from the image template; `AIPORT_CONSOLE` /
  `AIPORT_HOSTNAME` are written into it on every start. Edit other keys with
  `docker compose exec aiport vi /data/aiport.cfg` (or mount your own).
- `adopt_state.json`, `server.crt`/`server.key`: adoption survives restarts and
  image rebuilds.

`docker compose down -v` deletes the volume, i.e. the device identity. Remove the
AI Port in Protect first. The LAN stack has no entrypoint wrapper: set
`AIPORT_DEVICE_ID` / `AIPORT_MAC` explicitly if you need stable identity across
container recreation.

## Coral

`/dev/bus/usb` is bind-mounted with a cgroup rule for USB devices (major 189)
rather than passing a single `--device`: the USB Coral reconnects with a new
device number once libedgetpu loads its firmware. The image pins the same
`libedgetpu1-std` build (sha256-checked) as `install.sh`. If the detector logs
`Failed to load delegate`, unplug and replug the Coral.

Only the USB Coral is handled. A PCIe/M.2 Coral additionally needs the
`gasket`/`apex` kernel driver. A PCIe Coral shows up as `/dev/apex_0` and is
detected by the backend/web UI, but the driver is not installed by this image.

## Confirmed vs. untested

Confirmed on x86_64:
- `docker build` succeeds (CPU); the sha-pinned model downloads and verifies.
- Host-network smoke test: all four components come up, discovery answers real
  console probes on the LAN, and the control API binds `:443`.
- `onnxruntime` inference on a still image: 3 person + 1 vehicle on `bus.jpg`.

Confirmed on a real Raspberry Pi 4 Model B (Debian 13, aarch64, 2026-10-11),
container on host networking:
- `docker build` succeeds on arm64 (~9 min cold; `onnxruntime` 1.31.0 aarch64).
- Host-network smoke test PASS: all four components, `:443` bound, and discovery
  answered real console probes on the LAN.
- In-container inference: 3 person + 1 vehicle on `bus.jpg`, ~368 ms/frame.
- **Live end to end**: adopted by a real UNVR (controller 7.3.70), the console's
  firmware push pinned to 5.1.16 (fake update), four cameras paired, RTSP pulls
  running, the onnxruntime detector emitting a real `EventSmartDetect`
  (`person`, `edgeType: enter`) and a snapshot upload.
- Status web UI (`sysinfo_server.py` on `:8090`) serves on the Pi (GPU/NPU blank).

Confirmed on a Raspberry Pi 5 (Pi OS trixie) with a USB Coral, bridge stack next
to the UNVR container:
- Discovery, adoption, restart, a UniFi camera via Protect's relay.
- A Hikvision 4K ONVIF camera: video push, live view (app and browser),
  recording, MobileDet detection (~33 ms/frame on USB 2).
- `install.sh` with a Coral is not tested against a console on this board.

Untested:
- macvlan networking end to end (the config validates; no live LAN test).
- Sustained 4-camera CPU headroom: with four software-decoded 2304x1296 RTSP
  pulls the Pi 4 ran at load ~3.75 on 4 cores. It works, but the Cortex-A72 has
  little margin; the RK3588 board avoids this (decode and inference on separate
  hardware blocks).
- The known RTSP restart loop (`todo.md` #5) still occurs (~`Failed reading RTSP
  data: End of file` then auto-restart) — pre-existing, not container-specific.

## Raspberry Pi hardware decode (not in the container yet)

The Pi 4/5 exposes an H.264/HEVC decoder via V4L2 M2M (`bcm2835-codec`,
`/dev/video10`, driven through `/dev/vchiq`); `avclient.py` selects it
automatically on bare metal (`AIPORT_HWDEC=v4l2m2m`), which cuts ffmpeg's decode
CPU ~4-5x. Two caveats we found on the Pi 4:

- **In a container it stalls.** Passing `/dev/video10` + `/dev/vchiq` lets the
  *decoder* open (a file decodes), but the live RTSP + filter-graph pipeline then
  produces no frames (ffmpeg sits at ~0 CPU, no snapshots) — the container lacks
  further VPU plumbing. Treat container hardware decode as unsupported for now;
  bare metal works.
- **Decode isn't the whole cost.** With hardware decode on 3 cameras, ffmpeg
  still used ~1 core (driver + scale/mjpeg) and the CPU detector ~2.2 cores, so
  the Pi 4 stayed saturated. Hardware decode is necessary for headroom but not
  sufficient; the detector is the remaining bottleneck.

The Pi 5 has no H.264 hardware decoder at all (HEVC only); its CPU
software-decodes H.264, and the Coral handles detection.

## A gotcha worth knowing

`.dockerignore` patterns without a `/` match only the context **root**, not
subdirectories: `*.key` does not exclude `piport/server.key`. A stale
`server.crt`/`server.key` left in `piport/` by a bare-metal run was therefore
baked into the image and made every container fail `ssl load_cert_chain`
(`[SSL] PEM lib`). The ignore rules use `**/` so runtime state under `piport/`
can never ship.
