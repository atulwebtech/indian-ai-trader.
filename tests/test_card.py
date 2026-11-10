from telegram_bot import messages as M
from tests.test_hindi import fake_signal, make_forecast
from tests.test_telegram import T, make_bot


def test_trade_card_has_fixed_layout_in_both_languages():
    f, ds, _ = make_forecast()
    now = T("2026-01-29 11:02")
    for action, hi_word, en_word in (("BUY", "CALL (CE) LO", "TAKE CALL (CE)"), ("SELL", "PUT (PE) LO", "TAKE PUT (PE)")):
        s = fake_signal(action)
        hi, en = M.signal_message(s, f, ds, now, "hi"), M.signal_message(s, f, ds, now, "en")
        assert hi_word in hi and en_word in en
        for needle in ("📍 Entry zone", "🛑 Stop loss", "🎯 Target", "🚪 Exit:", "Aaj ka mahaul", "Kyun:", "Kab galat:"):
            assert needle in hi, needle
        for needle in ("📍 Entry zone", "🛑 Stop loss", "🎯 Target", "🚪 Exit:", "Today's mood", "Why:", "Invalid if:"):
            assert needle in en, needle
        assert hi.count("━") == en.count("━") > 0


def test_mood_headline_says_positive_negative_or_sideways():
    f, ds, _ = make_forecast()
    for bias, word in (("BULLISH", "POSITIVE"), ("BEARISH", "NEGATIVE"), ("NEUTRAL", "SIDEWAYS")):
        g = {**f, "bias": {**f["bias"], "bias": bias}}
        assert word in M.mood_line(g, "hi") and word in M.mood_line(g, "en")
        assert "guarantee nahi" in M.mood_line(g, "hi")
    msg = M.forecast_message(f, ds, T("2026-01-29 11:02"), title="PRE-MARKET FORECAST", lang="hi")
    assert "AAJ KA MAHAUL" in msg


def test_status_updates_tell_you_what_to_do_to_exit():
    s, now = fake_signal("BUY"), T("2026-01-29 11:30")
    assert "stop entry par" in M.status_update_message(s, "TARGET1_HIT", now, "hi")
    assert "nikal jayein" in M.status_update_message(s, "STOPPED", now, "hi")
    assert "move the remaining stop to entry" in M.status_update_message(s, "TARGET1_HIT", now, "en")


def test_plan_has_both_sides_and_targets_lie_beyond_the_entry():
    import re
    f, ds, _ = make_forecast()
    now = T("2026-01-29 11:02")
    for lang in ("hi", "en"):
        m = M.plan_message(f, ds, now, lang)
        assert "CALL (CE) PLAN" in m and "PUT (PE) PLAN" in m and "SIDEWAYS" in m and "📍 Entry level" in m
        assert m.count("🛑 Stop loss") == 4 and "🎯" in m          # two plans + two reverse trades
        # every target line is on the profit side of its entry level
        side, entry = None, None
        for line in m.splitlines():
            if "CALL (CE) PLAN" in line:
                side = 1
            elif "PUT (PE) PLAN" in line:
                side = -1
            elif "PALTA TRADE" in line or "REVERSE TRADE" in line:
                side = -1 if "PUT (PE) idea" in line or "PUT (PE) opens" in line else 1
            e = re.match(r"📍 Entry level: (\d+)", line)
            if e:
                entry = float(e.group(1))
            t = re.match(r"🎯 (?:T\d: )?(\d+)", line)
            if t and entry is not None:
                assert side * (float(t.group(1)) - entry) > 0, line
        assert "SHART WALA" in m or "CONDITIONAL" in m


def test_plan_command_and_jobs_exist(tmp_path):
    from tests.test_telegram import make_bot
    bot, _ = make_bot(tmp_path, now="2026-01-29 11:00")
    for lang in ("hi", "en"):
        bot.set_lang(lang)
        out = bot.handle_command("/plan")
        assert out.count("TRADE PLAN") == 2 and "Maaf" not in out and "Sorry" not in out
    from telegram_bot.bot import JOBS
    assert {"plan_open", "plan_mid", "plan_pm"} <= {j[0] for j in JOBS}


def test_flip_trade_is_the_opposite_side_with_entry_at_the_failed_stop():
    import re
    f, ds, _ = make_forecast()
    for lang in ("hi", "en"):
        put = "\n".join(M.flip_block(f, True, 23025.0, 23100.0, lang))         # a CALL was stopped at 23025
        call = "\n".join(M.flip_block(f, False, 23030.0, 22990.0, lang))       # a PUT was stopped at 23030
        assert "PUT (PE)" in put.splitlines()[0] and "CALL (CE)" in call.splitlines()[0]
        assert "📍 Entry level: 23025" in put and "📍 Entry level: 23030" in call
        stop_p = float(re.search(r"🛑 Stop loss: (\d+)", put).group(1))
        stop_c = float(re.search(r"🛑 Stop loss: (\d+)", call).group(1))
        assert stop_p > 23025 > 0 and stop_c < 23030                           # reverse stops sit beyond the new entry
        assert all(float(t) < 23025 for t in re.findall(r"🎯 (?:T\d: )?(\d+)", put))
        assert all(float(t) > 23030 for t in re.findall(r"🎯 (?:T\d: )?(\d+)", call))
    m = M.plan_message(f, ds, T("2026-01-29 11:02"), "hi")
    assert m.count("PALTA TRADE") == 2


