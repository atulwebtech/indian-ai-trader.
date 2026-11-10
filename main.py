"""Entry point for the Indian AI intraday forecasting assistant.

Usage:
  ./run.sh                      show status
  ./run.sh forecast             forecast for NIFTY and BANKNIFTY (real delayed data)
  ./run.sh forecast --data mock use fake test data instead
  ./run.sh signal               BUY / SELL / WAIT / NO TRADE check on the latest candle
  ./run.sh telegram-test        send one test message to your Telegram
  ./run.sh telegram             run the Telegram bot (keep the terminal open)
  ./run.sh backtest             long historical validation (about 15 minutes)
  ./run.sh calendar             refresh the official NSE holiday list
  ./run.sh groww-check          test the (read-only) Groww connection once you have API keys
  ./run.sh dashboard            local web dashboard at http://127.0.0.1:8050
  ./run.sh paper-report         results of the paper-trading ledger
"""
import argparse
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler

from config.settings import IST, ROOT, load_settings
from market_data.loader import load_data


def setup_logging() -> None:
    handler = RotatingFileHandler(ROOT / "logs" / "app.log", maxBytes=1_000_000, backupCount=3)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[handler])


def session_for_now(intra):
    """Before today's open (weekday, before 09:30) forecast today; otherwise use the last session."""
    now = datetime.now(IST)
    if now.date() > intra.index[-1].date() and now.weekday() < 5 and now.time() < datetime(2000, 1, 1, 9, 30).time():
        return now.date()
    return None


def cmd_forecast(mode: str) -> None:
    from forecasting.daily_forecast import generate_forecast
    from forecasting.render import render_text
    for inst in ("NIFTY", "BANKNIFTY"):
        intra, daily = load_data(inst, mode)
        f = generate_forecast(inst, intra, daily, session_date=session_for_now(intra))
        print(render_text(f))
        print("\n" + "=" * 78 + "\n")


def cmd_signal(mode: str) -> None:
    from risk_management.gate import SignalGate, params_for
    from strategies.signal_engine import evaluate
    gate = SignalGate(params_for(load_settings().signal_profile), path=ROOT / "data" / "signal_state.json")
    for inst in ("NIFTY", "BANKNIFTY"):
        intra, daily = load_data(inst, mode)
        s = evaluate(inst, intra, daily, gate)
        print(f"[{s['mode_label']}] {inst} @ {s['time'][:16]}: {s['action']}")
        for r in s.get("reasons", [])[:4]:
            print("   -", r)
        if s["action"] in ("BUY", "SELL"):
            print(f"   entry zone {s['entry_zone']}  stop {s['stop']}  targets {[t['level'] for t in s['targets']]}")
            print(f"   R:R {s['rr_t1']}/{s['rr_t2']}  quality {s['quality_score']} (not a win probability)")
        print()


def cmd_telegram(settings, mode: str, test_only: bool) -> None:
    from dataclasses import replace
    from telegram_bot.bot import Bot
    from telegram_bot.client import TelegramClient, TelegramError
    settings = replace(settings, data_mode=mode)   # label must reflect the data actually used
    try:
        client = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
        if test_only:
            text = (f"[{settings.mode_label}]\nTest message from Indian AI Trader / Indian AI Trader ka test message. "
                    f"Agar yeh padh pa rahe ho to Telegram jud gaya hai. Koi order kabhi nahi lagta.")
            client.send(text)
            print("Test message sent to your own chat.")
            for k, cid in enumerate(settings.telegram_extra_chat_ids, 1):
                try:
                    client.send(text, chat_id=cid)
                    print(f"Test message sent to extra person #{k}.")
                except TelegramError as e:
                    print(f"Extra person #{k}: NOT delivered ({e}). They must open the bot, press Start and send /myid first, "
                          f"and the number in .env must be exactly the one /myid showed.")
            print("Check Telegram.")
            return
        print("Telegram bot running. Press Ctrl+C to stop.")
        Bot(client, settings, mode=mode).run_forever()
        print("Stopped.")
    except TelegramError as e:
        print(f"Telegram problem: {e}")


def cmd_calendar() -> None:
    from market_data.calendar import CalendarError, NSECalendar, refresh
    try:
        d = refresh()
        print(f"Saved {len(d['days'])} official NSE holidays for {d['years']} to data/nse_holidays.json")
    except CalendarError as e:
        print(f"Could not refresh: {e}")
    cal = NSECalendar()
    today = datetime.now(IST).date()
    print(f"Today ({today}): {cal.day_type(today)}" + ("" if cal.verified(today) else "  [UNVERIFIED: list does not cover this year]"))


def cmd_groww_check(settings) -> None:
    """Read-only connection test. Does nothing until Groww credentials are in .env."""
    from groww_integration.auth import GrowwAuthError, GrowwSession
    from groww_integration.instruments import InstrumentError, load_master, nearest_future, validate_indices
    try:
        sess = GrowwSession(settings.groww_api_key, settings.groww_api_secret, settings.groww_totp_secret)
        client = sess.connect()
        master = load_master(client)
        idx = validate_indices(master)
        print("Groww login OK (read-only mode). Instrument master loaded:", len(master), "rows")
        for name, row in idx.items():
            fut = nearest_future(master, name)
            print(f"  {name}: {row['groww_symbol']} (token {row['exchange_token']}); nearest future "
                  f"{fut['trading_symbol'] if fut else 'none found'}")
    except (GrowwAuthError, InstrumentError) as e:
        print(f"Groww check did not pass: {e}")


