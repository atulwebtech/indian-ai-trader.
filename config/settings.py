"""Central settings. Secrets come only from the .env file."""
import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

IST = ZoneInfo("Asia/Kolkata")

# NSE regular cash-market session (IST)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
OPENING_RANGE_MINUTES = 15

# Timeframes supported by the data engine
TIMEFRAMES = ("5m", "15m", "1h", "1d")

# Instruments we analyse. Exchange tokens / trading symbols for Groww are NOT
# stored here; they must be read from Groww's official instrument master
# and validated at connection time.
INSTRUMENTS = ("NIFTY", "BANKNIFTY")

VALID_DATA_MODES = ("MOCK", "HISTORICAL", "LIVE")


@dataclass(frozen=True)
class Settings:
    data_mode: str
    telegram_bot_token: str
    telegram_chat_id: str
    groww_api_key: str
    groww_api_secret: str
    orders_enabled: bool = False  # hard-wired off, see below
    groww_totp_secret: str = ""
    telegram_lang: str = "hi"   # "hi" = Hinglish, "en" = English; can be changed in Telegram with /lang
    trades_only: bool = False               # True: Telegram sends only trade signals and their stop/target updates
    signal_profile: str = "standard"        # standard = strict; active = up to 5 signals a day, looser rules
    telegram_extra_chat_ids: tuple = ()     # more people who also get alerts and may use commands

    @property
    def mode_label(self) -> str:
        """Label that must appear on every alert and dashboard page."""
        return "LIVE DATA - PAPER TRADING ONLY" if self.data_mode == "LIVE" else f"PAPER MODE ({self.data_mode} DATA)"


def load_settings() -> Settings:
    mode = os.getenv("DATA_MODE", "MOCK").upper()
    if mode not in VALID_DATA_MODES:
        raise ValueError(f"DATA_MODE must be one of {VALID_DATA_MODES}, got {mode!r}")
    # ORDERS_ENABLED in .env is ignored on purpose: this project never trades.
    return Settings(
        data_mode=mode,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        trades_only=os.getenv("TRADES_ONLY", "false").strip().lower() in ("1", "true", "yes", "on"),
        signal_profile=os.getenv("SIGNAL_PROFILE", "standard").strip().lower(),
        telegram_extra_chat_ids=tuple(x.strip() for x in os.getenv("TELEGRAM_EXTRA_CHAT_IDS", "").split(",") if x.strip()),
        groww_api_key=os.getenv("GROWW_API_KEY", ""),
        groww_api_secret=os.getenv("GROWW_API_SECRET", ""),
        groww_totp_secret=os.getenv("GROWW_TOTP_SECRET", ""),
        telegram_lang=os.getenv("TELEGRAM_LANG", "hi").lower() if os.getenv("TELEGRAM_LANG", "hi").lower() in ("hi", "en") else "hi",
        orders_enabled=False,
    )
