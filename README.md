# Rectification SLA Pause Service (逾期升级 SLA 停表)

Legitimate causes — rainstorms, road closures, government orders — can make
on-site rectification temporarily impossible.  This service adds an
**auditable SLA pause ledger** on top of the overdue-escalation engine, so
the escalation clock can be stopped and resumed without ever rewriting
history.

Pure Python 3.11 standard library: `sqlite3` for persistence, `http.server`
for the API, `unittest` for tests.  No dependencies to install.

## Quick start

```bash
python3 run_server.py --db sla.db --port 8080     # serve the API
python3 -m unittest discover -s tests             # run the test suite (35 tests)
python3 scripts/export_openapi.py                 # regenerate openapi.json
```

The OpenAPI 3.0 document is served at `GET /openapi.json` and committed as
`openapi.json`; a test asserts the two never drift apart.

## Domain model

```
rectification_cases ──< escalations ──< penalties ──< penalty_corrections
        │                                    escalation_annotations
        ├──< sla_pauses >── events           compensation_suggestions
        └──< pause_intervals (one per approved pause)
```

* **Pause application** — binds an **event** (rainstorm, road closure, …),
  a **reason** and **evidence** (non-empty list of references).  `start_at`
  is evidence-backed and may be retroactive (the storm started before the
  paperwork); it may not be in the future or precede the case opening.
* **State machine** — `PENDING → APPROVED → ENDED`, plus `REJECTED` and
  `REVOKED` from `PENDING`.  Repeating the transition that produced a state
  is an idempotent retry (returns the same record); any other transition
  from a terminal state is a `409`.
* **Interval ledger** — exactly one `pause_intervals` row exists per pause
  that was ever approved.  It is inserted in the *same transaction* as the
  approval and closed in the *same transaction* as the end, so a crash or a
  failed approval can never leave half a stopwatch
  (`pauses.integrity_violations()` verifies the invariant).

## Escalation math (injected clock)

All time comes from an injected clock (`SystemClock` in the server,
`FrozenClock` in tests).  Levels fire on **effective overdue time**:

```
effective_elapsed = wall_elapsed − union(approved pause intervals)
effective_overdue = effective_elapsed − sla_seconds
levels: L1 at 0h, L2 at 24h, L3 at 72h overdue (L3 creates a penalty)
```

* Deducting the **union** — not the sum — means overlapping pauses never
  extend the deadline twice, structurally.
* While a pause runs, `remaining_seconds` is frozen; it resumes when the
  pause ends (see `GET /cases/{id}/status`).
* The engine is idempotent (`UNIQUE(case_id, level)` + `INSERT OR IGNORE`),
  so restart catch-up runs (`POST /engine/run`) never duplicate records.
* Once a case is **closed** (`POST /cases/{id}/rectify`), the engine skips
  it forever, running pauses are ended in the same transaction, and no new
  pause can be applied for or approved — the clock is never restarted.

## Late approvals and immutable history

Escalations and penalties are **never deleted or mutated** — a locked
penalty stays locked.  When a retroactively approved pause makes an
already-recorded escalation or penalty premature, the approval transaction
raises a `compensation_suggestion` (`PENDING_REVIEW`) instead:

* `POST /compensations/{id}/confirm` — **appends** a `penalty_corrections`
  credit / an `escalation_annotations` note.  The original penalty chain is
  preserved; confirming twice is an idempotent no-op, and a penalty can
  never be credited twice.
* `POST /compensations/{id}/dismiss` — closes the review with no changes.

## API overview

| Endpoint | Purpose |
| --- | --- |
| `POST /events`, `GET /events/{id}` | register / fetch pause-worthy events |
| `POST /cases`, `GET /cases[{/id}]` | open / list / fetch cases |
| `POST /cases/{id}/rectify` | close as rectified (clock stops forever) |
| `GET /cases/{id}/status` | stopwatch view: effective/remaining time |
| `GET /cases/{id}/trace` | retrospective audit output (追溯) |
| `POST /cases/{id}/pauses`, `GET /cases/{id}/pauses[?state=]` | apply / query |
| `GET /pauses/{id}` | fetch one application |
| `POST /pauses/{id}/approve` `/reject` `/end` `/revoke` | lifecycle |
| `POST /cases/{id}/engine/run`, `POST /engine/run` | run engine (idempotent) |
| `GET /cases/{id}/escalations` `/penalties` `/compensations` | query records |
| `POST /penalties/{id}/lock` | finalise a penalty |
| `POST /compensations/{id}/confirm` `/dismiss` | review suggestions |

Errors are `{"error": {"code", "message", "details?"}}` with `400` /
`404` / `409` as appropriate.

## Migrations

`sla/migrations.py` runs on every startup and applies each pending migration
in its own transaction, recorded in `schema_migrations`:

* **001_core** — the legacy system: cases (with ad-hoc `legacy_hold_seconds`),
  escalations, penalties.
* **002_sla_pauses** — the pause ledger (events, pauses, intervals,
  suggestions, corrections, annotations) **plus** conversion of every legacy
  hold into a complete `ENDED` pause record, in the same transaction.  Legacy
  holds carry no timing information, so they are anchored at the case
  opening (documented convention); the `legacy_hold_migrated` guard makes
  re-runs convert nothing twice.

## Layout

```
sla/
  clock.py        injected clock (System / Frozen)
  intervals.py    time-interval service: union / clip / covered seconds
  migrations.py   versioned transactional migrations + legacy conversion
  events.py       events that pauses bind to
  cases.py        case lifecycle (rectify closes and freezes the clock)
  escalation.py   clock-injected, pause-aware escalation engine
  pauses.py       pause state machine + late-approval scan + integrity check
  compensation.py reviewable suggestions -> appended corrections
  trace.py        retrospective output assembly
  api.py          route registry -> HTTP dispatch (and OpenAPI source)
  openapi.py      OpenAPI 3.0 builder
tests/            35 tests covering every acceptance criterion
```

## Acceptance coverage map

| Criterion | Tests |
| --- | --- |
| 批准后剩余时长正确续算 | `test_escalation_with_pauses.RemainingTimeResumesTest` |
| 重叠暂停和重复结束请求不双算 | `...OverlapAndDuplicateEndTest`, `test_intervals.py` |
| 暂停中整改后不再升级 | `...RectifiedDuringPauseTest` |
| 锁定处罚上的迟到批准不被静默撤销 | `test_late_approval_compensation.py` |
| 审批失败 / 重启重跑 / 旧事件迁移不留半截停表 | `test_migration.py` |
