"""HTTP form of a refused dispatch call (`opengrid.calls.CallRefused`), shared by the operator route and
the utility customer API so both answer a refusal identically: the refusal's own status (404/409/422/429)
with `{"reason_code", "detail"}` and, when the refusal was recorded, its `call_id`."""

from __future__ import annotations

from fastapi import HTTPException

from opengrid.calls import CallRefused


def call_refused_http(exc: CallRefused) -> HTTPException:
    detail: dict[str, str] = {"reason_code": exc.reason_code, "detail": exc.detail}
    if exc.call is not None:
        detail["call_id"] = str(exc.call.call_id)
    return HTTPException(exc.http_status, detail=detail)
