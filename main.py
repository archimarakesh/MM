"""Magic Market — FastAPI (фронт + API + админка) + aiogram-бот в одном процессе (Railway)."""
import asyncio
import base64
import html
import io
import logging
import os
import random
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import qrcode
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response

import auth
import db
import guard_bot
import paydome

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mm")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
# публичный https-адрес: APP_URL или автоматический домен Railway
_railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
APP_URL = os.getenv("APP_URL", "") or (f"https://{_railway_domain}" if _railway_domain else "")

MAX_RECEIPT_LEN = 6_000_000  # ~4.5 МБ файла в base64

# приветственный бонус за подписку на канал и чат (бот должен быть админом в обоих)
BONUS_CHANNEL_ID = os.getenv("BONUS_CHANNEL_ID", "")
BONUS_CHAT_ID = os.getenv("BONUS_CHAT_ID", "")
BONUS_AMOUNT = int(os.getenv("BONUS_AMOUNT", "100") or 100)

# автопостинг промо в канал (по умолчанию — в бонусный канал)
KYIV = ZoneInfo("Europe/Kyiv")
PROMO_CHANNEL_ID = os.getenv("PROMO_CHANNEL_ID", "") or BONUS_CHANNEL_ID
# кнопка под постом: после /newapp в BotFather поставь https://t.me/Magic_Marketplace_bot/shop —
# будет открывать мини-апп сразу (web_app-кнопки в каналах Telegram не разрешает)
PROMO_BUTTON_URL = os.getenv("PROMO_BUTTON_URL", "https://t.me/Magic_Marketplace_bot/shop")
# 4 поста в день: каждое из двух промо — по 2 раза (чередование по слотам)
PROMO_TIMES = [t.strip() for t in os.getenv("PROMO_TIMES", "10:00,14:00,18:00,21:00").split(",") if t.strip()]
PROMO_POSTS = [
    ("promo/egrow-1.png", ""),
    ("promo/egrow-4.png", ""),
]
CARD_LIMIT = 5000            # оплата картой — до 5 000 ₴, свыше только крипта
# сервисный сбор на авто-оплату картой (PayDome берёт 12% — часть перекладываем на юзера).
# 5% сверху ≈ покрывает ~4.4 п.п. из 12% комиссии; остальное на нас. Меняется env CARD_FEE_PCT.
CARD_FEE_PCT = float(os.getenv("CARD_FEE_PCT", "5") or 5)


def _receipt_ok(receipt: str) -> bool:
    return (receipt.startswith(("data:image/", "data:application/pdf"))
            and len(receipt) <= MAX_RECEIPT_LEN)

# ── криптовалюты для авто-счетов ─────────────────────────────────────────────
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
ETHERSCAN_KEY = os.getenv("ETHERSCAN_API_KEY", "")  # один ключ на ETH и BNB (Etherscan V2)
# dec — десятичные ончейн; disp — знаков в показываемой сумме; chain — тип проверки
CRYPTO = {
    "trc20": {"gecko": "tether", "label": "USDT TRC-20", "wallet_key": "wallet_trc20",
              "dec": 6, "disp": 3, "chain": "trc20"},
    "ton":   {"gecko": "the-open-network", "label": "GRAM", "wallet_key": "wallet_ton",
              "dec": 9, "disp": 4, "chain": "ton"},
    "trx":   {"gecko": "tron", "label": "TRX", "wallet_key": "wallet_trx",
              "dec": 6, "disp": 2, "chain": "trx"},
    "btc":   {"gecko": "bitcoin", "label": "BTC", "wallet_key": "wallet_btc",
              "dec": 8, "disp": 8, "chain": "utxo",
              "api": "https://mempool.space/api", "scheme": "bitcoin"},
    "eth":   {"gecko": "ethereum", "label": "ETH", "wallet_key": "wallet_eth",
              "dec": 18, "disp": 6, "chain": "evm", "chainid": 1},
    "bnb":   {"gecko": "binancecoin", "label": "BNB", "wallet_key": "wallet_bnb",
              "dec": 18, "disp": 6, "chain": "evm", "chainid": 56},
    "ltc":   {"gecko": "litecoin", "label": "LTC", "wallet_key": "wallet_ltc",
              "dec": 8, "disp": 8, "chain": "utxo",
              "api": "https://litecoinspace.org/api", "scheme": "litecoin"},
}
_rates: dict = {"ts": 0.0, "data": {}}


async def get_rates() -> dict:
    """Курс UAH за 1 монету по всем валютам (CoinGecko, кэш 2 минуты)."""
    if time.time() - _rates["ts"] < 120 and _rates["data"]:
        return _rates["data"]
    ids = ",".join(sorted({m["gecko"] for m in CRYPTO.values()}))
    async with aiohttp.ClientSession() as s:
        async with s.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": ids, "vs_currencies": "uah"},
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json()
    _rates["data"] = {k: float(data[m["gecko"]]["uah"])
                      for k, m in CRYPTO.items() if data.get(m["gecko"])}
    _rates["ts"] = time.time()
    return _rates["data"]


async def crypto_amount(currency: str, uah: int) -> str:
    """Сумма в крипте с уникальным «хвостом» — по ней распознаём платёж."""
    meta = CRYPTO[currency]
    rate = (await get_rates()).get(currency)
    if not rate:
        raise Exception("нет курса")
    disp = meta["disp"]
    base = uah / rate
    taken = await db.pending_amounts(currency)
    span = 10 ** min(disp, 4)
    for _ in range(120):
        amt = f"{base + random.randint(1, span - 1) / (10 ** disp):.{disp}f}"
        if amt not in taken:
            return amt
    raise ValueError("Не удалось создать счёт, попробуйте ещё раз")


