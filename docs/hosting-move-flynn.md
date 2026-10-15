# Hosting move: AI Port from Saul (M1 Pro) to Flynn (M2)

This moves the running pi-port AI Port from **Saul** (Apple M1 Pro) to **Flynn** (Apple M2) as
the **same device**: Protect keeps the adopted "AI Port", its paired cameras and settings. Only
the host changes.

## What moves

| Item | Value / where | How it moves |
| --- | --- | --- |
| Advertised MAC | `FCECDAD4C3CD` (derived from Saul's `en0`) | recorded in the bundle, **pinned with `--mac`** on Flynn |
| Device ID | `fe6488e7-7042-5bcb-ab86-6f0ad1a5baed` (`piport/aiport.cfg` default) | same repo file, also passed as `--device-id` |
| Adoption + mTLS certificate | `~/.piport-state` (`adopt_state.json`, `server.crt`, `server.key`, owned by root) | in the bundle |
| Protect face DB copy | `~/.piport-state/facedb/` | in the bundle (Protect re-sends it on connect anyway) |
| Models (not in git) | `piport/models/yolov5s.onnx`, `yolov5n.onnx`, YuNet/SFace face models | in the bundle |
| Plate models | `~/.cache` (fast-alpr) | downloaded again on the first plate read |
| IP address | Saul `192.168.1.227` | Flynn's own IP; reported to Protect on connect |

The bundle holds the device's private key and adoption token: copy it securely and delete it
once Flynn is running.

## Settings in use

| Setting | Value | Why |
| --- | --- | --- |
| Console | `AIPORT_CONSOLE_HOST=192.168.1.1` | the Protect console (Cottage GW) |
| Detector model | `AIPORT_MODEL_PATH=…/piport/models/yolov5s.onnx` | YOLOv5s on Core ML (~8 ms/frame on M1 Pro): better on small/distant objects than YOLOv5n |
| Box delay | `AIPORT_BOX_DELAY_MS=3000` | back-dates boxes to their frame time; measured 2.2-3.1 s on the Driveway camera |
| Tick rate | default 0.5 s on macOS (`AIPORT_TICK_S`) | passing cars get several checks |
| Sleep | `caffeinate -is` | a sleeping Mac drops off the console |

## Steps

### On Saul

1. Stop pi-port (Ctrl-C in its terminal). The export refuses to run while it is up: two hosts
   with one identity fight over the adoption.
2. Export:

   ```bash
   cd /Users/sgracey/Code/pi-port && sudo scripts/macos_move_host.sh export ~/piport-move.tgz
   ```

   It prints the identity (`PIPORT_MAC=FCECDAD4C3CD`, the device ID).
3. Copy `~/piport-move.tgz` to Flynn (AirDrop or `scp`).

### On Flynn

4. Install ffmpeg and pi-port (`macos-detectors-v2`):

   ```bash
   brew install ffmpeg
   git clone -b macos-detectors-v2 https://github.com/NorthernMan54/pi-port.git ~/Code/pi-port
   cd ~/Code/pi-port && python3 -m venv venv
   venv/bin/pip install -r piport/requirements.txt onnxruntime fast-alpr opencv-python-headless
   ```

5. Import (restores `~/.piport-state` and `piport/models/`, prints the start command):

   ```bash
   cd ~/Code/pi-port && sudo scripts/macos_move_host.sh import ~/piport-move.tgz
   ```

6. Check the interface (`route -n get default`, usually `en0`), then start:

   ```bash
   cd ~/Code/pi-port && sudo AIPORT_CONSOLE_HOST=192.168.1.1 AIPORT_BOX_DELAY_MS=3000 \
     AIPORT_MODEL_PATH=$HOME/Code/pi-port/piport/models/yolov5s.onnx \
     caffeinate -is venv/bin/python piport/run_all.py --iface en0 --state-dir ~/.piport-state \
     --mac FCECDAD4C3CD --device-id fe6488e7-7042-5bcb-ab86-6f0ad1a5baed
   ```

7. Delete `~/piport-move.tgz` on both Macs.

## Checking it worked

In Flynn's terminal output, within about 15 s:

- `WSS CONNECTED` and `CLASSIC AVCLIENT WSS CONNECTED`, with **no** repeating
  `connection ended ... reconnecting` (that loop means the identity didn't match: check `--mac`).
- `Detector initialized (backend=onnx, provider=CoreMLExecutionProvider, model=.../yolov5s.onnx)`.
- `starting merged RTSP pull` for the paired cameras: doorbell `8CEDE172C456`, Driveway
  `AABBCC3344CC`, Backyard `F4E2C60D4B4C`.
- `vehicle already present at start, parked` for the parked cars (no events opened for them).

In Protect, the AI Port should show online at Flynn's IP with its cameras still paired.

## If something goes wrong

- **Protect offers the AI Port for adoption, or the connection loops:** the identity didn't
  carry over. Stop Flynn, confirm `--mac FCECDAD4C3CD` and `--device-id` are on the command and
  that `~/.piport-state/adopt_state.json` exists. Adopting again in Protect also works (cameras
  then need re-pairing).
- **Going back to Saul:** stop Flynn first, then start Saul with its usual command; its
  `~/.piport-state` is unchanged.

## Not covered

- Running Saul and Flynn as **two** AI Ports at once: that needs a second identity (fresh
  `--device-id`, the MAC derived from Flynn's interface, a new adoption), not this move.
- Keeping pi-port running across reboots (a launchd job) on Flynn.
