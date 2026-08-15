"""Money is `Decimal`, never `float` (DECISIONS §9). The failure these guard is
silent: a ledger that drifts by paise keeps returning plausible totals while
being wrong. So the tests assert the *effect* — that float is refused and that
parse → sum stays exact — not the spelling of the implementation.
"""

from decimal import Decimal

import pytest

from kanakko.money import MAX_AMOUNT, format_amount, parse_amount


def test_float_is_refused_at_the_door():
    # 0.1 is the canonical binary-float trap; if it slips in, the paise are
    # already gone before we ever quantize.
    with pytest.raises(TypeError):
        parse_amount(0.1)
    with pytest.raises(TypeError):
        parse_amount(10.0)
    # bool is an int subclass — exclude it explicitly, or True would parse as 1.
    with pytest.raises(TypeError):
        parse_amount(True)


def test_format_refuses_float_too():
    with pytest.raises(TypeError):
        format_amount(1234.5)


def test_parse_then_sum_is_exact():
    # The whole point of §9: this equals 0.30 with Decimal and 0.30000...4 with
    # float. The sum-by-category path adds many of these.
    total = sum(
        (parse_amount(x) for x in ("0.10", "0.20", "500", "₹1,234.5")),
        Decimal("0"),
    )
    assert total == Decimal("1734.80")
    assert isinstance(total, Decimal)


def test_quantizes_to_paise():
    assert parse_amount("10.5") == Decimal("10.50")
    assert parse_amount("10.005") == Decimal("10.01")  # ROUND_HALF_UP
    assert parse_amount(500) == Decimal("500.00")


def test_strips_symbol_and_commas():
    assert parse_amount("₹1,00,000") == Decimal("100000.00")
    assert parse_amount("  ₹ 42 ") == Decimal("42.00")


@pytest.mark.parametrize(
    "bad", ["0", "-5", "abc", "", "   ", "₹", "nan", "NaN", "-nan"]
)
def test_rejects_non_positive_and_garbage(bad):
    # "nan" is the sharp one: Decimal("nan") is valid and quantizes without
    # raising, so without the is_finite guard this leaks an uncaught
    # decimal.InvalidOperation (an ArithmeticError, not ValueError) out of the
    # single door every amount is supposed to pass through.
    with pytest.raises(ValueError):
        parse_amount(bad)


def test_allow_zero_admits_only_zero_not_negatives():
    assert parse_amount("0", allow_zero=True) == Decimal("0.00")
    with pytest.raises(ValueError):
        parse_amount("-5", allow_zero=True)


def test_rejects_over_numeric_12_2():
    assert parse_amount(str(MAX_AMOUNT)) == MAX_AMOUNT  # boundary is allowed
    with pytest.raises(ValueError):
        parse_amount("10000000000")  # 11 integer digits


def test_format_is_grouped_two_decimals():
    assert format_amount(Decimal("1234.5")) == "₹1,234.50"
    assert format_amount(Decimal("500")) == "₹500.00"
