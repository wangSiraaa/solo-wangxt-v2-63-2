"""End-to-end HTTP API tests over a real socket, plus OpenAPI consistency."""

import http.client
import json
import pathlib
import threading
import unittest
from http.server import ThreadingHTTPServer

from sla.api import App, make_handler

from tests.helpers import T0

ROOT = pathlib.Path(__file__).resolve().parent.parent


class ApiTest(unittest.TestCase):
    def setUp(self):
        from sla.clock import FrozenClock
        self.clock = FrozenClock(T0)
        self.app = App(":memory:", clock=self.clock)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def req(self, method, path, body=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = raw if raw is not None else (
            json.dumps(body) if body is not None else None)
        conn.request(method, path, payload, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def test_full_pause_flow_over_http(self):
        status, event = self.req("POST", "/events", {
            "event_type": "RAINSTORM", "description": "level-3 rainstorm alert",
            "occurred_at": T0.isoformat()})
        self.assertEqual(status, 201)
        status, case = self.req("POST", "/cases",
                                {"title": "clear the blocked culvert", "sla_seconds": 72 * 3600})
        self.assertEqual(status, 201)
        case_id = case["id"]

        self.clock.advance(hours=10)
        status, pause = self.req("POST", f"/cases/{case_id}/pauses", {
            "event_id": event["id"], "reason": "site inaccessible",
            "evidence": ["photo://1"], "start_at": self.clock.now().isoformat()})
        self.assertEqual(status, 201)
        self.assertEqual(pause["state"], "PENDING")

        status, pause = self.req("POST", f"/pauses/{pause['id']}/approve")
        self.assertEqual(status, 200)
        self.assertEqual(pause["state"], "APPROVED")
        self.assertIsNotNone(pause["interval"])

        self.clock.advance(hours=5)
        status, view = self.req("GET", f"/cases/{case_id}/status")
        self.assertEqual(status, 200)
        self.assertEqual(view["paused_seconds"], 5 * 3600)
        self.assertEqual(view["effective_elapsed_seconds"], 10 * 3600)
        self.assertEqual(view["remaining_seconds"], 62 * 3600)

        status, pause = self.req("POST", f"/pauses/{pause['id']}/end")
        self.assertEqual(status, 200)
        self.assertEqual(pause["state"], "ENDED")
        # duplicate end over HTTP: idempotent, same ended_at
        status, again = self.req("POST", f"/pauses/{pause['id']}/end")
        self.assertEqual(status, 200)
        self.assertEqual(again["ended_at"], pause["ended_at"])

        status, listing = self.req("GET", f"/cases/{case_id}/pauses?state=ENDED")
        self.assertEqual(len(listing["pauses"]), 1)

        status, trace = self.req("GET", f"/cases/{case_id}/trace")
        self.assertEqual(status, 200)
        kinds = [e["type"] for e in trace["timeline"]]
        self.assertEqual(kinds[:2], ["CASE_OPENED", "PAUSE_APPLIED"])
        self.assertIn("PAUSE_APPROVED", kinds)
        self.assertIn("PAUSE_ENDED", kinds)
        self.assertEqual(trace["accounting"]["paused_seconds"], 5 * 3600)
        self.assertEqual(len(trace["pause_intervals"]["union"]), 1)

    def test_error_shapes(self):
        status, body = self.req("GET", "/pauses/pause_missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")

        status, body = self.req("POST", "/cases", {"title": "x"})  # missing sla_seconds
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "VALIDATION")

        status, body = self.req("POST", "/cases", raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "VALIDATION")

        status, case = self.req("POST", "/cases", {"title": "t", "sla_seconds": 3600})
        status, event = self.req("POST", "/events", {
            "event_type": "OTHER", "description": "d", "occurred_at": T0.isoformat()})
        # 400: reason/evidence fail validation (event exists this time)
        status, body = self.req("POST", f"/cases/{case['id']}/pauses",
                                {"event_id": event["id"], "reason": "", "evidence": [],
                                 "start_at": T0.isoformat()})
        self.assertEqual(status, 400)

        # 404: pause bound to a nonexistent event
        status, body = self.req("POST", f"/cases/{case['id']}/pauses",
                                {"event_id": "evt_x", "reason": "r", "evidence": ["e"],
                                 "start_at": T0.isoformat()})
        self.assertEqual(status, 404)

        # 409: ending a pause that was never approved
        status, pause = self.req("POST", f"/cases/{case['id']}/pauses", {
            "event_id": event["id"], "reason": "r", "evidence": ["e"],
            "start_at": T0.isoformat()})
        status, body = self.req("POST", f"/pauses/{pause['id']}/end")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "PAUSE_NOT_ACTIVE")

    def test_openapi_served_and_matches_committed_file(self):
        status, spec = self.req("GET", "/openapi.json")
        self.assertEqual(status, 200)
        self.assertEqual(spec["openapi"], "3.0.3")
        for path in ("/pauses/{pause_id}/approve", "/pauses/{pause_id}/end",
                     "/cases/{case_id}/pauses", "/cases/{case_id}/trace",
                     "/compensations/{suggestion_id}/confirm"):
            self.assertIn(path, spec["paths"], path)
        committed = json.loads((ROOT / "openapi.json").read_text())
        self.assertEqual(spec, committed, "committed openapi.json is stale; "
                                          "run scripts/export_openapi.py")


if __name__ == "__main__":
    unittest.main()
