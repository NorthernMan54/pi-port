import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import apple_fm  # noqa: E402
import avclient as av  # noqa: E402


class TestParseAnswer(unittest.TestCase):
    def test_protect_vocabulary(self):
        self.assertEqual(apple_fm.parse_answer("suv black"), ("suv", "black"))
        self.assertEqual(apple_fm.parse_answer("Sedan, Grey."), ("car", "gray"))
        self.assertEqual(apple_fm.parse_answer("pickup silver"), ("truck", "gray"))
        self.assertEqual(apple_fm.parse_answer("minivan white"), ("van", "white"))

    def test_camper_is_a_trailer(self):
        # The model called the parked camper a "van" until trailer was an allowed answer.
        self.assertEqual(apple_fm.parse_answer("camper brown"), ("trailer", "brown"))

    def test_unknown_words(self):
        self.assertEqual(apple_fm.parse_answer("I can't tell"), (None, None))
        self.assertEqual(apple_fm.parse_answer(""), (None, None))


class TestAsSessionUser(unittest.TestCase):
    def test_root_runs_it_as_the_sudo_user(self):
        with mock.patch.object(apple_fm.os, "geteuid", return_value=0), \
                mock.patch.dict(os.environ, {"SUDO_UID": "501", "SUDO_USER": "me"}):
            self.assertEqual(apple_fm._as_session_user(["helper"]),
                             ["launchctl", "asuser", "501", "sudo", "-u", "me", "--", "helper"])

    def test_a_user_runs_it_directly(self):
        with mock.patch.object(apple_fm.os, "geteuid", return_value=501):
            self.assertEqual(apple_fm._as_session_user(["helper"]), ["helper"])


class TestVote(unittest.TestCase):
    def test_weighted_by_box_area(self):
        # Live 15:13: a black SUV's crops came back truck, suv, suv.
        votes = [("truck", "black", 100), ("suv", "black", 150), ("suv", "black", 200)]
        self.assertEqual(av._fm_vote(votes),
                         {"vehicleType": {"val": "suv", "confidence": 78},
                          "color": {"val": "black", "confidence": 100}})

    def test_trailer_drops_the_type(self):
        fm = av._fm_vote([("trailer", "gray", 1)])
        self.assertIsNone(fm["vehicleType"])
        tr = {"extra": {"attributes": {"vehicleType": {"val": "truck", "confidence": 70}}},
              "fm_attrs": fm}
        av._apply_fm_attrs(tr)
        self.assertEqual(tr["extra"]["attributes"], {"color": {"val": "gray", "confidence": 100}})

    def test_unanswered_field_left_out(self):
        self.assertEqual(av._fm_vote([(None, "red", 1)]), {"color": {"val": "red", "confidence": 100}})


class TestMergeKeepsTheModel(unittest.TestCase):
    def test_per_frame_guess_does_not_replace_the_vote(self):
        tr = {"objectType": "vehicle",
              "fm_attrs": {"vehicleType": {"val": "suv", "confidence": 75},
                           "color": {"val": "black", "confidence": 100}}}
        av._merge_extra(tr, {"extra": {"attributes": {"vehicleType": {"val": "car", "confidence": 70},
                                                      "color": {"val": "blue", "confidence": 40}}}})
        self.assertEqual(tr["extra"]["attributes"]["vehicleType"]["val"], "suv")
        self.assertEqual(tr["extra"]["attributes"]["color"]["val"], "black")


class TestRequest(unittest.TestCase):
    def setUp(self):
        self.jobs = []
        self.now = [1_000_000]
        for name, value in (("VEHICLE_FM_SYNC", True), ("_fm_classifier", None),
                            ("_fm_job", lambda *a, **k: self.jobs.append(a)),
                            ("_frame_wall_ms", lambda dev: self.now[0])):
            patcher = mock.patch.object(av, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        av._stream_dims["D"] = (2560, 1440)
        self.addCleanup(av._stream_dims.pop, "D", None)

    def track(self, coord=(500, 300, 100, 150)):
        return {"objectType": "vehicle", "best": {"jpeg": b"x", "coord": list(coord)}}

    def test_at_most_three_crops_spaced_out(self):
        tr = self.track()
        for step in range(8):
            av._request_fm("D", tr)
            self.now[0] += 1000          # every second: only every other one is far enough apart
        self.assertEqual(len(self.jobs), 3)

    def test_too_small_or_cut_off_is_not_sent(self):
        av._request_fm("D", self.track((500, 300, 30, 150)))   # 77 px wide
        av._request_fm("D", self.track((707, 43, 94, 62)))     # 89 px high: the roof at the edge
        self.assertEqual(self.jobs, [])

    def test_nothing_once_the_model_is_unavailable(self):
        with mock.patch.object(av, "_fm_classifier", mock.Mock(available=False)):
            av._request_fm("D", self.track())
        self.assertEqual(self.jobs, [])


if __name__ == "__main__":
    unittest.main()
