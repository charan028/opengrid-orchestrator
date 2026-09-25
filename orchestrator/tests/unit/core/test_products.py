from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.products import ProductRule, derive_variable_kind, is_feasible, round_quantity

CONTINUOUS = ProductRule(min_qty_kw=Decimal("0"), increment_kw=Decimal("0"), block=False)
SEMI = ProductRule(min_qty_kw=Decimal("0.1"), increment_kw=Decimal("0.1"), block=False)
BLOCK = ProductRule(min_qty_kw=Decimal("5"), increment_kw=Decimal("0"), block=True)


def test_variable_kind_mapping():
    assert derive_variable_kind(CONTINUOUS) == "CONTINUOUS"
    assert derive_variable_kind(SEMI) == "SEMI_CONTINUOUS"
    assert derive_variable_kind(BLOCK) == "BINARY"


def test_continuous_rounds_to_capped_value():
    assert round_quantity(Decimal("3.456"), CONTINUOUS, Decimal("10")) == Decimal("3.456")
    assert round_quantity(Decimal("20"), CONTINUOUS, Decimal("10")) == Decimal("10")


def test_semi_continuous_ercot_as_min_0_1_increment_0_1():
    # ERCOT AS product rule per the seed data: min 0.1 MW, increment 0.1
    assert round_quantity(Decimal("0.05"), SEMI, Decimal("10")) == Decimal("0")
    assert round_quantity(Decimal("0.15"), SEMI, Decimal("10")) == Decimal("0.1")
    assert round_quantity(Decimal("0.37"), SEMI, Decimal("10")) == Decimal("0.3")


def test_block_all_or_nothing():
    assert round_quantity(Decimal("4.9"), BLOCK, Decimal("10")) == Decimal("0")
    assert round_quantity(Decimal("5.0"), BLOCK, Decimal("10")) == Decimal("5")
    assert round_quantity(Decimal("100"), BLOCK, Decimal("4")) == Decimal("0")  # exceeds max_kw


def test_semi_continuous_with_zero_increment_is_effectively_min_qty_gated():
    rule = ProductRule(min_qty_kw=Decimal("2"), increment_kw=Decimal("0"), block=False)
    assert round_quantity(Decimal("5"), rule, Decimal("10")) == Decimal("5")
    assert round_quantity(Decimal("1"), rule, Decimal("10")) == Decimal("0")


def test_is_feasible():
    assert not is_feasible(Decimal("0.05"), SEMI, Decimal("10"))
    assert is_feasible(Decimal("0.2"), SEMI, Decimal("10"))


@given(
    requested=st.decimals(min_value="0", max_value="1000", places=3),
    max_kw=st.decimals(min_value="0", max_value="1000", places=3),
)
def test_round_quantity_never_exceeds_max_or_requested(requested, max_kw):
    result = round_quantity(requested, SEMI, max_kw)
    assert result <= max_kw
    assert result <= requested or result == Decimal("0")
    assert result >= Decimal("0")
