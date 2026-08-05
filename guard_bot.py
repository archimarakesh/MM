"""Magic Market — Guard: капча-ознакомление, чистка системных сообщений,
модерация (бан/кик/мут) и журнал действий в личку админу.

Свой код и ОТДЕЛЬНЫЙ бот (токен GUARD_BOT_TOKEN), запускается в процессе магазина
(main.py вызывает run() фоновой задачей).

ENV:
  GUARD_BOT_TOKEN — токен отдельного бота (BotFather). Без него guard не запускается.
  GUARD_ADMIN_ID  — кому слать журнал (по умолчанию = ADMIN_ID). Админ должен нажать
                    Start в личке guard-бота, иначе журнал не дойдёт.
  RULES_CHAT_ID   — ID чата (если задан — guard работает только там).
  RULES_TIMEOUT   — секунд на ознакомление (по умолчанию 600 = 10 мин).
Guard-бот должен быть админом чата: ограничивать/банить участников и удалять сообщения.
"""
import asyncio
import html
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import award_image
import db
import quiz_bank as qb

KYIV = ZoneInfo("Europe/Kyiv")
ACTIVITY_PRIZES = [400, 300, 200]   # призы за 1-3 место, ₴ на баланс (бонусные, как приветственные)
ACTIVITY_HOUR = 12                  # понедельник, по Киеву
CONTEST_TIMES = os.getenv("CONTEST_TIMES", "11:00,19:00")   # баннер конкурса, по Киеву
CONTEST_BANNER = os.path.join("promo", "contest.png")
CONTEST_CAPTION = ("🏆 <b>Конкурс активности</b>\n"
                   "Топ-3 самых активных в чате за неделю получают "
                   "<b>400 / 300 / 200 ₴</b> на баланс магазина.\n"
                   "Итоги — каждый понедельник в 12:00. <b>/top</b> — текущий рейтинг.")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("guard")

GUARD_BOT_TOKEN = os.getenv("GUARD_BOT_TOKEN", "")
GUARD_ADMIN_ID = int(os.getenv("GUARD_ADMIN_ID", os.getenv("ADMIN_ID", "0")) or 0)
RULES_CHAT_ID = os.getenv("RULES_CHAT_ID", "")
RULES_TIMEOUT = int(os.getenv("RULES_TIMEOUT", "600") or 600)
# канал для /stats (подписки/отписки). По умолчанию берём промо/бонус-канал.
STATS_CHANNEL_ID = os.getenv("STATS_CHANNEL_ID",
                             os.getenv("PROMO_CHANNEL_ID", os.getenv("BONUS_CHANNEL_ID", "")))


def _stats_chat_ids() -> dict:
    """{chat_id(int): подпись} — чат и канал, по которым ведём статистику."""
    out = {}
    if RULES_CHAT_ID:
        try:
            out[int(RULES_CHAT_ID)] = "💬 Чат"
        except ValueError:
            pass
    if STATS_CHANNEL_ID:
        try:
            out[int(STATS_CHANNEL_ID)] = "📣 Канал"
        except ValueError:
            pass
    return out


# ── тематическая викторина ──────────────────────────────────────────────────
QUIZ_ENABLED = os.getenv("QUIZ_ENABLED", "1") not in ("0", "false", "")
QUIZ_TIME = os.getenv("QUIZ_TIME", "18:00")               # старт по Киеву
QUIZ_QUESTIONS = int(os.getenv("QUIZ_QUESTIONS", "5") or 5)
QUIZ_Q_PRIZE = int(os.getenv("QUIZ_Q_PRIZE", "20") or 20)    # за каждый правильный
QUIZ_WIN_PRIZE = int(os.getenv("QUIZ_WIN_PRIZE", "50") or 50)  # лучшему по итогу
QUIZ_POLL_SEC = int(os.getenv("QUIZ_POLL_SEC", str(10 * 60)) or 10 * 60)  # голосование за тему
QUIZ_Q_SEC = int(os.getenv("QUIZ_Q_SEC", "150") or 150)     # время на вопрос
# ── недельная реферальная гонка ─────────────────────────────────────────────
REF_RACE_PRIZE = int(os.getenv("REF_RACE_PRIZE", "1000") or 1000)
REF_RACE_MIN_TOTAL = int(os.getenv("REF_RACE_MIN_TOTAL", "20") or 20)  # общий порог за неделю
REF_RACE_HOUR = int(os.getenv("REF_RACE_HOUR", "13") or 13)  # понедельник, Киев
# где проверяем, что приглашённый не отписался (канал приоритетнее чата).
# Бот должен быть админом этой цели, иначе проверка пропускается (все засчитываются).
REF_CHECK_CHAT = os.getenv("REF_CHECK_CHAT", "") or STATS_CHANNEL_ID or RULES_CHAT_ID