def cmd_option_signal(settings, live: bool) -> None:
    """Option-buying decision (paper only). Needs the explicit --live flag; DATA_MODE in .env is never changed."""
    if not live:
        print("option-signal evaluates LIVE Groww data (read-only, paper signal). Add --live to confirm: ./run.sh option-signal --live")
        return
    from groww_integration.auth import GrowwAuthError, GrowwSession
    from option_live.runner import LiveOptionRunner, render
    try:
        sess = GrowwSession(settings.groww_api_key, settings.groww_api_secret, settings.groww_totp_secret)
        sess.connect()
    except GrowwAuthError as e:
        print(f"DATA UNAVAILABLE: {e}")
        return
    r = LiveOptionRunner(sess)
    r.start_feed()
    try:
        for inst in r.instruments:
            print(render(r.evaluate(inst)), "\n")
        print("Feed status:", r.feed.status())
    finally:
        r.feed.stop()


def cmd_option_monitor(settings, live: bool) -> None:
    """Continuous paper-signal monitor with Telegram alerts and a dashboard state file. Needs --live; never trades."""
    if not live:
        print("option-monitor runs continuously on LIVE Groww data (paper signals only). Confirm with: ./run.sh option-monitor --live")
        return
    from groww_integration.auth import GrowwAuthError, GrowwSession
    from option_live.monitor import Monitor
    from option_live.runner import LiveOptionRunner
    from paper_trading.option_ledger import OptionLedger
    from telegram_bot.client import TelegramClient, TelegramError
    from telegram_bot.option_alerts import OptionNotifier
    try:
        sess = GrowwSession(settings.groww_api_key, settings.groww_api_secret, settings.groww_totp_secret)
        sess.connect()
    except GrowwAuthError as e:
        print(f"DATA UNAVAILABLE: {e}")
        return
    client = None
    try:
        client = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
    except TelegramError:
        print("Telegram is not configured: alerts are printed here only.")
    ledger = OptionLedger()
    r = LiveOptionRunner(sess)
    r.start_feed()
    mon = Monitor(r, ledger, OptionNotifier(client, ledger, settings.telegram_extra_chat_ids))
    print(f"Option monitor running (PAPER). Capital {r.params.capital}, max risk/trade {r.gate.max_risk_rupees:.0f}. Ctrl+C to stop.")
    try:
        mon.run_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        r.feed.stop()


def cmd_option_backtest(settings, args) -> None:
    from backtesting.option_backtest import run_option_backtest
    run_option_backtest(settings, months=args.months, iv=args.iv)


def main() -> None:
    setup_logging()
    settings = load_settings()
    ap = argparse.ArgumentParser(description="Indian AI Trader (paper mode only, never places orders)")
    ap.add_argument("command", nargs="?", default="status", choices=["status", "forecast", "signal", "telegram", "telegram-test", "backtest", "calendar", "groww-check", "dashboard", "paper-report", "option-signal", "option-monitor", "option-backtest"])
    ap.add_argument("--data", choices=["mock", "historical"], default="historical",
                    help="historical = real delayed Yahoo candles (default); mock = fake test data")
    ap.add_argument("--live", action="store_true", help="option-signal: confirm you want to evaluate live Groww data")
    ap.add_argument("--months", type=int, default=6, help="option-backtest: months of 1-minute history to try to fetch")
    ap.add_argument("--iv", type=float, default=15.0, help="option-backtest: assumed implied volatility %% for MODELLED premiums")
    args = ap.parse_args()
    mode = args.data.upper()

    if args.command == "status":
        print(f"Indian AI Trader ready. Mode: {settings.mode_label}. Orders: DISABLED.")
        print("Try:  ./run.sh forecast     or     ./run.sh signal")
    elif args.command == "forecast":
        cmd_forecast(mode)
    elif args.command == "signal":
        cmd_signal(mode)
    elif args.command == "calendar":
        cmd_calendar()
    elif args.command == "option-monitor":
        cmd_option_monitor(settings, args.live)
    elif args.command == "option-backtest":
        cmd_option_backtest(settings, args)
    elif args.command == "option-signal":
        cmd_option_signal(settings, args.live)
    elif args.command == "groww-check":
        cmd_groww_check(settings)
    elif args.command == "dashboard":
        from dashboard.app import run_dashboard
        run_dashboard(settings, mode)
    elif args.command == "paper-report":
        from paper_trading.report import print_report
        print_report()
    elif args.command == "backtest":
        from backtesting.run import run_all
        run_all()
        print("Report saved in the reports/ folder.")
    else:
        cmd_telegram(settings, mode, test_only=args.command == "telegram-test")


if __name__ == "__main__":
    main()
