# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Unit tests for detector.py's pure post-processing; skipped if numpy/
rknnlite aren't importable so the suite still runs off-device.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import detector
    _IMPORT_ERR = None
except Exception as exc:  # numpy missing, etc.
    detector = None
    _IMPORT_ERR = exc


@unittest.skipIf(detector is None, f"detector unavailable ({_IMPORT_ERR})")
class TestBackendSelection(unittest.TestCase):
    """AIPORT_BACKEND / AIPORT_MODEL_PATH choose the backend without importing
    onnxruntime or rknnlite (only the string decision is under test)."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("AIPORT_BACKEND", "AIPORT_MODEL_PATH")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_forced(self):
        os.environ["AIPORT_BACKEND"] = "rknn"
        self.assertEqual(detector._resolve_backend(), "rknn")
        os.environ["AIPORT_BACKEND"] = "onnx"
        self.assertEqual(detector._resolve_backend(), "onnx")

    def test_model_path_extension(self):
        os.environ.pop("AIPORT_BACKEND", None)
        os.environ["AIPORT_MODEL_PATH"] = "/some/model.rknn"
        self.assertEqual(detector._resolve_backend(), "rknn")
        os.environ["AIPORT_MODEL_PATH"] = "/some/model.onnx"
        self.assertEqual(detector._resolve_backend(), "onnx")


@unittest.skipIf(detector is None, f"detector unavailable ({_IMPORT_ERR})")
class TestOnnxProviders(unittest.TestCase):
    """AIPORT_ORT_PROVIDER picks Core ML when onnxruntime offers it, else CPU."""

    MAC = ["CoreMLExecutionProvider", "CPUExecutionProvider"]

    def setUp(self):
        saved = os.environ.pop("AIPORT_ORT_PROVIDER", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "AIPORT_ORT_PROVIDER", saved)
        self.addCleanup(os.environ.pop, "AIPORT_ORT_PROVIDER", None)

    def test_auto_prefers_coreml(self):
        p = detector._onnx_providers(self.MAC)
        self.assertEqual(p[0][0], "CoreMLExecutionProvider")
        self.assertEqual(p[-1], "CPUExecutionProvider")

    def test_coreml_format_follows_input_dtype(self):
        fp16 = detector._onnx_providers(self.MAC, fp16_input=True)[0][1]
        fp32 = detector._onnx_providers(self.MAC, fp16_input=False)[0][1]
        self.assertEqual(fp16.get("ModelFormat"), "MLProgram")
        self.assertNotIn("ModelFormat", fp32)  # NeuralNetwork, Core ML's default

    def test_auto_without_coreml(self):
        self.assertEqual(detector._onnx_providers(["CPUExecutionProvider"]),
                         ["CPUExecutionProvider"])

    def test_forced_cpu(self):
        os.environ["AIPORT_ORT_PROVIDER"] = "cpu"
        self.assertEqual(detector._onnx_providers(self.MAC), ["CPUExecutionProvider"])

    def test_coreml_compile_failure_falls_back_to_cpu(self):
        import types
        from unittest import mock

        class _IO:
            name, type, shape = "images", "tensor(float)", [1, 25200, 85]

        class _Session:
            def __init__(self, path, so, providers):
                if providers[0] != "CPUExecutionProvider":
                    raise RuntimeError("Unable to parse ML Program")
                self.providers = providers

            def get_providers(self):
                return self.providers

            def get_inputs(self):
                return [_IO()]

            def get_outputs(self):
                return [_IO()]

        class _Opts:
            def add_session_config_entry(self, k, v):
                pass

        fake = types.SimpleNamespace(InferenceSession=_Session, SessionOptions=_Opts,
                                     get_available_providers=lambda: self.MAC)
        with mock.patch.dict(sys.modules, {"onnxruntime": fake}), \
                self.assertLogs("aiport-detector", "ERROR"):
            backend = detector._OnnxBackend()
        self.assertEqual(backend.provider, "CPUExecutionProvider")


@unittest.skipIf(detector is None, f"detector unavailable ({_IMPORT_ERR})")
class TestMapObjectType(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(detector._map_object_type("person"), "person")
        for v in ("car", "bus", "truck", "bicycle", "motorbike", "train", "boat", "aeroplane"):
            self.assertEqual(detector._map_object_type(v), "vehicle", v)
        for a in ("bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"):
            self.assertEqual(detector._map_object_type(a), "animal", a)

    def test_unmapped(self):
        for u in ("toaster", "banana", "traffic light", ""):
            self.assertIsNone(detector._map_object_type(u))

    def test_strips_whitespace(self):
        self.assertEqual(detector._map_object_type("  person\n"), "person")


@unittest.skipIf(detector is None, f"detector unavailable ({_IMPORT_ERR})")
class TestPostProcess(unittest.TestCase):
    def setUp(self):
        self.anchors = detector._load_anchors()
        self.labels = detector._load_labels()
        # 3 heads: 80x80, 40x40, 20x20; 3 anchors per head; 85 channels each
        # (4 box + 1 objectness + 80 classes)
        import numpy as np
        self.np = np
        self.grids = [(80, 80), (40, 40), (20, 20)]

    def _zeros(self, np):
        return [np.zeros((1, 255, h, w), dtype=np.float32) for h, w in self.grids]

    def test_all_zero_yields_nothing(self):
        boxes, classes, scores = detector._post_process(self._zeros(self.np), self.anchors)
        self.assertIsNone(boxes)
        self.assertIsNone(classes)
        self.assertIsNone(scores)

    def test_one_person_detection(self):
        np = self.np
        outs = self._zeros(np)
        # put a strong person (COCO class 0) at cell (40,40) of the 80x80 head,
        # anchor 0. Channels: [0:4]=box, [4]=objectness, [5+cls]=class prob.
        g = outs[0]
        g[0, 0, 40, 40] = 0.5   # box x
        g[0, 1, 40, 40] = 0.5   # box y
        g[0, 2, 40, 40] = 0.5   # box w (pre-sigmoid-ish)
        g[0, 3, 40, 40] = 0.5   # box h
        g[0, 4, 40, 40] = 1.0   # objectness
        g[0, 5 + 0, 40, 40] = 1.0  # class 0 = person
        boxes, classes, scores = detector._post_process(outs, self.anchors)
        self.assertIsNotNone(boxes)
        self.assertGreaterEqual(len(scores), 1)
        self.assertIn(0, list(classes))
        self.assertGreater(float(max(scores)), 0.9)
        self.assertEqual(self.labels[0], "person")
        self.assertEqual(detector._map_object_type(self.labels[0]), "person")
        x1, y1, x2, y2 = boxes[0]
        self.assertLessEqual(x1, x2)
        self.assertLessEqual(y1, y2)


@unittest.skipIf(detector is None, f"detector unavailable ({_IMPORT_ERR})")
class TestNMS(unittest.TestCase):
    """Regression guard for _nms_boxes: a missing '+ w[others]'/'+ h[others]'
    term makes the intersection always zero, so NMS silently keeps every
    duplicate detection."""

    def setUp(self):
        import numpy as np
        self.np = np

    def _check(self, mod):
        np = self.np
        dup = np.array([[0, 0, 10, 10], [0, 0, 10, 10], [100, 100, 110, 110]], dtype=np.float32)
        self.assertEqual(len(mod._nms_boxes(dup, np.array([0.9, 0.8, 0.7], np.float32))), 2)
        heavy = np.array([[0, 0, 10, 10], [1, 1, 11, 11]], dtype=np.float32)
        self.assertEqual(len(mod._nms_boxes(heavy, np.array([0.9, 0.8], np.float32))), 1)
        light = np.array([[0, 0, 10, 10], [5, 5, 15, 15]], dtype=np.float32)
        self.assertEqual(len(mod._nms_boxes(light, np.array([0.9, 0.8], np.float32))), 2)

    def test_rk3588(self):
        self._check(detector)


@unittest.skipIf(detector is None, f"detector unavailable ({_IMPORT_ERR})")
class TestUltralytics(unittest.TestCase):
    """The Ultralytics [1,N,85] ONNX output (xywh + obj + class scores, decode
    baked in) used by the CPU backend must dedupe via NMS and decode xywh."""

    def test_duplicate_suppressed(self):
        import numpy as np
        out = np.zeros((1, 3, 85), dtype=np.float32)
        for row in (0, 1):  # two identical strong class-0 (person) boxes
            out[0, row, :4] = [320, 320, 100, 100]
            out[0, row, 4] = 0.9
            out[0, row, 5] = 0.9
        boxes, classes, scores = detector._post_process_ultralytics(out)
        self.assertIsNotNone(boxes)
        self.assertEqual(len(boxes), 1)
        self.assertEqual(int(classes[0]), 0)
        self.assertAlmostEqual(float(scores[0]), 0.81, places=5)
        # xywh (320,320,100,100) -> xyxy (270,270,370,370)
        self.assertAlmostEqual(float(boxes[0][0]), 270.0, places=4)
        self.assertAlmostEqual(float(boxes[0][2]), 370.0, places=4)

    def test_empty(self):
        import numpy as np
        boxes, classes, scores = detector._post_process_ultralytics(np.zeros((1, 2, 85), np.float32))
        self.assertIsNone(boxes)



@unittest.skipIf(detector is None, f"detector unavailable ({_IMPORT_ERR})")
class TestAnchorFree(unittest.TestCase):
    """YOLOv8/v9 [1,4+nc,N] output: xywh + class scores, no objectness."""

    def test_layout_detection(self):
        self.assertEqual(detector._layout([[1, 84, 2100]], 80), "anchor_free")
        self.assertEqual(detector._layout([[1, 25200, 85]], 80), "ultralytics")
        self.assertEqual(detector._layout([[1, 255, 80, 80]] * 3, 80), "raw_heads")

    def test_duplicate_suppressed_and_score_is_class_conf(self):
        import numpy as np
        out = np.zeros((1, 84, 4), dtype=np.float32)
        for col in (0, 1):  # two identical class-2 (car) boxes
            out[0, :4, col] = [160, 160, 40, 20]
            out[0, 4 + 2, col] = 0.8
        out[0, 4 + 2, 2] = 0.3  # below OBJ_THRESH
        boxes, classes, scores = detector._post_process_anchor_free(out)
        self.assertEqual(len(boxes), 1)
        self.assertEqual(int(classes[0]), 2)
        self.assertAlmostEqual(float(scores[0]), 0.8, places=5)
        self.assertEqual([round(float(v), 3) for v in boxes[0]], [140.0, 150.0, 180.0, 170.0])

    def test_empty(self):
        import numpy as np
        boxes, _, _ = detector._post_process_anchor_free(np.zeros((1, 84, 5), np.float32))
        self.assertIsNone(boxes)


@unittest.skipIf(detector is None, f"detector unavailable ({_IMPORT_ERR})")
class TestModelInputSize(unittest.TestCase):
    """A 320x320 model gets 640x640 frames resized down and its boxes scaled back up."""

    def _backend(self):
        import types
        from unittest import mock
        import numpy as np
        seen = {}

        class _In:
            name, type, shape = "images", "tensor(float)", [1, 3, 320, 320]

        class _Out:
            shape = [1, 84, 1]

        class _Session:
            def __init__(self, path, so, providers):
                self.providers = providers

            def get_providers(self):
                return self.providers

            def get_inputs(self):
                return [_In()]

            def get_outputs(self):
                return [_Out()]

            def run(self, _names, feed):
                seen["shape"] = feed["images"].shape
                out = np.zeros((1, 84, 1), np.float32)
                out[0, :4, 0] = [100, 50, 20, 10]  # model-space xywh
                out[0, 4 + 0, 0] = 0.9  # person
                return [out]

        class _Opts:
            def add_session_config_entry(self, k, v):
                pass

        fake = types.SimpleNamespace(InferenceSession=_Session, SessionOptions=_Opts,
                                     get_available_providers=lambda: ["CPUExecutionProvider"])
        with mock.patch.dict(sys.modules, {"onnxruntime": fake}):
            backend = detector._OnnxBackend()
        return backend, seen

    def test_input_size_from_graph(self):
        backend, _ = self._backend()
        self.assertEqual(backend.input_size, (320, 320))
        self.assertEqual(backend._mode, "anchor_free")

    def test_frame_resized_and_boxes_scaled_to_frame(self):
        import numpy as np
        backend, seen = self._backend()
        det = detector.Detector.__new__(detector.Detector)
        det._labels, det._backend = detector._load_labels(), backend
        res = det.detect(np.zeros((640, 640, 3), np.uint8))
        self.assertEqual(seen["shape"], (1, 3, 320, 320))
        self.assertEqual(res[0]["objectType"], "person")
        # model xyxy (90,45,110,55) at 320 -> (180,90,220,110) at 640
        self.assertEqual([round(v, 3) for v in res[0]["box"]], [180.0, 90.0, 220.0, 110.0])

    def test_resize_without_opencv(self):
        import numpy as np
        from unittest import mock
        frame = np.arange(4 * 4 * 3, dtype=np.uint8).reshape(4, 4, 3)
        with mock.patch.dict(sys.modules, {"cv2": None}):
            out = detector._resize(frame, (2, 2))
        self.assertEqual(out.shape, (2, 2, 3))
        self.assertTrue((out[1, 1] == frame[2, 2]).all())


if __name__ == "__main__":
    unittest.main()
