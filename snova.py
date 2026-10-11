"""Magic Market — интеграция Super Nova (snova.me) для авто-оплаты картой.

Заменяет PayDome. Создаём заявку на обмен (Exchange Deal) под сумму в гривнах
на заданное направление оплаты (payment_settings) и получаем реквизиты для
показа клиенту сразу в ответе. Идентификатор заявки — наш собственный id
(передаём его сами в "id"), по нему же опрашивается статус.

Зачисляем ТОЛЬКО по статусу success/success_by_support (перепроверяем через
GetDeal-эквивалент — GET /api/v1/exchange/{id}/); вебхук — лишь триггер для
перепроверки, как и было с PayDome.

ENV:
  SNOVA_TOKEN             — Bearer-токен авторизации. Обязателен.
  SNOVA_PAYMENT_SETTINGS  — идентификатор направления оплаты картой в гривнах
                            (узнаётся через GET /api/v1/payment_settings/у провайдера).
  SNOVA_WEBHOOK_SECRET    — секрет для проверки подписи вебхука (заголовок SIGN).
                            Без него вебхук всё равно только триггерит
                            перепроверку статуса — ничего не зачисляется по
                            содержимому самого запроса.
"""
import hashlib
import hmac
import logging
import os

import aiohttp

log = logging.getLogger("snova")

TOKEN = os.getenv("SNOVA_TOKEN", "")
BASE = "https://snova.me"
PAYMENT_SETTINGS = os.getenv("SNOVA_PAYMENT_SETTINGS", "")
WEBHOOK_SECRET = os.getenv("SNOVA_WEBHOOK_SECRET", "")

PAID_STATUSES = {"success", "success_by_support"}


def enabled() -> bool:
    return bool(TOKEN and PAYMENT_SETTINGS)


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


async def create_exchange(deal_id: str, amount_uah: int, webhook_url: str = "",
                          user_uid: str | None = None) -> dict:
    """Создаёт заявку на обмен. Возвращает credentials/payment_amount/status."""
    payload = {
        "id": deal_id,
        "payment_settings": PAYMENT_SETTINGS,
        "needed_amount": amount_uah,
    }
    if webhook_url:
        payload["webhook"] = webhook_url
    if user_uid:
        payload["user_uid"] = user_uid
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{BASE}/api/v1/exchange/", json=payload, headers=_headers(),
                          timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
            if r.status not in (200, 201):
                raise ValueError(f"Snova exchange create {r.status}: {str(data)[:200]}")
    return data


async def payment_settings() -> list:
    """Список доступных направлений оплаты (для подбора SNOVA_PAYMENT_SETTINGS)."""
    if not TOKEN:
        raise ValueError("SNOVA_TOKEN не задан")
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{BASE}/api/v1/payment_settings/", headers=_headers(),
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
            if r.status != 200:
                raise ValueError(f"Snova payment_settings {r.status}: {str(data)[:200]}")
    return data


async def status(deal_id: str) -> dict | None:
    """Текущее состояние заявки (dict) или None при ошибке/недоступности."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BASE}/api/v1/exchange/{deal_id}/", headers=_headers(),
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception:
        log.warning("Snova status %s не получен", deal_id)
        return None


def verify_signature(url: str, raw_body: bytes, signature: str) -> bool:
    """HMAC-SHA256(secret, json_body + url) — как описано в доке вебхука Super Nova."""
    if not WEBHOOK_SECRET or not signature:
        return False
    mac = hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw_body + url.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, signature)
