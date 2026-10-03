#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
#
# One-shot installer for the Pi Port AI-Port emulator on an Orange Pi 5 Plus
# (RK3588 / aarch64). Does, in order:
#
#   1. apt prerequisites (python3-venv/pip, ffmpeg, iproute2, isc-dhcp-client,
#      curl, ca-certificates, diffutils, git)
#   2. a venv at piport/.venv + piport/requirements.txt (+ rknn-toolkit-lite2)
#   3. the model assets (scripts/fetch_models.py) and the NPU .rknn model
#   4. the system librknnrt.so upgrade the model needs (backed up, idempotent)
#   5. a macvlan systemd instance via instance_manager.py (survives reboot)
#   6. a systemd unit for the web UI (sysinfo_server.py) and starts it
#
# Run as root (it installs packages, writes /etc/systemd/system and creates a
# network interface):
#
#   sudo scripts/install.sh --console <UNVR-IP> [--parent-iface <iface>]
#
# Options:
#   --name NAME            instance name (default: main)
#   --parent-iface IFACE   physical NIC the macvlan instance is built on
#                          (alias: --iface; default: auto-detected)
#   --console HOST[:PORT]  UniFi Protect console address; written to aiport.cfg
#   --rknn PATH            prebuilt yolov5s_relu.rknn to install (see below)
#   --rknn-url URL         download the .rknn from URL (overrides PIPORT_RKNN_URL)
#   --rknn-sha256 HEX      verify a downloaded/supplied .rknn against this sha256
#   --webui-port N         web UI listen port (default: 8090)
#   --no-npu               skip rknn-toolkit-lite2, librknnrt upgrade and the
#                          .rknn requirement (protocol stack still installs)
#   -h, --help
#
# The NPU model is deliberately not shipped in the repo (binary). It can come
# from, in order: --rknn PATH, a local/submodule copy (models-rknn/ or
# rknn_convert/), --rknn-url URL, PIPORT_RKNN_URL, or the DEFAULT_RKNN_URL set
# below (published as a separate AGPL-3.0 model repo/release -- see README.md).
# rknn-toolkit2 can only convert ONNX -> RKNN on an x86_64 host, so a copy built
# elsewhere (fetch_models.py --convert) is the alternative.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIPORT_DIR="$REPO_ROOT/piport"
VENV="$PIPORT_DIR/.venv"
PY="$VENV/bin/python"
CFG="$PIPORT_DIR/aiport.cfg"
RKNN_DEST="$PIPORT_DIR/models/yolov5s_relu.rknn"
SYSTEMD_DIR="/etc/systemd/system"
RKNNRT_URL="https://raw.githubusercontent.com/airockchip/rknn-toolkit2/v2.3.2/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so"

# Known-good NPU model (RKNN 2.3.2/rk3588, 3 sigmoid heads); not committed --
# it lives in the separate AGPL-3.0 model repo. Empty URL disables the download.
DEFAULT_RKNN_URL="https://raw.githubusercontent.com/hutchx86/pi-port-models/v1/yolov5s_relu.rknn"
DEFAULT_RKNN_SHA256="f4fd0122ecca3117323b8268d270e6cc293e364d6a9dda8e83293d64c8377a3c"

NAME="main"
PARENT_IFACE=""
CONSOLE=""
RKNN=""
RKNN_URL=""
RKNN_SHA256=""
WEBUI_PORT="8090"
NO_NPU=0

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

verify_sha256() {  # verify_sha256 <file> <expected-hex>
    local got expected
    got="$(sha256sum "$1" | awk '{print $1}')"
    expected="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')"
    [ "$got" = "$expected" ]
}

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }

