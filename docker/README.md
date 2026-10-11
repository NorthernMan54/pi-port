# Pi Port in Docker

Runs one emulated AI Port in a container on a private Docker bridge network.
Use this when the **UniFi Protect console runs on the same host**, e.g.
[dciancu/unifi-protect-unvr-docker-arm64](https://github.com/dciancu/unifi-protect-unvr-docker-arm64)
with `network_mode: host`. For a console on another machine, use
`scripts/install.sh` (macvlan, a real LAN device) instead.

Verified on a Raspberry Pi 5 (Pi OS trixie) with a USB Coral, next to the
UNVR container: discovery, adoption, restart, a UniFi camera via Protect's
relay, and a Hikvision 4K ONVIF camera (video push, live view, recording,
detection) all work.

> [!NOTE]
> An assigned ONVIF camera gets its video *from* the AI Port (see the main
> README's ONVIF section): while this container is down, that camera does not
> record.

## Why a bridge network

| Network | Works with a console on the same host? |
| --- | --- |
| `host` | No: the console's nginx already holds `:443`, which adoption needs |
| `macvlan` | No: Linux blocks traffic between a host and its own macvlan interfaces |
| **bridge** (this setup) | Yes: the container has its own `:443`, and host <-> container traffic is routed without NAT, so the console sees the AI Port at its container IP |

The trade-off: the AI Port lives on the Docker subnet (default
`172.30.10.0/24`), so only that host can reach it. The console's L2 discovery
still finds it across the bridge.

## Run

```bash
cd docker
echo "AIPORT_CONSOLE=<console-ip>" > .env     # e.g. the host's own LAN IP
docker compose up -d --build
docker compose logs -f
```

Then adopt "AI Port" in Protect and pair a UniFi camera to it.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AIPORT_CONSOLE` | required | Console `host[:port]` (port defaults to 7442) |
| `AIPORT_ACCEL` | `edgetpu` | Detector baked into the image: `edgetpu`, `cpu` or `none` |
| `AIPORT_IP` / `AIPORT_SUBNET` | `172.30.10.10` / `172.30.10.0/24` | Container address; keep it fixed, the console stores it |
| `AIPORT_HOSTNAME` | `piport` | Advertised hostname |
| `AIPORT_DEBUG` | `false` | Debug logging |

The RK3588 NPU backend is not offered in the image; on an Orange Pi use
`scripts/install.sh`.

## State and identity

Everything persistent is in the `aiport-data` volume (`/data`):

- `identity.json`: the device_id and Ubiquiti-OUI MAC, generated on first
  start. Docker gives each container start a new random interface MAC, so the
  MAC is pinned here instead of being derived from the interface.
- `aiport.cfg`: seeded from the image template; `AIPORT_CONSOLE` /
  `AIPORT_HOSTNAME` are written into it on every start. Edit other keys with
  `docker compose exec aiport vi /data/aiport.cfg` (or mount your own).
- `adopt_state.json`, `server.crt`/`server.key`: adoption survives restarts and
  image rebuilds.

`docker compose down -v` deletes the volume, i.e. the device identity. Remove
the AI Port in Protect first.

## Coral

`/dev/bus/usb` is bind-mounted with a cgroup rule for USB devices (major 189)
rather than passing a single `--device`: the USB Coral reconnects with a new
device number once libedgetpu loads its firmware. The image pins the same
`libedgetpu1-std` build (sha256-checked) as `install.sh`. If the detector logs
`Failed to load delegate`, unplug and replug the Coral.

The board web UI (`sysinfo_server.py`, `:8090`) is not part of the container.

## Logs

Credentials in logged payloads (camera RTSP passwords, the console's login
password for the device, adoption tokens) are masked as `<redacted>`.