def _week_start(d):
    """Понедельник недели даты d (date)."""
    return d - timedelta(days=d.weekday())

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
_bad_cd: dict = {}      # key -> метка времени последнего предупреждения за мат
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
# ловим только прямые оскорбления в адрес людей; «литературный» мат
# (бля, пиздец, нахуй, ебать и т.п. как междометия) — допустим
_BAD_RE = re.compile(
    r"(мудак|муд[ао]з|пид[ао]р|педик|гандон|гондон|долбо[ёе]б"
    r"|ублюдок|у[ёе]бок|у[ёе]бищ|мраз[ьи]|чмо\b|шлюх|потаскух|сучен)",
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


BOT = None   # экземпляр guard-бота (админ чата) — им банит и основной модуль


async def run(notify=None, on_ban=None, on_unban=None):
    """Запуск guard-бота (polling). No-op, если GUARD_BOT_TOKEN не задан.
    notify — отправка личного сообщения через основной бот (для победителей).
    on_ban/on_unban(uid, reason) — зеркалим ручной бан в чате в универсальный
    бан (флаг в БД + бан в казино/маркете): «где бы ни забанил, банится везде»."""
    if not GUARD_BOT_TOKEN:
        log.info("GUARD_BOT_TOKEN не задан — guard-бот выключен")
        return

    from aiogram import Bot, Dispatcher, F
    from aiogram.enums import ContentType
    from aiogram.filters import Command, CommandObject
    from aiogram.types import (CallbackQuery, ChatPermissions, FSInputFile,
                               InlineKeyboardButton, InlineKeyboardMarkup, Message)

    global BOT
    bot = Bot(GUARD_BOT_TOKEN)
    BOT = bot                         # доступен основному модулю для Telegram-бана
    dp = Dispatcher()

    async def _universal_ban(uid, reason=""):
        if on_ban:
            try:
                await on_ban(int(uid), reason, "chat")
            except Exception:
                log.exception("Универсальный бан %s не прошёл", uid)

    async def _universal_unban(uid):
        if on_unban:
            try:
                await on_unban(int(uid), "chat")
            except Exception:
                log.exception("Универсальный разбан %s не прошёл", uid)

    async def _confirm_and_ban(chat_id, uid, reason="бан в чате"):
        """Зеркалим в универсальный бан ТОЛЬКО настоящий бан. Кик (авто-кик за
        правила, /kick) — это ban+unban: статус тут же возвращается в 'left'.
        Поэтому ждём и перепроверяем: если через паузу всё ещё 'kicked' —
        это реальный бан, иначе (кик) ничего не делаем."""
        await asyncio.sleep(6)
        try:
            m = await bot.get_chat_member(chat_id, uid)
        except Exception:
            return
        if getattr(m, "status", "") == "kicked":
            await _universal_ban(uid, reason)
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

    async def journal(text: str, kb=None):
        """Запись в журнал — в личку админу (виден только ему)."""
        if not GUARD_ADMIN_ID:
            return
        try:
            await bot.send_message(GUARD_ADMIN_ID, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            log.warning("Журнал не доставлен — админ не нажал Start у guard-бота?")

    async def log_action(icon: str, action: str, chat, by, target_name, target_id,
                         reason: str, snippet: str | None = None):
        extra = f"\n✉️ Сообщение: «{_esc(str(snippet)[:200])}»" if snippet else ""
        await journal(
            f"{icon} <b>{action}</b>\n"
            f"👤 Кого: {_esc(target_name)} (ID <code>{target_id}</code>)\n"
            f"🛡 Кто: {_esc(by)}\n"
            f"💬 Чат: {_esc(getattr(chat, 'title', chat))}\n"
            f"📝 Причина: {_esc(reason or 'не указана')}"
            f"{extra}\n"
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
                # tg://user — настоящий тег: кликабелен и шлёт уведомление даже без @username
                f"👋 <a href=\"tg://user?id={u.id}\"><b>{_esc(u.full_name)}</b></a>, "
                f"добро пожаловать!\n\n{RULES_TEXT}\n\n"
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

    # ── учёт подписок/отписок чата и канала (для /stats) ──────────────────────
    def _is_member(m) -> bool:
        """Считается ли статус «в чате/канале»."""
        st = getattr(m, "status", None)
        st = getattr(st, "value", st)          # ChatMemberStatus enum → строка
        if st in ("creator", "administrator", "member"):
            return True
        if st == "restricted":
            return bool(getattr(m, "is_member", False))
        return False

    @dp.chat_member()
    async def on_member(ev):
        """Вход/выход участника (chat_member). Работает и для чата, и для канала —
        бот должен быть админом. Считаем только отслеживаемые чат/канал."""
        chat_ids = _stats_chat_ids()
        if ev.chat.id not in chat_ids:
            return
        try:
            was, now = _is_member(ev.old_chat_member), _is_member(ev.new_chat_member)
        except Exception:
            return
        u = ev.new_chat_member.user
        if getattr(u, "is_bot", False):
            return
        # ручной бан в чате/канале (кем угодно — админом, Telegram-UI) → зеркалим
        # в универсальный бан: флаг в БД + бан в казино/маркете и других чатах
        new_status = getattr(ev.new_chat_member, "status", "")
        if new_status == "kicked" and getattr(ev.old_chat_member, "status", "") != "kicked":
            # НЕ баним сразу: авто-кик за правила и /kick делают ban+unban и дают
            # мгновенный статус 'kicked'. Перепроверяем через паузу — если всё ещё
            # забанен, это настоящий бан; кик — нет.
            asyncio.create_task(_confirm_and_ban(ev.chat.id, u.id))
        if was == now:
            return                              # смена роли, не вход/выход
        direction = "join" if now else "leave"
        try:
            await db.record_member_event(ev.chat.id, u.id, direction, u.full_name)
        except Exception:
            log.exception("Не записалось событие участника %s", u.id)

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
        # отметка в базе магазина: без неё бонус за подписку не выдаётся
        try:
            await db.accept_rules(target)
        except Exception:
            log.exception("Не записалось принятие правил: %s", target)
        try:
            await cb.message.delete()
        except Exception:
            pass
        await cb.answer("Добро пожаловать! Доступ в чат открыт ✅")

    # ── модерация (только админы чата или GUARD_ADMIN_ID) ──────────────────────
    @dp.message(Command("ban"))
    async def cmd_ban(message: Message, command: CommandObject):
        priv = message.chat.type == "private"
        # в личке команду принимаем только от владельца (GUARD_ADMIN_ID)
        if priv:
            if message.from_user.id != GUARD_ADMIN_ID:
                return
        elif not await is_admin(message.chat.id, message.from_user.id):
            return
        uid, name, reason = await resolve_target(message, command)
        if not uid:
            return await message.reply(
                "Кого банить? Пришлите: <code>/ban ID причина</code>" if priv
                else "Ответьте на сообщение или: /ban &lt;id&gt; &lt;причина&gt;",
                parse_mode="HTML")
        if not priv:
            try:
                await bot.ban_chat_member(message.chat.id, uid)
            except Exception:
                return await message.reply("Не удалось забанить — проверьте права бота")
        await _universal_ban(uid, reason or "бан командой /ban")   # везде сразу
        if priv:
            return await message.answer(
                f"🚫 <b>{_esc(name)}</b> забанен везде — казино, маркет и чаты.",
                parse_mode="HTML")
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
        priv = message.chat.type == "private"
        if priv:
            if message.from_user.id != GUARD_ADMIN_ID:
                return
        elif not await is_admin(message.chat.id, message.from_user.id):
            return
        uid, name, reason = await resolve_target(message, command)
        if not uid:
            return await message.reply(
                "Кого разбанить? Пришлите: <code>/unban ID</code>" if priv
                else "Укажите: /unban &lt;id&gt;", parse_mode="HTML")
        if not priv:
            try:
                await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
            except Exception:
                return await message.reply("Не удалось — проверьте права бота")
        await _universal_unban(uid)          # разбан везде: казино/маркет и чаты
        if priv:
            return await message.answer(
                f"✅ <b>{_esc(name)}</b> разбанен везде — доступ вернулся.",
                parse_mode="HTML")
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
        # кнопка принятия и здесь: старожилы (до появления приветствия) могут
        # подтвердить правила в любой момент — это условие бонуса за подписку
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Ознакомлен(а) с правилами",
                                 callback_data=f"ack:{message.from_user.id}"),
        ]])
        await bot.send_message(message.chat.id, RULES_TEXT, parse_mode="HTML",
                               reply_markup=kb)

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

    @dp.callback_query(F.data.startswith(("gban:", "gfree:", "gunmute:")))
    async def on_guard_decision(cb: CallbackQuery):
        """Решения админа из журнала: бан кандидата, помилование, снятие мута."""
        if cb.from_user.id != GUARD_ADMIN_ID:
            return await cb.answer("Только для администратора", show_alert=True)
        try:
            act, chat_id, uid = cb.data.split(":")
            chat_id, uid = int(chat_id), int(uid)
        except (ValueError, IndexError):
            return await cb.answer()
        if act == "gban":
            try:
                await bot.ban_chat_member(chat_id, uid)
                done = "🚫 Забанен(а)"
            except Exception:
                return await cb.answer("Не удалось — проверьте права бота", show_alert=True)
            await _universal_ban(uid, "бан из журнала guard")   # везде сразу
        else:
            try:
                await bot.restrict_chat_member(chat_id, uid, permissions=OPEN)
                done = "🔊 Мут снят" if act == "gunmute" else "✅ Помилован(а), мут снят"
            except Exception:
                return await cb.answer("Не удалось — проверьте права бота", show_alert=True)
        try:
            await cb.message.edit_text(cb.message.html_text + f"\n\n<b>{done}</b>",
                                       parse_mode="HTML")
        except Exception:
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await cb.answer(done)

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
        # текст нарушения — в журнал, иначе не разобрать, за что прилетело
        snippet = (message.text or message.caption
                   or ("[стикер]" if message.sticker else "[медиа]"))
        try:
            await db.bump_strike(uid, name)   # нарушение бьёт по недельному рейтингу активности
        except Exception:
            log.warning("Не удалось записать нарушение в активность")
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
            await journal(
                f"🔇 <b>Мут {MUTE_MINUTES} мин (авто)</b>\n"
                f"👤 Кого: {_esc(name)} (ID <code>{uid}</code>)\n"
                f"💬 Чат: {_esc(message.chat.title)}\n"
                f"📝 Причина: {_esc(reason)}\n"
                f"✉️ Сообщение: «{_esc(str(snippet)[:200])}»\n🕒 {_now()}",
                InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔊 Снять мут",
                                         callback_data=f"gunmute:{message.chat.id}:{uid}"),
                ]]))
        else:
            await send_temp(message.chat.id,
                            f"⚠️ <b>{_esc(name)}</b>, предупреждение {n}/{WARN_MUTE_AT} — {_esc(reason)}.")
            await log_action("⚠️", f"Предупреждение {n}/{WARN_MUTE_AT} (авто)", message.chat,
                             "автомодерация", name, uid, reason, snippet)

    @dp.message(Command("acheck"))
    async def cmd_acheck(message: Message):
        """Самопроверка счётчика активности — только для админа."""
        if message.from_user.id != GUARD_ADMIN_ID:
            return
        today = datetime.now(KYIV).date()
        start = today - timedelta(days=today.weekday())
        try:
            report = await db.activity_selftest(start)
            await message.answer(f"✅ Счётчик активности:\n{report}")
        except Exception as e:
            await message.answer(f"❌ Счётчик сломан:\n<code>{_esc(str(e)[:400])}</code>",
                                 parse_mode="HTML")

    @dp.message(Command("top"))
    async def cmd_top(message: Message):
        """Текущий рейтинг активности за идущую неделю."""
        if RULES_CHAT_ID and str(message.chat.id) != str(RULES_CHAT_ID):
            return
        today = datetime.now(KYIV).date()
        start = today - timedelta(days=today.weekday())     # понедельник этой недели
        try:
            rows = await db.top_activity(start, today, 10)
        except Exception:
            return
        if not rows:
            await message.answer("На этой неделе очков пока никто не набрал.")
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"🏆 <b>Топ активных за неделю</b> (с {start.strftime('%d.%m')})", ""]
        for i, r in enumerate(rows):
            mark = medals[i] if i < 3 else f"{i + 1}."
            prize = f" · <b>{ACTIVITY_PRIZES[i]} ₴</b>" if i < len(ACTIVITY_PRIZES) else ""
            lines.append(f"{mark} {_esc(r['name'])} — {r['score']} очк.{prize}")
        lines += ["", "Награждение — в понедельник в 12:00. Считаются содержательные "
                      "сообщения; флуд и нарушения снижают счёт."]
        await message.answer("\n".join(lines), parse_mode="HTML")

    async def _chat_total(chat_id: int):
        try:
            return await bot.get_chat_member_count(chat_id)
        except Exception:
            return None

    async def build_stats() -> str:
        """Отчёт по подпискам/отпискам чата и канала."""
        chat_ids = _stats_chat_ids()
        if not chat_ids:
            return ("Не задан ни чат, ни канал.\n"
                    "Укажите <code>RULES_CHAT_ID</code> и/или <code>STATS_CHANNEL_ID</code>.")
        now = datetime.now(timezone.utc)
        day_start = datetime.now(KYIV).replace(hour=0, minute=0, second=0, microsecond=0)
        periods = [("сегодня", day_start),
                   ("7 дней", now - timedelta(days=7)),
                   ("30 дней", now - timedelta(days=30))]
        blocks = []
        for chat_id, label in chat_ids.items():
            total = await _chat_total(chat_id)
            head = f"{label} — сейчас <b>{total if total is not None else '—'}</b>"
            lines = [head]
            for pname, since in periods:
                try:
                    f = await db.member_flow(chat_id, since)
                except Exception:
                    lines.append(f"  • {pname}: —")
                    continue
                sign = "+" if f["net"] >= 0 else ""
                lines.append(
                    f"  • {pname}: +{f['joined']} / −{f['left']} "
                    f"(чистыми {sign}{f['net']})")
            try:
                since_ts = await db.member_tracking_since(chat_id)
            except Exception:
                since_ts = None
            if since_ts:
                lines.append(f"  <i>учёт с {since_ts.astimezone(KYIV).strftime('%d.%m.%Y')}</i>")
            else:
                lines.append("  <i>данных пока нет — учёт только начался</i>")
            blocks.append("\n".join(lines))
        return ("📊 <b>Статистика подписок</b>\n"
                "<i>подписалось / отписалось · чистый прирост</i>\n\n"
                + "\n\n".join(blocks)
                + "\n\n<i>Telegram не отдаёт историю — считаем с момента запуска.</i>")

    @dp.message(Command("stats", "стата", "статистика"))
    async def cmd_stats(message: Message):
        """Отчёт по подпискам чата/канала — только для админа."""
        if message.from_user.id != GUARD_ADMIN_ID and \
           not await is_admin(message.chat.id, message.from_user.id):
            return
        try:
            report = await build_stats()
        except Exception as e:
            report = f"❌ Не удалось собрать статистику:\n<code>{_esc(str(e)[:400])}</code>"
        await message.answer(report, parse_mode="HTML")

    async def snapshot_loop():
        """Раз в 6 часов пишем текущее число участников — базовая линия и график."""
        while True:
            try:
                await asyncio.sleep(6 * 3600)
                for chat_id in _stats_chat_ids():
                    total = await _chat_total(chat_id)
                    if total is not None:
                        await db.save_member_snapshot(chat_id, total)
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Ошибка снимка участников")
                await asyncio.sleep(300)

    _last_pt = {}                                          # анти-очередь по очкам
    WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ]{3,}")   # хотя бы одно живое слово

    async def track_activity(uid: int, name: str, message):
        """Очко за содержательное сообщение. Короткие реплики, команды, эмодзи,
        стикеры и очереди подряд не считаются — иначе приз можно нафлудить."""
        text = (message.text or message.caption or "").strip()
        if len(text) < 4 or text.startswith("/") or not WORD_RE.search(text):
            return
        if time.time() - _last_pt.get(uid, 0) < 20:        # не чаще очка в 20 секунд
            return
        _last_pt[uid] = time.time()
        try:
            await db.bump_activity(uid, name)
        except Exception:
            # полный стектрейс: молчаливая ошибка тут уже прятала сломанную схему
            log.exception("Не удалось записать активность %s", uid)

    async def post_contest():
        """Баннер конкурса: предыдущий удаляем, чтобы в чате всегда висел один."""
        if not (RULES_CHAT_ID and os.path.exists(CONTEST_BANNER)):
            return
        prev = await db.get_kv("contest_msg")
        if prev and ":" in prev:
            chat, mid = prev.rsplit(":", 1)
            try:
                await bot.delete_message(int(chat), int(mid))
            except Exception:
                pass          # уже удалён вручную / нет прав — не критично
        try:
            m = await bot.send_photo(int(RULES_CHAT_ID), FSInputFile(CONTEST_BANNER),
                                     caption=CONTEST_CAPTION, parse_mode="HTML")
            await db.set_kv("contest_msg", f"{int(RULES_CHAT_ID)}:{m.message_id}")
        except Exception:
            log.warning("Не удалось опубликовать баннер конкурса")

    async def contest_poster():
        """Публикация баннера конкурса по расписанию (по умолчанию 11:00 и 19:00)."""
        if not RULES_CHAT_ID:
            return
        times = [t.strip() for t in CONTEST_TIMES.split(",") if t.strip()]
        while True:
            try:
                now = datetime.now(KYIV)
                slots = []
                for t in times:
                    hh, mm = map(int, t.split(":"))
                    slots.append(now.replace(hour=hh, minute=mm, second=0, microsecond=0))
                future = [s for s in slots if s > now]
                nxt = min(future) if future else min(slots) + timedelta(days=1)
                await asyncio.sleep(max(30, (nxt - now).total_seconds()))
                await post_contest()
            except Exception:
                log.exception("Ошибка постинга баннера конкурса")
                await asyncio.sleep(300)

    async def announce_winners(winners, start, end):
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"🏆 <b>Самые активные за неделю</b> "
                 f"({start.strftime('%d.%m')}–{end.strftime('%d.%m')})", ""]
        for w in winners:
            lines.append(f"{medals[w['place'] - 1]} <b>{_esc(w['name'])}</b> — "
                         f"<b>{w['amount']} ₴</b> на баланс "
                         f"<i>({w['points']} сообщ., дней в чате: {w['days']})</i>")
        lines += ["", "Бонус уже на балансе — можно потратить в магазине.",
                  "Неделя началась заново. Считаются содержательные сообщения, флуд — нет 😉"]
        text = "\n".join(lines)
        img = award_image.render(winners, os.path.join("promo", "award_week.png"))
        try:
            if img:   # баннер с никами победителей
                await bot.send_photo(int(RULES_CHAT_ID), FSInputFile(img),
                                     caption=text, parse_mode="HTML")
            else:
                await bot.send_message(int(RULES_CHAT_ID), text, parse_mode="HTML")
        except Exception:
            log.warning("Не удалось объявить победителей в чате")
        for w in winners:
            if notify:
                try:
                    await notify(w["user_id"],
                                 f"🏆 Вы вошли в топ-{w['place']} самых активных в чате за неделю!\n"
                                 f"Начислено <b>{w['amount']} ₴</b> на баланс.")
                except Exception:
                    pass

    async def weekly_awards():
        """Каждый понедельник в 12:00 по Киеву награждаем тройку самых активных."""
        if not RULES_CHAT_ID:
            log.info("RULES_CHAT_ID не задан — награды за активность выключены")
            return
        while True:
            try:
                now = datetime.now(KYIV)
                nxt = now.replace(hour=ACTIVITY_HOUR, minute=0, second=0, microsecond=0)
                days = (7 - now.weekday()) % 7          # weekday(): понедельник = 0
                if days == 0 and nxt <= now:
                    days = 7
                nxt += timedelta(days=days)
                await asyncio.sleep(max(30, (nxt - now).total_seconds()))
                end = nxt.date() - timedelta(days=1)    # вчера — воскресенье
                start = end - timedelta(days=6)         # прошлый понедельник
                winners = await db.award_activity_week(start, end, ACTIVITY_PRIZES)
                if winners:
                    await announce_winners(winners, start, end)
                    log.info("Награды за активность выданы: %s", len(winners))
                else:
                    log.info("Награды за активность: победителей нет (или уже выданы)")
            except Exception:
                log.exception("Ошибка наград за активность")
                await asyncio.sleep(300)

    # ── тематическая викторина: опрос темы → вопросы-опросы → призы ──────────
    # poll_id/correct — активный вопрос-опрос; future ставит победителя.
    # chat — где идёт викторина; allow_admin — засчитывать ответ владельца (тест в ЛС)
    quiz_state = {"poll_id": None, "correct": None, "future": None,
                  "chat": None, "allow_admin": False}
    quiz_busy = {"on": False}

    async def quiz_send(text, **kw):
        return await bot.send_message(int(RULES_CHAT_ID), text, parse_mode="HTML", **kw)

    async def pick_fresh_questions(key, title, persist=True):
        """QUIZ_QUESTIONS вопросов темы, не повторяя уже заданные. Когда свежие
        в теме заканчиваются — уведомляем админа и начинаем круг заново.
        persist=False (тест в ЛС) — просто случайные, банк не расходуем."""
        pool = qb.theme_questions(key)
        if not persist:
            random.shuffle(pool)
            return pool[:QUIZ_QUESTIONS]
        hmap = {qb.qhash(item[0]): item for item in pool}
        try:
            asked = await db.quiz_asked_hashes(key)
        except Exception:
            asked = set()
        fresh = [h for h in hmap if h not in asked]
        if len(fresh) < QUIZ_QUESTIONS:
            if asked:                                   # реально исчерпали банк темы
                await journal(
                    f"🔔 <b>Викторина: вопросы кончаются</b>\n"
                    f"Тема «{_esc(title)}» — все {len(hmap)} вопросов уже задавались, "
                    f"начинаю круг заново.\nСтоит добавить свежие вопросы в "
                    f"<code>quiz_bank.py</code> (тема <code>{key}</code>).")
            try:
                await db.quiz_reset_theme(key)
            except Exception:
                pass
            fresh = list(hmap.keys())
        chosen = random.sample(fresh, min(QUIZ_QUESTIONS, len(fresh)))
        try:
            await db.quiz_mark_asked(key, chosen)
        except Exception:
            pass
        return [hmap[h] for h in chosen]

    async def run_quiz(force=False, poll_sec=None, chat=None, dry=False, allow_admin=False):
        if quiz_busy["on"]:
            return
        target = chat if chat is not None else (int(RULES_CHAT_ID) if RULES_CHAT_ID else None)
        if target is None:
            return
        today = datetime.now(KYIV).date()
        if not force and not await db.quiz_claim_day(today):
            return                                    # сегодня уже запускали
        quiz_busy["on"] = True
        quiz_state["chat"], quiz_state["allow_admin"] = target, allow_admin

        async def send(text):
            return await bot.send_message(target, text, parse_mode="HTML")

        try:
            # темы идут по кругу: показываем только ещё не выпадавшие в этом круге,
            # круг пройден — начинаем заново (в тесте круг не расходуем)
            all_keys = qb.theme_keys()
            if dry:
                avail = list(all_keys)
            else:
                used = await db.quiz_used_themes()
                avail = [k for k in all_keys if k not in used]
                if not avail:
                    await db.quiz_reset_theme_cycle()
                    avail = list(all_keys)
                    try:
                        await send("🔄 Все темы прошли круг — начинаем новый!")
                    except Exception:
                        pass
            if len(avail) == 1:
                key = avail[0]                        # тема одна — без опроса
            else:
                labels = [f"{qb.theme_meta(k)[1]} {qb.theme_meta(k)[0]}" for k in avail]
                try:
                    poll = await bot.send_poll(
                        target, "🧠 Тема викторины? Голосуйте — запускаю по итогам!",
                        labels, is_anonymous=True)
                except Exception:
                    log.warning("Викторина: опрос не отправлен (бот админ в чате?)")
                    return
                await asyncio.sleep(max(15, poll_sec if poll_sec is not None else QUIZ_POLL_SEC))
                try:
                    stopped = await bot.stop_poll(target, poll.message_id)
                    counts = [o.voter_count for o in stopped.options]
                except Exception:
                    counts = []
                if counts and max(counts) > 0:
                    best = max(counts)
                    idx = random.choice([i for i, c in enumerate(counts) if c == best])
                else:
                    idx = random.randrange(len(avail))  # никто не голосовал — случайная
                key = avail[idx]
            title, emoji = qb.theme_meta(key)
            if not dry:
                await db.quiz_mark_theme_used(key)      # эта тема выпала в круге
            questions = await pick_fresh_questions(key, title, persist=not dry)
            await send(
                f"🏆 Тема: <b>{emoji} {_esc(title)}</b>\n"
                f"{len(questions)} вопросов · за верный <b>+{QUIZ_Q_PRIZE} ₴</b>, "
                f"лучшему <b>+{QUIZ_WIN_PRIZE} ₴</b>."
                + ("\n🧪 Тестовый режим — призы не начисляются."
                   if dry else "\nТапайте вариант в опросе — засчитывается первый верный!"))
            await asyncio.sleep(2)
            scores, names = {}, {}
            loop = asyncio.get_running_loop()
            for i, (q, correct, distractors) in enumerate(questions):
                opts, correct_id = qb.build_options(correct, distractors, random)
                fut = loop.create_future()
                quiz_state["future"] = fut
                quiz_state["correct"] = correct_id
                try:
                    poll_msg = await bot.send_poll(
                        target,
                        f"Вопрос {i+1}/{len(questions)} · {title}: {q}"[:300],
                        opts, type="quiz", correct_option_id=correct_id,
                        is_anonymous=False, open_period=max(5, min(600, QUIZ_Q_SEC)))
                    quiz_state["poll_id"] = poll_msg.poll.id
                except Exception:
                    log.exception("Викторина: вопрос-опрос не отправлен")
                    quiz_state["future"] = None
                    continue
                try:
                    uid, uname = await asyncio.wait_for(fut, timeout=QUIZ_Q_SEC + 3)
                    won = True
                except asyncio.TimeoutError:
                    won = False
                finally:
                    quiz_state["future"], quiz_state["poll_id"], quiz_state["correct"] = None, None, None
                if won:
                    scores[uid] = scores.get(uid, 0) + 1
                    names[uid] = uname
                    if not dry:
                        try:
                            await db.chat_reward(uid, uname, QUIZ_Q_PRIZE)
                        except Exception:
                            log.exception("Викторина: приз за вопрос не начислен")
                    bonus = "" if dry else f" <b>+{QUIZ_Q_PRIZE} ₴</b>"
                    await send(f"✅ Первым верно ответил <b>{_esc(uname)}</b>!{bonus}\n"
                               f"Правильный ответ: <b>{_esc(correct)}</b>")
                else:
                    await send(f"⏰ Время вышло. Правильный ответ: <b>{_esc(correct)}</b>")
                await asyncio.sleep(3)
            if scores:
                order = sorted(scores.items(), key=lambda kv: -kv[1])
                win_id, win_score = order[0]
                if not dry:
                    try:
                        await db.chat_reward(win_id, names[win_id], QUIZ_WIN_PRIZE)
                    except Exception:
                        log.exception("Викторина: приз победителю не начислен")
                medals = ["🥇", "🥈", "🥉"]
                board = "\n".join(
                    f"{medals[i] if i < 3 else f'{i+1}.'} {_esc(names[u])} — {sc} прав."
                    for i, (u, sc) in enumerate(order))
                tail = ("🧪 Тест окончен — призы не начислялись."
                        if dry else f"👑 Победитель дня: <b>{_esc(names[win_id])}</b> "
                                    f"(+{QUIZ_WIN_PRIZE} ₴ бонусом). Приз — на баланс магазина!")
                await send(f"🎉 <b>Викторина окончена!</b>\n\n{board}\n\n{tail}")
                if not dry and notify:
                    try:
                        await notify(win_id, f"👑 Вы победили в викторине дня! "
                                             f"Начислено {QUIZ_WIN_PRIZE} ₴ бонусом на баланс.")
                    except Exception:
                        pass
                if not force and not dry:
                    await db.quiz_finish_day(today, key, win_id, names[win_id], win_score)
            else:
                await send("Никто не ответил 🤷" + ("" if dry else " Ждём вас завтра!"))
                if not force and not dry:
                    await db.quiz_finish_day(today, key, None, None, 0)
        finally:
            quiz_state["poll_id"] = quiz_state["future"] = None
            quiz_state["correct"] = quiz_state["chat"] = None
            quiz_busy["on"] = False

    async def quiz_scheduler():
        if not (RULES_CHAT_ID and QUIZ_ENABLED):
            log.info("Викторина выключена (нет чата или QUIZ_ENABLED=0)")
            return
        try:
            hh, mm = map(int, QUIZ_TIME.split(":"))
        except ValueError:
            hh, mm = 18, 0
        while True:
            try:
                now = datetime.now(KYIV)
                nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if nxt <= now:
                    nxt += timedelta(days=1)
                await asyncio.sleep(max(30, (nxt - now).total_seconds()))
                await run_quiz()
            except Exception:
                log.exception("Ошибка викторины")
                await asyncio.sleep(300)

    _sub_cache = {}   # uid -> (subscribed:bool, ts) — кэш проверки подписки (5 мин)

    async def still_subscribed(uid: int) -> bool:
        """Приглашённый всё ещё подписан на канал/чат? Не смогли проверить
        (бот не админ / сбой) — не наказываем, считаем подписанным."""
        if not REF_CHECK_CHAT:
            return True
        hit = _sub_cache.get(uid)
        if hit and time.time() - hit[1] < 300:
            return hit[0]
        ok = True
        try:
            m = await bot.get_chat_member(int(REF_CHECK_CHAT), uid)
            st = getattr(m.status, "value", m.status)
            ok = st not in ("left", "kicked")
        except Exception:
            ok = True
        _sub_cache[uid] = (ok, time.time())
        return ok

    async def race_standings_live(ws, we):
        """Зачёт гонки с проверкой подписки: считаем только тех приглашённых,
        кто сейчас подписан. Возвращает (standings[], total)."""
        try:
            acts = await db.ref_race_activated(ws, we)
        except Exception:
            return [], 0
        counts, meta = {}, {}
        for a in acts:
            if await still_subscribed(a["friend_id"]):
                rid = a["referrer_id"]
                counts[rid] = counts.get(rid, 0) + 1
                meta[rid] = (a["name"], a["username"])
        standings = sorted(
            ({"user_id": rid, "cnt": n, "name": meta[rid][0], "username": meta[rid][1]}
             for rid, n in counts.items()),
            key=lambda x: (-x["cnt"], x["user_id"]))
        return standings, sum(counts.values())

    async def ref_race_weekly():
        if not RULES_CHAT_ID:
            return
        while True:
            try:
                now = datetime.now(KYIV)
                nxt = now.replace(hour=REF_RACE_HOUR, minute=0, second=0, microsecond=0)
                days = (7 - now.weekday()) % 7
                if days == 0 and nxt <= now:
                    days = 7
                nxt += timedelta(days=days)
                await asyncio.sleep(max(30, (nxt - now).total_seconds()))
                this_mon = nxt.date()
                prev_mon = this_mon - timedelta(days=7)
                # перепроверка подписки на выплате: отписавшиеся в зачёт не идут
                standings, total = await race_standings_live(prev_mon, this_mon)
                if total >= REF_RACE_MIN_TOTAL and standings:
                    w = standings[0]
                    if await db.ref_race_award_record(prev_mon, w["user_id"], w["cnt"], REF_RACE_PRIZE):
                        await quiz_send(
                            "🏁 <b>Реферальная гонка недели</b>\n\n"
                            f"Все вместе привели <b>{total}</b> друзей (подписанных) — цель взята! 🎯\n\n"
                            f"👑 Больше всех — <b>{_esc(w['name'])}</b> "
                            f"(<b>{w['cnt']}</b>). Приз <b>{REF_RACE_PRIZE} ₴</b> уже на балансе.\n"
                            "Спасибо всем, кто растит комьюнити 🔥")
                        if notify:
                            try:
                                await notify(w["user_id"],
                                             "🏁 Вы выиграли реферальную гонку недели! "
                                             f"Начислено {REF_RACE_PRIZE} ₴ бонусом.")
                            except Exception:
                                pass
                        log.info("Реф-гонка: победитель %s (%s реф., всего %s подписанных)",
                                 w["user_id"], w["cnt"], total)
                else:
                    log.info("Реф-гонка: общий порог %s (подписанных) за неделю не взят",
                             REF_RACE_MIN_TOTAL)
            except Exception:
                log.exception("Ошибка реф-гонки")
                await asyncio.sleep(300)

    @dp.message(Command("quiz", "викторина"))
    async def cmd_quiz(message: Message):
        """Ручной запуск викторины — только админ.
        В ЛС боту — приватный тест (в этом чате, без начисления призов)."""
        if message.from_user.id != GUARD_ADMIN_ID:
            return
        if quiz_busy["on"]:
            await message.answer("Викторина уже идёт 🙂")
            return
        if message.chat.type == "private":
            await message.answer("🧪 Запускаю тест викторины здесь, в ЛС — призы не "
                                 "начисляются. Проголосуй в опросе (20 сек) и отвечай на вопросы.")
            asyncio.create_task(run_quiz(force=True, poll_sec=20,
                                         chat=message.chat.id, dry=True, allow_admin=True))
        else:
            await message.answer("Запускаю тестовую викторину в чате (опрос темы — 30 сек).")
            asyncio.create_task(run_quiz(force=True, poll_sec=30))

    @dp.message(Command("gonka", "гонка", "race", "рефгонка"))
    async def cmd_gonka(message: Message):
        """Текущий зачёт реферальной гонки недели."""
        if RULES_CHAT_ID and message.chat.type != "private" \
                and str(message.chat.id) != str(RULES_CHAT_ID):
            return
        today = datetime.now(KYIV).date()
        ws = _week_start(today)
        we = ws + timedelta(days=7)
        try:
            rows, total = await race_standings_live(ws, we)   # только подписанные
        except Exception:
            return
        rows = rows[:10]
        reached = total >= REF_RACE_MIN_TOTAL
        goal = (f"Всего за неделю: <b>{total}</b> / {REF_RACE_MIN_TOTAL} "
                + ("✅ цель взята — топ-1 получит приз!" if reached
                   else f"(осталось {REF_RACE_MIN_TOTAL - total})"))
        head = ("🏁 <b>Реферальная гонка недели</b>\n"
                f"Общая цель: <b>{REF_RACE_MIN_TOTAL}+</b> приглашённых со всех участников — "
                f"тогда тот, кто привёл больше всех, забирает <b>{REF_RACE_PRIZE} ₴</b>. "
                f"Считаются только оставшиеся подписанными. Итоги — в понедельник.\n\n{goal}\n\n")
        if not rows:
            await message.answer(
                head + "Пока никто никого не привёл. Дерзай — ссылка в приложении, «Профиль»!",
                parse_mode="HTML")
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows):
            mark = medals[i] if i < 3 else f"{i+1}."
            nm = _esc(r["name"]) + (f" · @{_esc(r['username'])}" if r["username"] else "")
            lines.append(f"{mark} {nm} — <b>{r['cnt']}</b>")
        await message.answer(head + "\n".join(lines), parse_mode="HTML")

    @dp.poll_answer()
    async def on_poll_answer(ans):
        """Ответ на вопрос-опрос: первый верный вариант — победитель вопроса."""
        qs = quiz_state
        if not qs["poll_id"] or ans.poll_id != qs["poll_id"]:
            return
        if not ans.option_ids or ans.option_ids[0] != qs["correct"]:
            return                                     # выбран неверный вариант
        u = ans.user
        if not u or (u.id == GUARD_ADMIN_ID and not qs["allow_admin"]):
            return                                     # владельца в чате не считаем
        fut = qs["future"]
        if fut and not fut.done():
            qs["poll_id"] = None                       # закрываем сразу — гонок нет
            fut.set_result((u.id, u.full_name))

    @dp.message()
    async def moderate(message: Message):
        if message.chat.type == "private":
            return
        if RULES_CHAT_ID and str(message.chat.id) != str(RULES_CHAT_ID):
            return
        if not message.from_user or message.sender_chat or message.from_user.is_bot:
            return  # каналы/анонимные админы/боты не модерируем
        uid = message.from_user.id
        # очки — всем, кроме владельца (он платит призы); модерация админов
        # по-прежнему не касается
        if uid != GUARD_ADMIN_ID:
            await track_activity(uid, message.from_user.full_name, message)
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

        # реклама/ссылки — 2-й страйк: мут и решение о бане за админом
        if bad_link(text):
            try:
                await message.delete()
            except Exception:
                pass
            lst = _prune(_link_strikes.get(key, []), WARN_EXPIRE)
            lst.append(time.time())
            _link_strikes[key] = lst
            if len(lst) >= LINK_BAN_AT:
                until = datetime.now(timezone.utc) + timedelta(minutes=MUTE_MINUTES)
                try:
                    await bot.restrict_chat_member(message.chat.id, uid,
                                                   permissions=MUTED, until_date=until)
                except Exception:
                    pass
                await send_temp(message.chat.id,
                                f"🔇 <b>{_esc(name)}</b>: повторная реклама — мут, "
                                "решение о бане за администратором.")
                await journal(
                    f"🚫 <b>Кандидат на бан: реклама/ссылки повторно</b>\n"
                    f"👤 Кто: {_esc(name)} (ID <code>{uid}</code>)\n"
                    f"💬 Чат: {_esc(message.chat.title)}\n"
                    f"✉️ Сообщение: «{_esc(text[:200])}»\n"
                    f"🔇 Пока выдан мут на {MUTE_MINUTES} мин.\n🕒 {_now()}",
                    InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🚫 Забанить",
                                             callback_data=f"gban:{message.chat.id}:{uid}"),
                        InlineKeyboardButton(text="✅ Помиловать",
                                             callback_data=f"gfree:{message.chat.id}:{uid}"),
                    ]]))
            else:
                await send_temp(message.chat.id,
                                f"⚠️ <b>{_esc(name)}</b>, ссылки и реклама запрещены — сообщение удалено. "
                                "Повтор — бан.")
                await log_action("🔗", "Удаление (реклама/ссылки, авто)", message.chat,
                                 "автомодерация", name, uid, "реклама/ссылки, 1-е предупреждение", text)
            return

        # мат/оскорбления: сообщение убираем всегда, но предупреждение и страйк —
        # не чаще раза в 2 минуты, иначе серия из трёх словечек мгновенно даёт мут
        if has_profanity(text):
            try:
                await message.delete()
            except Exception:
                pass
            if time.time() - _bad_cd.get(key, 0) > 120:
                _bad_cd[key] = time.time()
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

    async def initial_snapshot():
        """Стартовый снимок — чтобы базовая линия и «учёт с …» появились сразу."""
        for chat_id in _stats_chat_ids():
            total = await _chat_total(chat_id)
            if total is not None:
                try:
                    await db.save_member_snapshot(chat_id, total)
                except Exception:
                    log.exception("Стартовый снимок не записан: %s", chat_id)

    log.info("Guard-бот запущен. RULES_CHAT_ID=%r, STATS_CHANNEL_ID=%r, ADMIN=%s, таймаут=%s c",
             RULES_CHAT_ID, STATS_CHANNEL_ID, GUARD_ADMIN_ID, RULES_TIMEOUT)
    awards = asyncio.create_task(weekly_awards())
    contest = asyncio.create_task(contest_poster())
    snaps = asyncio.create_task(snapshot_loop())
    quiz = asyncio.create_task(quiz_scheduler())
    race = asyncio.create_task(ref_race_weekly())
    await initial_snapshot()
    # chat_member нужно запросить явно: aiogram включит его в allowed_updates,
    # только если тип обновления зарегистрирован (у нас есть @dp.chat_member)
    allowed = dp.resolve_used_update_types()
    for u in ("chat_member", "poll_answer"):
        if u not in allowed:
            allowed.append(u)
    try:
        await dp.start_polling(bot, allowed_updates=allowed)
    finally:
        awards.cancel()
        contest.cancel()
        snaps.cancel()
        quiz.cancel()
        race.cancel()


if __name__ == "__main__":
    asyncio.run(run())
