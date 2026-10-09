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

Confirmed (this repo, x86_64):
- `docker build` succeeds; the sha-pinned model downloads and verifies.
- Host-network smoke test: all four components come up, discovery answers real console
  probes on the LAN, and the control API binds `:443`.
- `onnxruntime` inference on a still image: 3 person + 1 vehicle on the stock `bus.jpg`.
- The arm64 dependency set resolves (base image is multi-arch; `onnxruntime` 1.31.0
  publishes `cp311` `manylinux_2_28_aarch64` wheels; `ffmpeg`/`iproute2` are standard
  Debian arm64 packages).

Untested:
- A full arm64 image build and run on real Pi 4 / Pi 5 hardware (this host has no arm64
  emulation, so the emulated build's `RUN` steps cannot execute -- the base image and
  wheels resolve, but the actual Pi run is unverified).
- Real cameras / real RTSP frames through the live capture pipeline.
- macvlan networking end to end (the config validates; no live LAN test).
- CPU contention between `onnxruntime` and software `ffmpeg` decode under a 4-camera
  load. The RK3588 variant does not have this: decode and inference run on separate
  hardware blocks (RKVDEC + NPU).
