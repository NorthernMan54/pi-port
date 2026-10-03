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


if __name__ == "__main__":
    unittest.main()
