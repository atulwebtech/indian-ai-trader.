import os

# Tests must not depend on the developer's real .env (extra Telegram people, signal profile).
# load_dotenv() never overrides variables that are already set, so pin them before anything imports settings.
os.environ["TELEGRAM_EXTRA_CHAT_IDS"] = ""
os.environ["SIGNAL_PROFILE"] = "standard"
os.environ["TRADES_ONLY"] = "false"

# Tests must never log in to Groww with the developer's real keys (that is a live network call and changes results).
os.environ["GROWW_API_KEY"] = ""
os.environ["GROWW_API_SECRET"] = ""
os.environ["GROWW_TOTP_SECRET"] = ""