def qr_data_url(text: str) -> str:
    img = qrcode.make(text, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def invoice_public(inv: dict, with_qr: bool = True) -> dict:
    cur = inv["currency"]
    meta = CRYPTO.get(cur, {})
    if meta.get("chain") == "utxo":
        payload = f'{meta["scheme"]}:{inv["address"]}?amount={inv["amount_crypto"]}'
    else:
        payload = inv["address"]
    d = {"id": inv["id"], "currency": cur, "label": meta.get("label", cur),
         "amount_uah": inv["amount_uah"], "amount_crypto": inv["amount_crypto"],
         "address": inv["address"], "status": inv["status"],
         "expires": inv["expires"].isoformat()}
    if with_qr and inv["status"] == 0:
        d["qr"] = qr_data_url(payload)
    return d


async def _check_invoice(s: aiohttp.ClientSession, inv: dict) -> str | None:
    """Ищет входящий платёж с точной суммой. Возвращает txid или None.
    Любая ошибка/неизвестная сеть → None (никогда не зачисляет по ошибке)."""
    cur = inv["currency"]
    meta = CRYPTO.get(cur)
    if not meta:
        return None
    addr = inv["address"]
    want = int(round(float(inv["amount_crypto"]) * 10 ** meta["dec"]))
    t0 = inv["created"].timestamp() - 3600
    since_ms = str(int(inv["created"].timestamp() * 1000) - 60000)
    chain = meta["chain"]
    try:
        if chain == "trc20":
            url = f"https://api.trongrid.io/v1/accounts/{addr}/transactions/trc20"
            params = {"only_to": "true", "limit": "50", "contract_address": USDT_CONTRACT,
                      "min_timestamp": since_ms}
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = (await r.json()).get("data", [])
            for t in data:
                if t.get("to") == addr and int(t.get("value", 0)) == want:
                    return t.get("transaction_id", "ok")
        elif chain == "trx":
            url = f"https://api.trongrid.io/v1/accounts/{addr}/transactions"
            params = {"only_to": "true", "limit": "50", "min_timestamp": since_ms}
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = (await r.json()).get("data", [])
            for t in data:
                c = (t.get("raw_data", {}).get("contract") or [{}])[0]
                if c.get("type") != "TransferContract":
                    continue
                if int(c.get("parameter", {}).get("value", {}).get("amount", 0)) == want:
                    return t.get("txID", "ok")
        elif chain == "utxo":
            async with s.get(f'{meta["api"]}/address/{addr}/txs',
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                txs = await r.json()
            for tx in txs:
                st = tx.get("status", {})
                if not st.get("confirmed") or st.get("block_time", 0) < t0:
                    continue
                for v in tx.get("vout", []):
                    if v.get("scriptpubkey_address") == addr and v.get("value") == want:
                        return tx.get("txid", "ok")
        elif chain == "ton":
            async with s.get("https://toncenter.com/api/v2/getTransactions",
                             params={"address": addr, "limit": "30"},
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = (await r.json()).get("result", [])
            for tx in data:
                if tx.get("utime", 0) < t0:
                    continue
                if int(tx.get("in_msg", {}).get("value", 0)) == want:
                    return tx.get("transaction_id", {}).get("hash", "ok")
        elif chain == "evm":
            if not ETHERSCAN_KEY:
                return None
            async with s.get("https://api.etherscan.io/v2/api",
                             params={"chainid": meta["chainid"], "module": "account",
                                     "action": "txlist", "address": addr, "sort": "desc",
                                     "apikey": ETHERSCAN_KEY},
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                res = (await r.json()).get("result", [])
            if isinstance(res, list):
                for t in res:
                    if (t.get("to") or "").lower() != addr.lower():
                        continue
                    if t.get("isError") != "0":
                        continue
                    if int(t.get("timeStamp", 0)) < t0:
                        continue
                    if int(t.get("value", 0)) == want:
                        return t.get("hash", "ok")
    except Exception:
        log.warning("Проверка счёта #%s (%s) не удалась", inv["id"], cur)
    return None


async def _settle(inv: dict, txid: str) -> bool:
    res = await db.invoice_paid(inv["id"], txid)
    if not res:
        return False
    if res["order_code"]:
        await notify(res["user_id"],
                     f"✅ Заказ <b>{res['order_code']}</b> оплачен — принят в работу.")
        await notify(ADMIN_ID,
                     f"₿ <b>Заказ {res['order_code']} оплачен криптой</b> "
                     f"({inv['amount_crypto']} {CRYPTO[inv['currency']]['label']} = {res['amount']} ₴). "
                     f"Админка → Заказы.")
    else:
        await notify(res["user_id"],
                     f"✅ Оплата получена — баланс пополнен на <b>{res['amount']} ₴</b>.")
        await notify(ADMIN_ID,
                     f"₿ Крипто-пополнение: счёт #{inv['id']}, +{res['amount']} ₴ "
                     f"({inv['amount_crypto']} {CRYPTO[inv['currency']]['label']}).")
    return True


# ── трекер Новой Почты ───────────────────────────────────────────────────────
NP_API = "https://api.novaposhta.ua/v2.0/json/"
NP_RECEIVED = {"9", "10", "11"}  # «Отримано» в разных вариантах


async def np_tracker():
    """Раз в 30 минут проверяет ТТН заказов «В пути» и ставит «Получен»."""
    while True:
        try:
            orders = await db.shipped_orders()
            if orders:
                payload = {
                    "apiKey": os.getenv("NP_API_KEY", ""),
                    "modelName": "TrackingDocument",
                    "calledMethod": "getStatusDocuments",
                    "methodProperties": {"Documents": [
                        {"DocumentNumber": o["ttn"], "Phone": ""} for o in orders]},
                }
                async with aiohttp.ClientSession() as s:
                    async with s.post(NP_API, json=payload,
                                      timeout=aiohttp.ClientTimeout(total=20)) as r:
                        data = (await r.json()).get("data", []) or []
                by_ttn = {str(d.get("Number", "")): str(d.get("StatusCode", "")) for d in data}
                for o in orders:
                    if by_ttn.get(o["ttn"]) in NP_RECEIVED and await db.mark_delivered(o["id"]):
                        await notify(o["user_id"],
                                     f"🎉 Заказ <b>{o['code']}</b> получен! "
                                     "Будем рады вашей оценке ★ в «Истории».")
                        await notify(ADMIN_ID, f"📬 Заказ {o['code']} получен (по данным НП).")
        except Exception:
            log.exception("Ошибка трекера Новой Почты")
        await asyncio.sleep(1800)


async def invoice_checker():
    """Фоновая проверка неоплаченных счетов раз в минуту."""
    while True:
        try:
            pend = await db.pending_invoices()
            if pend:
                async with aiohttp.ClientSession() as s:
                    for inv in pend:
                        txid = await _check_invoice(s, inv)
                        if txid:
                            await _settle(inv, txid)
        except Exception:
            log.exception("Ошибка проверщика счетов")
        await asyncio.sleep(60)


async def post_promo(path: str, caption: str):
    """Публикация одного промо-поста в канал с кнопкой на мини-апп."""
    from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛍 Открыть Magic Market", url=PROMO_BUTTON_URL),
    ]])
    await bot.send_photo(int(PROMO_CHANNEL_ID), FSInputFile(path),
                         caption=caption or None, parse_mode="HTML", reply_markup=kb)


async def promo_poster():
    """Автопостинг промо в канал: 4 слота в день, каждая картинка по 2 раза."""
    if not (bot and PROMO_CHANNEL_ID and PROMO_TIMES):
        log.warning("Промо-постинг выключен: нет бота или PROMO_CHANNEL_ID")
        return
    while True:
        try:
            now = datetime.now(KYIV)
            slots = []
            for i, t in enumerate(PROMO_TIMES):
                hh, mm = map(int, t.split(":"))
                slots.append((now.replace(hour=hh, minute=mm, second=0, microsecond=0), i))
            future = [(dt, i) for dt, i in slots if dt > now]
            if future:
                nxt, idx = min(future)
            else:
                nxt = min(dt for dt, _ in slots) + timedelta(days=1)
                idx = 0
            await asyncio.sleep(max(5, (nxt - now).total_seconds()))
            slot_key = nxt.strftime("%Y-%m-%d %H:%M")
            if await db.get_kv("last_promo") == slot_key:
                continue  # уже постили в этот слот (например, после рестарта)
            path, caption = PROMO_POSTS[idx % len(PROMO_POSTS)]
            if os.path.exists(path):
                await post_promo(path, caption)
                await db.set_kv("last_promo", slot_key)
                log.info("Промо-пост отправлен: %s (%s)", path, slot_key)
            else:
                log.warning("Промо-файл не найден: %s", path)
        except Exception:
            log.exception("Ошибка промо-постинга")
            await asyncio.sleep(300)


async def card_checker():
    """Страховка к вебхуку: раз в 30с опрашиваем неоплаченные card-платежи."""
    if not paydome.enabled():
        return
    while True:
        try:
            for p in await db.card_payment_pending():
                await _card_settle(p["payment_id"])
        except Exception:
            log.exception("Ошибка проверщика card-платежей")
        await asyncio.sleep(30)


async def grow_harvester():
    """Раз в 5 минут двигает стадии программ по расписанию; на сборе — выплаты."""
    while True:
        try:
            for g in await db.advance_grow_stages():
                await notify(g["user_id"],
                             f"🧺 Урожай «{g['name']}» собран! "
                             f"Выплата <b>{g['payout']} ₴</b> зачислена на баланс.")
        except Exception:
            log.exception("Ошибка стадий E-growing")
        await asyncio.sleep(300)

# ── бот ──────────────────────────────────────────────────────────────────────
bot = dp = None
if BOT_TOKEN:
    from aiogram import BaseMiddleware, Bot, Dispatcher
    from aiogram.filters import Command, CommandObject, CommandStart
    from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                               MenuButtonWebApp, Message, WebAppInfo)

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    class ThrottleMiddleware(BaseMiddleware):
        """Троттлинг апдейтов на пользователя: гасит флуд /start и сообщений,
        не отвечая и не трогая БД (защита от DDoS через спам команд)."""
        async def __call__(self, handler, event, data):
            uid = getattr(getattr(event, "from_user", None), "id", None)
            if uid and uid != ADMIN_ID and not rate_limit(f"bot:{uid}", 3, 8):
                return  # >3 сообщений за 8 сек от одного юзера — молча дропаем
            return await handler(event, data)

    dp.message.middleware(ThrottleMiddleware())

    @dp.message(CommandStart())
    async def cmd_start(message: Message, command: CommandObject):
        u = message.from_user
        ref_by = None
        if command.args and command.args.startswith("ref_"):
            try:
                ref_by = int(command.args[4:])
            except ValueError:
                pass
        await db.upsert_user(u.id, u.full_name, u.username, ref_by)
        kb = None
        if APP_URL:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🛍 Открыть Magic Market",
                                     web_app=WebAppInfo(url=APP_URL)),
            ]])
        await message.answer(
            "Добро пожаловать в <b>Magic Market</b> ✦\n"
            "Магазин открывается по кнопке ниже.",
            parse_mode="HTML", reply_markup=kb)

    @dp.message(Command("id"))
    async def cmd_id(message: Message):
        await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>",
                             parse_mode="HTML")

    @dp.message(Command("chatid"))
    async def cmd_chatid(message: Message):
        await message.answer(f"ID этого чата: <code>{message.chat.id}</code>",
                             parse_mode="HTML")

    @dp.message(Command("promo"))
    async def cmd_promo(message: Message):
        if not ADMIN_ID or message.from_user.id != ADMIN_ID:
            return
        if not PROMO_CHANNEL_ID:
            await message.answer("Канал не задан: добавьте PROMO_CHANNEL_ID или BONUS_CHANNEL_ID.")
            return
        sent = 0
        for path, caption in PROMO_POSTS:
            if not os.path.exists(path):
                await message.answer(f"Файл не найден: {path}")
                continue
            try:
                await post_promo(path, caption)
                sent += 1
            except Exception as e:
                await message.answer(f"Ошибка постинга {path}: {e}")
        await message.answer(f"✅ Опубликовано постов: {sent}/{len(PROMO_POSTS)}")

    @dp.channel_post()
    async def channel_chatid(message: Message):
        if (message.text or "").strip().startswith("/chatid"):
            await message.answer(f"ID этого канала: <code>{message.chat.id}</code>",
                                 parse_mode="HTML")


