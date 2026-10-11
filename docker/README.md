# Pi Port -- CPU Docker image (x86_64 + arm64)

Runs the full AI Port emulator on a generic CPU, no NPU:

| Platform | Notes |
| --- | --- |
| `linux/amd64` | x86_64 server / desktop |
| `linux/arm64` | Raspberry Pi 4 (Cortex-A72), Pi 5 (Cortex-A76), any arm64 server |

One image serves all three: the only difference from the RK3588/Orange Pi build is
the detector backend. `detector.py` selects the `onnxruntime` CPU backend at runtime
(`AIPORT_BACKEND=onnx`) instead of `rknnlite` on the RK3588 NPU. Everything else --
discovery, adoption, both WebSockets, pairing, motion/tracking, snapshot upload -- is
the same code as `piport/`. The RK3588 board still uses `scripts/install.sh`, not this.

Video decode uses software `ffmpeg` (already the fallback in `avclient.py` when the
Rockchip `ffmpeg-rkmpp` binary is absent). The model is the 3.8 MiB Ultralytics
`yolov5n.onnx` (~25 fps on an i7-12800H against the ~8 inferences/s a 4-camera setup
needs).

## Quick start

```bash
# from the repo root
docker build -f docker/Dockerfile -t piport-cpu .
docker run --rm --network host --cap-add NET_ADMIN \
    -e AIPORT_CONSOLE_HOST=<console-ip> piport-cpu
```

Or with the Compose stack at the **repo root** (`compose.yaml`, host networking):

```bash
AIPORT_CONSOLE_HOST=<console-ip> docker compose up -d --build
```

For a dedicated LAN IP per container (or several instances on one host), use
`compose.macvlan.yaml` instead -- see [Networking](#networking).

Then adopt "AI Port" in Protect and pair a camera to it, exactly as on the Pi.

`run-smoke-test.sh` builds and starts the stack on host networking, then checks all
four components are alive -- no console or camera required:

```bash
./docker/run-smoke-test.sh
```

It broadcasts discovery on the host LAN and binds `:443`/`:10001`, so run it only on a
box and network you control.

## Status web UI (optional)

`compose.yaml` includes a `webui` service that runs `sysinfo_server.py` on `:8090`
(`http://<host>:8090`): CPU per-core + memory. It has **no auth** -- LAN use only. The
GPU/NPU panels read RK3588-only sysfs paths and stay blank on x86/Pi, and the
instance create/destroy buttons are `instance_manager` (board) features, so ignore them
off-board. Remove the `webui` service from the compose file if you do not want it.

## Importing into Komodo

The repo-root `compose.yaml` is the Komodo Stack file (Komodo clones the repo and runs
compose from the clone root, which is exactly the build context). Create a **Stack**
with source **Git Repo** = `hutchx86/pi-port`, branch `main`, file `compose.yaml`
(or `compose.macvlan.yaml` for a dedicated IP per container), then
set the stack environment and deploy:

| Variable | Meaning |
| --- | --- |
| `AIPORT_CONSOLE_HOST` | The Protect console IP to dial before adoption |
| `AIPORT_CONSOLE_PORT` | Console port (default `7442`) |
| `AIPORT_MAC` | Optional fixed MAC; blank derives a Ubiquiti-OUI MAC |
| `AIPORT_DEVICE_ID` | Device identity (one instance per host; see Networking) |
| `AIPORT_HOSTNAME` / `AIPORT_NAME` | DHCP hostname / Protect device name |
| `AIPORT_IFACE` | Pin an interface; blank auto-detects the default-route NIC |

Set `extra_args = "--build"` (Komodo stack config) so each deploy rebuilds the image
after a code change -- otherwise compose reuses the existing `piport-cpu` image.

Komodo builds the image from the repo (context = repo root), so the stack needs no
pre-built image and no manual model fetch -- the CPU model is downloaded during build
(see below). For the full identity/network surface (static IP, parent interface,
secret-free identity), mount a cfg instead -- copy `piport/aiport.cfg` and add:

```yaml
    volumes:
      - ./aiport.cfg:/app/aiport.cfg:ro
```

Every `config.ENV_OVERRIDES` key can be set as an environment variable; env wins over
the mounted file.

## Networking

The L2 discovery beacon (`UDP/10001`) must be broadcast-visible on the console's LAN,
and the console must reach the container's `:443` at a real, stable LAN IP. Docker's
default bridge breaks both (it source-NATs traffic and serves DNS through a stub at
`127.0.0.11`), so there are two supported modes:

**Host networking -- `compose.yaml` (simplest).** The container shares the host's IP,
DNS and interfaces and is **not** a distinct LAN device. The host must be on the same
L2 LAN as the console, and the host's `:443`/`UDP-10001` must be free (a Traefik/Caddy/
nginx on `:443` will clash). One instance per host -- a second container would collide.

