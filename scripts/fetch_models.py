#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Fetch the model assets a detector backend needs (see piport/detectors/).

  rknn     the open Rockchip YOLOv5s model (ONNX + anchors + COCO labels +
           calibration subset) for the .rknn conversion workspace
  edgetpu  Google's SSDLite MobileDet COCO Edge TPU model (the one Frigate ships)
  cpu      the official Ultralytics YOLOv5n ONNX

Model binaries are not stored in this repo (see README).

  python3 scripts/fetch_models.py                      # rknn assets (default)
  python3 scripts/fetch_models.py --backend edgetpu    # Coral model
  python3 scripts/fetch_models.py --convert            # also build the .rknn (needs rknn-toolkit2, x86)
"""
import argparse
import hashlib
import os
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZOO = "https://raw.githubusercontent.com/airockchip/rknn_model_zoo/v2.3.2"
ONNX_URL = ("https://ftrg.zbox.filez.com/v2/delivery/data/"
            "95f00b0fc900458ba134f8b180b3f7a1/examples/yolov5/yolov5s_relu.onnx")
UA = {"User-Agent": "piport-fetch-models"}

# Single-file models per backend: dest name -> (url, sha256). Pinned digests;
# a mismatch deletes the download and aborts.
BACKEND_MODELS = {
    "edgetpu": {
        # Google Coral test_data (Apache-2.0), 320x320 uint8, in-graph NMS.
        "ssdlite_mobiledet_coco_qat_postprocess_edgetpu.tflite": (
            "https://github.com/google-coral/test_data/raw/release-frogfish/"
            "ssdlite_mobiledet_coco_qat_postprocess_edgetpu.tflite",
            "4a2be2bbb614e576d56dcb914fa52752bf0f13411512710d355d97faa1b35641"),
    },
    "cpu": {
        # Ultralytics release (AGPL-3.0), decode baked in -> one [1,N,85] output.
        "yolov5n.onnx": (
            "https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5n.onnx",
            "04f0e55c26f58d17145b36045780fe1250d5bd2187543e11568e5141d05b3262"),
    },
}

# target dirs, keyed by short name
DIRS = {
    "models": os.path.join(ROOT, "piport", "models"),
    "rknn_convert": os.path.join(ROOT, "rknn_convert"),
}

# remote -> short names of dirs it goes into
ASSETS = {
    f"{ZOO}/examples/yolov5/model/anchors_yolov5.txt": ["models", "rknn_convert"],
    f"{ZOO}/examples/yolov5/model/coco_80_labels_list.txt": ["models", "rknn_convert"],
    f"{ZOO}/examples/yolov5/model/bus.jpg": ["rknn_convert"],
    f"{ZOO}/datasets/COCO/coco_subset_20.txt": ["rknn_convert"],
}


def download(url, dest, force=False):
    if os.path.exists(dest) and not force:
        print(f"skip (exists) {dest}")
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    print(f"got {dest}")


def verify_sha256(path, expected):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    got = digest.hexdigest()
    if got != expected:
        os.remove(path)
        sys.exit(f"error: {path} sha256 {got} != expected {expected} (removed)")
    print(f"verified {path}")


def fetch_backend_model(backend, force=False):
    for name, (url, sha256) in BACKEND_MODELS[backend].items():
        dest = os.path.join(DIRS["models"], name)
        download(url, dest, force)
        verify_sha256(dest, sha256)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--convert", action="store_true",
                    help="run rknn_convert/convert.py to build yolov5s_relu.rknn")
    ap.add_argument("--backend", choices=["rknn", *BACKEND_MODELS], default="rknn",
                    help="which detector backend's assets to fetch (default: rknn)")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    args = ap.parse_args()
    if args.convert and args.backend != "rknn":
        ap.error("--convert only applies to --backend rknn")

    if args.backend != "rknn":
        fetch_backend_model(args.backend, args.force)
        return

    for url, targets in ASSETS.items():
        name = url.rsplit("/", 1)[1]
        for t in targets:
            download(url, os.path.join(DIRS[t], name), args.force)

    # Rockchip yolov5s_relu ONNX feeds the .rknn conversion workspace only.
    download(ONNX_URL, os.path.join(DIRS["rknn_convert"], "yolov5s_relu.onnx"), args.force)

    subset_list = os.path.join(DIRS["rknn_convert"], "coco_subset_20.txt")
    with open(subset_list) as f:
        for line in f:
            name = os.path.basename(line.strip())
            if not name:
                continue
            download(f"{ZOO}/datasets/COCO/subset/{name}",
                     os.path.join(DIRS["rknn_convert"], "subset", name), args.force)

    if not args.convert:
        return
    try:
        import rknn  # noqa: F401
    except ImportError:
        sys.exit("rknn-toolkit2 not installed (x86_64 build host); skipping conversion")
    out = os.path.join(DIRS["models"], "yolov5s_relu.rknn")
    subprocess.run([sys.executable, "convert.py", "yolov5s_relu.onnx", "rk3588",
                    os.path.relpath(out, DIRS["rknn_convert"])],
                   cwd=DIRS["rknn_convert"], check=True)
    print(f"built {out}")


if __name__ == "__main__":
    main()