async def notify(chat_id: int, text: str):
    """Уведомление в Telegram; ошибки не роняют API."""
    if not bot or not chat_id:
        return
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        log.warning("Не удалось отправить уведомление %s", chat_id)


# ── приложение ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init()
    task = menu_task = None
    if dp:
        log.info("APP_URL = %r, ADMIN_ID = %r", APP_URL, ADMIN_ID)

        async def _set_menu():
            # в фоне, чтобы не задерживать приём запросов на старте
            if not APP_URL:
                log.warning("APP_URL/RAILWAY_PUBLIC_DOMAIN не заданы — кнопка WebApp не будет показана")
                return
            try:
                await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                    text="Magic Market", web_app=WebAppInfo(url=APP_URL)))
                log.info("Кнопка меню WebApp установлена")
            except Exception:
                log.exception("Не удалось установить кнопку меню")

        menu_task = asyncio.create_task(_set_menu())
        task = asyncio.create_task(dp.start_polling(bot))
        log.info("Бот запущен (polling)")
    else:
        log.warning("BOT_TOKEN не задан — бот не запущен, API без авторизации не работает")
    if paydome.enabled() and APP_URL:
        asyncio.create_task(paydome.set_webhook(f"{APP_URL}/api/paydome/webhook"))
        log.info("PayDome включён, вебхук: %s/api/paydome/webhook", APP_URL)
    checker = asyncio.create_task(invoice_checker())
    tracker = asyncio.create_task(np_tracker())
    harvester = asyncio.create_task(grow_harvester())
    poster = asyncio.create_task(promo_poster())
    guard = asyncio.create_task(guard_bot.run())  # отдельный бот-охранник чата, свой токен
    card_task = asyncio.create_task(card_checker())
    yield
    checker.cancel()
    tracker.cancel()
    harvester.cancel()
    poster.cancel()
    guard.cancel()
    card_task.cancel()
    if menu_task:
        menu_task.cancel()
    if task:
        task.cancel()


