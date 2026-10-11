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


async def balance() -> dict:
    """Доступный баланс USDT провайдера."""
    if not TOKEN:
        raise ValueError("SNOVA_TOKEN не задан")
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{BASE}/api/v1/balance/", headers=_headers(),
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
            if r.status != 200:
                raise ValueError(f"Snova balance {r.status}: {str(data)[:200]}")
    return data


async def withdraw_usdt(amount: float, wallet: str) -> dict:
    """Вывод USDT на кошелёк TRC-20 с баланса провайдера."""
    if not TOKEN:
        raise ValueError("SNOVA_TOKEN не задан")
    payload = {"amount": amount, "wallet": wallet}
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{BASE}/api/v1/balance/withdrawal/", json=payload, headers=_headers(),
                          timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
            if r.status not in (200, 201):
                raise ValueError(f"Snova withdrawal {r.status}: {str(data)[:200]}")
    return data


async def payout_create(currency: str, fiat_amount: float, card_number: str,
                        bank_name: str, cardholder_name: str, comment: str = "",
                        external_id: str | None = None) -> dict:
    """Вывод баланса в фиат прямо на банковскую карту (PayOut)."""
    if not TOKEN:
        raise ValueError("SNOVA_TOKEN не задан")
    payload = {
        "currency": currency, "fiat_amount": fiat_amount, "card_number": card_number,
        "bank_name": bank_name, "cardholder_name": cardholder_name,
    }
    if comment:
        payload["comment"] = comment
    if external_id:
        payload["external_id"] = external_id
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{BASE}/api/v1/payout/", json=payload, headers=_headers(),
                          timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
            if r.status not in (200, 201):
                raise ValueError(f"Snova payout create {r.status}: {str(data)[:200]}")
    return data


def verify_signature(url: str, raw_body: bytes, signature: str) -> bool:
    """HMAC-SHA256(secret, json_body + url) — как описано в доке вебхука Super Nova."""
    if not WEBHOOK_SECRET or not signature:
        return False
    mac = hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw_body + url.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, signature)
