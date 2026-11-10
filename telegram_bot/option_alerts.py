"""Telegram alerts for option paper signals, formatted as HTML: bold headings, coloured emojis and monospace <pre> tables
(Telegram has no real tables). Every message says PAPER SIGNAL. Duplicates are blocked by a persistent key (signal id + event)
so a restart or a repeated evaluation never sends the same alert twice."""
import html
import logging

log = logging.getLogger(__name__)
e = html.escape


def table(rows, header=None) -> str:
    """Monospace box table. rows = list of lists of strings. Returns text for use INSIDE <pre> (already escaped)."""
    allrows = ([header] if header else []) + rows
    w = [max(len(str(r[i])) for r in allrows) for i in range(len(allrows[0]))]
    line = lambda l, m, r: l + m.join("─" * (x + 2) for x in w) + r
    cell = lambda r: "│" + "│".join(f" {str(c):<{w[i]}} " if i == 0 else f" {str(c):>{w[i]}} " for i, c in enumerate(r)) + "│"
    out = [line("┌", "┬", "┐")]
    if header:
        out += [cell(header), line("├", "┼", "┤")]
    out += [cell(r) for r in rows]
    out.append(line("└", "┴", "┘"))
    return e("\n".join(out))


def _kv(rows) -> str:
    w = max(len(k) for k, _ in rows)
    return e("\n".join(f"{k:<{w}}  {v}" for k, v in rows))


def fmt_signal(sig: dict) -> str:
    o, u, p = sig["option"], sig["underlying"], sig["premium"]
    call = sig["action"] == "BUY CALL"
    dot, side = ("🟢", "CALL") if call else ("🔴", "PUT")
    t = p["targets"]
    rows = [["Entry", f"{p['entry']:.2f}", f"{u['entry']:.1f}"], ["Stop Loss", f"{p['stop']:.2f}", f"{u['stop']:.1f}"]]
    for i, x in enumerate(t):
        rows.append([f"Target {i + 1}", f"{x['premium']:.2f}", f"{u['targets'][i]['level']:.1f}"])
    levels = table(rows, ["", "OPTION", "INDEX"])
    rr = " / ".join(f"{x['rr']}" for x in t)
    info = _kv([("Contract", o["symbol"]), ("Strike", f"{o['strike']:.0f} {o['type']}   Expiry {o['expiry']}"),
                ("Risk:Reward", f"T1..T{len(t)} = {rr}"), ("Size", f"{p['lots']} lot x {p['lot_size']} = {p['quantity']} qty"),
                ("Max loss", f"Rs {p['max_loss_rupees']:,.0f}"), ("Score", f"{sig['score']}/100 ({sig['classification']})"),
                ("Regime", sig["regime_name"]), ("Pattern", sig["pattern"])])
    reasons = "\n".join(f"✅ {e(r)}" for r in sig["why_trade"][:6])
    return (f"{dot} <b>{e(sig['instrument'])} | BUY {side}</b>   📄 <b>PAPER SIGNAL</b>\n"
            f"🕙 {e(sig['time'][11:19])} IST     📍 Underlying <b>{sig['underlying_price']}</b>\n"
            f"<pre>{levels}</pre>\n<pre>{info}</pre>\n"
            f"<b>WHY THIS TRADE?</b>\n{reasons}\n\n"
            f"⚠️ <b>Invalidation:</b> {e(sig['invalidation'])}\n"
            f"⏳ Enter before {e(sig['entry_valid_until'][11:19])} IST\n"
            f"<i>Premium levels are delta/gamma estimates. No order is placed.</i>\n"
            f"📡 Data: {e(sig['data']['label'])}   🆔 <code>{e(sig['id'])}</code>")


EVENT_STYLE = {"ACTIVE": ("🟢", "ENTRY TRIGGERED"), "TARGET1_HIT": ("🎯", "TARGET 1 HIT"), "TARGET2_HIT": ("🏆", "TARGET 2 HIT"),
               "TARGET3_HIT": ("🏆", "TARGET 3 HIT"), "STOP_LOSS": ("🛑", "STOP LOSS HIT"),
               "INVALIDATED": ("⚠️", "SIGNAL INVALIDATED / EXIT"), "EXPIRED": ("⏱", "SIGNAL EXPIRED")}


def fmt_event(rec: dict, result: dict, premium_ltp: float, underlying: float, net=None) -> str:
    icon, head = EVENT_STYLE.get(result["status"], ("ℹ️", result["status"]))
    rows = [("Option", rec["option"]["symbol"]), ("Premium now", f"{premium_ltp}"), ("Entry / SL", f"{rec['premium']['entry']} / {rec['premium']['stop']}"),
            ("Underlying", f"{underlying}")]
    if net is not None:
        rows.append(("Paper P&L", f"Rs {net:,.0f} (after costs)"))
    tail = ""
    if net is not None:
        tail = f"\n{'🟩' if net > 0 else '🟥'} <b>Paper P&amp;L: Rs {net:,.0f}</b> after costs"
    elif result.get("final") and not rec["entered"]:
        tail = "\n<i>Never entered: not counted as a trade.</i>"
    return (f"{icon} <b>{e(rec['instrument'])} | {e(rec['action'])} | {head}</b>\n{e(result['event'] or '')}\n"
            f"<pre>{_kv(rows)}</pre>{tail}\n🆔 <code>{e(rec['id'])}</code>   📄 PAPER SIGNAL")


def fmt_setup(inst: str, candidate: str, why: list, score) -> str:
    return (f"🟡 <b>{e(inst)} | SETUP DETECTED</b> (not a trade yet)\nCandidate: <b>{e(candidate)}</b>   score {score}\n"
            "<b>Waiting for:</b>\n" + "\n".join(f"⏳ {e(w)}" for w in why[:5]) + "\n<i>PAPER only: wait for a CONFIRMED signal.</i>")


def fmt_data(state: str, inst: str, reason: str) -> str:
    icon = "🟢" if state == "RESTORED" else "🔴"
    return (f"{icon} <b>MARKET DATA {state}: {e(inst)}</b>\n{e(reason)}\n" +
            ("<i>Live signals resume.</i>" if state == "RESTORED" else "<i>Live signals are paused until the feed is LIVE again.</i>"))


class OptionNotifier:
    def __init__(self, client, ledger, extra_chat_ids=(), printer=print):
        self.client, self.ledger, self.extra, self.printer = client, ledger, tuple(extra_chat_ids), printer

    def send(self, key: str, text: str, now) -> bool:
        """Send once per key. Returns True if sent now. Never raises: a Telegram failure must not stop the market loop."""
        if self.ledger.alert_sent(key):
            return False
        self.printer(f"--- ALERT [{key}] ---\n{text}\n")
        ok = True
        if self.client is not None:
            try:
                self.client.send(text, parse_mode="HTML")    # your own chat: a failure here means "not sent", retried next step
            except Exception as ex:
                ok = False
                log.warning("Telegram alert failed (%s): %s", key, type(ex).__name__)
            if ok:
                for cid in self.extra:                       # extra people are best-effort and must not cause a re-send to you
                    if cid != self.client.chat_id:
                        try:
                            self.client.send(text, chat_id=cid, parse_mode="HTML")
                        except Exception as ex:
                            log.warning("Extra-chat alert failed (%s): %s", key, type(ex).__name__)
        if ok:
            self.ledger.mark_alert(key, now)
        return ok
