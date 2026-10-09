# syntax=docker/dockerfile:1
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
#
# Pi Port -- CPU AI Port emulator image (onnxruntime), one build for linux/amd64
# (x86_64 servers) and linux/arm64 (Raspberry Pi 4/5, any CPU-only box). The
# detector selects the onnxruntime backend at runtime; the RK3588 NPU path is
# built on the board by scripts/install.sh instead, not here.
#
# Build with the repo root as context (the CPU model is gitignored and is
# downloaded here, sha256-pinned -- no pre-fetch needed):
#   docker build -f docker/Dockerfile -t piport-cpu .
#   docker buildx build --platform linux/amd64,linux/arm64 -f docker/Dockerfile -t piport-cpu .
#
# Run needs host (or macvlan) networking and root: see docker/README.md.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AIPORT_BACKEND=onnx

# ffmpeg: software H.264/H.265 decode (no Rockchip rkmpp here). iproute2:
# config.resolve_ip shells out to `ip`. ca-certificates: mTLS to the console.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg \
        iproute2 \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY piport/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt onnxruntime

COPY piport/ ./
# Drop test/build cruft that must not ship in the runtime image.
RUN rm -rf tests __pycache__

# CPU YOLOv5n ONNX (gitignored). URL + sha256 mirror scripts/fetch_models.py
# (YOLOV5N_URL / YOLOV5N_SHA256); keep the two in sync. ADD --checksum makes a
# corrupt or substituted download fail the build.
ADD --checksum=sha256:04f0e55c26f58d17145b36045780fe1250d5bd2187543e11568e5141d05b3262 \
    https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5n.onnx \
    models/yolov5n.onnx
RUN test -f models/yolov5n.onnx \
 || { echo >&2 "models/yolov5n.onnx missing after ADD"; exit 1; }

# :10001/udp L2 discovery, :443/tcp adopt/control. :8090/tcp is the optional
# dev web UI (sysinfo_server.py) if you add it to the entrypoint.
EXPOSE 10001/udp 443/tcp 8090/tcp

# run_all.py binds :443/:10001, so the container must stay root (no USER).
# SIGINT (not the default SIGTERM) so run_all.py's KeyboardInterrupt handler
# stops its four children instead of orphaning them.
STOPSIGNAL SIGINT
ENTRYPOINT ["python3", "run_all.py"]
