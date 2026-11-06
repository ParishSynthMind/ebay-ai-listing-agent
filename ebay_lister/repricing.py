"""A small, predictable repricing rule set. Code only, no AI: price changes must be explainable.

For each SKU, in this order:
1. Start from the current price.
2. If competitor prices are known, aim just under the lowest one (undercut).
3. If it has not sold for stale_days (from the Fulfillment API's orders), drop it by stale_drop_pct.
4. Never move more than max_step_pct in one run, up or down.
5. End the price in .99 if asked (rounding up, not down, if down would cross the floor or the step).
6. Clamp to the ceiling and the floor. The floor always wins: the result is NEVER below it.
A SKU with no floor is skipped, not guessed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

DEFAULTS = {"undercut": 0.50, "max_step_pct": 10.0, "stale_days": 14, "stale_drop_pct": 5.0, "round_99": True,
            "floor": None, "ceiling": None, "min_change": 0.01}


@dataclass
class PriceDecision:
    sku: str
    old: float
    new: float
    reasons: list[str] = field(default_factory=list)
    skipped: str = ""

    @property
    def changed(self) -> bool:
        return not self.skipped and abs(self.new - self.old) >= 0.005


def _rules_for(sku: str, config: dict) -> dict:
    r = dict(DEFAULTS)
    r.update(config.get("defaults") or {})
    r.update((config.get("skus") or {}).get(sku) or {})
    return r


def _ninety_nine(x: float, up: bool) -> float:
    """The nearest price ending in .99 below x (or above x if up)."""
    c = math.floor(x) + 0.99
    if up:
        c = c if c >= x - 1e-9 else c + 1
    else:
        c = c if c <= x + 1e-9 else c - 1
    return round(c, 2) if c > 0 else round(x, 2)


def last_sold(orders: list[dict]) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for o in orders:
        when = datetime.fromisoformat(o["creationDate"].replace("Z", "+00:00"))
        for li in o.get("lineItems", []):
            sku = li.get("sku")
            if sku and (sku not in out or when > out[sku]):
                out[sku] = when
    return out


def decide(sku: str, current: float, config: dict, competitors: list[float] | None = None,
           sold_at: datetime | None = None, listed_at: datetime | None = None, now: datetime | None = None) -> PriceDecision:
    r = _rules_for(sku, config)
    d = PriceDecision(sku, current, current)
    floor, ceiling = r.get("floor"), r.get("ceiling")
    if floor is None:
        d.skipped = "no floor set for this SKU, so it is left alone"
        return d
    if ceiling is not None and ceiling < floor:
        d.skipped = f"ceiling {ceiling} is below floor {floor}; fix the rules"
        return d
    now = now or datetime.now(timezone.utc)
    target = current
    comps = sorted(c for c in (competitors or []) if c and c > 0)
    if comps:
        target = round(comps[0] - float(r["undercut"]), 2)
        d.reasons.append(f"lowest competitor {comps[0]:.2f}, undercut by {float(r['undercut']):.2f}")
    since = sold_at or listed_at
    if since is not None and (now - since).days >= int(r["stale_days"]):
        stale = round(current * (1 - float(r["stale_drop_pct"]) / 100), 2)
        if stale < target:
            target = stale
        d.reasons.append(f"no sale for {(now - since).days} days, drop {float(r['stale_drop_pct']):g}%")
    step = current * float(r["max_step_pct"]) / 100
    if abs(target - current) > step:
        target = round(current + math.copysign(step, target - current), 2)
        d.reasons.append(f"limited to a {float(r['max_step_pct']):g}% step")
    if r.get("round_99") and target != current:
        # .99 below the target first (stays under a competitor), else .99 above, as long as it keeps inside
        # the floor and the step; if neither does, leave it unrounded
        ok = [c for c in (_ninety_nine(target, up=False), _ninety_nine(target, up=True))
              if c >= floor and abs(c - current) <= step + 1e-9]
        target = ok[0] if ok else target
    if ceiling is not None and target > ceiling:
        target = float(ceiling)
        d.reasons.append(f"held at the ceiling {float(ceiling):.2f}")
    if target < floor:
        target = float(floor)
        d.reasons.append(f"held at the floor {float(floor):.2f}")
    if abs(target - current) < float(r["min_change"]):
        target = current
    d.new = round(target, 2)
    assert d.new >= floor, "repricing must never go below the floor"
    if not d.reasons:
        d.reasons.append("no rule applied")
    return d
