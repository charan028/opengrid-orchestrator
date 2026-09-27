"""core.services is the one owner of the service-type values: it and the og.contract CHECK Literal agree."""

from __future__ import annotations

from typing import get_args

from opengrid.core import services
from opengrid.core.models.engine import ServiceType


def test_the_constants_are_exactly_the_service_type_literal() -> None:
    assert get_args(ServiceType) == services.ALL_SERVICE_TYPES


def test_hold_services_are_the_as_award_and_the_regulated_toll() -> None:
    assert {"ERCOT_AS", "REGULATED_CAPACITY"} == services.HOLD_SERVICE_TYPES


def test_the_allocator_and_guardian_share_the_one_hold_set() -> None:
    from opengrid.allocator import models
    from opengrid.guardian import checks

    assert models.HOLD_SERVICE_TYPES is services.HOLD_SERVICE_TYPES
    assert checks.HOLD_SERVICE_TYPES is services.HOLD_SERVICE_TYPES
