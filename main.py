"""Magic Market — FastAPI (фронт + API + админка) + aiogram-бот в одном процессе (Railway)."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

import auth
import db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mm")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
# публичный https-адрес: APP_URL или автоматический домен Railway
_railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
APP_URL = os.getenv("APP_URL", "") or (f"https://{_railway_domain}" if _railway_domain else "")

MAX_RECEIPT_LEN = 6_000_000  # ~4.5 МБ картинки в base64

# ── бот ──────────────────────────────────────────────────────────────────────
bot = dp = None
if BOT_TOKEN:
    from aiogram import Bot, Dispatcher
    from aiogram.filters import Command, CommandObject, CommandStart
    from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                               MenuButtonWebApp, Message, WebAppInfo)

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

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
    task = None
    if dp:
        log.info("APP_URL = %r, ADMIN_ID = %r", APP_URL, ADMIN_ID)
        if APP_URL:
            # синяя кнопка меню с мини-аппом — не нужно настраивать в BotFather
            try:
                await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                    text="Magic Market", web_app=WebAppInfo(url=APP_URL)))
                log.info("Кнопка меню WebApp установлена")
            except Exception:
                log.exception("Не удалось установить кнопку меню")
        else:
            log.warning("APP_URL/RAILWAY_PUBLIC_DOMAIN не заданы — кнопка WebApp не будет показана")
        task = asyncio.create_task(dp.start_polling(bot))
        log.info("Бот запущен (polling)")
    else:
        log.warning("BOT_TOKEN не задан — бот не запущен, API без авторизации не работает")
    yield
    if task:
        task.cancel()


app = FastAPI(title="Magic Market", lifespan=lifespan)


def tg_user(request: Request) -> dict:
    user = auth.validate(request.headers.get("X-Init-Data", ""), BOT_TOKEN)
    if not user:
        raise HTTPException(401, "Невалидные данные Telegram")
    return user


def admin_user(request: Request) -> dict:
    u = tg_user(request)
    if not ADMIN_ID or u["id"] != ADMIN_ID:
        raise HTTPException(403, "Доступ только для владельца")
    return u


async def _snap(uid: int) -> dict:
    snap = await db.snapshot(uid)
    snap["is_admin"] = bool(ADMIN_ID) and uid == ADMIN_ID
    return snap


@app.get("/")
async def index():
    return FileResponse("index.html")


@app.get("/logo.png")
async def logo():
    return FileResponse("logo.png")


# ── пользовательское API ─────────────────────────────────────────────────────
@app.post("/api/auth")
async def api_auth(request: Request):
    u = tg_user(request)
    name = " ".join(filter(None, [u.get("first_name"), u.get("last_name")]))
    await db.upsert_user(u["id"], name, u.get("username"))
    return await _snap(u["id"])


@app.post("/api/order")
async def api_order(request: Request):
    u = tg_user(request)
    b = await request.json()
    try:
        snap = await db.create_order(
            u["id"], int(b["product_id"]), int(b["grams"]),
            str(b.get("pay", "balance")), dict(b.get("ship") or {}))
    except ValueError as e:
        raise HTTPException(400, str(e))
    ship = b.get("ship") or {}
    await notify(ADMIN_ID,
                 f"🛒 <b>Новый заказ {snap['order_code']}</b>\n"
                 f"{b.get('product_name', '')} · {b['grams']} г · {snap['order_total']} ₴\n"
                 f"{ship.get('name', '')} · {ship.get('phone', '')}\n"
                 f"{ship.get('city', '')}, НП №{ship.get('np', '')}")
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
    amount = int(b.get("amount", 0))
    receipt = str(b.get("receipt", ""))
    if amount <= 0:
        raise HTTPException(400, "Неверная сумма")
    if not receipt.startswith("data:image/") or len(receipt) > MAX_RECEIPT_LEN:
        raise HTTPException(400, "Приложите фото квитанции")
    tid = await db.topup_receipt(u["id"], amount, str(b.get("method", "")), receipt)
    await notify(ADMIN_ID,
                 f"💳 <b>Квитанция #{tid} на проверку</b>\n"
                 f"{u.get('first_name', '')} (@{u.get('username', '—')}) · "
                 f"{amount} ₴ · {b.get('method', '')}\nОткройте админку → Пополнения.")
    return await _snap(u["id"])


@app.post("/api/transfer/create")
async def api_transfer_create(request: Request):
    u = tg_user(request)
    return {"code": await db.transfer_create(u["id"])}


@app.post("/api/transfer/redeem")
async def api_transfer_redeem(request: Request):
    u = tg_user(request)
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
        "settings": await db.get_settings(),
        "topups": await db.admin_topups(),
        "orders": await db.admin_orders(),
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


@app.post("/api/admin/ttn")
async def api_admin_ttn(request: Request):
    admin_user(request)
    b = await request.json()
    try:
        res = await db.set_ttn(str(b.get("order", "")), str(b.get("ttn", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await notify(res["user_id"],
                 f"📦 Заказ <b>{res['code']}</b> отправлен!\n"
                 f"ТТН Новой Почты: <code>{res['ttn']}</code>")
    return {"orders": await db.admin_orders()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
