"""Magic Market — Guard: капча-ознакомление с правилами чата.

Свой код и ОТДЕЛЬНЫЙ бот (свой токен GUARD_BOT_TOKEN), но запускается в том же
процессе, что и магазин (main.py вызывает run() как фоновую задачу).
Новый участник → мьют + правила с кнопкой «Ознакомлен». Не нажал за N минут → кик.

ENV:
  GUARD_BOT_TOKEN — токен отдельного бота (BotFather). Без него гейт просто не запускается.
  RULES_CHAT_ID   — ID чата (необязательно; если задан — гейт только там).
  RULES_TIMEOUT   — секунд на ознакомление (по умолчанию 300 = 5 мин).
Guard-бот должен быть админом чата: ограничивать/банить участников и удалять сообщения.
"""
import asyncio
import html
import logging
import os

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("guard")

GUARD_BOT_TOKEN = os.getenv("GUARD_BOT_TOKEN", "")
RULES_CHAT_ID = os.getenv("RULES_CHAT_ID", "")
RULES_TIMEOUT = int(os.getenv("RULES_TIMEOUT", "300") or 300)

RULES_TEXT = (
    "📜 <b>Правила Magic Market</b>\n\n"
    "1. Только 18+. Уважайте участников — без оскорблений, токсичности и разжигания.\n"
    "2. Запрещены спам, флуд и реклама без согласования с администрацией.\n"
    "3. Никакого скама, попрошайничества и обмана — бан без предупреждения.\n"
    "4. Все вопросы по заказам и оплате — только через бота и официальную поддержку "
    "@magicmarket_boss. Админы <b>не пишут первыми</b> и <b>никогда</b> не просят пароли, "
    "коды и переводы в личку.\n"
    "5. Остерегайтесь двойников: проверяйте @username, не переходите по подозрительным ссылкам.\n"
    "6. Не публикуйте чужие личные данные, чеки, ТТН и переписки.\n"
    "7. Решения администрации окончательны. Нарушение = мут или бан.\n\n"
    "Нажимая кнопку ниже, вы подтверждаете согласие с правилами."
)

_pending: dict = {}  # (chat_id, user_id) -> asyncio.Task


def _esc(s) -> str:
    return html.escape(str(s or ""))


async def run():
    """Запуск guard-бота (polling). No-op, если GUARD_BOT_TOKEN не задан."""
    if not GUARD_BOT_TOKEN:
        log.info("GUARD_BOT_TOKEN не задан — guard-бот выключен")
        return

    from aiogram import Bot, Dispatcher, F
    from aiogram.filters import Command
    from aiogram.types import (CallbackQuery, ChatPermissions, InlineKeyboardButton,
                               InlineKeyboardMarkup, Message)

    bot = Bot(GUARD_BOT_TOKEN)
    dp = Dispatcher()
    MUTED = ChatPermissions(can_send_messages=False, can_send_media_messages=False,
                            can_send_polls=False, can_send_other_messages=False,
                            can_add_web_page_previews=False)
    OPEN = ChatPermissions(can_send_messages=True, can_send_media_messages=True,
                           can_send_polls=True, can_send_other_messages=True,
                           can_add_web_page_previews=True)

    async def gate_user(chat_id: int, u):
        key = (chat_id, u.id)
        old = _pending.pop(key, None)
        if old:
            old.cancel()
        try:
            await bot.restrict_chat_member(chat_id, u.id, permissions=MUTED)
        except Exception:
            log.warning("Не удалось замьютить %s — проверьте права бота", u.id)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Ознакомлен(а) с правилами", callback_data=f"ack:{u.id}"),
        ]])
        mins = RULES_TIMEOUT // 60
        try:
            m = await bot.send_message(
                chat_id,
                f"👋 <b>{_esc(u.full_name)}</b>, добро пожаловать!\n\n{RULES_TEXT}\n\n"
                f"⏳ Нажмите кнопку в течение {mins} мин — иначе автоматический кик.",
                parse_mode="HTML", reply_markup=kb)
        except Exception:
            return
        _pending[key] = asyncio.create_task(kick_after(chat_id, u.id, m.message_id))

    async def kick_after(chat_id: int, user_id: int, msg_id: int):
        try:
            await asyncio.sleep(RULES_TIMEOUT)
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)  # кик, не вечный бан
            try:
                await bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
            log.info("Автокик %s из %s (не ознакомился)", user_id, chat_id)
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("Автокик не удался: %s", user_id)
        finally:
            _pending.pop((chat_id, user_id), None)

    @dp.message(Command("chatid"))
    async def chatid(message: Message):
        await message.answer(f"ID этого чата: <code>{message.chat.id}</code>", parse_mode="HTML")

    @dp.message(F.new_chat_members)
    async def on_join(message: Message):
        if RULES_CHAT_ID and str(message.chat.id) != str(RULES_CHAT_ID):
            return
        for u in message.new_chat_members:
            if not u.is_bot:
                await gate_user(message.chat.id, u)
        try:
            await message.delete()  # убрать служебное «X вошёл в группу»
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("ack:"))
    async def on_ack(cb: CallbackQuery):
        try:
            target = int(cb.data.split(":")[1])
        except (IndexError, ValueError):
            return
        if cb.from_user.id != target:
            await cb.answer("Это кнопка для другого участника 🙂", show_alert=True)
            return
        task = _pending.pop((cb.message.chat.id, target), None)
        if task:
            task.cancel()
        try:
            await bot.restrict_chat_member(cb.message.chat.id, target, permissions=OPEN)
        except Exception:
            log.warning("Не удалось снять мьют с %s", target)
        try:
            await cb.message.delete()
        except Exception:
            pass
        await cb.answer("Добро пожаловать! Доступ в чат открыт ✅")

    log.info("Guard-бот запущен. RULES_CHAT_ID=%r, таймаут=%s c", RULES_CHAT_ID, RULES_TIMEOUT)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run())