ORIG_ARGS=("$@")
while [ $# -gt 0 ]; do
    case "$1" in
        --name)         NAME="${2:?}"; shift 2 ;;
        --parent-iface|--iface) PARENT_IFACE="${2:?}"; shift 2 ;;
        --console)      CONSOLE="${2:?}"; shift 2 ;;
        --rknn)         RKNN="${2:?}"; shift 2 ;;
        --rknn-url)     RKNN_URL="${2:?}"; shift 2 ;;
        --rknn-sha256)  RKNN_SHA256="${2:?}"; shift 2 ;;
        --webui-port)   WEBUI_PORT="${2:?}"; shift 2 ;;
        --no-npu)       NO_NPU=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "unknown argument: $1 (see --help)" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "run as root: sudo $0 ${ORIG_ARGS[*]}"
[ -f "$PIPORT_DIR/run_all.py" ] || die "piport/ not found under $REPO_ROOT -- run this from the repo"
case "$NAME" in *[!a-zA-Z0-9_-]*|"") die "invalid --name '$NAME' (letters/digits/-/_ only, max 12)" ;; esac
[ "${#NAME}" -le 12 ] || die "--name must be <= 12 characters (becomes a Linux interface name)"

ARCH="$(uname -m)"
if [ "$ARCH" != "aarch64" ] && [ "$NO_NPU" -eq 0 ]; then
    die "this installer targets aarch64/RK3588, got $ARCH. For the x86/no-NPU Docker variant see the 'experimental' branch; to force a CPU-only install here, pass --no-npu"
fi

log "installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3-venv python3-pip ffmpeg isc-dhcp-client iproute2 curl ca-certificates diffutils git

if [ ! -x "$PY" ]; then
    log "creating venv at $VENV"
    python3 -m venv "$VENV"
fi
log "installing Python dependencies"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$PIPORT_DIR/requirements.txt"
if [ "$NO_NPU" -eq 0 ]; then
    log "installing rknn-toolkit-lite2"
    "$PY" -m pip install --quiet rknn-toolkit-lite2
fi

log "fetching model assets (ONNX + labels/anchors; binaries are gitignored)"
"$PY" "$REPO_ROOT/scripts/fetch_models.py"

if [ "$NO_NPU" -eq 0 ]; then
    # Source precedence: --rknn file > local/submodule copy > download, where
    # the download URL is --rknn-url > PIPORT_RKNN_URL > DEFAULT_RKNN_URL.
    rknn_url="${RKNN_URL:-${PIPORT_RKNN_URL:-$DEFAULT_RKNN_URL}}"
    verified=0

    # (a) explicit local build
    if [ -n "$RKNN" ]; then
        [ -f "$RKNN" ] || die "--rknn '$RKNN' does not exist"
        install -m 644 "$RKNN" "$RKNN_DEST"
        log "installed NPU model from $RKNN"
    fi

    # (b) local/submodule copy: models-rknn/ (linked model repo) or a hand-built
    # rknn_convert/ copy. Only if nothing is installed yet.
    if [ ! -f "$RKNN_DEST" ]; then
        if [ -f "$REPO_ROOT/.gitmodules" ] && command -v git >/dev/null 2>&1; then
            log "initializing model submodule(s)"
            git -c "safe.directory=$REPO_ROOT" -C "$REPO_ROOT" \
                submodule update --init --recursive >/dev/null 2>&1 \
                || warn "git submodule update failed; falling back to URL/existing model"
        fi
        for cand in "$REPO_ROOT/models-rknn/yolov5s_relu.rknn" \
                    "$REPO_ROOT/rknn_convert/yolov5s_relu.rknn"; do
            if [ -f "$cand" ]; then
                install -m 644 "$cand" "$RKNN_DEST"
                log "using local NPU model $cand"
                break
            fi
        done
    fi

    # An explicit --rknn-sha256 must match the supplied model; drop a rejected file
    # so a later re-run doesn't mistake it for a good install.
    if [ -n "$RKNN_SHA256" ] && [ -f "$RKNN_DEST" ]; then
        if ! verify_sha256 "$RKNN_DEST" "$RKNN_SHA256"; then
            rm -f "$RKNN_DEST"
            die "NPU model sha256 mismatch against --rknn-sha256"
        fi
        log "NPU model sha256 verified"
        verified=1
    fi

    # (c) download. Atomic: fetch to a temp file, verify, then install -- so an
    # interrupted/killed download can never leave a partial model in place.
    if [ ! -f "$RKNN_DEST" ] && [ -n "$rknn_url" ]; then
        log "downloading NPU model from $rknn_url"
        tmp="$(mktemp)"
        if ! curl -fL -o "$tmp" "$rknn_url"; then
            rm -f "$tmp"
            if [ "$rknn_url" = "$DEFAULT_RKNN_URL" ]; then
                warn "default NPU model download failed (offline, or URL not published yet)"
            else
                die "failed to download NPU model from $rknn_url"
            fi
        else
            # The published digest is the expectation for the default URL.
            dl_sha="$RKNN_SHA256"
            if [ -z "$dl_sha" ] && [ -n "$rknn_url" ] && [ "$rknn_url" = "$DEFAULT_RKNN_URL" ]; then
                dl_sha="$DEFAULT_RKNN_SHA256"
            fi
            if [ -n "$dl_sha" ] && ! verify_sha256 "$tmp" "$dl_sha"; then
                rm -f "$tmp"
                die "downloaded NPU model failed sha256 verification ($rknn_url)"
            fi
            if [ -n "$dl_sha" ]; then
                verified=1
            fi
            install -m 644 "$tmp" "$RKNN_DEST"
            rm -f "$tmp"
            log "installed downloaded NPU model"
        fi
    fi

    if [ ! -f "$RKNN_DEST" ]; then
        cat >&2 <<'EOF'