def test_stop_out_message_carries_the_reverse_trade(tmp_path):
    from tests.test_telegram import make_bot
    bot, _ = make_bot(tmp_path, now="2026-01-29 11:00")
    bot.set_lang("hi")
    t = bot._flip_text("NIFTY", {**fake_signal("BUY"), "direction": "BUY", "stop": 23025.0}, bot.clock())
    assert "PALTA TRADE" in t and "PUT (PE)" in t and "salaah nahi" in t


def test_check_parser_reads_free_text():
    from telegram_bot.check import parse
    p = parse("/check nifty call 23300 sl 23270 target 23360")
    assert p == {"instrument": "NIFTY", "side": 1, "entry": 23300.0, "stop": 23270.0, "target": 23360.0}
    p = parse("BankNifty PUT at 54800 stoploss 54860 t 54700")
    assert p["instrument"] == "BANKNIFTY" and p["side"] == -1 and (p["entry"], p["stop"], p["target"]) == (54800.0, 54860.0, 54700.0)
    assert parse("nifty put")["entry"] is None
    assert parse("hello there") == {} and parse("23300 sl 23270") == {}


def test_check_command_replies_in_both_languages(tmp_path):
    from tests.test_telegram import make_bot
    bot, _ = make_bot(tmp_path, now="2026-01-29 11:00")
    px = float(bot._data("NIFTY")[0].close.iloc[-1])
    msg = f"nifty call {px:.0f} sl {px - 40:.0f} target {px + 80:.0f}"
    for lang, needles in (("hi", ("IDEA KI JAANCH", "Checklist:", "Risk", "salaah nahi")), ("en", ("IDEA CHECK", "Checklist:", "Risk", "not advice"))):
        bot.set_lang(lang)
        for text in ("/check " + msg, msg):                      # with and without the slash
            out = bot.handle_command(text)
            for n in needles:
                assert n in out, (lang, n, out)
            assert "✅" in out or "❌" in out or "⚠️" in out
    assert "/check" in bot.handle_command("/check")              # usage hint
    wrong = bot.handle_command(f"/check nifty call {px:.0f} sl {px + 40:.0f}")
    assert "galat taraf" in wrong or "wrong side" in wrong


def test_extra_person_gets_alerts_and_their_own_answers(tmp_path):
    from dataclasses import replace
    from tests.test_telegram import make_bot

    class Rec:
        chat_id = "42"

        def __init__(self):
            self.log, self.updates = [], []

        def send(self, text, chat_id=None):
            self.log.append((chat_id or "42", text))
            return 1

        def get_updates(self, offset=None, timeout=5):
            u, self.updates = self.updates, []
            return u

    bot, _ = make_bot(tmp_path, now="2026-01-29 11:00")
    bot.client = Rec()
    bot.settings = replace(bot.settings, telegram_extra_chat_ids=("777",))
    bot._send("hello alert", key="k1")
    assert [c for c, _ in bot.client.log] == ["42", "777"]
    assert bot._send("hello alert", key="k1") is False                      # still de-duplicated
    bot.client.log.clear()
    bot.client.updates = [{"update_id": 1, "message": {"chat": {"id": 777}, "text": "/status"}},
                          {"update_id": 2, "message": {"chat": {"id": 999}, "text": "/status"}},     # a stranger
                          {"update_id": 3, "message": {"chat": {"id": 42}, "text": "/help"}}]
    bot.process_updates()
    assert [c for c, _ in bot.client.log] == ["777", "42"]                   # each answer goes to the asker; the stranger gets nothing


def test_extra_ids_are_read_from_env(monkeypatch):
    from config.settings import load_settings
    monkeypatch.setenv("TELEGRAM_EXTRA_CHAT_IDS", " 111, 222 ,")
    assert load_settings().telegram_extra_chat_ids == ("111", "222")


def test_myid_answers_anyone_with_only_their_own_number(tmp_path):
    from tests.test_telegram import make_bot
    bot, _ = make_bot(tmp_path, now="2026-01-29 11:00")
    got = []
    bot.client.send = lambda text, chat_id=None: got.append((chat_id, text)) or 1
    bot.client.updates = [{"update_id": 1, "message": {"chat": {"id": 555}, "text": "/myid"}},
                          {"update_id": 2, "message": {"chat": {"id": 555}, "text": "/status"}}]
    bot.process_updates()
    assert len(got) == 1 and got[0][0] == "555" and "555" in got[0][1]       # the stranger got their id, nothing else


def test_trades_only_mode_schedules_only_scans(tmp_path):
    from dataclasses import replace
    bot, clock = make_bot(tmp_path, now="2026-01-29 09:40")
    assert any(not j.startswith("scan:") for j in bot.due_jobs(clock["now"]))
    bot.settings = replace(bot.settings, trades_only=True)
    assert all(j.startswith("scan:") for j in bot.due_jobs(clock["now"]))


def test_compact_card_has_only_the_trade():
    out = M.compact_signal_message(fake_signal("SELL"), T("2026-01-29 11:02"), "en")
    for needle in ("PUT (PE)", "Entry 22985 - 23000", "Stop loss 23030", "T1 22950 | T2 22905"):
        assert needle in out, needle
    assert len(out.splitlines()) <= 6 and "Backtest" not in out and "Support" not in out
