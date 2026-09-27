"""opengrid.calls -- the one dispatch-call path (decision log D-29, D-33; 02a S5 capacity holds).

A "call" deploys a capacity hold now or later: an ERCOT_AS award (ERCOT's deployment, the market sim,
an operator) or a utility's discharge call on a tolling obligation (the operator's "Utility call...", the
utility customer API, the grid link). Every origin goes through `issue_call` / `cancel_call` and the pure
checks in `rules` -- product-duration cap, no overlap, discharge only (+charge/-discharge), within the
committed kW and the reservation window, idempotency per principal, per-principal rate limits -- and every
call is traced (K10), recorded in `og.dispatch_call` and, when not from an operator, alerted.

`PgCallStore` is the Postgres `CallStore`; any process constructs it on its own pool.
"""

from opengrid.calls.models import (
    AwardView,
    CallKind,
    CallOrigin,
    CallOutcome,
    CallRecord,
    CallRefused,
    CallRequest,
    CallState,
    CallStatus,
)
from opengrid.calls.pg_store import PgCallStore
from opengrid.calls.ports import CallStore
from opengrid.calls.rules import CallLimits
from opengrid.calls.service import (
    UTILITY_CALL_REASON_PREFIX,
    call_status,
    cancel_call,
    cancel_deployment,
    find_call_by_key,
    issue_call,
    list_calls,
    status_of,
)

__all__ = [
    "UTILITY_CALL_REASON_PREFIX",
    "AwardView",
    "CallKind",
    "CallLimits",
    "CallOrigin",
    "CallOutcome",
    "CallRecord",
    "CallRefused",
    "CallRequest",
    "CallState",
    "CallStatus",
    "CallStore",
    "PgCallStore",
    "call_status",
    "cancel_call",
    "cancel_deployment",
    "find_call_by_key",
    "issue_call",
    "list_calls",
    "status_of",
]
