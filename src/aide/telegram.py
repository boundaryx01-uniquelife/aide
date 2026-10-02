from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, List, Optional, Sequence, Tuple

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # Telegram hard limit is 4096

# Rows of (label, callback_data) pairs. callback_data must be <= 64 bytes.
Buttons = Sequence[Sequence[Tuple[str, str]]]


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


def _call(method: str, token: str, params: dict, timeout: float = 15) -> Any:
    """POST to the Bot API and return `result`.

    Errors are re-raised WITHOUT the request URL, because the URL contains the bot token.
    """
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN 이 설정되지 않았습니다.")
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(API.format(token=token, method=method), data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise TelegramError(f"Telegram HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TelegramError(f"Telegram 연결 실패: {type(e).__name__}") from None
    except (ValueError, UnicodeDecodeError):
        raise TelegramError("Telegram 응답을 해석할 수 없습니다.") from None
    if not payload.get("ok"):
        raise TelegramError("Telegram 이 요청을 거부했습니다.")
    return payload.get("result")


def _keyboard(buttons: Buttons) -> str:
    return json.dumps(
        {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]},
        ensure_ascii=False,
    )


def send(text: str, *, token: str, chat_id: str, buttons: Optional[Buttons] = None) -> None:
    """Send plain text (no parse_mode, so markup inside untrusted titles is inert).

    If `buttons` is given they are attached to the LAST chunk of the message.
    """
    if not token or not chat_id:
        raise TelegramError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 설정되지 않았습니다.")
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        params = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}
        if buttons and i == len(chunks) - 1:
            params["reply_markup"] = _keyboard(buttons)
        _call("sendMessage", token, params)


def get_updates(token: str, offset: int = 0, timeout: int = 0) -> list:
    """Fetch pending button presses. `offset` acknowledges everything below it.

    Only callback_query updates are requested; plain chat messages are never delivered.
    """
    result = _call(
        "getUpdates",
        token,
        {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": json.dumps(["callback_query"]),
        },
        timeout=timeout + 10,
    )
    return result if isinstance(result, list) else []


def answer_callback(token: str, callback_id: str, text: str) -> None:
    _call("answerCallbackQuery", token, {"callback_query_id": callback_id, "text": text})


def clear_buttons(token: str, chat_id: str, message_id: int) -> None:
    """Remove the keyboard from a message so it cannot be pressed twice."""
    _call(
        "editMessageReplyMarkup",
        token,
        {"chat_id": chat_id, "message_id": message_id, "reply_markup": json.dumps({"inline_keyboard": []})},
    )
