"""₹ amounts as `Decimal`, never `float` (DECISIONS §9).

Postgres stores amounts as `NUMERIC(12,2)`; Python carries them as `Decimal`
so parse → store → sum stays exact. `float` is refused at the door: once an
amount has been through binary floating point the paise are already gone, so
accepting one would launder a lost value into the ledger. Every amount that
enters the system goes through `parse_amount`; every amount shown to a user
goes through `format_amount`.
"""

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

# NUMERIC(12,2): ten digits before the point, two after → up to 9,999,999,999.99.
MAX_AMOUNT = Decimal("9999999999.99")
_PAISE = Decimal("0.01")


def parse_amount(value: str | int | Decimal) -> Decimal:
    """A positive ₹ amount as a `Decimal` quantized to paise.

    Accepts a string (optionally with a ₹ sign, commas, or surrounding space),
    an `int`, or a `Decimal`. Rejects `float` outright — see the module note.
    Raises `ValueError` if the value is not a number, is not positive, or does
    not fit `NUMERIC(12,2)`.
    """
    if isinstance(value, bool) or isinstance(value, float):
        # bool is an int subclass; a float has already lost precision.
        raise TypeError(f"amount must not be a {type(value).__name__}: {value!r}")

    if isinstance(value, str):
        cleaned = value.replace("₹", "").replace(",", "").strip()
        if not cleaned:
            raise ValueError("amount is empty")
        source: str | int = cleaned
    else:
        source = value  # int or Decimal — exact already

    try:
        amount = Decimal(source).quantize(_PAISE, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ValueError(f"not a valid amount: {value!r}") from None

    # Decimal("nan") is a *valid* Decimal and quantizes without raising, so it
    # slips past the InvalidOperation net above — but any comparison against it
    # (the `<= 0` below) then signals InvalidOperation uncaught. Reject here.
    if not amount.is_finite():
        raise ValueError(f"not a valid amount: {value!r}")
    if amount <= 0:
        raise ValueError(f"amount must be positive: {value!r}")
    if amount > MAX_AMOUNT:
        raise ValueError(f"amount exceeds NUMERIC(12,2): {value!r}")
    return amount


def format_amount(amount: Decimal) -> str:
    """`₹1,234.50` — the display form for the confirm card and dashboard.

    Thousands-grouped, always two decimals. Takes a `Decimal`; a `float` here
    would be the same precision leak `parse_amount` guards against.
    """
    if isinstance(amount, bool) or isinstance(amount, float):
        raise TypeError(f"amount must be a Decimal, not {type(amount).__name__}")
    return f"₹{Decimal(amount).quantize(_PAISE, rounding=ROUND_HALF_UP):,.2f}"


def demo() -> None:
    """Runnable self-check: the money path never touches float."""
    assert parse_amount("500") == Decimal("500.00")
    assert parse_amount("₹1,234.5") == Decimal("1234.50")
    assert parse_amount(500) == Decimal("500.00")
    assert parse_amount(Decimal("0.1")) + parse_amount(Decimal("0.2")) == Decimal("0.30")
    assert format_amount(Decimal("1234.5")) == "₹1,234.50"

    for bad in (10.0, 0.1, True):
        try:
            parse_amount(bad)
        except TypeError:
            pass
        else:
            raise AssertionError(f"float/bool accepted: {bad!r}")

    for bad in ("0", "-5", "abc", "", "1e12", "nan", "NaN"):
        try:
            parse_amount(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid amount accepted: {bad!r}")

    print("money demo ok")


if __name__ == "__main__":
    demo()
