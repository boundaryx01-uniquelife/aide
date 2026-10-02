from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import List

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 4000  # Telegram hard limit is 4096


class TelegramError(Exception):
    pass


def split_message(text: str, limit: int = MAX_LEN) -> List[str]:
    """Split on line boundaries so a long digest never exceeds Telegram's limit."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single absurdly long line
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def send(text: str, *, token: str, chat_id: str) -> None:
    """Send plain text (no parse_mode, so markup inside untrusted titles is inert).

    Errors are re-raised WITHOUT the request URL, because the URL contains the bot token.
    """
    if not token or not chat_id:
        raise TelegramError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 설정되지 않았습니다.")
    for chunk in split_message(text):
        body = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}
        ).encode("utf-8")
        req = urllib.request.Request(API.format(token=token), data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise TelegramError(f"Telegram HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TelegramError(f"Telegram 연결 실패: {type(e).__name__}") from None
        except (ValueError, UnicodeDecodeError):
            raise TelegramError("Telegram 응답을 해석할 수 없습니다.") from None
        if not payload.get("ok"):
            raise TelegramError("Telegram 이 요청을 거부했습니다.")