ERROR: piport/models/yolov5s_relu.rknn is missing.

The .rknn cannot be built on the Pi -- rknn-toolkit2 (the converter) is
x86_64-only. On an x86_64 Linux box:

    python3 scripts/fetch_models.py --convert

then copy piport/models/yolov5s_relu.rknn to this Pi, or re-run with
  --rknn <path>   /   --rknn-url <url>   /   PIPORT_RKNN_URL=<url> ...

To install the protocol stack now and add NPU detection later, re-run with
  --no-npu
EOF
        exit 1
    fi
    if [ "$verified" -eq 0 ]; then
        log "NPU model installed without a pinned digest; pass --rknn-sha256 to verify"
    fi
fi

# librknnrt.so must match rknn-toolkit2 2.3.2; the stock OS version is too old.
if [ "$NO_NPU" -eq 0 ]; then
    lib="$(ldconfig -p 2>/dev/null | awk '/librknnrt\.so/{print $NF; exit}' || true)"
    lib="${lib:-/usr/lib/librknnrt.so}"
    tmp="$(mktemp)"
    log "fetching librknnrt.so (v2.3.2 runtime)"
    if ! curl -fL -o "$tmp" "$RKNNRT_URL"; then
        rm -f "$tmp"
        die "failed to download librknnrt.so from $RKNNRT_URL (offline?)"
    fi
    if [ -f "$lib" ] && cmp -s "$tmp" "$lib"; then
        log "system librknnrt.so already current ($lib)"
    else
        backup="/root/librknnrt.so.orig-backup"
        if [ -f "$lib" ] && [ ! -f "$backup" ]; then
            cp -a "$lib" "$backup"
            log "backed up original librknnrt.so -> $backup"
        fi
        install -m 755 "$tmp" "$lib"
        ldconfig
        log "installed librknnrt.so -> $lib"
    fi
    rm -f "$tmp"
fi

