# syntax=docker/dockerfile:1
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
#
# CPU AI Port emulator image (onnxruntime) for linux/amd64 + linux/arm64 (Pi 4/5).
# Build with the repo root as context: docker build -f docker/Dockerfile -t piport-cpu .

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AIPORT_BACKEND=onnx

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

# CPU YOLOv5n ONNX (gitignored; URL+sha256 mirror scripts/fetch_models.py).
# --checksum fails the build on a corrupt or substituted download.
ADD --checksum=sha256:04f0e55c26f58d17145b36045780fe1250d5bd2187543e11568e5141d05b3262 \
    https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5n.onnx \
    models/yolov5n.onnx
RUN test -f models/yolov5n.onnx \
 || { echo >&2 "models/yolov5n.onnx missing after ADD"; exit 1; }

EXPOSE 10001/udp 443/tcp 8090/tcp

# Stays root (binds :443/:10001). SIGINT so run_all.py reaps its four children.
STOPSIGNAL SIGINT
ENTRYPOINT ["python3", "run_all.py"]
