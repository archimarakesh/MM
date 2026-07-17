"""Проверка подписи Telegram WebApp initData (HMAC-SHA256)."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE = 24 * 3600  # сутки


def validate(init_data: str, bot_token: str) -> dict | None:
    """Возвращает объект user из initData или None, если подпись невалидна."""
    if not init_data or not bot_token:
        return None
    data = dict(parse_qsl(init_data))
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash):
        return None
    auth_date = int(data.get("auth_date", 0))
    if auth_date and time.time() - auth_date > MAX_AGE:
        return None
    try:
        return json.loads(data.get("user", ""))
    except (ValueError, TypeError):
        return None
