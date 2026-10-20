import json
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import avclient as av  # noqa: E402


class FakeWs:
    """Answers each timeSync request like Protect: t1 = t2 = our clock + offset_ms."""

    def __init__(self, offset_ms=0, answer=True, extra=None, payload=None):
        self.sent, self.offset_ms, self.answer = [], offset_ms, answer
        self.queue = list(extra or [])
        self.payload = payload
        self.reply_id = 900

    def send(self, data):
        msg = json.loads(data)
        self.sent.append(msg)
        if self.answer:
            now = 1_792_000_000_000 + self.offset_ms
            self.reply_id += 1
            self.queue.append({"from": "UniFiVideo", "to": "ubnt_avclient",
                               "functionName": msg["functionName"], "messageId": self.reply_id,
                               "inResponseTo": msg["messageId"], "responseExpected": False,
                               "payload": self.payload if self.payload is not None
                               else {"t1": now, "t2": now}})

    def recv(self, timeout=None):
        if not self.queue:
            raise TimeoutError
        return json.dumps(self.queue.pop(0)).encode()


class TestTimeSyncBatch(unittest.TestCase):
    def run_batch(self, ws, **kw):
        # Our clock reads 1 792 000 000 000 ms throughout: rtt 0, offset exactly as served.
        with mock.patch.object(av.time, "time", lambda: 1_792_000_000.0):
            return av._time_sync_batch(ws, **kw)

    def test_ten_chained_requests_like_a_real_device(self):
        ws = FakeWs(offset_ms=250)
        offset, other = self.run_batch(ws)
        self.assertEqual(offset, 250)
        self.assertEqual(other, [])
        self.assertEqual(len(ws.sent), 10)
        first = ws.sent[0]
        self.assertEqual((first["from"], first["to"]), ("ubnt_avclient", "UniFiVideo"))
        self.assertEqual(first["functionName"], "ubnt_avclient_timeSync")
        self.assertTrue(first["responseExpected"])
        self.assertEqual(first["inResponseTo"], 0)
        self.assertEqual(first["payload"], {"timeDelta": 0})
        # each later request answers Protect's previous reply
        self.assertEqual([m["inResponseTo"] for m in ws.sent[1:]], list(range(901, 910)))

    def test_no_reply_stops_after_one_timeout(self):
        ws = FakeWs(answer=False)
        offset, _ = self.run_batch(ws, timeout=0.01)
        self.assertIsNone(offset)
        self.assertEqual(len(ws.sent), 1)

    def test_reply_without_times_stops(self):
        ws = FakeWs(payload={"oops": 1})
        offset, _ = self.run_batch(ws)
        self.assertIsNone(offset)
        self.assertEqual(len(ws.sent), 1)

    def test_other_messages_are_kept_for_later(self):
        other_msg = {"functionName": "ChangeDeviceSettings", "messageId": 5, "inResponseTo": 0,
                     "payload": {"timezone": "America/Toronto"}}
        ws = FakeWs(extra=[other_msg])
        offset, other = self.run_batch(ws)
        self.assertEqual(offset, 0)
        self.assertEqual(other, [other_msg])

    def test_offset_maths(self):
        # T0 = 1000, Protect t1 = t2 = 1600, T3 = 1200: offset ((600) + (400)) / 2 = 500, rtt 200
        clock = iter([1.0, 1.2])
        ws = FakeWs(payload={"t1": 1600, "t2": 1600})
        with mock.patch.object(av.time, "time", lambda: next(clock)):
            offset, _ = av._time_sync_batch(ws, rounds=1)
        self.assertEqual(offset, 500)

    def test_off_by_default(self):
        if "AIPORT_TIMESYNC" not in os.environ:
            self.assertEqual(av.TIMESYNC, "off")


if __name__ == "__main__":
    unittest.main()