app = FastAPI(title="Magic Market", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)


def tg_user(request: Request) -> dict:
    user = auth.validate(request.headers.get("X-Init-Data", ""), BOT_TOKEN)
    if not user:
        raise HTTPException(401, "Невалидные данные Telegram")
    return user


def client_ip(request: Request) -> str:
    # за прокси Railway реальный IP — ПОСЛЕДНИЙ в X-Forwarded-For (его дописывает
    # доверенный прокси); первые элементы клиент может подделать сам
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else ""


def esc(s) -> str:
    """HTML-escape пользовательских строк для Telegram-уведомлений (parse_mode=HTML)."""
    return html.escape(str(s or ""))


def pint(v, default: int = 0) -> int:
    """Безопасный int из пользовательского ввода — без 500 на мусоре."""
    try:
        return int(v)
    except (TypeError, ValueError):
        raise HTTPException(400, "Неверное числовое значение")


# простой антибрут для чувствительных действий: {ключ: [метки времени]}
_rl: dict = defaultdict(list)


def rate_limit(key: str, limit: int, window: int) -> bool:
    """True — можно; False — превышен лимит за окно (секунды)."""
    now = time.time()
    if len(_rl) > 50_000:            # защита от разрастания памяти под флудом
        for k in [k for k, v in _rl.items() if not v or now - v[-1] > 3600]:
            _rl.pop(k, None)
    hits = [t for t in _rl[key] if now - t < window]
    _rl[key] = hits
    if len(hits) >= limit:
        return False
    hits.append(now)
    return True


def client_device(request: Request) -> str:
    return request.headers.get("x-device", "")[:64]


def admin_user(request: Request) -> dict:
    u = tg_user(request)
    if not ADMIN_ID or u["id"] != ADMIN_ID:
        raise HTTPException(403, "Доступ только для владельца")
    return u


async def _snap(uid: int) -> dict:
    snap = await db.snapshot(uid)
    snap["is_admin"] = bool(ADMIN_ID) and uid == ADMIN_ID
    snap["bonus_offer"] = (not snap.get("bonus_claimed")
                           and bool(bot and BONUS_CHANNEL_ID and BONUS_CHAT_ID))
    snap["bonus_amount"] = BONUS_AMOUNT
    snap["card_auto"] = paydome.enabled()
    snap["card_fee"] = CARD_FEE_PCT if paydome.enabled() else 0
    return snap


