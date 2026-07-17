"""Magic Market — FastAPI (фронт + API) + aiogram-бот в одном процессе (Railway)."""
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
# публичный https-адрес: APP_URL или автоматический домен Railway
_railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
APP_URL = os.getenv("APP_URL", "") or (f"https://{_railway_domain}" if _railway_domain else "")

# ── бот ──────────────────────────────────────────────────────────────────────
bot = dp = None
if BOT_TOKEN:
    from aiogram import Bot, Dispatcher
    from aiogram.filters import CommandObject, CommandStart
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


# ── приложение ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init()
    task = None
    if dp:
        log.info("APP_URL = %r", APP_URL)
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


@app.get("/")
async def index():
    return FileResponse("index.html")


@app.post("/api/auth")
async def api_auth(request: Request):
    u = tg_user(request)
    name = " ".join(filter(None, [u.get("first_name"), u.get("last_name")]))
    await db.upsert_user(u["id"], name, u.get("username"))
    return await db.snapshot(u["id"])


@app.post("/api/order")
async def api_order(request: Request):
    u = tg_user(request)
    b = await request.json()
    try:
        return await db.create_order(
            u["id"], str(b["product"]), int(b["grams"]), int(b["total"]),
            str(b.get("pay", "balance")), dict(b.get("ship") or {}))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/topup")
async def api_topup(request: Request):
    u = tg_user(request)
    b = await request.json()
    amount = int(b.get("amount", 0))
    if amount <= 0:
        raise HTTPException(400, "Неверная сумма")
    return await db.topup(u["id"], amount)


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
