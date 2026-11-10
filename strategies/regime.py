"""Market regime classification. Picks which strategy family is allowed; unclear conditions return CHOPPY (no trade)."""
import numpy as np

BREAKOUT_STRATEGIES = ("TRENDING_BULLISH", "TRENDING_BEARISH", "BREAKOUT", "BREAKDOWN")
REVERSAL_STRATEGIES = ("REVERSAL", "SIDEWAYS", "TRENDING_BULLISH", "TRENDING_BEARISH")   # reversals only vs. exhaustion in trends


def classify_regime(adx: float, plus_di: float, minus_di: float, ema_stack: str, structure: str, atr_ratio: float,
                    bb_width_pct: float, bb_width_median: float, broke_level: str = None, reversal_confirmed: bool = False) -> dict:
    """ema_stack: BULLISH/BEARISH/MIXED; structure: UPTREND/DOWNTREND/MIXED/UNDEFINED;
    atr_ratio: short-term ATR / longer ATR; broke_level: 'UP'/'DOWN' if price just closed beyond a major level."""
    notes = []
    vol = "HIGH_VOLATILITY" if atr_ratio >= 1.4 else "LOW_VOLATILITY" if atr_ratio <= 0.7 else "NORMAL_VOLATILITY"
    expanding = atr_ratio >= 1.1 or (bb_width_median and bb_width_pct > 1.1 * bb_width_median)
    trending = adx is not None and not np.isnan(adx) and adx >= 22
    if reversal_confirmed:
        regime = "REVERSAL"
        notes.append("Confirmed reversal pattern at a level")
    elif broke_level and (trending or expanding):
        # ADX lags: a break out of a quiet range is confirmed by trend strength OR by volatility expansion
        regime = "BREAKOUT" if broke_level == "UP" else "BREAKDOWN"
    elif trending and ema_stack == "BULLISH" and plus_di > minus_di and structure in ("UPTREND", "MIXED"):
        regime = "TRENDING_BULLISH"
    elif trending and ema_stack == "BEARISH" and minus_di > plus_di and structure in ("DOWNTREND", "MIXED"):
        regime = "TRENDING_BEARISH"
    elif adx is not None and not np.isnan(adx) and adx < 18 and bb_width_pct <= bb_width_median:
        regime = "SIDEWAYS"
    else:
        regime = "CHOPPY"
        notes.append("Trend strength, EMA stack and structure do not agree")
    allowed = {"TRENDING_BULLISH": ["BREAKOUT", "RETEST", "REJECTION_WITH_TREND"],
               "TRENDING_BEARISH": ["BREAKOUT", "RETEST", "REJECTION_WITH_TREND"],
               "BREAKOUT": ["BREAKOUT", "RETEST"], "BREAKDOWN": ["BREAKOUT", "RETEST"],
               "REVERSAL": ["REJECTION", "REVERSAL"], "SIDEWAYS": ["REJECTION"], "CHOPPY": []}[regime]
    return {"regime": regime, "volatility": vol, "allowed_setups": allowed, "notes": notes}
