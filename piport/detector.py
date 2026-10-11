# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""YOLOv5 object detection with a runtime-selected backend.

Two backends, same ``Detector`` interface and identical output, so callers
(``avclient.py``) need no changes:

  * ``rknn`` -- RK3588 NPU via ``rknn-toolkit-lite2``, the INT8
    ``yolov5s_relu.rknn`` built from Rockchip's rknn_model_zoo v2.3.2
    (box decode, filtering and NMS adapted from that repo's yolov5 script).
  * ``onnx`` -- generic CPU via ``onnxruntime`` (x86_64 and arm64: Pi 4/5 and
    any CPU-only server), no NPU. Two ONNX layouts are auto-detected:
    Ultralytics YOLOv5 (default ``models/yolov5n.onnx``, decode/sigmoid baked
    into the graph) and Rockchip ``yolov5s_relu.onnx`` (three raw heads).

Backend choice (``AIPORT_BACKEND`` = ``auto``/``rknn``/``onnx``): in ``auto``
the NPU is used when rknnlite and its model are present, otherwise the CPU
ONNX backend. ``AIPORT_MODEL_PATH`` overrides the model file (``.rknn`` selects
the NPU, ``.onnx`` the CPU).
"""
import importlib.util
import logging
import os
import threading

import numpy as np
try:
    from rknnlite.api import RKNNLite
except ImportError:  # allow importing this module for unit tests off-device
    RKNNLite = None

log = logging.getLogger("aiport-detector")

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(HERE, "models")
RKNN_MODEL_PATH = os.path.join(MODELS_DIR, "yolov5s_relu.rknn")
ONNX_MODEL_PATH = os.path.join(MODELS_DIR, "yolov5n.onnx")
LABELS_PATH = os.path.join(MODELS_DIR, "coco_80_labels_list.txt")
ANCHORS_PATH = os.path.join(MODELS_DIR, "anchors_yolov5.txt")

IMG_SIZE = (640, 640)  # (width, height) -- matches the model's fixed input
# Objectness x class gate; raised from 0.35 to suppress night false "person" hits
# (0.36-0.48). Override with AIPORT_OBJ_THRESH.
OBJ_THRESH = float(os.environ.get("AIPORT_OBJ_THRESH", "0.5"))
NMS_THRESH = 0.45

# AI Port's objectType vocabulary is just person/vehicle/animal; map YOLO's
# 80 COCO classes down onto those and drop the rest.
_VEHICLE_CLASSES = {"bicycle", "car", "motorbike", "aeroplane", "bus", "train", "truck", "boat"}
_ANIMAL_CLASSES = {"bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"}


def _map_object_type(coco_label):
    label = coco_label.strip()
    if label == "person":
        return "person"
    if label in _VEHICLE_CLASSES:
        return "vehicle"
    if label in _ANIMAL_CLASSES:
        return "animal"
    return None


def _load_anchors():
    with open(ANCHORS_PATH) as f:
        values = [float(x.strip()) for x in f if x.strip()]
    # 9 (x,y) pairs in 3 head-scales of 3 anchors, matching the model's
    # 80x80/40x40/20x20 output heads.
    pairs = list(zip(values[0::2], values[1::2]))
    return [pairs[0:3], pairs[3:6], pairs[6:9]]


def _load_labels():
    with open(LABELS_PATH) as f:
        return [line.strip() for line in f if line.strip()]


# --- Rockchip raw-head layout ([1,255,H,W] x3) -----------------------------
# Used by the rknn backend and by the onnx backend on the Rockchip ONNX.

def _box_process(position, anchors):
    grid_h, grid_w = position.shape[2:4]
    col, row = np.meshgrid(np.arange(0, grid_w), np.arange(0, grid_h))
    col = col.reshape(1, 1, grid_h, grid_w)
    row = row.reshape(1, 1, grid_h, grid_w)
    grid = np.concatenate((col, row), axis=1)
    stride = np.array([IMG_SIZE[1] // grid_h, IMG_SIZE[0] // grid_w]).reshape(1, 2, 1, 1)

    col = col.repeat(len(anchors), axis=0)
    row = row.repeat(len(anchors), axis=0)
    anchors = np.array(anchors).reshape(len(anchors), 2, 1, 1)

    box_xy = position[:, :2, :, :] * 2 - 0.5
    box_wh = pow(position[:, 2:4, :, :] * 2, 2) * anchors

    box_xy += grid
    box_xy *= stride
    box = np.concatenate((box_xy, box_wh), axis=1)

    xyxy = np.copy(box)
    xyxy[:, 0, :, :] = box[:, 0, :, :] - box[:, 2, :, :] / 2
    xyxy[:, 1, :, :] = box[:, 1, :, :] - box[:, 3, :, :] / 2
    xyxy[:, 2, :, :] = box[:, 0, :, :] + box[:, 2, :, :] / 2
    xyxy[:, 3, :, :] = box[:, 1, :, :] + box[:, 3, :, :] / 2
    return xyxy


def _filter_boxes(boxes, box_confidences, box_class_probs):
    box_confidences = box_confidences.reshape(-1)
    class_max_score = np.max(box_class_probs, axis=-1)
    classes = np.argmax(box_class_probs, axis=-1)
    pos = np.where(class_max_score * box_confidences >= OBJ_THRESH)
    scores = (class_max_score * box_confidences)[pos]
    return boxes[pos], classes[pos], scores


def _nms_boxes(boxes, scores):
    x = boxes[:, 0]
    y = boxes[:, 1]
    w = boxes[:, 2] - boxes[:, 0]
    h = boxes[:, 3] - boxes[:, 1]
    areas = w * h
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x[i], x[order[1:]])
        yy1 = np.maximum(y[i], y[order[1:]])
        xx2 = np.minimum(x[i] + w[i], x[order[1:]] + w[order[1:]])
        yy2 = np.minimum(y[i] + h[i], y[order[1:]] + h[order[1:]])
        w1 = np.maximum(0.0, xx2 - xx1 + 0.00001)
        h1 = np.maximum(0.0, yy2 - yy1 + 0.00001)
        inter = w1 * h1
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        inds = np.where(ovr <= NMS_THRESH)[0]
        order = order[inds + 1]
    return np.array(keep)


def _nms_per_class(boxes, classes, scores):
    nboxes, nclasses, nscores = [], [], []
    for c in set(classes.tolist()):
        idx = np.where(classes == c)
        b, s = boxes[idx], scores[idx]
        keep = _nms_boxes(b, s)
        if len(keep):
            nboxes.append(b[keep])
            nclasses.append(np.full(len(keep), c))
            nscores.append(s[keep])
    if not nboxes:
        return None, None, None
    return np.concatenate(nboxes), np.concatenate(nclasses), np.concatenate(nscores)


def _post_process(outputs, anchors):
    boxes, scores, classes_conf = [], [], []
    reshaped = [o.reshape([len(anchors[0]), -1] + list(o.shape[-2:])) for o in outputs]
    for i, data in enumerate(reshaped):
        boxes.append(_box_process(data[:, :4, :, :], anchors[i]))
        scores.append(data[:, 4:5, :, :])
        classes_conf.append(data[:, 5:, :, :])

    def flatten(x):
        ch = x.shape[1]
        return x.transpose(0, 2, 3, 1).reshape(-1, ch)

    boxes = np.concatenate([flatten(b) for b in boxes])
    classes_conf = np.concatenate([flatten(c) for c in classes_conf])
    scores = np.concatenate([flatten(s) for s in scores])

    boxes, classes, scores = _filter_boxes(boxes, scores, classes_conf)
    if boxes.shape[0] == 0:
        return None, None, None
    return _nms_per_class(boxes, classes, scores)


# --- Ultralytics layout ([1,N,5+num_classes], decode baked in) -------------

def _xywh2xyxy(xywh):
    xyxy = np.copy(xywh)
    xyxy[:, 0] = xywh[:, 0] - xywh[:, 2] / 2
    xyxy[:, 1] = xywh[:, 1] - xywh[:, 3] / 2
    xyxy[:, 2] = xywh[:, 0] + xywh[:, 2] / 2
    xyxy[:, 3] = xywh[:, 1] + xywh[:, 3] / 2
    return xyxy


def _post_process_ultralytics(output):
    pred = output[0]  # (N, 5+num_classes): xywh(px), obj, class scores
    obj = pred[:, 4]
    class_conf = pred[:, 5:]
    classes = np.argmax(class_conf, axis=1)
    scores = obj * class_conf[np.arange(class_conf.shape[0]), classes]
    keep = scores >= OBJ_THRESH
    if not np.any(keep):
        return None, None, None
    boxes = _xywh2xyxy(pred[keep, :4])
    return _nms_per_class(boxes, classes[keep], scores[keep])


def _has_onnxruntime():
    return importlib.util.find_spec("onnxruntime") is not None


def _resolve_backend():
    """Pick 'rknn' (NPU) or 'onnx' (CPU). AIPORT_BACKEND forces one; otherwise
    an explicit AIPORT_MODEL_PATH extension decides, else prefer the NPU when
    rknnlite and its model are present, else CPU onnxruntime."""
    forced = os.environ.get("AIPORT_BACKEND", "auto").strip().lower()
    if forced in ("rknn", "onnx"):
        return forced
    model = os.environ.get("AIPORT_MODEL_PATH")
    if model:
        if model.endswith(".rknn"):
            return "rknn"
        if model.endswith(".onnx"):
            return "onnx"
    if RKNNLite is not None and os.path.exists(RKNN_MODEL_PATH):
        return "rknn"
    if _has_onnxruntime() and os.path.exists(ONNX_MODEL_PATH):
        return "onnx"
    # Nothing usable; report the most actionable reason.
    if RKNNLite is not None and not os.path.exists(RKNN_MODEL_PATH):
        raise RuntimeError(
            f"no detector model found: {RKNN_MODEL_PATH} is missing (run "
            f"scripts/fetch_models.py --convert, or set AIPORT_MODEL_PATH)")
    if _has_onnxruntime() and not os.path.exists(ONNX_MODEL_PATH):
        raise RuntimeError(
            f"no detector model found: {ONNX_MODEL_PATH} is missing (run "
            f"scripts/fetch_models.py, or set AIPORT_MODEL_PATH)")
    raise RuntimeError(
        "no detector backend available: install rknn-toolkit-lite2 (RK3588 NPU) "
        "or onnxruntime (CPU) -- see requirements.txt / docker/README.md")


class _RknnBackend:
    """One shared RKNNLite behind a lock -- the NPU serializes inference
    anyway, so it's as fast as one instance per camera."""

    name = "rknn"

    def __init__(self):
        if RKNNLite is None:
            raise RuntimeError(
                "rknnlite is not installed -- install rknn-toolkit-lite2 on the device "
                "(see requirements.txt); detector.py imports without it only for unit tests")
        self._lock = threading.Lock()
        self._anchors = _load_anchors()
        self.model_path = os.environ.get("AIPORT_MODEL_PATH") or RKNN_MODEL_PATH
        self._rknn = RKNNLite()
        ret = self._rknn.load_rknn(self.model_path)
        if ret != 0:
            raise RuntimeError(f"load_rknn failed: {ret}")
        ret = self._rknn.init_runtime()
        if ret != 0:
            raise RuntimeError(f"init_runtime failed: {ret}")

    def raw(self, frame_rgb):
        # RKNNLite needs an explicit batch dim: (1,H,W,3), not (H,W,3).
        batched = frame_rgb[np.newaxis, ...]
        with self._lock:
            return self._rknn.inference(inputs=[batched])

    def boxes(self, raw_outputs):
        return _post_process(raw_outputs, self._anchors)


class _OnnxBackend:
    """One shared onnxruntime InferenceSession behind a lock (CPU)."""

    name = "onnx"

    def __init__(self):
        try:
            import onnxruntime as ort  # imported here so the NPU path doesn't need it
        except ImportError as exc:
            raise RuntimeError(
                "onnxruntime is not installed -- pip install onnxruntime, or run the "
                "CPU Docker image (see docker/README.md)") from exc
        self._lock = threading.Lock()
        self._labels = _load_labels()
        self._anchors = _load_anchors()
        self.model_path = os.environ.get("AIPORT_MODEL_PATH") or ONNX_MODEL_PATH
        so = ort.SessionOptions()
        # YOLOv5 is one sequential graph: no inter-op parallelism needed.
        so.inter_op_num_threads = 1
        intra = os.environ.get("AIPORT_ORT_INTRA_THREADS")
        if intra:
            so.intra_op_num_threads = int(intra)  # 0 = auto (all cores)
        # No spin-wait between inferences: onnxruntime's default spin burned ~3
        # extra cores on a 3-camera Pi 4 for no throughput. Park threads instead.
        for key in ("session.intra_op.allow_spinning", "session.inter_op.allow_spinning"):
            try:
                so.add_session_config_entry(key, "0")
            except Exception:
                pass
        self._session = ort.InferenceSession(self.model_path, so, providers=["CPUExecutionProvider"])
        inp = self._session.get_inputs()[0]
        self._input_name = inp.name
        # Ultralytics release ONNX is fp16; honour the graph's input dtype.
        self._in_dtype = np.float16 if "float16" in inp.type else np.float32
        shapes = [o.shape for o in self._session.get_outputs()]
        # Single [1,N,5+classes] output => Ultralytics; else raw heads.
        if len(shapes) == 1 and len(shapes[0]) == 3 and shapes[0][-1] == 5 + len(self._labels):
            self._mode = "ultralytics"
        else:
            self._mode = "raw_heads"

    def raw(self, frame_rgb):
        # NHWC uint8 -> NCHW float [1,3,H,W], scaled to [0,1].
        chw = frame_rgb.transpose(2, 0, 1).astype(np.float32) / 255.0
        batched = chw[np.newaxis, ...].astype(self._in_dtype)
        with self._lock:
            outputs = self._session.run(None, {self._input_name: batched})
        return [np.asarray(o, dtype=np.float32) for o in outputs]

    def boxes(self, raw_outputs):
        if self._mode == "ultralytics":
            return _post_process_ultralytics(raw_outputs[0])
        return _post_process(raw_outputs, self._anchors)


class Detector:
    """YOLOv5 detection with the rknn (NPU) or onnx (CPU) backend selected at
    construction. ``detect()`` output is identical for both."""

    def __init__(self):
        backend = _resolve_backend()
        self._labels = _load_labels()
        self._backend = _RknnBackend() if backend == "rknn" else _OnnxBackend()
        log.info("Detector initialized (backend=%s, model=%s)",
                 self._backend.name, self._backend.model_path)

    def detect(self, frame_rgb):
        """frame_rgb: HxWx3 uint8 RGB, already resized to IMG_SIZE (plain
        stretch, no letterboxing). Returns {"objectType", "score", "box":
        (x1,y1,x2,y2)} in IMG_SIZE pixel space, score-descending, mapped to
        AI Port's person/vehicle/animal vocabulary."""
        raw = self._backend.raw(frame_rgb)
        boxes, classes, scores = self._backend.boxes(raw)
        if boxes is None:
            return []
        results = []
        for box, cls, score in zip(boxes, classes, scores):
            coco_label = self._labels[int(cls)] if int(cls) < len(self._labels) else None
            mapped = _map_object_type(coco_label) if coco_label else None
            if not mapped:
                continue
            x1, y1, x2, y2 = [float(v) for v in box]
            results.append({"objectType": mapped, "score": float(score), "box": (x1, y1, x2, y2)})
        results.sort(key=lambda r: -r["score"])
        return results
