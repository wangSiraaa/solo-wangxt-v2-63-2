"""Auditable SLA pause (停表) service for overdue rectification escalation.

Legitimate causes (rainstorms, road closures, ...) can make on-site
rectification temporarily impossible.  This package adds an auditable SLA
pause ledger on top of the escalation engine:

* every pause application binds an event, a reason and evidence;
* pauses flow through PENDING -> APPROVED -> ENDED (or REJECTED / REVOKED);
* escalation math runs on an injected clock and deducts the *union* of
  approved pause intervals, so overlapping pauses never extend twice;
* once a case is closed the clock is never restarted;
* pauses never delete recorded escalations nor rewrite locked penalties --
  late approvals only raise reviewable compensation suggestions which, once
  confirmed, append corrections while preserving the original penalty chain.
"""

__version__ = "1.0.0"
