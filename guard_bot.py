"""Magic Market — Guard: капча-ознакомление, чистка системных сообщений,
модерация (бан/кик/мут) и журнал действий в личку админу.

Свой код и ОТДЕЛЬНЫЙ бот (токен GUARD_BOT_TOKEN), запускается в процессе магазина
(main.py вызывает run() фоновой задачей).

ENV:
  GUARD_BOT_TOKEN — токен отдельного бота (BotFather). Без него guard не запускается.
  GUARD_ADMIN_ID  — кому слать журнал (по умолчанию = ADMIN_ID). Админ должен нажать
                    Start в личке guard-бота, иначе журнал не дойдёт.
  RULES_CHAT_ID   — ID чата (если задан — guard работает только там).
  RULES_TIMEOUT   — секунд на ознакомление (по умолчанию 300 = 5 мин).
Guard-бот должен быть админом чата: ограничивать/банить участников и удалять сообщения.
"""
import asyncio
import html
import logging
import os
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("guard")

GUARD_BOT_TOKEN = os.getenv("GUARD_BOT_TOKEN", "")
GUARD_ADMIN_ID = int(os.getenv("GUARD_ADMIN_ID", os.getenv("ADMIN_ID", "0")) or 0)
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


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")


async def run():
    """Запуск guard-бота (polling). No-op, если GUARD_BOT_TOKEN не задан."""
    if not GUARD_BOT_TOKEN:
        log.info("GUARD_BOT_TOKEN не задан — guard-бот выключен")
        return

    from aiogram import Bot, Dispatcher, F
    from aiogram.enums import ContentType
    from aiogram.filters import Command, CommandObject
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
    SERVICE = {
        ContentType.NEW_CHAT_MEMBERS, ContentType.LEFT_CHAT_MEMBER,
        ContentType.NEW_CHAT_TITLE, ContentType.NEW_CHAT_PHOTO, ContentType.DELETE_CHAT_PHOTO,
        ContentType.GROUP_CHAT_CREATED, ContentType.SUPERGROUP_CHAT_CREATED,
        ContentType.CHANNEL_CHAT_CREATED, ContentType.MESSAGE_AUTO_DELETE_TIMER_CHANGED,
        ContentType.PINNED_MESSAGE, ContentType.MIGRATE_TO_CHAT_ID,
        ContentType.MIGRATE_FROM_CHAT_ID,
    }

    async def journal(text: str):
        """Запись в журнал — в личку админу (виден только ему)."""
        if not GUARD_ADMIN_ID:
            return
        try:
            await bot.send_message(GUARD_ADMIN_ID, text, parse_mode="HTML")
        except Exception:
            log.warning("Журнал не доставлен — админ не нажал Start у guard-бота?")

    async def log_action(icon: str, action: str, chat, by, target_name, target_id, reason: str):
        await journal(
            f"{icon} <b>{action}</b>\n"
            f"👤 Кого: {_esc(target_name)} (ID <code>{target_id}</code>)\n"
            f"🛡 Кто: {_esc(by)}\n"
            f"💬 Чат: {_esc(getattr(chat, 'title', chat))}\n"
            f"📝 Причина: {_esc(reason or 'не указана')}\n"
            f"🕒 {_now()}")

    async def is_admin(chat_id: int, user_id: int) -> bool:
        if user_id == GUARD_ADMIN_ID:
            return True
        try:
            m = await bot.get_chat_member(chat_id, user_id)
            return m.status in ("administrator", "creator")
        except Exception:
            return False

    async def resolve_target(message, command):
        """Возвращает (user_id, name, reason). Цель — из reply или из первого аргумента."""
        args = (command.args or "").strip() if command else ""
        if message.reply_to_message and message.reply_to_message.from_user:
            tu = message.reply_to_message.from_user
            return tu.id, tu.full_name, args
        parts = args.split(maxsplit=1)
        if parts and parts[0].lstrip("-").isdigit():
            return int(parts[0]), f"ID {parts[0]}", (parts[1] if len(parts) > 1 else "")
        return None, None, args

    async def gate_user(chat, u):
        key = (chat.id, u.id)
        old = _pending.pop(key, None)
        if old:
            old.cancel()
        try:
            await bot.restrict_chat_member(chat.id, u.id, permissions=MUTED)
        except Exception:
            log.warning("Не удалось замьютить %s — проверьте права бота", u.id)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Ознакомлен(а) с правилами", callback_data=f"ack:{u.id}"),
        ]])
        mins = RULES_TIMEOUT // 60
        try:
            m = await bot.send_message(
                chat.id,
                f"👋 <b>{_esc(u.full_name)}</b>, добро пожаловать!\n\n{RULES_TEXT}\n\n"
                f"⏳ Нажмите кнопку в течение {mins} мин — иначе автоматический кик.",
                parse_mode="HTML", reply_markup=kb)
        except Exception:
            return
        _pending[key] = asyncio.create_task(
            kick_after(chat.id, chat.title, u.id, u.full_name, m.message_id))

    async def kick_after(chat_id, chat_title, user_id, name, msg_id):
        try:
            await asyncio.sleep(RULES_TIMEOUT)
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)  # кик, не вечный бан
            try:
                await bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
            await journal(
                f"👢 <b>Автокик</b>\n"
                f"👤 Кого: {_esc(name)} (ID <code>{user_id}</code>)\n"
                f"💬 Чат: {_esc(chat_title)}\n"
                f"📝 Причина: не ознакомился с правилами за отведённое время\n"
                f"🕒 {_now()}")
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("Автокик не удался: %s", user_id)
        finally:
            _pending.pop((chat_id, user_id), None)

    # ── системные сообщения: чистим, новичков — на капчу ──────────────────────
    @dp.message(F.content_type.in_(SERVICE))
    async def on_service(message: Message):
        if RULES_CHAT_ID and str(message.chat.id) != str(RULES_CHAT_ID):
            return
        if message.new_chat_members:
            for u in message.new_chat_members:
                if not u.is_bot:
                    await gate_user(message.chat, u)
        try:
            await message.delete()
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

    # ── модерация (только админы чата или GUARD_ADMIN_ID) ──────────────────────
    @dp.message(Command("ban"))
    async def cmd_ban(message: Message, command: CommandObject):
        if message.chat.type == "private" or not await is_admin(message.chat.id, message.from_user.id):
            return
        uid, name, reason = await resolve_target(message, command)
        if not uid:
            return await message.reply("Ответьте на сообщение или: /ban &lt;id&gt; &lt;причина&gt;",
                                       parse_mode="HTML")
        try:
            await bot.ban_chat_member(message.chat.id, uid)
        except Exception:
            return await message.reply("Не удалось забанить — проверьте права бота")
        if message.reply_to_message:
            try:
                await message.reply_to_message.delete()
            except Exception:
                pass
        await log_action("🚫", "Бан", message.chat, message.from_user.full_name, name, uid, reason)
        try:
            await message.delete()
        except Exception:
            pass

    @dp.message(Command("kick"))
    async def cmd_kick(message: Message, command: CommandObject):
        if message.chat.type == "private" or not await is_admin(message.chat.id, message.from_user.id):
            return
        uid, name, reason = await resolve_target(message, command)
        if not uid:
            return await message.reply("Ответьте на сообщение или: /kick &lt;id&gt; &lt;причина&gt;",
                                       parse_mode="HTML")
        try:
            await bot.ban_chat_member(message.chat.id, uid)
            await bot.unban_chat_member(message.chat.id, uid)
        except Exception:
            return await message.reply("Не удалось кикнуть — проверьте права бота")
        await log_action("👢", "Кик", message.chat, message.from_user.full_name, name, uid, reason)
        try:
            await message.delete()
        except Exception:
            pass

    @dp.message(Command("mute"))
    async def cmd_mute(message: Message, command: CommandObject):
        if message.chat.type == "private" or not await is_admin(message.chat.id, message.from_user.id):
            return
        uid, name, rest = await resolve_target(message, command)
        if not uid:
            return await message.reply("Ответьте на сообщение или: /mute &lt;id&gt; &lt;мин&gt; &lt;причина&gt;",
                                       parse_mode="HTML")
        minutes, reason = 60, rest
        parts = (rest or "").split(maxsplit=1)
        if parts and parts[0].isdigit():
            minutes = max(1, min(43200, int(parts[0])))
            reason = parts[1] if len(parts) > 1 else ""
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        try:
            await bot.restrict_chat_member(message.chat.id, uid, permissions=MUTED, until_date=until)
        except Exception:
            return await message.reply("Не удалось замьютить — проверьте права бота")
        await log_action("🔇", f"Мут на {minutes} мин", message.chat,
                         message.from_user.full_name, name, uid, reason)
        try:
            await message.delete()
        except Exception:
            pass

    @dp.message(Command("unmute"))
    async def cmd_unmute(message: Message, command: CommandObject):
        if message.chat.type == "private" or not await is_admin(message.chat.id, message.from_user.id):
            return
        uid, name, reason = await resolve_target(message, command)
        if not uid:
            return await message.reply("Ответьте на сообщение или: /unmute &lt;id&gt;", parse_mode="HTML")
        try:
            await bot.restrict_chat_member(message.chat.id, uid, permissions=OPEN)
        except Exception:
            return await message.reply("Не удалось — проверьте права бота")
        await log_action("🔊", "Размут", message.chat, message.from_user.full_name, name, uid, reason)
        try:
            await message.delete()
        except Exception:
            pass

    @dp.message(Command("unban"))
    async def cmd_unban(message: Message, command: CommandObject):
        if message.chat.type == "private" or not await is_admin(message.chat.id, message.from_user.id):
            return
        uid, name, reason = await resolve_target(message, command)
        if not uid:
            return await message.reply("Укажите: /unban &lt;id&gt;", parse_mode="HTML")
        try:
            await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
        except Exception:
            return await message.reply("Не удалось — проверьте права бота")
        await log_action("✅", "Разбан", message.chat, message.from_user.full_name, name, uid, reason)
        try:
            await message.delete()
        except Exception:
            pass

    @dp.message(Command("chatid"))
    async def cmd_chatid(message: Message):
        await message.answer(f"ID этого чата: <code>{message.chat.id}</code>", parse_mode="HTML")

    log.info("Guard-бот запущен. RULES_CHAT_ID=%r, ADMIN=%s, таймаут=%s c",
             RULES_CHAT_ID, GUARD_ADMIN_ID, RULES_TIMEOUT)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run())