@app.get("/")
async def index():
    return FileResponse("index.html")


_IMMUTABLE = {"Cache-Control": "public, max-age=86400"}


@app.get("/logo.png")
async def logo():
    return FileResponse("logo.png", headers=_IMMUTABLE)


@app.get("/logo.webp")
async def logo_webp():
    return FileResponse("logo.webp", headers=_IMMUTABLE)


@app.get("/growphoto/{pid}")
async def grow_photo(pid: int, size: str = "f"):
    data = await db.grow_plan_photo(pid, "t" if size == "t" else "f")
    if not data:
        raise HTTPException(404, "Нет фото")
    header, b64 = data.split(",", 1)
    mime = header.split(":", 1)[1].split(";", 1)[0]
    return Response(base64.b64decode(b64), media_type=mime,
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/growlive/{photo_id}")
async def grow_live(photo_id: int, size: str = "f"):
    data = await db.grow_live_photo(photo_id, "t" if size == "t" else "f")
    if not data:
        raise HTTPException(404, "Нет фото")
    header, b64 = data.split(",", 1)
    mime = header.split(":", 1)[1].split(";", 1)[0]
    return Response(base64.b64decode(b64), media_type=mime,
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/photo/{pid}/{idx}")
async def product_photo(pid: int, idx: int, size: str = "f"):
    data = await db.product_photo(pid, idx, "t" if size == "t" else "f")
    if not data:
        raise HTTPException(404, "Нет фото")
    header, b64 = data.split(",", 1)
    mime = header.split(":", 1)[1].split(";", 1)[0]
    # URL содержит версию (?v=), поэтому кэшируем навсегда
    return Response(base64.b64decode(b64), media_type=mime,
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ── пользовательское API ─────────────────────────────────────────────────────
@app.post("/api/auth")
async def api_auth(request: Request):
    u = tg_user(request)
    name = " ".join(filter(None, [u.get("first_name"), u.get("last_name")]))
    await db.upsert_user(u["id"], name, u.get("username"))
    await db.touch_device(u["id"], client_device(request), client_ip(request))
    return await _snap(u["id"])


@app.post("/api/order")
async def api_order(request: Request):
    u = tg_user(request)
    b = await request.json()
    pay = str(b.get("pay", "balance"))
    ship = dict(b.get("ship") or {})
    ship_txt = (f"{esc(ship.get('name'))} · {esc(ship.get('phone'))}\n"
                f"{esc(ship.get('city'))}, НП №{esc(ship.get('np'))}")
    product_id, grams = pint(b.get("product_id")), pint(b.get("grams"))
    try:
        if pay in CRYPTO:
            address = (await db.get_settings()).get(CRYPTO[pay]["wallet_key"], "")
            if not address:
                raise ValueError("Этот способ оплаты сейчас недоступен")
            total = await db.order_total(product_id, grams)
            try:
                amt = await crypto_amount(pay, total)
            except ValueError:
                raise
            except Exception:
                raise ValueError("Не удалось получить курс — попробуйте через минуту")
            snap, code, inv = await db.create_order_invoice(
                u["id"], product_id, grams, pay, ship, amt, address)
            snap["invoice"] = invoice_public(inv)
            snap["invoice"]["order"] = code
        elif pay == "card":
            receipt = str(b.get("receipt", ""))
            if not _receipt_ok(receipt):
                raise ValueError("Приложите квитанцию об оплате (фото или PDF)")
            total = await db.order_total(product_id, grams)
            if total > CARD_LIMIT:
                raise ValueError(f"Картой — до {CARD_LIMIT} ₴, такой заказ оплатите криптой")
            snap = await db.create_order(
                u["id"], product_id, grams, "card", ship, receipt)
            await notify(ADMIN_ID,
                         f"🛒 <b>Заказ {snap['order_code']} — квитанция на проверку</b>\n"
                         f"{esc(snap.get('order_product'))} · {snap['order_grams']} г · {snap['order_total']} ₴\n"
                         f"{ship_txt}\nАдминка → Заказы.")
        else:
            snap = await db.create_order(
                u["id"], product_id, grams, "balance", ship)
            await notify(ADMIN_ID,
                         f"🛒 <b>Новый заказ {snap['order_code']} (оплачен с баланса)</b>\n"
                         f"{esc(snap.get('order_product'))} · {snap['order_grams']} г · {snap['order_total']} ₴\n"
                         f"{ship_txt}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    snap["is_admin"] = bool(ADMIN_ID) and u["id"] == ADMIN_ID
    return snap


@app.post("/api/rate")
async def api_rate(request: Request):
    u = tg_user(request)
    b = await request.json()
    try:
        return await db.rate_order(u["id"], str(b.get("order", "")), int(b.get("stars", 0)))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/topup/receipt")
async def api_topup_receipt(request: Request):
    u = tg_user(request)
    b = await request.json()
    amount = pint(b.get("amount"))
    receipt = str(b.get("receipt", ""))
    if amount <= 0:
        raise HTTPException(400, "Неверная сумма")
    if b.get("method") != "card":
        raise HTTPException(400, "Квитанция — только для оплаты картой")
    if amount > CARD_LIMIT:
        raise HTTPException(400, f"Картой — до {CARD_LIMIT} ₴, для больших сумм используйте крипту")
    if not _receipt_ok(receipt):
        raise HTTPException(400, "Приложите квитанцию (фото или PDF)")
    try:
        tid = await db.topup_receipt(u["id"], amount, "card", receipt)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await notify(ADMIN_ID,
                 f"💳 <b>Квитанция #{tid} на проверку</b>\n"
                 f"{esc(u.get('first_name'))} (@{esc(u.get('username') or '—')}) · "
                 f"{amount} ₴ · карта\nОткройте админку → Пополнения.")
    return await _snap(u["id"])


@app.post("/api/invoice")
async def api_invoice(request: Request):
    u = tg_user(request)
    if not rate_limit(f"inv:{u['id']}", 8, 60):
        raise HTTPException(429, "Слишком часто — подождите минуту")
    b = await request.json()
    amount = pint(b.get("amount"))
    cur = str(b.get("currency", ""))
    if amount < 10:
        raise HTTPException(400, "Минимальная сумма — 10 ₴")
    if cur not in CRYPTO:
        raise HTTPException(400, "Неизвестная валюта")
    address = (await db.get_settings()).get(CRYPTO[cur]["wallet_key"], "")
    if not address:
        raise HTTPException(400, "Этот способ оплаты сейчас недоступен")
    try:
        amt = await crypto_amount(cur, amount)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, "Не удалось получить курс — попробуйте через минуту")
    inv = await db.create_invoice(u["id"], amount, cur, amt, address)
    return invoice_public(inv)


@app.post("/api/invoice/active")
async def api_invoice_active(request: Request):
    u = tg_user(request)
    inv = await db.active_invoice(u["id"])
    return {"invoice": invoice_public(inv) if inv else None}


@app.post("/api/invoice/status")
async def api_invoice_status(request: Request):
    u = tg_user(request)
    if not rate_limit(f"invstat:{u['id']}", 20, 60):
        raise HTTPException(429, "Слишком часто — подождите")
    b = await request.json()
    inv = await db.invoice_get(pint(b.get("id")), u["id"])
    if not inv:
        raise HTTPException(404, "Счёт не найден")
    if inv["status"] == 0:
        async with aiohttp.ClientSession() as s:
            txid = await _check_invoice(s, inv)
        if txid and await _settle(inv, txid):
            inv["status"] = 1
    return {"status": inv["status"]}


@app.post("/api/invoice/cancel")
async def api_invoice_cancel(request: Request):
    u = tg_user(request)
    b = await request.json()
    await db.invoice_cancel(pint(b.get("id")), u["id"])
    return {"ok": True}


# ── PayDome: авто-карта для пополнения ────────────────────────────────────────
async def _card_settle(payment_id: str) -> bool:
    """Источник истины — GetPaymentStatus. Зачисляем только при Paid."""
    if await paydome.status(payment_id) != paydome.STATUS_PAID:
        return False
    res = await db.card_payment_paid(payment_id)
    if not res:
        return False
    await notify(res["user_id"],
                 f"✅ Оплата картой получена — баланс пополнен на <b>{res['amount']} ₴</b>.")
    await notify(ADMIN_ID, f"💳 PayDome: +{res['amount']} ₴ (payment {payment_id}).")
    return True


@app.post("/api/card/create")
async def api_card_create(request: Request):
    u = tg_user(request)
    if not rate_limit(f"cardc:{u['id']}", 6, 60):
        raise HTTPException(429, "Слишком часто — подождите минуту")
    if not paydome.enabled():
        raise HTTPException(400, "Авто-оплата картой сейчас недоступна")
    b = await request.json()
    amount = pint(b.get("amount"))   # сколько юзер хочет получить на баланс
    if amount < 10:
        raise HTTPException(400, "Минимальная сумма — 10 ₴")
    if amount > CARD_LIMIT:
        raise HTTPException(400, f"Картой — до {CARD_LIMIT} ₴, для больших сумм используйте крипту")
    charge = round(amount * (1 + CARD_FEE_PCT / 100))   # к оплате с сервисным сбором
    try:
        card = await paydome.get_card(charge)
    except ValueError:
        raise HTTPException(400, "Не удалось получить карту — попробуйте позже")
    if not card.get("card") or not card.get("payment_id"):
        raise HTTPException(400, "Провайдер не выдал карту — попробуйте позже")
    p = await db.card_payment_create(u["id"], card["payment_id"], card["card"],
                                     amount, card["pay_uah"])
    return {"payment_id": p["payment_id"], "card": p["card"],
            "pay_uah": p["pay_uah"], "amount": p["amount_uah"],
            "expires": p["expires"].isoformat()}


@app.post("/api/card/active")
async def api_card_active(request: Request):
    u = tg_user(request)
    p = await db.card_payment_active(u["id"])
    if not p:
        return {"card": None}
    return {"payment_id": p["payment_id"], "card": p["card"],
            "pay_uah": p["pay_uah"], "amount": p["amount_uah"],
            "expires": p["expires"].isoformat()}


@app.post("/api/card/status")
async def api_card_status(request: Request):
    u = tg_user(request)
    if not rate_limit(f"cardstat:{u['id']}", 20, 60):
        raise HTTPException(429, "Слишком часто — подождите")
    b = await request.json()
    pid = str(b.get("payment_id", ""))
    p = await db.card_payment_active(u["id"])
    paid = False
    if p and p["payment_id"] == pid:
        paid = await _card_settle(pid)
    return {"paid": paid}


@app.post("/api/card/cancel")
async def api_card_cancel(request: Request):
    u = tg_user(request)
    b = await request.json()
    await db.card_payment_cancel(u["id"], str(b.get("payment_id", "")))
    return {"ok": True}


@app.post("/api/paydome/webhook")
async def api_paydome_webhook(request: Request):
    """Вебхук PayDome — только триггер: перепроверяем статус через API и зачисляем при Paid."""
    try:
        b = await request.json()
    except Exception:
        return {"ok": True}
    pid = b.get("paymentId")
    if pid:
        try:
            await _card_settle(str(pid))
        except Exception:
            log.warning("Обработка вебхука PayDome не удалась: %s", pid)
    return {"ok": True}


@app.post("/api/account/delete")
async def api_account_delete(request: Request):
    u = tg_user(request)
    await db.delete_account(u["id"])
    await notify(ADMIN_ID,
                 f"🗑 Пользователь {esc(u.get('first_name'))} "
                 f"(@{esc(u.get('username') or '—')}, ID {u['id']}) удалил аккаунт.")
    return {"ok": True}


async def _is_member(chat_id: str, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(int(chat_id), user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False


@app.post("/api/bonus/claim")
async def api_bonus_claim(request: Request):
    u = tg_user(request)
    if not (bot and BONUS_CHANNEL_ID and BONUS_CHAT_ID):
        raise HTTPException(400, "Бонус временно недоступен")
    if not (await _is_member(BONUS_CHANNEL_ID, u["id"])
            and await _is_member(BONUS_CHAT_ID, u["id"])):
        raise HTTPException(400, "Подпишитесь на канал и вступите в чат, затем нажмите ещё раз")
    try:
        await db.claim_bonus(u["id"], BONUS_AMOUNT,
                             client_device(request), client_ip(request))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await notify(ADMIN_ID,
                 f"🎁 {esc(u.get('first_name'))} (@{esc(u.get('username') or '—')}) получил "
                 f"приветственный бонус {BONUS_AMOUNT} ₴.")
    return await _snap(u["id"])


@app.post("/api/pin/verify")
async def api_pin_verify(request: Request):
    u = tg_user(request)
    if not rate_limit(f"pin:{u['id']}", 12, 300):
        raise HTTPException(429, "Слишком много попыток — подождите")
    b = await request.json()
    return await db.verify_pin(u["id"], str(b.get("pin", "")))


@app.post("/api/pin/set")
async def api_pin_set(request: Request):
    u = tg_user(request)
    b = await request.json()
    try:
        return await db.set_pin(u["id"], str(b.get("pin", "")), str(b.get("old", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/promo/redeem")
async def api_promo_redeem(request: Request):
    u = tg_user(request)
    if not rate_limit(f"promo:{u['id']}", 6, 300):
        raise HTTPException(429, "Слишком много попыток — подождите")
    b = await request.json()
    try:
        snap = await db.promo_redeem(u["id"], str(b.get("code", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    snap["is_admin"] = bool(ADMIN_ID) and u["id"] == ADMIN_ID
    await notify(ADMIN_ID,
                 f"🎟 {esc(u.get('first_name'))} (@{esc(u.get('username') or '—')}) "
                 f"активировал промокод на {snap.get('promo_amount')} ₴.")
    return snap


@app.post("/api/admin/promo/generate")
async def api_admin_promo_generate(request: Request):
    admin_user(request)
    b = await request.json()
    amount, count = pint(b.get("amount")), pint(b.get("count"))
    if amount < 1 or count < 1:
        raise HTTPException(400, "Укажите сумму и количество")
    codes = await db.promo_generate(amount, count)
    return {"codes": codes, "promos": await db.admin_promos()}


@app.post("/api/admin/promo/list")
async def api_admin_promo_list(request: Request):
    admin_user(request)
    return {"promos": await db.admin_promos()}


@app.post("/api/withdraw")
async def api_withdraw(request: Request):
    u = tg_user(request)
    b = await request.json()
    try:
        snap = await db.create_withdrawal(
            u["id"], pint(b.get("amount")),
            str(b.get("method", "")), str(b.get("requisites", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    snap["is_admin"] = bool(ADMIN_ID) and u["id"] == ADMIN_ID
    await notify(ADMIN_ID,
                 f"💸 <b>Заявка на вывод {int(b.get('amount', 0))} ₴</b>\n"
                 f"{esc(u.get('first_name'))} (@{esc(u.get('username') or '—')}) · {esc(b.get('method'))}\n"
                 f"<code>{esc(str(b.get('requisites', ''))[:100])}</code>\nАдминка → Выводы.")
    return snap


@app.post("/api/admin/withdraw")
async def api_admin_withdraw(request: Request):
    admin_user(request)
    b = await request.json()
    try:
        res = await db.withdrawal_decide(int(b.get("id", 0)), bool(b.get("approve")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if res["approved"]:
        await notify(res["user_id"],
                     f"✅ Вывод <b>{res['amount']} ₴</b> выполнен — проверьте поступление.")
    else:
        await notify(res["user_id"],
                     f"↩️ Заявка на вывод <b>{res['amount']} ₴</b> отклонена — средства возвращены на баланс. "
                     "Если это ошибка — напишите в поддержку.")
    return {"withdrawals": await db.admin_withdrawals()}


@app.post("/api/grow/buy")
async def api_grow_buy(request: Request):
    u = tg_user(request)
    b = await request.json()
    pct = pint(b.get("pct"))
    try:
        snap = await db.buy_share(u["id"], pint(b.get("plan_id")), pct)
    except ValueError as e:
        raise HTTPException(400, str(e))
    snap["is_admin"] = bool(ADMIN_ID) and u["id"] == ADMIN_ID
    await notify(ADMIN_ID,
                 f"🌱 {esc(u.get('first_name'))} (@{esc(u.get('username') or '—')}) купил долю "
                 f"{pct}% в программе #{int(b.get('plan_id', 0))}.")
    return snap


@app.post("/api/admin/grow")
async def api_admin_grow(request: Request):
    admin_user(request)
    b = await request.json()
    try:
        await db.save_grow_plan(b)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, f"Проверьте поля программы: {e}")
    except Exception:
        log.exception("Ошибка сохранения программы выращивания")
        raise HTTPException(400, "Не удалось сохранить программу — подробности в логах сервера")
    return {"grow_plans": await db.get_grow_plans(include_inactive=True)}


@app.post("/api/admin/grow/stage")
async def api_admin_grow_stage(request: Request):
    admin_user(request)
    b = await request.json()
    notes = await db.set_grow_stage(int(b.get("id", 0)), int(b.get("stage", 0)))
    for n in notes:
        await notify(n["user_id"],
                     f"🧺 Урожай «{n['name']}» собран! Выплата <b>{n['payout']} ₴</b> зачислена на баланс.")
    return {"grow_plans": await db.get_grow_plans(include_inactive=True)}


@app.post("/api/admin/grow/photo")
async def api_admin_grow_photo(request: Request):
    admin_user(request)
    b = await request.json()
    try:
        await db.add_grow_photo(int(b.get("plan_id", 0)),
                                str(b.get("photo", "")), str(b.get("note", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"grow_plans": await db.get_grow_plans(include_inactive=True)}


@app.post("/api/admin/grow/photo/delete")
async def api_admin_grow_photo_delete(request: Request):
    admin_user(request)
    b = await request.json()
    await db.delete_grow_photo(int(b.get("id", 0)))
    return {"grow_plans": await db.get_grow_plans(include_inactive=True)}


@app.post("/api/admin/grow/delete")
async def api_admin_grow_delete(request: Request):
    admin_user(request)
    b = await request.json()
    refunds = await db.delete_grow_plan(int(b.get("id", 0)))
    for r in refunds:
        await notify(r["user_id"],
                     f"↩️ Программа выращивания закрыта — вложенные <b>{r['amount']} ₴</b> "
                     "возвращены на баланс.")
    return {"grow_plans": await db.get_grow_plans(include_inactive=True)}


@app.post("/api/transfer/create")
async def api_transfer_create(request: Request):
    u = tg_user(request)
    return {"code": await db.transfer_create(u["id"])}


@app.post("/api/transfer/redeem")
async def api_transfer_redeem(request: Request):
    u = tg_user(request)
    # антибрут: не больше 5 попыток за 5 минут на юзера и на IP
    if not (rate_limit(f"redeem:{u['id']}", 5, 300)
            and rate_limit(f"redeem_ip:{client_ip(request)}", 10, 300)):
        raise HTTPException(429, "Слишком много попыток — подождите несколько минут")
    b = await request.json()
    try:
        return await db.transfer_redeem(str(b.get("code", "")), u["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── админка ──────────────────────────────────────────────────────────────────
@app.post("/api/admin/data")
async def api_admin_data(request: Request):
    admin_user(request)
    return {
        "products": await db.get_products(include_inactive=True),
        "grow_plans": await db.get_grow_plans(include_inactive=True),
        "settings": await db.get_settings(),
        "topups": await db.admin_topups(),
        "withdrawals": await db.admin_withdrawals(),
        "orders": await db.admin_orders(),
        "promos": await db.admin_promos(),
    }


@app.post("/api/admin/product")
async def api_admin_product(request: Request):
    admin_user(request)
    b = await request.json()
    try:
        pid = await db.save_product(b)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, f"Проверьте поля товара: {e}")
    return {"id": pid, "products": await db.get_products(include_inactive=True)}


@app.post("/api/admin/product/delete")
async def api_admin_product_delete(request: Request):
    admin_user(request)
    b = await request.json()
    await db.delete_product(int(b.get("id", 0)))
    return {"products": await db.get_products(include_inactive=True)}


@app.post("/api/admin/settings")
async def api_admin_settings(request: Request):
    admin_user(request)
    await db.set_settings(await request.json())
    return {"settings": await db.get_settings()}


@app.post("/api/admin/topup")
async def api_admin_topup(request: Request):
    admin_user(request)
    b = await request.json()
    try:
        res = await db.topup_decide(int(b.get("id", 0)), bool(b.get("approve")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if res["approved"]:
        await notify(res["user_id"],
                     f"✅ Оплата подтверждена — баланс пополнен на <b>{res['amount']} ₴</b>.")
    else:
        await notify(res["user_id"],
                     "❌ Квитанция не прошла проверку. Если это ошибка — напишите в поддержку.")
    return {"topups": await db.admin_topups()}


@app.post("/api/admin/order")
async def api_admin_order(request: Request):
    admin_user(request)
    b = await request.json()
    try:
        res = await db.order_decide(str(b.get("order", "")), bool(b.get("approve")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if res["approved"]:
        await notify(res["user_id"],
                     f"✅ Оплата заказа <b>{res['code']}</b> подтверждена — принят в работу.")
    else:
        await notify(res["user_id"],
                     f"❌ Оплата заказа <b>{res['code']}</b> не прошла проверку — заказ отменён. "
                     "Если это ошибка — напишите в поддержку.")
    return {"orders": await db.admin_orders()}


@app.post("/api/admin/ttn")
async def api_admin_ttn(request: Request):
    admin_user(request)
    b = await request.json()
    try:
        res = await db.set_ttn(str(b.get("order", "")), str(b.get("ttn", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await notify(res["user_id"],
                 f"📦 Заказ <b>{res['code']}</b> в пути!\n"
                 f"ТТН Новой Почты: <code>{res['ttn']}</code>")
    return {"orders": await db.admin_orders()}


@app.post("/api/admin/work")
async def api_admin_work(request: Request):
    admin_user(request)
    b = await request.json()
    try:
        res = await db.order_to_work(str(b.get("order", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await notify(res["user_id"], f"🛠 Заказ <b>{res['code']}</b> принят в работу — собираем.")
    return {"orders": await db.admin_orders()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