set_ini() {  # set_ini <section> <key> <value> <file>
    local section="$1" key="$2" value="$3" file="$4" tmp
    tmp="$(mktemp)"
    awk -v sec="$section" -v k="$key" -v v="$value" '
        /^\[/ { insec = ($0 == "[" sec "]") }
        insec && $0 ~ ("^" k "[ \t]*=") { print k " = " v; next }
        { print }
    ' "$file" > "$tmp"
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

if [ -n "$CONSOLE" ] || [ -n "$PARENT_IFACE" ]; then
    [ -f "$CFG.bak-preinstall" ] || cp -a "$CFG" "$CFG.bak-preinstall"
fi
if [ -n "$CONSOLE" ]; then
    console_host="${CONSOLE%%:*}"
    console_port="7442"
    if [ "$CONSOLE" != "$console_host" ]; then
        console_port="${CONSOLE##*:}"
    fi
    set_ini console host "$console_host" "$CFG"
    set_ini console port "$console_port" "$CFG"
    log "set [console] host=$console_host port=$console_port in aiport.cfg"
fi
if [ -n "$PARENT_IFACE" ]; then
    set_ini network parent_iface "$PARENT_IFACE" "$CFG"
    log "set [network] parent_iface=$PARENT_IFACE in aiport.cfg"
fi

cfg_host="$("$PY" -c "import sys; sys.path.insert(0, '$PIPORT_DIR'); import config; print(config.load_config()['host'])")"
if [ -z "$cfg_host" ]; then
    die "no console host configured; pass --console <UNVR-IP> or set [console] host in $CFG"
fi

INSTANCE_UNIT="$SYSTEMD_DIR/aiport-instance-${NAME}.service"
INSTANCE_IFACE="ap-${NAME}"
INSTANCE_IFACE="${INSTANCE_IFACE:0:15}"
if [ -f "$INSTANCE_UNIT" ]; then
    log "instance '$NAME' already exists; refreshing unit + restarting"
    "$PY" "$PIPORT_DIR/instance_manager.py" refresh "$NAME"
else
    log "creating macvlan instance '$NAME' (systemd unit: aiport-instance-${NAME}.service)"
    im_args=(create "$NAME")
    if [ -n "$PARENT_IFACE" ]; then
        im_args+=(--parent-iface "$PARENT_IFACE")
    fi
    "$PY" "$PIPORT_DIR/instance_manager.py" "${im_args[@]}"
fi

# dhclient exits 0 on timeout, so poll for a real IP instead of trusting
# systemctl "active"; a restart recreates the macvlan before the lease appears.
inst_ip=""
for _ in $(seq 1 20); do
    inst_ip="$(ip -4 -o addr show dev "$INSTANCE_IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1 || true)"
    if [ -n "$inst_ip" ]; then
        break
    fi
    sleep 0.5
done

log "writing web UI unit (piport-webui.service), port $WEBUI_PORT"
cat > "$SYSTEMD_DIR/piport-webui.service" <<EOF
[Unit]
Description=Pi Port web UI (sysinfo + instance manager)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PIPORT_DIR
ExecStart=$PY $PIPORT_DIR/sysinfo_server.py --bind-ip 0.0.0.0 --port $WEBUI_PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now piport-webui.service
systemctl restart piport-webui.service

box_ip="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1 || true)"
sleep 1
if curl -fsS "http://127.0.0.1:${WEBUI_PORT}/api/stats" >/dev/null 2>&1; then
    webui_state="up"
else
    webui_state="NOT responding (check: journalctl -u piport-webui -n 50)"
fi

if [ -n "$inst_ip" ]; then
    instance_state="active, $inst_ip"
else
    instance_state="running but NO IP on $INSTANCE_IFACE (DHCP failed? journalctl -u aiport-instance-${NAME})"
fi

cat <<EOF

============================================================
Pi Port installed.

  console        : $cfg_host
  instance unit  : aiport-instance-${NAME}.service  ($instance_state)
  web UI         : http://${box_ip:-<box-ip>}:${WEBUI_PORT}   [$webui_state]
  logs           : journalctl -u aiport-instance-${NAME} -f
                   journalctl -u piport-webui -f

Next: adopt "AI Port" from the Protect console UI, then pair a camera.
Re-run safely any time to reinstall/update; the instance identity is preserved.
Note: the distro ffmpeg (software decode) is installed; swap in a Rockchip
rkmpp build for hardware H.264 decode if your board has one.
============================================================
EOF
