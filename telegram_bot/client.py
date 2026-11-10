"""Minimal Telegram Bot API client (sendMessage / getUpdates only). The bot token is never logged."""
import json
import logging
import re
import time

import requests

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000   # Telegram's hard limit is 4096 characters


class TelegramError(Exception):
    pass


def split_message(text: str, limit: int = MAX_LEN) -> list:
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            line, rest = line[:limit], line[limit:]
            parts.append(line)
            line = rest
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return [p.rstrip("\n") for p in parts if p.strip()]


class TelegramClient:
    def __init__(self, token: str, chat_id: str, session=None, retries: int = 3):
        if not token or not chat_id:
            raise TelegramError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        self._token, self.chat_id = token, str(chat_id)
        self.session = session or requests.Session()
        self.retries = retries

    def _redact(self, text: str) -> str:
        return str(text).replace(self._token, "<token>")

    def _call(self, method: str, http="post", http_timeout=15, **params):
        last = None
        for attempt in range(1, self.retries + 1):
            try:
                url = API.format(token=self._token, method=method)
                r = (self.session.post(url, json=params, timeout=http_timeout) if http == "post"
                     else self.session.get(url, params=params, timeout=http_timeout))
                data = r.json()
                if r.status_code == 429:
                    time.sleep(float(data.get("parameters", {}).get("retry_after", 2)))
                    continue
                if not data.get("ok"):
                    raise TelegramError(f"{method} failed: {data.get('description', r.status_code)}")
                return data["result"]
            except TelegramError:
                raise
            except (requests.RequestException, ValueError) as e:
                last = self._redact(e)
                log.warning("Telegram %s attempt %d/%d failed: %s", method, attempt, self.retries, last)
                time.sleep(min(2 ** attempt, 8))
        raise TelegramError(f"{method} unreachable after {self.retries} tries: {last}")

    def send(self, text: str, chat_id=None, parse_mode: str = None) -> int:
        """parse_mode='HTML' lets alerts use bold, emojis and <pre> tables. An HTML message is never split (that could cut a tag);
        if it is too long it is sent as plain text with the tags removed."""
        if parse_mode == "HTML":
            if len(text) > MAX_LEN:
                text, parse_mode = re.sub(r"<[^>]+>", "", text), None
            else:
                self._call("sendMessage", chat_id=chat_id or self.chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
                return 1
        n = 0
        for part in split_message(text):
            self._call("sendMessage", chat_id=chat_id or self.chat_id, text=part, disable_web_page_preview=True)
            n += 1
        return n

    def get_me(self) -> dict:
        """Checks that the token is valid (Telegram getMe). Never returns the token."""
        r = self._call("getMe", http="get")
        return {"username": r.get("username"), "name": r.get("first_name")}

    def get_updates(self, offset=None, timeout=20) -> list:
        params = {"timeout": timeout, "allowed_updates": json.dumps(["message"])}   # Telegram wants a JSON text
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", http="get", http_timeout=timeout + 10, **params)
