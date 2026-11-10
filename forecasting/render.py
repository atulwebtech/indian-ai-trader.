"""Plain-text rendering of a forecast (console, Telegram, reports)."""


def _mode_label(f: dict) -> str:
    return "LIVE DATA - PAPER TRADING ONLY" if f["source"] == "LIVE" else f"PAPER MODE - {f['source']} DATA"


def _targets(ts, arrow):
    lines = []
    for t in ts:
        r = t.get("historical_reach_rate")
        rate = f" | past days moved this far from the open: {r:.0%}" if r is not None else ""
        lines.append(f"  {t['label']}: {t['level']:.0f} (zone {t['zone_low']:.0f}-{t['zone_high']:.0f}){rate}")
    return "\n".join(lines) or "  (none)"


def render_text(f: dict) -> str:
    b, sc = f["bias"], f["scenarios"]
    o = [f"[{_mode_label(f)}] {f['instrument']} FORECAST - {f['session_date']} ({f['mode']})",
         f"Data as of: {f['data_as_of']}",
         f"{f['reference_note']}: {f['reference_price']:.2f}",
         f"Bias: {b['bias']} (score {b['score']:+d} of {b['of']}, conviction {b['conviction']})",
         f"Support {f['support']:.0f} | Resistance {f['resistance']:.0f}", ""]
    for key in ("bullish", "bearish"):
        s = sc[key]
        o += [f"{s['name']} SCENARIO [{s['role']}]", f" Trigger: {s['trigger']}", _targets(s["targets"], key),
              f" Invalidation: {s['invalidation']}", ""]
    s = sc["sideways"]
    o += [f"SIDEWAYS SCENARIO [{s['role']}]", f" {s['trigger']}", f" Invalidation: {s['invalidation']}", ""]
    o.append("REVERSAL ZONES (need confirmation - touching is not enough)")
    for z in f["reversal_zones"]:
        o.append(f"  {z['type']} {z['zone'][0]:.0f}-{z['zone'][1]:.0f}: {'; '.join(z['needs'][:2])}")
    v = f["volatility"]
    o += ["", f"Volatility: daily ATR {v['daily_atr']:.0f}, intraday ATR {v['intraday_atr']:.0f}, range {v['range_state']}",
          f"Expected range for the rest of the day: {v['expected_range_low']:.0f} - {v['expected_range_high']:.0f}"]
    if not f["volume"]["available"]:
        o.append("Volume: unavailable for spot index")
    w = f.get("typical_extreme_windows")
    if w:
        hi = max(w["high_by_window"], key=w["high_by_window"].get)
        lo = max(w["low_by_window"], key=w["low_by_window"].get)
        o.append(f"History ({w['days']} days): day high most often forms {hi} ({w['high_by_window'][hi]:.0%}), low {lo} ({w['low_by_window'][lo]:.0%})")
    o += ["", f.get("disclaimer", "")]
    return "\n".join(o)
