"""Turn raw levels into ranked target zones.

Every price comes from a measured level (previous-day high/low, pivots, opening range, swing
support/resistance, session extremes) or, when too few exist, an explicit ATR projection.
Round numbers only add a little weight to a level that already exists.
"""
import math

ROUND_STEP = {"NIFTY": 50, "BANKNIFTY": 100}
MIN_DIST_U = 0.15      # nearest target must be at least this far (in 'u' units)
MAX_DIST_U = 1.30      # and no farther than this
ATR_PROJECTIONS = (0.35, 0.70, 1.00)

WEIGHTS = {"prev_high": 3, "prev_low": 3, "prev_close": 1.5, "pivot_P": 1.5, "pivot_1": 2, "pivot_2": 1,
           "pivot_3": 1, "opening_range": 2, "session_extreme": 2, "swing_sr_strong": 2, "swing_sr": 1}


def collect_candidates(prev: dict, pivots: dict, or_: dict, sr: dict, session_hi_lo=None) -> list:
    c = []
    if prev:
        c += [(prev["prev_high"], "Prev day high", "prev_high"), (prev["prev_low"], "Prev day low", "prev_low"),
              (prev["prev_close"], "Prev close", "prev_close")]
    for k, v in (pivots or {}).items():
        key = "pivot_P" if k == "P" else f"pivot_{k[1]}"
        c.append((v, f"Pivot {k}", key))
    if or_:
        c += [(or_["or_high"], "Opening range high", "opening_range"),
              (or_["or_low"], "Opening range low", "opening_range")]
    if session_hi_lo:
        c += [(session_hi_lo[0], "Session high", "session_extreme"), (session_hi_lo[1], "Session low", "session_extreme")]
    for side in ("supports", "resistances"):
        for lv in sr.get(side, []):
            strong = lv["touches"] >= 3
            c.append((lv["price"], f"Swing {'support' if side == 'supports' else 'resistance'} ({lv['touches']} touches)",
                      "swing_sr_strong" if strong else "swing_sr"))
    return [{"price": float(p), "source": s, "weight": WEIGHTS[k]} for p, s, k in c]


def merge(cands: list, tol: float, instrument: str) -> list:
    """Cluster levels closer than `tol`; strength = summed weights (confluence)."""
    out = []
    for c in sorted(cands, key=lambda x: x["price"]):
        if out and c["price"] - out[-1]["_hi"] <= tol:
            g = out[-1]
            g["members"].append(c)
            g["_hi"] = c["price"]
        else:
            out.append({"members": [c], "_hi": c["price"]})
    merged = []
    step = ROUND_STEP.get(instrument)
    for g in out:
        m = g["members"]
        w = sum(x["weight"] for x in m)
        price = sum(x["price"] * x["weight"] for x in m) / w
        strength = w
        sources = [x["source"] for x in m]
        if step:
            nearest = round(price / step) * step
            if abs(nearest - price) <= tol:
                strength += 0.5
                sources.append(f"Round number {nearest}")
        merged.append({"price": price, "strength": strength, "sources": sources})
    return merged


def build_ladder(merged: list, ref: float, direction: int, u: float, band_half: float, n: int = 3) -> list:
    """direction +1 = upside targets, -1 = downside. Returns up to n zones nearest-first."""
    pool = [c for c in merged if MIN_DIST_U * u <= direction * (c["price"] - ref) <= MAX_DIST_U * u
            and c["strength"] >= 1.5]
    pool.sort(key=lambda c: direction * (c["price"] - ref))
    chosen = []
    for c in pool:
        if all(abs(c["price"] - x["price"]) >= MIN_DIST_U * u for x in chosen):
            chosen.append(c)
        if len(chosen) == n:
            break
    for frac in ATR_PROJECTIONS:
        if len(chosen) == n:
            break
        p = ref + direction * frac * u
        if all(abs(p - x["price"]) >= 0.5 * MIN_DIST_U * u for x in chosen):
            chosen.append({"price": p, "strength": 0.0, "sources": [f"ATR projection ({frac:.2f} x expected range)"]})
    chosen.sort(key=lambda c: direction * (c["price"] - ref))
    return [{"label": f"T{i + 1}", "level": round(c["price"], 2), "zone_low": round(c["price"] - band_half, 2),
             "zone_high": round(c["price"] + band_half, 2), "distance": round(abs(c["price"] - ref), 2),
             "distance_in_u": round(abs(c["price"] - ref) / u, 2), "strength": round(c["strength"], 1),
             "sources": c["sources"]} for i, c in enumerate(chosen[:n])]
