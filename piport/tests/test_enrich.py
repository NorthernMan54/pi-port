# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Unit tests for enrich.py (vehicle attributes) and avclient's enrichment
plumbing; the plate/face model test runs only when its models are installed
and AIPORT_ENRICH_SAMPLES points at sample images."""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402

import enrich  # noqa: E402


def _solid(rgb, size=64):
    return np.full((size, size, 3), rgb, dtype=np.uint8)


class TestDominantColor(unittest.TestCase):
    FULL = [0, 0, 1000, 1000]

    def check(self, rgb, expected):
        color, conf = enrich.dominant_color(_solid(rgb), self.FULL)
        self.assertEqual(color, expected, rgb)
        self.assertEqual(conf, 100)

    def test_basic_colors(self):
        self.check((200, 20, 20), "red")
        self.check((20, 40, 200), "blue")
        self.check((20, 160, 40), "green")
        self.check((230, 210, 30), "yellow")

    def test_neutrals(self):
        self.check((10, 10, 10), "black")
        self.check((245, 245, 245), "white")
        self.check((170, 170, 175), "silver")
        self.check((90, 90, 90), "gray")

    def test_brown(self):
        self.check((100, 60, 30), "brown")

    def test_empty_crop(self):
        self.assertIsNone(enrich.dominant_color(_solid((1, 2, 3)), [0, 0, 0, 0]))

    def test_uses_box_center_not_edges(self):
        frame = _solid((20, 40, 200), size=100)       # blue background
        frame[30:90, 20:80] = (200, 20, 20)            # red car body
        color, _ = enrich.dominant_color(frame, [200, 200, 600, 700])
        self.assertEqual(color, "red")


class TestVehicleAttributes(unittest.TestCase):
    def test_type_and_color(self):
        a = enrich.vehicle_attributes("car", _solid((200, 20, 20)), [0, 0, 1000, 1000])
        self.assertEqual(a["vehicleType"]["val"], "sedan")
        self.assertEqual(a["color"], {"val": "red", "confidence": 100})

    def test_unknown_label_keeps_color(self):
        a = enrich.vehicle_attributes("train", _solid((10, 10, 10)), [0, 0, 1000, 1000])
        self.assertNotIn("vehicleType", a)
        self.assertEqual(a["color"]["val"], "black")

    def test_nothing(self):
        self.assertIsNone(enrich.vehicle_attributes("boat", None, [0, 0, 10, 10]))


class TestCoordHelpers(unittest.TestCase):
    def test_expand_and_back(self):
        x1, y1, x2, y2 = enrich._expand_px([250, 250, 500, 500], 2000, 1000, margin=1.0)
        self.assertEqual((x1, y1, x2, y2), (500, 250, 1500, 750))
        self.assertEqual(enrich._to_norm(x1, y1, x2, y2, 2000, 1000), [250, 250, 500, 500])

    def test_expand_clamps(self):
        self.assertEqual(enrich._expand_px([0, 0, 1000, 1000], 100, 100, margin=2.0),
                         (0, 0, 100, 100))


try:
    import avclient
    _AV_ERR = None
except Exception as exc:  # websockets/requests missing
    avclient = None
    _AV_ERR = exc


@unittest.skipIf(avclient is None, f"avclient unavailable ({_AV_ERR})")
class TestMergeExtra(unittest.TestCase):
    def test_plate_keeps_best_read(self):
        tr = {"objectType": "licensePlate", "extra": {"licensePlate": "ABC123"}, "extra_score": 0.9}
        avclient._merge_extra(tr, {"score": 0.6, "extra": {"licensePlate": "A8C123"}})
        self.assertEqual(tr["extra"]["licensePlate"], "ABC123")
        avclient._merge_extra(tr, {"score": 0.95, "extra": {"licensePlate": "ABC124"}})
        self.assertEqual(tr["extra"]["licensePlate"], "ABC124")

    def test_name_not_cleared_by_unmatched_tick(self):
        tr = {"objectType": "face", "extra": {"name": "Alex", "tag": "Alex"}}
        avclient._merge_extra(tr, {"score": 0.9, "extra": {"name": "", "tag": ""}})
        self.assertEqual(tr["extra"]["name"], "Alex")

    def test_vehicle_gains_plate(self):
        tr = {"objectType": "vehicle", "extra": {"attributes": {"color": {"val": "red"}}}}
        avclient._merge_extra(tr, {"score": 0.8, "extra": {"licensePlate": "XYZ9"}})
        self.assertEqual(tr["extra"]["licensePlate"], "XYZ9")
        self.assertIn("attributes", tr["extra"])

    def test_descriptor_carries_extra(self):
        sent = []
        orig = avclient.send_msg
        avclient.send_msg = lambda ws, fn, payload, **kw: sent.append(payload)
        try:
            avclient._send_smart_detect_event(
                None, "DEV", edge_type="enter", object_type="licensePlate",
                coord=[1, 2, 3, 4], confidence=0.9,
                descriptor_extra={"licensePlate": "ABC123", "name": "ABC123"})
        finally:
            avclient.send_msg = orig
        d = sent[0]["descriptors"][0]
        self.assertEqual((d["objectType"], d["licensePlate"], d["name"]),
                         ("licensePlate", "ABC123", "ABC123"))
        self.assertEqual(sent[0]["objectTypes"], ["licensePlate"])


_SAMPLES = os.environ.get("AIPORT_ENRICH_SAMPLES")


@unittest.skipUnless(_SAMPLES and enrich.lpr_available() and enrich.face_available(),
                     "set AIPORT_ENRICH_SAMPLES (plate.png, lena.jpg) with fast-alpr + face models")
class TestModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import cv2
        cls.cv2 = cv2
        cls.e = enrich.Enricher(faces_dir=None)

    def test_plate(self):
        img = self.cv2.imread(os.path.join(_SAMPLES, "plate.png"))
        plates = self.e.plates(img, [[0, 0, 1000, 1000]])
        self.assertEqual([p["text"] for p in plates], ["5AU5341"])

    def test_face(self):
        img = self.cv2.imread(os.path.join(_SAMPLES, "lena.jpg"))
        faces = self.e.faces(img, [[0, 0, 1000, 1000]])
        self.assertEqual(len(faces), 1)
        self.assertEqual(faces[0]["name"], "")


if __name__ == "__main__":
    unittest.main()
