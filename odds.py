from __future__ import annotations

import json
import os
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Period constants
# ---------------------------------------------------------------------------

PERIOD_KEYS: tuple[str, ...] = ("1d", "3d", "1w", "2w", "1mo", "3mo", "6mo", "1y")

_DEFAULT_PERIOD_SECONDS: dict[str, int] = {
    "1d":  86_400,
    "3d":  259_200,
    "1w":  604_800,
    "2w":  1_209_600,
    "1mo": 2_592_000,
    "3mo": 7_776_000,
    "6mo": 15_552_000,
    "1y":  31_536_000,
}

PERIOD_LABELS: dict[str, str] = {
    "1d":  "1日",
    "3d":  "3日",
    "1w":  "1週間",
    "2w":  "2週間",
    "1mo": "1か月",
    "3mo": "3か月",
    "6mo": "半年",
    "1y":  "1年",
}

PERIOD_MULT: dict[str, float] = {
    "1d":  1.0,
    "3d":  1.3,
    "1w":  1.7,
    "2w":  2.2,
    "1mo": 3.0,
    "3mo": 5.0,
    "6mo": 8.0,
    "1y":  15.0,
}

# Allow test overrides via env var
_override_raw = os.environ.get("PERIOD_SECONDS_OVERRIDE", "").strip()
if _override_raw:
    _overrides: dict[str, int] = json.loads(_override_raw)
    PERIOD_SECONDS: dict[str, int] = {**_DEFAULT_PERIOD_SECONDS, **_overrides}
else:
    PERIOD_SECONDS = _DEFAULT_PERIOD_SECONDS


# ---------------------------------------------------------------------------
# Pure calculation functions
# ---------------------------------------------------------------------------

def live_periods(elapsed_sec: float) -> list[str]:
    """Return period keys whose milestone has NOT yet been reached."""
    return [k for k in PERIOD_KEYS if PERIOD_SECONDS[k] > elapsed_sec]


def calc_weight(live_count: int) -> int:
    """Weight for a bet placed when `live_count` periods were still alive."""
    return live_count * live_count


class EntryInput(NamedTuple):
    entry_id: int
    period_key: str
    amount: int   # always 100
    weight: int


def find_winners(elapsed_sec: float, alive_periods: list[str]) -> list[str]:
    """
    Return the period key(s) from `alive_periods` whose milestone is closest
    to `elapsed_sec`.  May return 2 keys if exactly equidistant.
    """
    if not alive_periods:
        return []
    min_dist = min(abs(PERIOD_SECONDS[p] - elapsed_sec) for p in alive_periods)
    return [p for p in alive_periods if abs(PERIOD_SECONDS[p] - elapsed_sec) == min_dist]


def calc_payouts(
    entries: list[EntryInput],
    winners: list[str],
    elapsed_sec: float,
    total_pool: int,
) -> dict[int, int]:
    """
    Calculate payout for each entry.

    Returns a dict mapping entry_id -> payout (int).

    Edge cases:
    - No entries → empty dict
    - No winner (winners=[]) → empty dict
    - Winner period has no bets → return-of-stake for all entries
    - Tie with empty group → all pool goes to the populated group
    """
    if not entries or not winners:
        return {}

    # Check if winning period(s) have any bets at all
    winner_set = set(winners)
    winner_entries = [e for e in entries if e.period_key in winner_set]
    if not winner_entries:
        # Return stake to everyone
        return {e.entry_id: e.amount for e in entries}

    # Distance decay k
    # Use the single winner's seconds; for ties, there are two candidates.
    # We handle each winner group independently.
    result: dict[int, int] = {}

    if len(winners) == 1:
        w_sec = PERIOD_SECONDS[winners[0]]
        k = min(w_sec, elapsed_sec) / max(w_sec, elapsed_sec) if max(w_sec, elapsed_sec) > 0 else 0.0
        mult = PERIOD_MULT[winners[0]]
        _distribute_group(entries, winner_entries, total_pool, k, mult, result)
    else:
        # Tie: split pool by weight-bet sum ratio between the two groups
        w_short, w_long = winners[0], winners[1]  # sorted by PERIOD_KEYS order
        # Re-order to ensure short < long
        if PERIOD_SECONDS[w_short] > PERIOD_SECONDS[w_long]:
            w_short, w_long = w_long, w_short

        group_short = [e for e in entries if e.period_key == w_short]
        group_long  = [e for e in entries if e.period_key == w_long]

        wsum_short = sum(e.weight * e.amount for e in group_short)
        wsum_long  = sum(e.weight * e.amount for e in group_long)
        total_wsum = wsum_short + wsum_long

        if total_wsum == 0:
            return {e.entry_id: e.amount for e in entries}

        # Split pool proportionally; if one side empty, other gets all
        if not group_short:
            pool_long, pool_short = total_pool, 0
        elif not group_long:
            pool_short, pool_long = total_pool, 0
        else:
            pool_short = round(total_pool * wsum_short / total_wsum)
            pool_long  = total_pool - pool_short

        for w, group, pool in (
            (w_short, group_short, pool_short),
            (w_long,  group_long,  pool_long),
        ):
            if not group:
                continue
            w_sec = PERIOD_SECONDS[w]
            k = min(w_sec, elapsed_sec) / max(w_sec, elapsed_sec) if max(w_sec, elapsed_sec) > 0 else 0.0
            mult = PERIOD_MULT[w]
            _distribute_group(entries, group, pool, k, mult, result)

    # Loser entries get 0 payout
    for e in entries:
        if e.entry_id not in result:
            result[e.entry_id] = 0

    return result


def calc_best_case_payout(
    period_key: str,
    my_weighted_amount: int,
    group_weighted_amount: int,
    total_pool: int,
) -> int:
    """該当 period が k=1 で勝った場合の理論上限払戻。DB 非依存の純関数。"""
    if group_weighted_amount == 0:
        return 0
    mult = PERIOD_MULT[period_key]
    return round(mult * total_pool * my_weighted_amount / group_weighted_amount)


def _distribute_group(
    all_entries: list[EntryInput],
    group: list[EntryInput],
    pool: int,
    k: float,
    mult: float,
    result: dict[int, int],
) -> None:
    """Compute payout for entries in `group` and write into `result`."""
    total_wbet = sum(e.weight * e.amount for e in group)
    if total_wbet == 0:
        for e in group:
            result[e.entry_id] = 0
        return

    for e in group:
        base_share = pool * (e.weight * e.amount) / total_wbet
        payout = round(k * mult * base_share)
        result[e.entry_id] = payout
