"""
Money as integer minor units. There is no float in this module.

₹499 is 49900 paise. Every price, cost and total in Quintek's billing is an
`int` of the currency's smallest unit, and the only place a decimal appears is
`format()`, on its way to a screen.

The reason is not tidiness. `0.1 + 0.2 != 0.3` in binary floating point, and
an invoicing system that adds prices in floats accumulates error that
eventually lands in somebody's bill and cannot be explained. Integers are
exact, and the arithmetic below is closed over them.

Provider costs need finer resolution than a paise -- a single call can cost a
few thousandths of one -- so token pricing is carried in MICRO minor units
(millionths of a paise) and converted to paise only when totalling. Rounding
happens once, at the boundary, with the direction stated.
"""

from __future__ import annotations

from dataclasses import dataclass

# Minor units per major unit, per currency. INR: 100 paise to the rupee.
MINOR_PER_MAJOR = {"INR": 100, "USD": 100, "EUR": 100}
SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€"}

MICRO = 1_000_000     # micro-minor-units in one minor unit


class MoneyError(ValueError):
    pass


@dataclass(frozen=True)
class Money:
    """An exact amount. `minor` is paise for INR."""

    minor: int
    currency: str = "INR"

    def __post_init__(self):
        if not isinstance(self.minor, int) or isinstance(self.minor, bool):
            raise MoneyError(
                f"money must be an integer number of minor units, got {type(self.minor).__name__}"
                f" ({self.minor!r}). A float here is how rounding error reaches an invoice.")
        if self.currency not in MINOR_PER_MAJOR:
            raise MoneyError(f"unknown currency {self.currency!r}")

    # ---------- construction ----------

    @classmethod
    def from_major(cls, amount: int, currency: str = "INR") -> "Money":
        """`Money.from_major(499)` -> ₹499.00. Integers only, deliberately."""
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise MoneyError(
                "from_major takes whole major units; for a fractional amount pass minor "
                "units directly so the rounding is yours and not this function's")
        return cls(amount * MINOR_PER_MAJOR[currency], currency)

    @classmethod
    def zero(cls, currency: str = "INR") -> "Money":
        return cls(0, currency)

    # ---------- arithmetic ----------

    def _same(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise MoneyError(
                f"cannot combine {self.currency} and {other.currency}: there is no exchange "
                "rate in this module, and guessing one silently would be worse than failing")

    def __add__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.minor - other.minor, self.currency)

    def __mul__(self, factor: int) -> "Money":
        if not isinstance(factor, int) or isinstance(factor, bool):
            raise MoneyError("multiply money by an integer; use `scale` for a ratio")
        return Money(self.minor * factor, self.currency)

    def scale(self, numerator: int, denominator: int, *, round_up: bool = False) -> "Money":
        """
        Multiply by a ratio without leaving integers.

        Proration needs this: 17 days of a 30-day month at ₹499 is
        `price.scale(17, 30)`. The rounding direction is explicit because
        `round_up` is the difference between charging a customer an extra
        paise and absorbing it, and that should be a decision, not a default.
        """
        if denominator == 0:
            raise MoneyError("cannot scale by a zero denominator")
        total = self.minor * numerator
        quotient, remainder = divmod(total, denominator)
        if remainder and round_up:
            quotient += 1 if total > 0 else 0
        return Money(quotient, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.minor, self.currency)

    def __lt__(self, other: "Money") -> bool:
        self._same(other)
        return self.minor < other.minor

    def __le__(self, other: "Money") -> bool:
        self._same(other)
        return self.minor <= other.minor

    # ---------- presentation ----------

    @property
    def major(self) -> int:
        return self.minor // MINOR_PER_MAJOR[self.currency]

    def format(self, *, decimals: bool = True) -> str:
        per = MINOR_PER_MAJOR[self.currency]
        sign = "-" if self.minor < 0 else ""
        whole, part = divmod(abs(self.minor), per)
        symbol = SYMBOLS.get(self.currency, self.currency + " ")
        if not decimals and part == 0:
            return f"{sign}{symbol}{whole:,}"
        return f"{sign}{symbol}{whole:,}.{part:02d}"

    def __repr__(self) -> str:
        return f"Money({self.minor}, {self.currency!r})  # {self.format()}"


def token_cost_micro(tokens: int | None, price_per_million_micro: int | None) -> int:
    """
    Cost of `tokens` at a per-million price, in MICRO minor units.

    Kept in micro units so a call costing a fraction of a paise is not rounded
    to zero. Thousands of such calls are the entire AI cost line, and rounding
    each to zero would report it as free.
    """
    if not tokens or not price_per_million_micro:
        return 0
    return (tokens * price_per_million_micro) // 1_000_000


def micro_to_money(micro: int, currency: str = "INR", *, round_up: bool = True) -> Money:
    """
    Convert accumulated micro units to real money, rounding ONCE at the end.

    Rounds up by default: under-reporting what the AI cost is the more
    dangerous error for a business trying to protect a contribution margin.
    """
    quotient, remainder = divmod(abs(micro), MICRO)
    if remainder and round_up:
        quotient += 1
    return Money(-quotient if micro < 0 else quotient, currency)
