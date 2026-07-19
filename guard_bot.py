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
import re
import time
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
    "2. <b>Мат и оскорбления</b> — сообщение удаляется + предупреждение. "
    "3 предупреждения = мут на час.\n"
    "3. <b>Реклама и ссылки</b> (сайты, чужие каналы/чаты, любые ссылки) запрещены: "
    "1-й раз — удаление и предупреждение, 2-й раз — бан.\n"
    "4. <b>Флуд</b> (много сообщений подряд), <b>КАПС</b>, спам эмодзи (10+) и "
    "стикерами (4+ подряд) — удаление + предупреждение.\n"
    "5. Никакого скама, попрошайничества и обмана — бан без предупреждения.\n"
    "6. Все вопросы по заказам и оплате — только через бота и официальную поддержку "
    "@magicmarket_boss. Админы <b>не пишут первыми</b> и <b>никогда</b> не просят пароли, "
    "коды и переводы в личку.\n"
    "7. Остерегайтесь двойников: проверяйте @username, не переходите по подозрительным ссылкам.\n"
    "8. Не публикуйте чужие личные данные, чеки, ТТН и переписки.\n"
    "9. Пожаловаться на нарушителя: ответьте на его сообщение командой <code>/report</code>.\n"
    "10. Решения администрации окончательны."
)

_pending: dict = {}  # (chat_id, user_id) -> asyncio.Task (капча-таймер)

# ── автомодерация: конфиг ────────────────────────────────────────────────────
WARN_EXPIRE = 24 * 3600        # предупреждения сгорают через 1 день
WARN_MUTE_AT = 3              # 3 предупреждения → мут
MUTE_MINUTES = 60            # мут за предел предупреждений
LINK_BAN_AT = 2              # 2-е нарушение по ссылкам → бан
FLOOD_MAX = 5                # больше 5 сообщений...
FLOOD_WINDOW = 7            # ...за 7 секунд → флуд
CAPS_MIN_LEN = 10
CAPS_RATIO = 0.7
EMOJI_MAX = 10
STICKER_MAX = 3              # 4-й стикер подряд — нарушение
TEMP_MSG_TTL = 15           # авто-удаление предупреждений в чате, сек

# наши ссылки/юзернеймы — не наказываются
WHITELIST = ("magic_marketplace_bot", "magicmarket_boss",
             "hjlpbvv65kq0yjay", "0_b77etkgvpizgy6")

# состояние в памяти (сбрасывается при рестарте — мягко в пользу юзеров)
_warns: dict = {}       # key -> [метки времени] общий счётчик
_link_strikes: dict = {}  # key -> [метки времени] ссылки/реклама
_msg_times: dict = {}   # key -> [метки времени] для флуда
_stickers: dict = {}    # key -> счётчик подряд идущих стикеров
_report_cd: dict = {}   # user_id -> метка времени последней жалобы (антиспам)
_rules_cd: dict = {}    # chat_id -> метка времени последнего /rules (антиспам)

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF"
    "\U0001F1E6-\U0001F1FF\U00002190-\U000021FF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U00002700-\U000027BF]")
_LINK_RE = re.compile(
    r"(?:https?://|www\.|t\.me/|telegram\.me/|tg://)\S+"
    r"|\b[\w-]+\.(?:com|net|org|ru|ua|io|me|xyz|top|shop|site|online|info|biz|link|click)\b",
    re.I)
# @упоминания участников не считаем рекламой — ловим только ссылки/инвайты/домены
# матные корни и явные оскорбления (рус/укр) — базовый список, ловит очевидное
_BAD_RE = re.compile(
    r"(ху[йяеё]|пизд|бля[дт]ь?|\bбля\b|еб[аеёу]|ёб|уеб|уёб|заеб|наеб|въеб|отъеб|разъеб"
    r"|сук[аиуе]\b|сученьк|мудак|муд[ао]з|манда\b|залуп|гандон|гондон|пид[ао]р|педик"
    r"|долбоёб|долбоеб|дебил|дегенерат|ублюдок|уёбок|уебок|мраз[ьи]|чмо\b|курв[аи]"
    r"|придур|дол?боящер|шлюх|потаскух)",
    re.I)


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _prune(lst, window):
    now = time.time()
    return [t for t in lst if now - t < window]


def has_profanity(text: str) -> bool:
    return bool(text) and bool(_BAD_RE.search(text))


def bad_link(text: str) -> bool:
    for tok in _LINK_RE.findall(text or ""):
        tl = tok.lower()
        if not any(w in tl for w in WHITELIST):
            return True
    return False


def is_caps(text: str) -> bool:
    letters = [c for c in (text or "") if c.isalpha()]
    if len(letters) < CAPS_MIN_LEN:
        return False
    up = sum(1 for c in letters if c.isupper())
    return up / len(letters) >= CAPS_RATIO


