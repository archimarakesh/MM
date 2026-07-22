"""Magic Market — интеграция PayDome (авто-выдача карты для оплаты).

Провайдер выдаёт карту под каждый платёж; зачисляем ТОЛЬКО по статусу Paid
(деньги реально поступили на сервис). Источник истины — GetPaymentStatus,
вебхук лишь триггерит перепроверку.

ENV:
  PAYDOME_TOKEN — токен авторизации (заголовок X-Token). Обязателен.
  PAYDOME_BASE  — базовый URL боевого API (напр. https://api.lieplefol.com). Обязателен.
                  test.lieplefol.com — только swagger/тест-стенд.
  PAYDOME_UNIT  — множитель суммы: 1 = целые гривны, 100 = копейки (по умолчанию 1).
                  ⚠️ ПРОВЕРИТЬ на боевом GetCard до запуска — ошибка = зачисление ×100!
  PAYDOME_TTL   — сколько секунд показывать карту (по умолчанию 1800 = 30 мин).
"""
import logging
import os

import aiohttp

log = logging.getLogger("paydome")

TOKEN = os.getenv("PAYDOME_TOKEN", "")
BASE = (os.getenv("PAYDOME_BASE", "") or "https://paymentchecker.lieplefol.com").rstrip("/")
UNIT = int(os.getenv("PAYDOME_UNIT", "1") or 1)      # 1=грн (проверено), 100=копейки
TTL = int(os.getenv("PAYDOME_TTL", "1800") or 1800)
UAH = 1                                              # FiatCurrency.UAH
STATUS_PAID = 2                                      # PaymentStatus.Paid


def enabled() -> bool:
    return bool(TOKEN and BASE)


def _headers() -> dict:
    return {"X-Token": TOKEN}


async def get_card(amount_uah: int) -> dict:
    """Запрос карты под сумму (в гривнах). Возвращает card, paymentId, amount (грн к оплате)."""
    params = {"amount": amount_uah * UNIT, "currency": UAH, "strict": "false"}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{BASE}/Payment/GetCard", params=params, headers=_headers(),
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                raise ValueError(f"PayDome GetCard {r.status}: {(await r.text())[:120]}")
            d = await r.json()
    raw = d.get("amount")
    # видно, уникализирует ли сервис сумму копейками: если raw дробное, а мы округляем —
    # клиент переведёт не ту сумму и платёж не распознается (номер карты не логируем)
    log.info("PayDome GetCard: отправили %s, вернулось amount=%r (%s), к оплате %s, payment=%s",
             amount_uah * UNIT, raw, type(raw).__name__, round((raw or 0) / UNIT),
             d.get("paymentId"))
    return {
        "card": d.get("card"),
        "payment_id": d.get("paymentId"),
        "pay_uah": round((raw or 0) / UNIT),   # сколько показать к оплате
    }


async def status(payment_id: str) -> int | None:
    """Текущий статус платежа (int) или None при ошибке."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BASE}/Payment/GetPaymentStatus",
                             params={"paymentId": payment_id}, headers=_headers(),
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return None
                return int(await r.json())
    except Exception:
        log.warning("PayDome status %s не получен", payment_id)
        return None


async def set_webhook(url: str) -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{BASE}/SetUrl", json={"callbackUrl": url}, headers=_headers(),
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                ok = r.status == 200
                if not ok:
                    log.warning("SetUrl %s: %s", r.status, (await r.text())[:120])
                return ok
    except Exception:
        log.warning("PayDome SetUrl не удалось")
        return False