**macvlan -- `compose.macvlan.yaml` (dedicated IP per container).** Each container gets
its own LAN IP **and** MAC, its own `:443`, and can coexist with the host and with other
instances. This is the Docker equivalent of the board's `instance_manager.py` macvlan,
and the way to run several AI Ports on one host. Requirements and caveats:

- Docker's macvlan driver has **no DHCP** -- assign a static, free `ipv4_address` (and a
  matching `AIPORT_MAC`/`mac_address`); the file shows the pattern.
- The **parent NIC must be wired**; Wi-Fi APs drop the extra MAC.
- The **host cannot reach the container IP** without an extra macvlan shim interface on
  the host (the console on the LAN can).
- Give every instance a unique IP, MAC and `AIPORT_DEVICE_ID`, or the console sees one
  device instead of several.

Both modes require the target host to be on the console's LAN. Choose host mode for a
quick single-instance test, macvlan when the host already uses `:443` or you want more
than one instance.

## Model

The CPU model is gitignored, so the Dockerfile downloads it during build and verifies
it with `ADD --checksum=sha256:...` (URL and sha256 mirror `scripts/fetch_models.py`;
keep them in sync). No pre-fetch step is needed for a container build or a Komodo
deploy. A local `python3 scripts/fetch_models.py` also places `yolov5n.onnx` in
`piport/models/` for non-container runs.

## Multi-arch build

```bash
docker buildx build \
    --platform linux/amd64,linux/arm64 \
    -f docker/Dockerfile -t <registry>/piport-cpu:latest --push .
```

On a Pi, a plain `docker build` is native arm64. Cross-building arm64 elsewhere needs
QEMU/binfmt (`docker run --privileged --rm tonistiigi/binfmt --install arm64`).

## Confirmed vs. untested

Confirmed on x86_64:
- `docker build` succeeds; the sha-pinned model downloads and verifies.
- Host-network smoke test: all four components come up, discovery answers real console
  probes on the LAN, and the control API binds `:443`.
- `onnxruntime` inference on a still image: 3 person + 1 vehicle on the stock `bus.jpg`.

Confirmed on a real Raspberry Pi 4 Model B (Debian 13, aarch64, 2026-10-11), container on
host networking:
- `docker build` succeeds on arm64 (~9 min cold; `onnxruntime` 1.31.0 aarch64).
- Host-network smoke test PASS: all four components, `:443` bound, and discovery answered
  real console probes (`10.10.10.5`/`.61`/`10.10.10.1`) on the LAN.
- In-container inference: 3 person + 1 vehicle on `bus.jpg`, ~368 ms/frame.
- **Live end to end**: adopted by a real UNVR (controller 7.3.70), the console's firmware
  push pinned to 5.1.16 (fake update), four cameras paired, RTSP pulls running, the
  onnxruntime detector emitting a real `EventSmartDetect` (`person`, `edgeType: enter`) and
  a snapshot upload.
- Status web UI (`sysinfo_server.py` on `:8090`) serves on the Pi (GPU/NPU blank).

Untested:
- macvlan networking end to end (the config validates; no live LAN test).
- Pi 5.
- Sustained 4-camera CPU headroom: with four software-decoded 2304x1296 RTSP pulls the
  Pi 4 ran at load ~3.75 on 4 cores. It works, but the Cortex-A72 has little margin; the
  RK3588 board avoids this (decode and inference on separate hardware blocks).
- The known RTSP restart loop (`todo.md` #5) still occurs (~`Failed reading RTSP data:
  End of file` then auto-restart) -- pre-existing, not container-specific.

## Raspberry Pi hardware decode (not in the container yet)

The Pi 4/5 exposes an H.264/HEVC decoder via V4L2 M2M (`bcm2835-codec`, `/dev/video10`,
driven through `/dev/vchiq`); `avclient.py` selects it automatically on bare metal
(`AIPORT_HWDEC=v4l2m2m`), which cuts ffmpeg's decode CPU ~4-5x. Two caveats we found on
the Pi 4:

- **In a container it stalls.** Passing `/dev/video10` + `/dev/vchiq` lets the *decoder*
  open (a file decodes), but the live RTSP + filter-graph pipeline then produces no
  frames (ffmpeg sits at ~0 CPU, no snapshots) -- the container lacks further VPU
  plumbing. Treat container hardware decode as unsupported for now; bare metal works.
- **Decode isn't the whole cost.** With hardware decode on 3 cameras, ffmpeg still used
  ~1 core (driver + scale/mjpeg) and the CPU detector ~2.2 cores, so the Pi 4 stayed
  saturated. Hardware decode is necessary for headroom but not sufficient; the detector
  is the remaining bottleneck.

## A gotcha worth knowing

`.dockerignore` patterns without a `/` match only the context **root**, not
subdirectories: `*.key` does not exclude `piport/server.key`. A stale `server.crt`/
`server.key` left in `piport/` by a bare-metal run was therefore baked into the image and
made every container fail `ssl load_cert_chain` (`[SSL] PEM lib`). The ignore rules use
`**/` so runtime state under `piport/` can never ship.