def emoji_count(text: str) -> int:
    return len(_EMOJI_RE.findall(text or ""))


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
                "Нажимая кнопку, вы подтверждаете согласие с правилами.\n"
                f"⏳ Успейте в течение {mins} мин — иначе автоматический кик.",
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

    @dp.message(F.is_automatic_forward)
    async def on_channel_forward(message: Message):
        # пост из канала, автоматически пересланный в привязанный чат, Telegram
        # закрепляет сам — открепляем, чтобы промо не висело закреплённым
        if RULES_CHAT_ID and str(message.chat.id) != str(RULES_CHAT_ID):
            return
        try:
            await bot.unpin_chat_message(message.chat.id, message.message_id)
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

    @dp.message(Command("rules", "правила"))
    async def cmd_rules(message: Message):
        if message.chat.type != "private":
            if RULES_CHAT_ID and str(message.chat.id) != str(RULES_CHAT_ID):
                return
            now = time.time()
            if now - _rules_cd.get(message.chat.id, 0) < 20:  # антиспам /rules
                try:
                    await message.delete()
                except Exception:
                    pass
                return
            _rules_cd[message.chat.id] = now
            try:
                await message.delete()
            except Exception:
                pass
        await bot.send_message(message.chat.id, RULES_TEXT, parse_mode="HTML")

    # ── жалобы участников → в журнал админу с кнопками действий ────────────────
    @dp.message(Command("report", "жалоба"))
    async def cmd_report(message: Message, command: CommandObject):
        if message.chat.type == "private":
            return
        if RULES_CHAT_ID and str(message.chat.id) != str(RULES_CHAT_ID):
            return
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if not target or target.is_bot:
            m = await message.reply("Ответьте командой /report на сообщение нарушителя.")
            asyncio.create_task(_del_later(message.chat.id, m.message_id))
            try:
                await message.delete()
            except Exception:
                pass
            return
        rk = message.from_user.id
        now = time.time()
        if now - _report_cd.get(rk, 0) < 30:  # антиспам жалоб
            try:
                await message.delete()
            except Exception:
                pass
            return
        _report_cd[rk] = now
        reason = (command.args or "").strip() or "не указана"
        snippet = (message.reply_to_message.text or message.reply_to_message.caption or "[медиа/стикер]")[:300]
        if not GUARD_ADMIN_ID:
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔇 Мут 60м", callback_data=f"rmute:{message.chat.id}:{target.id}"),
            InlineKeyboardButton(text="🚫 Бан", callback_data=f"rban:{message.chat.id}:{target.id}"),
            InlineKeyboardButton(text="✖", callback_data="rclose"),
        ]])
        try:
            await bot.send_message(
                GUARD_ADMIN_ID,
                f"🚨 <b>Жалоба</b>\n"
                f"👤 На кого: {_esc(target.full_name)} (@{_esc(target.username or '—')}, ID <code>{target.id}</code>)\n"
                f"🙋 От кого: {_esc(message.from_user.full_name)} (@{_esc(message.from_user.username or '—')})\n"
                f"💬 Чат: {_esc(message.chat.title)}\n"
                f"📝 Причина: {_esc(reason)}\n"
                f"✉️ Сообщение: {_esc(snippet)}\n"
                f"🕒 {_now()}",
                parse_mode="HTML", reply_markup=kb)
        except Exception:
            log.warning("Жалоба не доставлена — админ не нажал Start у guard-бота?")
        m = await message.reply("✅ Жалоба отправлена администрации.")
        asyncio.create_task(_del_later(message.chat.id, m.message_id))
        try:
            await message.delete()
        except Exception:
            pass

    async def _del_later(chat_id, msg_id, secs=8):
        await asyncio.sleep(secs)
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    @dp.callback_query(F.data == "rclose")
    async def on_rclose(cb: CallbackQuery):
        if cb.from_user.id != GUARD_ADMIN_ID:
            return await cb.answer()
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.answer("Закрыто")

    @dp.callback_query(F.data.startswith(("rmute:", "rban:")))
    async def on_report_action(cb: CallbackQuery):
        if cb.from_user.id != GUARD_ADMIN_ID:
            return await cb.answer("Только для администратора", show_alert=True)
        try:
            act, chat_id, uid = cb.data.split(":")
            chat_id, uid = int(chat_id), int(uid)
        except (ValueError, IndexError):
            return await cb.answer()
        if act == "rban":
            try:
                await bot.ban_chat_member(chat_id, uid)
                done = "🚫 Забанен"
            except Exception:
                return await cb.answer("Не удалось — проверьте права бота", show_alert=True)
            await log_action("🚫", "Бан (по жалобе)", cb.message.chat, "админ (жалоба)",
                             f"ID {uid}", uid, "решение по жалобе")
        else:
            until = datetime.now(timezone.utc) + timedelta(minutes=MUTE_MINUTES)
            try:
                await bot.restrict_chat_member(chat_id, uid, permissions=MUTED, until_date=until)
                done = f"🔇 Мут {MUTE_MINUTES}м"
            except Exception:
                return await cb.answer("Не удалось — проверьте права бота", show_alert=True)
            await log_action("🔇", f"Мут {MUTE_MINUTES}м (по жалобе)", cb.message.chat,
                             "админ (жалоба)", f"ID {uid}", uid, "решение по жалобе")
        try:
            await cb.message.edit_text(cb.message.html_text + f"\n\n✅ <b>{done}</b>",
                                       parse_mode="HTML")
        except Exception:
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await cb.answer(done)

    # ── автомодерация ─────────────────────────────────────────────────────────
    async def send_temp(chat_id: int, text: str):
        """Короткое предупреждение в чат с авто-удалением."""
        try:
            m = await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception:
            return

        async def _rm():
            await asyncio.sleep(TEMP_MSG_TTL)
            try:
                await bot.delete_message(chat_id, m.message_id)
            except Exception:
                pass
        asyncio.create_task(_rm())

    async def add_warn(message, target_name, reason):
        """Общий счётчик: 3 предупреждения → мут. Возвращает None."""
        key = (message.chat.id, message.from_user.id)
        lst = _prune(_warns.get(key, []), WARN_EXPIRE)
        lst.append(time.time())
        _warns[key] = lst
        n = len(lst)
        uid, name = message.from_user.id, target_name
        if n >= WARN_MUTE_AT:
            _warns[key] = []
            until = datetime.now(timezone.utc) + timedelta(minutes=MUTE_MINUTES)
            try:
                await bot.restrict_chat_member(message.chat.id, uid, permissions=MUTED, until_date=until)
            except Exception:
                pass
            await send_temp(message.chat.id,
                            f"🔇 <b>{_esc(name)}</b> получает мут на {MUTE_MINUTES} мин — "
                            f"предел предупреждений ({_esc(reason)}).")
            await log_action("🔇", f"Мут {MUTE_MINUTES} мин (авто)", message.chat,
                             "автомодерация", name, uid, reason)
        else:
            await send_temp(message.chat.id,
                            f"⚠️ <b>{_esc(name)}</b>, предупреждение {n}/{WARN_MUTE_AT} — {_esc(reason)}.")
            await log_action("⚠️", f"Предупреждение {n}/{WARN_MUTE_AT} (авто)", message.chat,
                             "автомодерация", name, uid, reason)

    @dp.message()
    async def moderate(message: Message):
        if message.chat.type == "private":
            return
        if RULES_CHAT_ID and str(message.chat.id) != str(RULES_CHAT_ID):
            return
        if not message.from_user or message.sender_chat or message.from_user.is_bot:
            return  # каналы/анонимные админы/боты не модерируем
        uid = message.from_user.id
        if await is_admin(message.chat.id, uid):
            return
        key = (message.chat.id, uid)
        name = message.from_user.full_name

        # спам стикерами: до 3 подряд, 4-й — нарушение
        if message.sticker:
            streak = _stickers.get(key, 0) + 1
            _stickers[key] = streak
            if streak > STICKER_MAX:
                try:
                    await message.delete()
                except Exception:
                    pass
                await add_warn(message, name, "спам стикерами")
            return
        _stickers[key] = 0

        text = message.text or message.caption or ""

        # реклама/ссылки — отдельный 2-страйк → бан
        if bad_link(text):
            try:
                await message.delete()
            except Exception:
                pass
            lst = _prune(_link_strikes.get(key, []), WARN_EXPIRE)
            lst.append(time.time())
            _link_strikes[key] = lst
            if len(lst) >= LINK_BAN_AT:
                try:
                    await bot.ban_chat_member(message.chat.id, uid)
                except Exception:
                    pass
                await send_temp(message.chat.id,
                                f"🚫 <b>{_esc(name)}</b> забанен(а) за рекламу/ссылки (повторно).")
                await log_action("🚫", "Бан (реклама/ссылки, авто)", message.chat,
                                 "автомодерация", name, uid, "реклама/ссылки, 2-е нарушение")
            else:
                await send_temp(message.chat.id,
                                f"⚠️ <b>{_esc(name)}</b>, ссылки и реклама запрещены — сообщение удалено. "
                                "Повтор — бан.")
                await log_action("🔗", "Удаление (реклама/ссылки, авто)", message.chat,
                                 "автомодерация", name, uid, "реклама/ссылки, 1-е предупреждение")
            return

        # мат/оскорбления
        if has_profanity(text):
            try:
                await message.delete()
            except Exception:
                pass
            await add_warn(message, name, "мат/оскорбления")
            return

        # капс
        if is_caps(text):
            try:
                await message.delete()
            except Exception:
                pass
            await add_warn(message, name, "капс")
            return

        # эмодзи-спам
        if emoji_count(text) > EMOJI_MAX:
            try:
                await message.delete()
            except Exception:
                pass
            await add_warn(message, name, "эмодзи-спам")
            return

        # флуд
        times = _prune(_msg_times.get(key, []), FLOOD_WINDOW)
        times.append(time.time())
        _msg_times[key] = times
        if len(times) > FLOOD_MAX:
            try:
                await message.delete()
            except Exception:
                pass
            await add_warn(message, name, "флуд")

    log.info("Guard-бот запущен. RULES_CHAT_ID=%r, ADMIN=%s, таймаут=%s c",
             RULES_CHAT_ID, GUARD_ADMIN_ID, RULES_TIMEOUT)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run())
