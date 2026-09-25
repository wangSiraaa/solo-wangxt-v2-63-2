"""HTTP API.

A single route registry drives both request dispatch and the OpenAPI
document, so the spec can never drift from the implementation.  The app is
framework-free (``http.server``) and every handler is a thin adapter over
the service modules; all time comes from the app's injected clock.
"""

from __future__ import annotations

import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import cases, compensation, events, escalation, pauses, trace
from .clock import SystemClock
from .db import connect
from .errors import ApiError, NotFound, Validation
from .migrations import migrate

ROUTES: list[dict] = []


def route(method: str, path: str, *, summary: str, tags: list[str],
          request: str | None = None, response: str | None = None,
          status: int = 200, description: str = "", query: list[str] | None = None):
    """Register a route; metadata feeds both dispatch and OpenAPI."""
    def deco(fn):
        ROUTES.append({
            "method": method, "path": path, "handler": fn, "summary": summary,
            "description": description or summary, "tags": tags,
            "request": request, "response": response, "status": status,
            "query": query or [],
        })
        return fn
    return deco


# --------------------------------------------------------------------------- app

class App:
    def __init__(self, db_path: str = ":memory:", clock=None, run_migrations: bool = True):
        self.conn = connect(db_path)
        self.clock = clock or SystemClock()
        self.lock = threading.RLock()
        if run_migrations:
            migrate(self.conn, self.clock)

    # -- dispatch ----------------------------------------------------------
    def dispatch(self, method: str, raw_path: str, raw_body: bytes = b"") -> tuple[int, object]:
        with self.lock:
            try:
                body = {}
                if raw_body:
                    body = json.loads(raw_body)
                    if not isinstance(body, dict):
                        raise Validation("request body must be a JSON object")
                return self._dispatch(method, raw_path, body)
            except ApiError as exc:
                return exc.status, exc.payload()
            except json.JSONDecodeError as exc:
                err = Validation(f"request body is not valid JSON: {exc}")
                return err.status, err.payload()
            except Exception as exc:  # pragma: no cover - defensive
                traceback.print_exc()
                err = ApiError(f"internal error: {exc}")
                return err.status, err.payload()

    def _dispatch(self, method: str, raw_path: str, body):
        parts = urlsplit(raw_path)
        path = parts.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        for r in ROUTES:
            params = _match(r["path"], path)
            if params is not None and r["method"] == method:
                result = r["handler"](self, params, query, body or {})
                if isinstance(result, tuple):
                    return result
                return r["status"], result
        raise NotFound(f"no route for {method} {path}")


def _match(pattern: str, path: str) -> dict | None:
    p_seg, s_seg = pattern.strip("/").split("/"), path.strip("/").split("/")
    if len(p_seg) != len(s_seg):
        return None
    params = {}
    for p, s in zip(p_seg, s_seg):
        if p.startswith("{") and p.endswith("}"):
            params[p[1:-1]] = s
        elif p != s:
            return None
    return params


# --------------------------------------------------------------------------- routes

@route("GET", "/health", summary="Liveness probe", tags=["meta"])
def _health(app, params, query, body):
    return {"status": "ok"}


@route("GET", "/openapi.json", summary="OpenAPI 3.0 document for this service",
       tags=["meta"], response="OpenAPI")
def _openapi(app, params, query, body):
    from .openapi import build_openapi
    return build_openapi()


@route("POST", "/events", summary="Register a pause-worthy event (rainstorm, road closure, ...)",
       tags=["events"], request="EventCreate", response="Event", status=201)
def _create_event(app, params, query, body):
    return events.create_event(app.conn, app.clock, body.get("event_type"),
                               body.get("description", ""), body.get("occurred_at"))


@route("GET", "/events/{event_id}", summary="Fetch an event", tags=["events"], response="Event")
def _get_event(app, params, query, body):
    return events.get_event(app.conn, params["event_id"])


@route("POST", "/cases", summary="Open a rectification case",
       tags=["cases"], request="CaseCreate", response="Case", status=201)
def _create_case(app, params, query, body):
    return cases.create_case(app.conn, app.clock, body.get("title"),
                             body.get("sla_seconds"), body.get("opened_at"))


@route("GET", "/cases", summary="List rectification cases", tags=["cases"],
       response="CaseList")
def _list_cases(app, params, query, body):
    return {"cases": cases.list_cases(app.conn)}


@route("GET", "/cases/{case_id}", summary="Fetch a case", tags=["cases"], response="Case")
def _get_case(app, params, query, body):
    return cases.get_case(app.conn, params["case_id"])


@route("POST", "/cases/{case_id}/rectify",
       summary="Close the case as rectified (the SLA clock is never restarted afterwards)",
       tags=["cases"], response="Case")
def _rectify_case(app, params, query, body):
    return cases.rectify_case(app.conn, app.clock, params["case_id"])


@route("GET", "/cases/{case_id}/status",
       summary="Stopwatch view: effective elapsed / remaining time with pauses deducted",
       tags=["cases"], response="CaseStatus")
def _case_status(app, params, query, body):
    return escalation.case_status(app.conn, app.clock, params["case_id"])


@route("GET", "/cases/{case_id}/trace",
       summary="Retrospective audit output for the case",
       tags=["cases"], response="Trace")
def _case_trace(app, params, query, body):
    return trace.case_trace(app.conn, app.clock, params["case_id"])


