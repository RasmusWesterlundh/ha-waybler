"""Pure price optimization logic for the Waybler integration.

No Home Assistant dependencies — fully unit-testable.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import PriceEntry


def filter_upcoming(prices: list[PriceEntry], now: datetime) -> list[PriceEntry]:
    """Return price entries that start at or after *now*.

    Both *now* and entry timestamps must be timezone-aware.
    """
    return [p for p in prices if p.starts_at >= now]


def n_cheapest_hours(prices: list[PriceEntry], n: float) -> float | None:
    """Return the price ceiling that covers the *n* cheapest upcoming hours.

    Sorts entries by price ascending, takes the first ceil(n) entries, and
    returns the maximum price among them — i.e. the limit that ensures the
    charger runs during exactly those hours.

    Returns None if the price list is empty.
    """
    if not prices:
        return None
    count = math.ceil(n)
    cheapest = sorted(prices, key=lambda p: p.price)[:count]
    return max(p.price for p in cheapest)


def below_average(prices: list[PriceEntry]) -> float | None:
    """Return the mean price of all upcoming entries.

    Charging will run whenever the spot price is below the mean.
    Returns None if the price list is empty.
    """
    if not prices:
        return None
    return statistics.mean(p.price for p in prices)


def percentile(prices: list[PriceEntry], p: int) -> float | None:
    """Return the price at the *p*-th percentile (0–100).

    E.g. p=40 means the limit is set so the cheapest 40 % of hours will charge.
    Returns None if the price list is empty.
    """
    if not prices:
        return None
    sorted_prices = sorted(p_.price for p_ in prices)
    # Use linear interpolation between nearest ranks
    idx = (p / 100) * (len(sorted_prices) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_prices) - 1)
    frac = idx - lo
    return sorted_prices[lo] + frac * (sorted_prices[hi] - sorted_prices[lo])


def fixed(value: float) -> float:
    """Return *value* unchanged — passthrough for the fixed-limit strategy."""
    return value


def compute_price_limit(
    prices: list[PriceEntry],
    strategy: str,
    remaining_hours: float = 0.0,
    min_hours: float = 4.0,
    percentile_value: int = 40,
    fixed_limit: float | None = None,
) -> float | None:
    """Compute the optimal spot price limit for the given strategy.

    Args:
        prices: Upcoming price entries (already filtered to future hours).
        strategy: One of the STRATEGY_* constants from const.py.
        remaining_hours: Hours still needed today (used by n_cheapest only).
            If <= 0 the charge target is already met and None is returned.
        min_hours: Target charge hours for n_cheapest strategy.
        percentile_value: Percentile (0–100) for the percentile strategy.
        fixed_limit: Price ceiling for the fixed strategy.

    Returns:
        Computed price limit (float), or None if the target is already met /
        the price list is empty / no fixed limit was configured.
    """
    from .const import (  # local import to avoid circular at module level
        STRATEGY_BELOW_AVG,
        STRATEGY_FIXED,
        STRATEGY_N_CHEAPEST,
        STRATEGY_PERCENTILE,
    )

    if strategy == STRATEGY_N_CHEAPEST:
        if remaining_hours <= 0:
            return None
        return n_cheapest_hours(prices, remaining_hours)

    if strategy == STRATEGY_BELOW_AVG:
        return below_average(prices)

    if strategy == STRATEGY_PERCENTILE:
        return percentile(prices, percentile_value)

    if strategy == STRATEGY_FIXED:
        return fixed_limit  # may be None if unconfigured

    return None