@route("POST", "/cases/{case_id}/engine/run",
       summary="Run the escalation engine for one case (idempotent)",
       tags=["engine"], response="EngineRunResult")
def _run_engine_case(app, params, query, body):
    return escalation.run_case(app.conn, app.clock, params["case_id"])


@route("POST", "/engine/run",
       summary="Restart catch-up: run the engine for every open case (idempotent)",
       tags=["engine"], response="EngineRunAllResult")
def _run_engine_all(app, params, query, body):
    return escalation.run_all_open_cases(app.conn, app.clock)


@route("GET", "/cases/{case_id}/escalations", summary="List recorded escalations",
       tags=["cases"], response="EscalationList")
def _list_escalations(app, params, query, body):
    return {"escalations": escalation.list_escalations(app.conn, params["case_id"])}


@route("GET", "/cases/{case_id}/penalties", summary="List penalties with their correction chain",
       tags=["cases"], response="PenaltyList")
def _list_penalties(app, params, query, body):
    return {"penalties": escalation.list_penalties(app.conn, params["case_id"])}


@route("POST", "/penalties/{penalty_id}/lock",
       summary="Finalise a penalty; locked penalties can only be corrected by appending",
       tags=["penalties"], response="Penalty")
def _lock_penalty(app, params, query, body):
    return escalation.lock_penalty(app.conn, app.clock, params["penalty_id"])


@route("POST", "/cases/{case_id}/pauses",
       summary="Apply for an SLA pause bound to an event, reason and evidence",
       tags=["pauses"], request="PauseApply", response="Pause", status=201)
def _apply_pause(app, params, query, body):
    return pauses.apply_pause(app.conn, app.clock, params["case_id"],
                              body.get("event_id"), body.get("reason"),
                              body.get("evidence"), body.get("start_at"))


@route("GET", "/cases/{case_id}/pauses", summary="List pause applications for a case",
       tags=["pauses"], response="PauseList", query=["state"])
def _list_pauses(app, params, query, body):
    return {"pauses": pauses.list_pauses(app.conn, params["case_id"], query.get("state"))}


@route("GET", "/pauses/{pause_id}", summary="Fetch a pause application",
       tags=["pauses"], response="Pause")
def _get_pause(app, params, query, body):
    return pauses.get_pause(app.conn, params["pause_id"])


@route("POST", "/pauses/{pause_id}/approve",
       summary="Approve a pause (effective immediately; late approvals raise "
               "compensation suggestions instead of rewriting history)",
       tags=["pauses"], request="DecisionNote", response="Pause")
def _approve_pause(app, params, query, body):
    return pauses.approve_pause(app.conn, app.clock, params["pause_id"], body.get("note"))


@route("POST", "/pauses/{pause_id}/reject", summary="Reject a pause application",
       tags=["pauses"], request="DecisionNote", response="Pause")
def _reject_pause(app, params, query, body):
    return pauses.reject_pause(app.conn, app.clock, params["pause_id"], body.get("note"))


@route("POST", "/pauses/{pause_id}/end",
       summary="End a running pause (idempotent; duplicates never double-count)",
       tags=["pauses"], response="Pause")
def _end_pause(app, params, query, body):
    return pauses.end_pause(app.conn, app.clock, params["pause_id"])


@route("POST", "/pauses/{pause_id}/revoke",
       summary="Withdraw a pending pause application",
       tags=["pauses"], request="DecisionNote", response="Pause")
def _revoke_pause(app, params, query, body):
    return pauses.revoke_pause(app.conn, app.clock, params["pause_id"], body.get("note"))


@route("GET", "/cases/{case_id}/compensations",
       summary="List compensation suggestions raised by late approvals",
       tags=["compensations"], response="SuggestionList", query=["status"])
def _list_suggestions(app, params, query, body):
    return {"suggestions": compensation.list_suggestions(
        app.conn, params["case_id"], query.get("status"))}


@route("POST", "/compensations/{suggestion_id}/confirm",
       summary="Confirm a suggestion: append the correction, preserve the original chain",
       tags=["compensations"], request="DecisionNote", response="Suggestion")
def _confirm_suggestion(app, params, query, body):
    return compensation.confirm_suggestion(app.conn, app.clock,
                                           params["suggestion_id"], body.get("note"))


@route("POST", "/compensations/{suggestion_id}/dismiss",
       summary="Dismiss a suggestion after review",
       tags=["compensations"], request="DecisionNote", response="Suggestion")
def _dismiss_suggestion(app, params, query, body):
    return compensation.dismiss_suggestion(app.conn, app.clock,
                                           params["suggestion_id"], body.get("note"))


# --------------------------------------------------------------------------- server

def make_handler(app: App, verbose: bool = False):
    class Handler(BaseHTTPRequestHandler):
        def _handle(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            status, payload = app.dispatch(method, self.path, raw)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = lambda self: self._handle("GET")   # noqa: E731
        do_POST = lambda self: self._handle("POST")  # noqa: E731

        def log_message(self, fmt, *args):
            if verbose:
                super().log_message(fmt, *args)

    return Handler


def serve(host: str, port: int, db_path: str, verbose: bool = True) -> None:
    app = App(db_path)
    server = ThreadingHTTPServer((host, port), make_handler(app, verbose))
    print(f"SLA pause service listening on http://{host}:{port}")
    print(f"OpenAPI document: http://{host}:{port}/openapi.json")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        server.shutdown()
