"""Magic Market — хранилище PostgreSQL (asyncpg, Railway DATABASE_URL)."""
import base64
import hashlib
import json
import os
import random
import secrets
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO

import asyncpg
from PIL import Image

PIN_MAX_FAILS = 3
PIN_LOCK = timedelta(hours=1)


def _hash_pin(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 100_000).hex()

# уровни рефералки: (приглашено от, доля с покупок рефералов)
REF_TIERS = [(0, 0.05), (10, 0.07), (50, 0.10)]
TRANSFER_TTL = 15 * 60      # код переноса живёт 15 минут
ORDER_CODE_BASE = 1000      # MM-1001, MM-1002, ...
AUTO_DELIVER_DAYS = 5       # через сколько дней после отправки заказ считается полученным

DEFAULT_TIERS = [
    {"from": 1, "k": 1.00}, {"from": 10, "k": 0.90}, {"from": 25, "k": 0.80},
    {"from": 50, "k": 0.70}, {"from": 100, "k": 0.60},
]
MAX_GRAMS = 100
MAX_PHOTO_LEN = 400_000   # ~300 КБ картинки в base64
MAX_PHOTOS = 4
MAX_PENDING_ORDERS = 8    # незакрытых заказов на юзера (антиспам/сток)

# бухгалтерия: комиссия на вывод прибыли и фикс-расход на отправку посылки
WITHDRAW_FEE_PCT = int(os.getenv("WITHDRAW_FEE_PCT", "10") or 10)
PARCEL_COST = int(os.getenv("PARCEL_COST", "80") or 80)
MAX_PENDING_TOPUPS = 8    # квитанций на проверке на юзера

# ── вейджер (отыгрыш бонуса) ─────────────────────────────────────────────────
# Бонус (locked) нельзя вывести, пока не сделан оборот ставок = бонус × WAGER_X.
# Пока вейджер активен: выигрыш с чисто-бонусной игры «липкий» (тоже в locked),
# а ставка ограничена BONUS_MAX_BET — нельзя слить весь бонус в один спин.
BONUS_WAGER_X = int(os.getenv("BONUS_WAGER_X", "15") or 15)
BONUS_MAX_BET = int(os.getenv("BONUS_MAX_BET", "100") or 100)
SEED_PRODUCTS = [
    ("Golden Reserve", "Флагманская позиция", "🏆", "ХИТ", 120),
    ("Black Label", "Тёмная классика", "🖤", "", 95),
    ("Royal Amber", "Янтарная серия", "💎", "NEW", 150),
    ("Velvet Night", "Мягкий профиль", "🌙", "", 80),
    ("Imperial Gold", "Лимитированный выпуск", "👑", "LIMIT", 210),
    ("Silk Road", "Восточная коллекция", "🐫", "", 105),
]
# ключи реквизитов оплаты в settings
PAYMENT_KEYS = ["card_number", "card_holder",
                "wallet_trc20", "wallet_trx", "wallet_btc", "wallet_ltc",
                "wallet_ton", "wallet_eth", "wallet_bnb"]
INVOICE_TTL = 30 * 60       # крипто-счёт живёт 30 минут

_pool: asyncpg.Pool | None = None


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/mm")
    return url.replace("postgres://", "postgresql://", 1)


async def init():
    global _pool
    _pool = await asyncpg.create_pool(_dsn(), min_size=2, max_size=10,
                                      command_timeout=30)
    async with _pool.acquire() as c:
        await c.execute("""
            CREATE TABLE IF NOT EXISTS users(
                tg_id      BIGINT PRIMARY KEY,
                name       TEXT,
                username   TEXT,
                balance    BIGINT NOT NULL DEFAULT 0,
                ref_by     BIGINT,
                ref_earned BIGINT NOT NULL DEFAULT 0,
                created    TIMESTAMPTZ NOT NULL DEFAULT now());
            ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_claimed BOOLEAN NOT NULL DEFAULT false;
            -- заплатили ли пригласившему бонус за активацию этого друга (раз на друга)
            ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_activated BOOLEAN NOT NULL DEFAULT false;
            -- отметка «принял правила чата» (ставит guard-бот) — гейт для бонуса:
            -- иначе бонус успевали забрать до автокика за непринятие правил
            ALTER TABLE users ADD COLUMN IF NOT EXISTS rules_accepted TIMESTAMPTZ;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS device_hash TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_ip TEXT;
            -- locked: часть баланса (бонус), которую нельзя вывести — только потратить в магазине или отыграть в казино
            ALTER TABLE users ADD COLUMN IF NOT EXISTS locked BIGINT NOT NULL DEFAULT 0;
            -- wager_req: сколько ещё оборота ставок нужно, чтобы бонус «отыгрался»
            -- и locked стал выводимым. Начисляется = сумма бонуса × BONUS_WAGER_X.
            ALTER TABLE users ADD COLUMN IF NOT EXISTS wager_req BIGINT NOT NULL DEFAULT 0;
            -- серверный пин-код (хэш + соль), счётчик попыток и блокировка
            ALTER TABLE users ADD COLUMN IF NOT EXISTS pin_hash TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS pin_salt TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS pin_fails INT NOT NULL DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS pin_locked_until TIMESTAMPTZ;
            -- универсальный бан: один флаг на общей базе — им гейтятся оба
            -- приложения (маркет и казино), а Telegram-бан ставится отдельно
            ALTER TABLE users ADD COLUMN IF NOT EXISTS banned BOOLEAN NOT NULL DEFAULT false;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS banned_at TIMESTAMPTZ;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_reason TEXT;
            -- анти-абьюз реф-гонки: платформа (iPhone/Android + модель на Android),
            -- премиум и наличие фото телеграм-аккаунта — сигналы качества аккаунта
            ALTER TABLE users ADD COLUMN IF NOT EXISTS platform TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS tg_premium BOOLEAN;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS tg_photo BOOLEAN;
            CREATE TABLE IF NOT EXISTS bonus_claims(
                user_id     BIGINT PRIMARY KEY,
                device_hash TEXT,
                ip          TEXT,
                created     TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE INDEX IF NOT EXISTS bonus_claims_dev_idx ON bonus_claims(device_hash);
            CREATE INDEX IF NOT EXISTS bonus_claims_ip_idx ON bonus_claims(ip);
            CREATE TABLE IF NOT EXISTS products(
                id     BIGSERIAL PRIMARY KEY,
                name   TEXT NOT NULL,
                sub    TEXT DEFAULT '',
                emoji  TEXT DEFAULT '📦',
                tag    TEXT DEFAULT '',
                base   BIGINT NOT NULL,
                tiers  TEXT,
                active BOOLEAN NOT NULL DEFAULT true,
                pos    INT NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS orders(
                id      BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                product TEXT,
                grams   INT,
                total   BIGINT,
                status  INT NOT NULL DEFAULT 0,
                ttn     TEXT,
                ship    TEXT,
                date    TEXT);
            ALTER TABLE products ADD COLUMN IF NOT EXISTS photos TEXT;
            ALTER TABLE products ADD COLUMN IF NOT EXISTS stock INT;
            ALTER TABLE products ADD COLUMN IF NOT EXISTS genetics TEXT DEFAULT '';
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS product_id BIGINT;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipped_at TIMESTAMPTZ;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS pay TEXT;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS receipt TEXT;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS np_status INT;
            -- сколько из суммы заказа покрыто бонусами (locked) — для бухгалтерии
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS bonus_part BIGINT NOT NULL DEFAULT 0;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS np_status_text TEXT;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS np_eta TEXT;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS np_arrival TEXT;
            -- реальная метка времени заказа: date хранится строкой 'DD.MM.YYYY'
            -- (для показа), а агрегаты по времени считаем по created
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS created TIMESTAMPTZ NOT NULL DEFAULT now();
            -- взяли в работу (status→1): дедлайн отправки — ближайшая полночь Киева;
            -- delay_paid — за сколько просроченных суток уже начислена компенсация
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS worked_at TIMESTAMPTZ;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS delay_paid INT NOT NULL DEFAULT 0;
            UPDATE orders SET ship=NULL WHERE status=3 AND ship IS NOT NULL;
            CREATE INDEX IF NOT EXISTS orders_user_idx ON orders(user_id);
            CREATE TABLE IF NOT EXISTS topups(
                id      BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount  BIGINT NOT NULL,
                method  TEXT,
                receipt TEXT,
                status  INT NOT NULL DEFAULT 0,
                created TIMESTAMPTZ NOT NULL DEFAULT now(),
                decided TIMESTAMPTZ);
            CREATE TABLE IF NOT EXISTS invoices(
                id            BIGSERIAL PRIMARY KEY,
                user_id       BIGINT NOT NULL,
                amount_uah    BIGINT NOT NULL,
                currency      TEXT NOT NULL,
                amount_crypto TEXT NOT NULL,
                address       TEXT NOT NULL,
                status        INT NOT NULL DEFAULT 0,
                txid          TEXT,
                created       TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires       TIMESTAMPTZ NOT NULL);
            ALTER TABLE invoices ADD COLUMN IF NOT EXISTS order_id BIGINT;
            CREATE TABLE IF NOT EXISTS card_payments(
                id         BIGSERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                payment_id TEXT UNIQUE NOT NULL,
                card       TEXT,
                amount_uah BIGINT NOT NULL,
                pay_uah    BIGINT NOT NULL,
                status     INT NOT NULL DEFAULT 0,
                created    TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires    TIMESTAMPTZ NOT NULL);
            CREATE INDEX IF NOT EXISTS card_payments_user_idx ON card_payments(user_id);
            CREATE TABLE IF NOT EXISTS withdrawals(
                id         BIGSERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                amount     BIGINT NOT NULL,
                method     TEXT,
                requisites TEXT,
                status     INT NOT NULL DEFAULT 0,
                created    TIMESTAMPTZ NOT NULL DEFAULT now(),
                decided    TIMESTAMPTZ);
            CREATE TABLE IF NOT EXISTS ratings(
                order_id   BIGINT PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                product_id BIGINT NOT NULL,
                stars      INT NOT NULL);
            -- текстовый отзыв + режим подписи: 0 = ник и аватар, 1 = инкогнито
            ALTER TABLE ratings ADD COLUMN IF NOT EXISTS text    TEXT;
            ALTER TABLE ratings ADD COLUMN IF NOT EXISTS anon    INT NOT NULL DEFAULT 0;
            ALTER TABLE ratings ADD COLUMN IF NOT EXISTS name    TEXT;
            ALTER TABLE ratings ADD COLUMN IF NOT EXISTS avatar  TEXT;   -- data-URI мини-аватар (не для инкогнито)
            ALTER TABLE ratings ADD COLUMN IF NOT EXISTS hidden  INT NOT NULL DEFAULT 0;  -- модерация: 1 = скрыт
            ALTER TABLE ratings ADD COLUMN IF NOT EXISTS created TIMESTAMPTZ NOT NULL DEFAULT now();
            CREATE INDEX IF NOT EXISTS ratings_product_idx ON ratings(product_id);
            CREATE TABLE IF NOT EXISTS grow_plans(
                id         BIGSERIAL PRIMARY KEY,
                name       TEXT NOT NULL,
                sub        TEXT DEFAULT '',
                photo      TEXT,
                price      BIGINT NOT NULL,
                payout     BIGINT NOT NULL DEFAULT 0,
                days       INT NOT NULL DEFAULT 0,
                bloom_days INT NOT NULL DEFAULT 0,
                slots      INT,
                active     BOOLEAN NOT NULL DEFAULT true,
                pos        INT NOT NULL DEFAULT 0);
            ALTER TABLE grow_plans ADD COLUMN IF NOT EXISTS stages TEXT;
            ALTER TABLE grow_plans ADD COLUMN IF NOT EXISTS genetics TEXT DEFAULT '';
            -- таблица могла быть создана старой версией, где payout/days были NOT NULL без дефолта
            ALTER TABLE grow_plans ALTER COLUMN payout SET DEFAULT 0;
            ALTER TABLE grow_plans ALTER COLUMN days SET DEFAULT 0;
            ALTER TABLE grow_plans ALTER COLUMN bloom_days SET DEFAULT 0;
            ALTER TABLE grow_plans ADD COLUMN IF NOT EXISTS stage INT NOT NULL DEFAULT 0;
            ALTER TABLE grow_plans ADD COLUMN IF NOT EXISTS stage_at TIMESTAMPTZ NOT NULL DEFAULT now();
            ALTER TABLE grow_plans ADD COLUMN IF NOT EXISTS sold_pct BIGINT NOT NULL DEFAULT 0;
            ALTER TABLE grow_plans ADD COLUMN IF NOT EXISTS done BOOLEAN NOT NULL DEFAULT false;
            ALTER TABLE grow_plans ADD COLUMN IF NOT EXISTS start_at TIMESTAMPTZ NOT NULL DEFAULT now();
            CREATE TABLE IF NOT EXISTS grow_photos(
                id      BIGSERIAL PRIMARY KEY,
                plan_id BIGINT NOT NULL,
                photo   TEXT NOT NULL,
                note    TEXT DEFAULT '',
                created TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE INDEX IF NOT EXISTS grow_photos_plan_idx ON grow_photos(plan_id);
            CREATE TABLE IF NOT EXISTS shares(
                id         BIGSERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                plan_id    BIGINT NOT NULL,
                pct        INT NOT NULL,
                invested   BIGINT NOT NULL,
                profit_pct INT NOT NULL,
                payout     BIGINT NOT NULL,
                stage      INT NOT NULL,
                created    TIMESTAMPTZ NOT NULL DEFAULT now(),
                status     INT NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS shares_user_idx ON shares(user_id);
            CREATE INDEX IF NOT EXISTS shares_plan_idx ON shares(plan_id);
            CREATE TABLE IF NOT EXISTS wallet_ops(
                op_id         TEXT PRIMARY KEY,          -- идемпотентность: повтор не спишет дважды
                user_id       BIGINT NOT NULL,
                delta         BIGINT NOT NULL,           -- <0 списание, >0 начисление
                kind          TEXT NOT NULL,             -- bet / win / прочее
                ref           TEXT,                      -- ссылка на раунд во внешнем сервисе
                balance_after BIGINT NOT NULL,
                created       TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE INDEX IF NOT EXISTS wallet_ops_user_idx ON wallet_ops(user_id, created DESC);
            -- real_delta: изменение ВЫВОДИМЫХ средств этой операцией (Δ(balance−locked)).
            -- Реальный банк казино = −Σ real_delta по ставкам/выигрышам: бонусные
            -- ставки не считаются, а момент, когда бонус отыгрался и стал выводимым,
            -- автоматически попадает как реальная выплата.
            ALTER TABLE wallet_ops ADD COLUMN IF NOT EXISTS real_delta BIGINT NOT NULL DEFAULT 0;
            CREATE INDEX IF NOT EXISTS wallet_ops_kind_idx ON wallet_ops(kind);
            CREATE TABLE IF NOT EXISTS chat_activity(
                user_id BIGINT NOT NULL,
                day     DATE   NOT NULL,
                points  INT    NOT NULL DEFAULT 0,
                msgs    INT    NOT NULL DEFAULT 0,
                strikes INT    NOT NULL DEFAULT 0,
                name    TEXT,
                PRIMARY KEY(user_id, day));
            -- страховка от старой схемы: IF NOT EXISTS таблицу не обновляет
            ALTER TABLE chat_activity ADD COLUMN IF NOT EXISTS points INT NOT NULL DEFAULT 0;
            ALTER TABLE chat_activity ADD COLUMN IF NOT EXISTS msgs INT NOT NULL DEFAULT 0;
            ALTER TABLE chat_activity ADD COLUMN IF NOT EXISTS strikes INT NOT NULL DEFAULT 0;
            ALTER TABLE chat_activity ADD COLUMN IF NOT EXISTS name TEXT;
            CREATE UNIQUE INDEX IF NOT EXISTS chat_activity_ud ON chat_activity(user_id, day);
            CREATE TABLE IF NOT EXISTS activity_awards(
                week_start DATE   NOT NULL,
                place      INT    NOT NULL,
                user_id    BIGINT NOT NULL,
                amount     BIGINT NOT NULL,
                score      INT    NOT NULL DEFAULT 0,
                created    TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(week_start, place));
            CREATE TABLE IF NOT EXISTS grows(
                id         BIGSERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                plan_id    BIGINT,
                name       TEXT,
                price      BIGINT,
                payout     BIGINT,
                days       INT,
                bloom_days INT,
                started    TIMESTAMPTZ NOT NULL DEFAULT now(),
                ends       TIMESTAMPTZ NOT NULL,
                status     INT NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS grows_user_idx ON grows(user_id);
            CREATE TABLE IF NOT EXISTS promos(
                code    TEXT PRIMARY KEY,
                amount  BIGINT NOT NULL,
                used_by BIGINT,
                used_at TIMESTAMPTZ,
                created TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE INDEX IF NOT EXISTS promos_used_idx ON promos(used_by);
            CREATE TABLE IF NOT EXISTS settings(
                key   TEXT PRIMARY KEY,
                value TEXT);
            CREATE TABLE IF NOT EXISTS transfers(
                code    TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                expires DOUBLE PRECISION NOT NULL);
            -- подписки/отписки в чате и канале (для /stats guard-бота).
            -- Telegram истории не отдаёт — считаем с момента запуска трекинга.
            CREATE TABLE IF NOT EXISTS member_events(
                id      BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                dir     TEXT   NOT NULL,          -- 'join' | 'leave'
                name    TEXT,
                at      TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE INDEX IF NOT EXISTS member_events_chat_at ON member_events(chat_id, at);
            -- периодический снимок общего числа участников (базовая линия + график)
            CREATE TABLE IF NOT EXISTS member_snapshots(
                chat_id BIGINT NOT NULL,
                at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                total   INT NOT NULL,
                PRIMARY KEY(chat_id, at));
            -- ежедневная тематическая викторина в чате: одна запись на день
            CREATE TABLE IF NOT EXISTS quiz_days(
                day          DATE PRIMARY KEY,
                theme        TEXT,
                winner_id    BIGINT,
                winner_name  TEXT,
                winner_score INT,
                started      TIMESTAMPTZ NOT NULL DEFAULT now());
            -- какие вопросы уже задавались (чтобы не повторялись до конца круга)
            CREATE TABLE IF NOT EXISTS quiz_asked(
                theme    TEXT NOT NULL,
                qhash    TEXT NOT NULL,
                asked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(theme, qhash));
            -- какие ТЕМЫ уже выпадали в текущем круге (темы идут по кругу)
            CREATE TABLE IF NOT EXISTS quiz_used_themes(
                theme   TEXT PRIMARY KEY,
                used_at TIMESTAMPTZ NOT NULL DEFAULT now());
            -- недельная реферальная гонка: один победитель за неделю
            CREATE TABLE IF NOT EXISTS ref_race_awards(
                week_start DATE   PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                cnt        INT    NOT NULL,
                amount     BIGINT NOT NULL,
                created    TIMESTAMPTZ NOT NULL DEFAULT now());
            -- ── Розыгрыш (лотерея) ───────────────────────────────────────────
            -- круг розыгрыша: открыт до дедлайна, потом разыгрывается и закрывается
            CREATE TABLE IF NOT EXISTS lottery_rounds(
                id       SERIAL PRIMARY KEY,
                status   TEXT NOT NULL DEFAULT 'open',   -- open | finished
                deadline TIMESTAMPTZ NOT NULL,
                prizes   TEXT NOT NULL DEFAULT '5000,2500,1000',
                drawn_at TIMESTAMPTZ,
                created  TIMESTAMPTZ NOT NULL DEFAULT now());
            -- подтверждённые рефералы: пара уникальна ГЛОБАЛЬНО — одного человека
            -- нельзя переиспользовать в следующем круге. ticketed=true — уже в билете.
            CREATE TABLE IF NOT EXISTS lottery_refs(
                referrer BIGINT NOT NULL,
                referral BIGINT NOT NULL,
                round_id INT    NOT NULL,
                ticketed BOOLEAN NOT NULL DEFAULT false,
                counted  TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(referrer, referral));
            CREATE INDEX IF NOT EXISTS lottery_refs_owner ON lottery_refs(referrer, ticketed);
            -- билеты (числа) в круге: одно число — одна строка
            CREATE TABLE IF NOT EXISTS lottery_tickets(
                round_id INT    NOT NULL,
                number   INT    NOT NULL,
                user_id  BIGINT NOT NULL,
                created  TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(round_id, number));
            CREATE INDEX IF NOT EXISTS lottery_tickets_user ON lottery_tickets(round_id, user_id);
            -- победители круга (место → пользователь/число/приз)
            CREATE TABLE IF NOT EXISTS lottery_winners(
                round_id INT    NOT NULL,
                place    INT    NOT NULL,
                user_id  BIGINT NOT NULL,
                number   INT    NOT NULL,
                prize    BIGINT NOT NULL,
                name     TEXT,
                paid     BOOLEAN NOT NULL DEFAULT false,
                PRIMARY KEY(round_id, place));
        """)
        if await c.fetchval("SELECT COUNT(*) FROM products") == 0:
            for i, (name, sub, emoji, tag, base) in enumerate(SEED_PRODUCTS):
                await c.execute(
                    "INSERT INTO products(name, sub, emoji, tag, base, tiers, pos) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7)",
                    name, sub, emoji, tag, base, json.dumps(DEFAULT_TIERS), i)


# ── авто-статус «Получен» ────────────────────────────────────────────────────
async def _auto_deliver(c):
    await c.execute("""
        UPDATE orders SET status=3, ship=NULL
        WHERE status IN (1,2) AND shipped_at IS NOT NULL AND shipped_at < $1
    """, datetime.now(timezone.utc) - timedelta(days=AUTO_DELIVER_DAYS))


# ── пользователи ─────────────────────────────────────────────────────────────
async def upsert_user(tg_id: int, name: str, username: str | None, ref_by: int | None = None):
    if ref_by == tg_id:
        ref_by = None
    async with _pool.acquire() as c:
        await c.execute("""
            INSERT INTO users(tg_id, name, username, ref_by) VALUES($1,$2,$3,$4)
            ON CONFLICT (tg_id) DO UPDATE SET name=$2, username=$3
        """, tg_id, name, username, ref_by)


# ── товары ───────────────────────────────────────────────────────────────────
def _make_thumb(data_url: str) -> str:
    """Миниатюра 420px для каталога — грузится в разы быстрее полного фото."""
    try:
        _, b64 = data_url.split(",", 1)
        im = Image.open(BytesIO(base64.b64decode(b64))).convert("RGB")
        im.thumbnail((420, 420), Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, "JPEG", quality=75)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return data_url


def _product_row(r, rating) -> dict:
    try:
        photos = len(json.loads(r["photos"])) if r["photos"] else 0
    except (ValueError, TypeError):
        photos = 0
    return {
        "id": r["id"], "name": r["name"], "sub": r["sub"], "emoji": r["emoji"],
        "genetics": r["genetics"] or "",
        "tag": r["tag"], "base": r["base"],
        "tiers": json.loads(r["tiers"]) if r["tiers"] else DEFAULT_TIERS,
        "active": r["active"], "photos": photos,
        "pv": len(r["photos"] or ""),  # версия фото для кэш-бастинга
        "stock": r["stock"],           # None = не ограничено
        "rating": rating.get(r["id"], {"avg": 0, "count": 0, "reviews": 0}),
    }


async def product_photo(pid: int, idx: int, size: str = "f") -> str | None:
    async with _pool.acquire() as c:
        val = await c.fetchval("SELECT photos FROM products WHERE id=$1", pid)
    if not val:
        return None
    arr = json.loads(val)
    if not 0 <= idx < len(arr):
        return None
    item = arr[idx]
    if isinstance(item, dict):
        return item.get(size) or item.get("f")
    return item  # старый формат — одна строка


async def get_products(include_inactive: bool = False, conn=None) -> list:
    c = conn or _pool
    q = "SELECT * FROM products" + ("" if include_inactive else " WHERE active") + " ORDER BY pos, id"
    rows = await c.fetch(q)
    rrows = await c.fetch(
        "SELECT product_id, AVG(stars) AS avg, COUNT(*) AS cnt, "
        "COUNT(*) FILTER (WHERE text IS NOT NULL AND text <> '' AND hidden=0) AS reviews "
        "FROM ratings GROUP BY product_id")
    rating = {r["product_id"]: {"avg": round(float(r["avg"]), 1),
                                "count": r["cnt"], "reviews": r["reviews"]} for r in rrows}
    return [_product_row(r, rating) for r in rows]


async def save_product(d: dict) -> int:
    tiers = json.dumps(d.get("tiers") or DEFAULT_TIERS)
    async with _pool.acquire() as c:
        old: list = []
        if d.get("id"):
            val = await c.fetchval("SELECT photos FROM products WHERE id=$1", int(d["id"]))
            old = json.loads(val) if val else []
        # фото: {"old": i} — оставить существующее, строка data: — новое
        photos = []
        for ph in (d.get("photos") or [])[:MAX_PHOTOS]:
            if isinstance(ph, dict) and "old" in ph:
                i = int(ph["old"])
                if 0 <= i < len(old):
                    photos.append(old[i])
            elif isinstance(ph, str) and ph.startswith("data:image/") and len(ph) <= MAX_PHOTO_LEN:
                photos.append({"f": ph, "t": _make_thumb(ph)})
        pj = json.dumps(photos)
        stock = d.get("stock")
        stock = None if stock in (None, "") else max(0, int(stock))
        genetics = str(d.get("genetics", "")).strip()
        if d.get("id"):
            await c.execute("""
                UPDATE products SET name=$2, sub=$3, emoji=$4, tag=$5, base=$6, tiers=$7,
                                    active=$8, photos=$9, stock=$10, genetics=$11
                WHERE id=$1
            """, int(d["id"]), d["name"], d.get("sub", ""), d.get("emoji", "📦"),
                d.get("tag", ""), int(d["base"]), tiers, bool(d.get("active", True)),
                pj, stock, genetics)
            return int(d["id"])
        return await c.fetchval("""
            INSERT INTO products(name, sub, emoji, tag, base, tiers, photos, stock, genetics, pos)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,
                   COALESCE((SELECT MAX(pos)+1 FROM products), 0))
            RETURNING id
        """, d["name"], d.get("sub", ""), d.get("emoji", "📦"),
            d.get("tag", ""), int(d["base"]), tiers, pj, stock, genetics)


async def delete_product(pid: int):
    async with _pool.acquire() as c:
        await c.execute("DELETE FROM ratings WHERE product_id=$1", pid)
        await c.execute("DELETE FROM products WHERE id=$1", pid)


def price_for(product: dict, grams: int) -> int:
    tier = product["tiers"][0]
    for t in product["tiers"]:
        if grams >= t["from"]:
            tier = t
    return round(product["base"] * tier["k"] * grams)


# ── реквизиты оплаты ─────────────────────────────────────────────────────────
async def get_settings(conn=None) -> dict:
    c = conn or _pool
    rows = await c.fetch("SELECT key, value FROM settings")
    s = {r["key"]: r["value"] for r in rows}
    return {k: s.get(k, "") for k in PAYMENT_KEYS}


async def set_settings(d: dict):
    async with _pool.acquire() as c:
        for k in PAYMENT_KEYS:
            if k in d:
                await c.execute("""
                    INSERT INTO settings(key, value) VALUES($1,$2)
                    ON CONFLICT (key) DO UPDATE SET value=$2
                """, k, str(d[k]).strip())


async def get_kv(key: str) -> str | None:
    async with _pool.acquire() as c:
        return await c.fetchval("SELECT value FROM settings WHERE key=$1", key)


async def set_kv(key: str, value: str):
    async with _pool.acquire() as c:
        await c.execute("""
            INSERT INTO settings(key, value) VALUES($1,$2)
            ON CONFLICT (key) DO UPDATE SET value=$2
        """, key, value)


def payment_public(s: dict) -> dict:
    """Только заполненные реквизиты — что показывать покупателю."""
    return {k: v for k, v in s.items() if v}


# ── снапшот пользователя ─────────────────────────────────────────────────────
async def snapshot(tg_id: int, conn: asyncpg.Connection | None = None) -> dict:
    if conn is None:
        async with _pool.acquire() as c:
            return await snapshot(tg_id, c)
    c = conn
    await _auto_deliver(c)
    u = await c.fetchrow("SELECT * FROM users WHERE tg_id=$1", tg_id)
    cnt = await c.fetchval("SELECT COUNT(*) FROM users WHERE ref_by=$1", tg_id)
    rows = await c.fetch("SELECT * FROM orders WHERE user_id=$1 ORDER BY id DESC", tg_id)
    rate = {r["order_id"]: r for r in await c.fetch(
        "SELECT order_id, stars, text, anon FROM ratings WHERE user_id=$1", tg_id)}
    orders = [{
        "id": f"MM-{r['id'] + ORDER_CODE_BASE}",
        "product": r["product"], "grams": r["grams"], "total": r["total"],
        "status": r["status"], "ttn": r["ttn"], "date": r["date"],
        "delay_paid": r["delay_paid"],
        "ship": json.loads(r["ship"]) if r["ship"] else None,
        "np_status": r["np_status"], "np_status_text": r["np_status_text"],
        "np_eta": r["np_eta"], "np_arrival": r["np_arrival"],
        "stars": rate.get(r["id"], {}).get("stars") if rate.get(r["id"]) else None,
        "review": (rate[r["id"]]["text"] if rate.get(r["id"]) else None),
        "review_anon": (rate[r["id"]]["anon"] if rate.get(r["id"]) else 0),
    } for r in rows]
    return {
        "balance": u["balance"], "ref_count": cnt, "ref_earned": u["ref_earned"],
        "ref_percent": round(ref_percent(cnt) * 100),
        "bonus_claimed": u["bonus_claimed"],
        "withdrawable": max(0, u["balance"] - (u["locked"] or 0)),
        "locked": (u["locked"] or 0),
        "wager": ((u["wager_req"] or 0) if (u["locked"] or 0) > 0 else 0),
        "has_pin": bool(u["pin_hash"]),
        "pin_locked_until": u["pin_locked_until"].isoformat() if u["pin_locked_until"] else None,
        "orders": orders,
        "products": await get_products(conn=c),
        "payment": payment_public(await get_settings(conn=c)),
        "payments": await payments_history(tg_id, c),
        "grow_plans": await get_grow_plans(conn=c),
        "shares": await user_shares(tg_id, c),
    }


async def payments_history(tg_id: int, c) -> list:
    """История пополнений: карта (ручная проверка) + крипто-счета."""
    await _expire_invoices(c)  # с возвратом стока и отменой заказов, а не голым UPDATE
    tt = await c.fetch("""
        SELECT amount, method, status, created FROM topups
        WHERE user_id=$1 ORDER BY id DESC LIMIT 30
    """, tg_id)
    inv = await c.fetch("""
        SELECT amount_uah AS amount, currency AS method, status, created FROM invoices
        WHERE user_id=$1 ORDER BY id DESC LIMIT 30
    """, tg_id)
    wd = await c.fetch("""
        SELECT amount, method, status, created FROM withdrawals
        WHERE user_id=$1 ORDER BY id DESC LIMIT 30
    """, tg_id)
    cp = await c.fetch("""
        SELECT amount_uah AS amount, status, created FROM card_payments
        WHERE user_id=$1 ORDER BY id DESC LIMIT 30
    """, tg_id)
    pr = await c.fetch("""
        SELECT amount, used_at AS created FROM promos
        WHERE used_by=$1 ORDER BY used_at DESC LIMIT 30
    """, tg_id)
    rows = [{**dict(r), "kind": "card"} for r in tt] \
        + [{**dict(r), "kind": "crypto"} for r in inv] \
        + [{**dict(r), "kind": "out"} for r in wd] \
        + [{**dict(r), "method": "card", "kind": "cardauto"} for r in cp] \
        + [{**dict(r), "method": "promo", "status": 1, "kind": "promo"} for r in pr]
    rows.sort(key=lambda r: r["created"], reverse=True)
    return [{**r, "created": r["created"].isoformat()} for r in rows[:40]]


# ── заказы ───────────────────────────────────────────────────────────────────
# статусы: -2 отменён, -1 ждёт оплаты/проверки, 0 оплачен, 1 в работе, 2 в пути (ТТН), 3 получен
def ref_percent(invited: int) -> float:
    p = REF_TIERS[0][1]
    for n, k in REF_TIERS:
        if invited >= n:
            p = k
    return p


async def _spend_locked(c, tg_id: int, amount: int):
    """Трата уменьшает «залоченный» бонус (его можно потратить в магазине).
    Если бонус закончился — снимаем и вейджер (нечего отыгрывать)."""
    await c.execute(
        "UPDATE users SET locked = GREATEST(0, locked - $1), "
        "wager_req = CASE WHEN locked - $1 <= 0 THEN 0 ELSE wager_req END "
        "WHERE tg_id=$2", amount, tg_id)


# очередь реф-уведомлений: заполняется в _ref_bonus, main.py разбирает её и
# шлёт рефереру DM после того, как транзакция оплаты успешно закоммитилась
_REF_NOTIFY: list = []


def pop_ref_notify() -> list:
    q = _REF_NOTIFY[:]
    _REF_NOTIFY.clear()
    return q


# мгновенный бонус пригласившему за активацию друга (друг забрал welcome-бонус).
# 0 — фича выключена. Начисляется в locked (нельзя вывести; тратится в магазине или отыгрывается в казино).
REF_ACTIVATION_BONUS = int(os.getenv("REF_ACTIVATION_BONUS", "50") or 50)
_ACTIVATION_NOTIFY: list = []


def pop_activation_notify() -> list:
    q = _ACTIVATION_NOTIFY[:]
    _ACTIVATION_NOTIFY.clear()
    return q


async def _ref_bonus(c, buyer_id: int, total: int):
    """Процент рефереру по его уровню — начисляется только после фактической оплаты."""
    ref_by = await c.fetchval("SELECT ref_by FROM users WHERE tg_id=$1", buyer_id)
    if ref_by:
        invited = await c.fetchval("SELECT COUNT(*) FROM users WHERE ref_by=$1", ref_by)
        bonus = round(total * ref_percent(invited))
        await c.execute("""
            UPDATE users SET balance=balance+$1, ref_earned=ref_earned+$1 WHERE tg_id=$2
        """, bonus, ref_by)
        if bonus > 0:
            _REF_NOTIFY.append({"ref_by": ref_by, "bonus": bonus, "pct": round(ref_percent(invited) * 100)})


async def _order_product_total(c, product_id: int, grams: int, lock: bool = False):
    q = "SELECT * FROM products WHERE id=$1 AND active" + (" FOR UPDATE" if lock else "")
    p = await c.fetchrow(q, product_id)
    if not p:
        raise ValueError("Товар не найден")
    if not 1 <= grams <= MAX_GRAMS:
        raise ValueError(f"Вес — от 1 до {MAX_GRAMS} грамм")
    if p["stock"] is not None and grams > p["stock"]:
        raise ValueError("Такого количества нет в наличии — напишите админу")
    return p, price_for(_product_row(p, {}), grams)


async def _take_stock(c, product_id: int, grams: int):
    await c.execute(
        "UPDATE products SET stock=stock-$1 WHERE id=$2 AND stock IS NOT NULL", grams, product_id)


async def _restock(c, product_id, grams):
    if product_id:
        await c.execute(
            "UPDATE products SET stock=stock+$1 WHERE id=$2 AND stock IS NOT NULL", grams, product_id)


async def order_total(product_id: int, grams: int) -> int:
    async with _pool.acquire() as c:
        _, total = await _order_product_total(c, product_id, grams)
    return total


async def _guard_pending_orders(c, tg_id: int):
    n = await c.fetchval("SELECT COUNT(*) FROM orders WHERE user_id=$1 AND status=-1", tg_id)
    if n >= MAX_PENDING_ORDERS:
        raise ValueError("Слишком много незавершённых заказов — оплатите или дождитесь обработки")


async def _insert_order(c, tg_id, p, grams, total, status, pay, ship,
                        receipt=None, bonus_part=0) -> int:
    return await c.fetchval("""
        INSERT INTO orders(user_id, product_id, product, grams, total, status, pay,
                           receipt, ship, date, bonus_part)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id
    """, tg_id, p["id"], p["name"], grams, total, status, pay, receipt,
        json.dumps(ship, ensure_ascii=False), datetime.now().strftime("%d.%m.%Y"),
        int(bonus_part))


async def create_order(tg_id: int, product_id: int, grams: int, pay: str,
                       ship: dict, receipt: str | None = None) -> dict:
    """Оплата с баланса (сразу оплачен) или картой (квитанция на проверку)."""
    async with _pool.acquire() as c, c.transaction():
        p, total = await _order_product_total(c, product_id, grams, lock=True)
        u = await c.fetchrow("SELECT * FROM users WHERE tg_id=$1 FOR UPDATE", tg_id)
        if pay == "balance":
            if u["balance"] < total:
                raise ValueError("Недостаточно средств — пополните баланс")
            # какая часть оплачена бонусами: locked списывается первым
            bonus_part = min(int(u["locked"] or 0), total)
            await c.execute("UPDATE users SET balance=balance-$1 WHERE tg_id=$2", total, tg_id)
            await _spend_locked(c, tg_id, total)
            oid = await _insert_order(c, tg_id, p, grams, total, 0, "balance", ship,
                                      bonus_part=bonus_part)
            await _ref_bonus(c, tg_id, total)
        elif pay == "card":
            await _guard_pending_orders(c, tg_id)
            oid = await _insert_order(c, tg_id, p, grams, total, -1, "card", ship, receipt)
        else:
            raise ValueError("Неизвестный способ оплаты")
        await _take_stock(c, product_id, grams)
        snap = await snapshot(tg_id, c)
        snap["order_code"] = f"MM-{oid + ORDER_CODE_BASE}"
        snap["order_total"] = total
        snap["order_product"] = p["name"]
        snap["order_grams"] = grams
        return snap


async def create_order_invoice(tg_id: int, product_id: int, grams: int, currency: str,
                               ship: dict, amount_crypto: str, address: str) -> tuple:
    """Заказ с оплатой криптой: заказ «ждёт оплаты» + привязанный счёт."""
    async with _pool.acquire() as c, c.transaction():
        await _guard_pending_orders(c, tg_id)
        p, total = await _order_product_total(c, product_id, grams, lock=True)
        oid = await _insert_order(c, tg_id, p, grams, total, -1, currency, ship)
        await _take_stock(c, product_id, grams)
        # новый счёт отменяет прежний неоплаченный (и его заказ, если был) с возвратом остатка
        await _cancel_user_pending_invoices(c, tg_id, exclude_order_id=oid)
        inv = await c.fetchrow("""
            INSERT INTO invoices(user_id, amount_uah, currency, amount_crypto, address, order_id, expires)
            VALUES($1,$2,$3,$4,$5,$6, now() + make_interval(secs => $7))
            RETURNING *
        """, tg_id, total, currency, amount_crypto, address, oid, INVOICE_TTL)
        snap = await snapshot(tg_id, c)
        code = f"MM-{oid + ORDER_CODE_BASE}"
        snap["order_code"] = code
        snap["order_total"] = total
        snap["order_product"] = p["name"]
        snap["order_grams"] = grams
        return snap, code, dict(inv)


async def order_decide(order_code: str, approve: bool) -> dict:
    """Подтверждение/отклонение оплаты заказа картой (статус -1)."""
    try:
        oid = int(order_code.split("-")[1]) - ORDER_CODE_BASE
    except (IndexError, ValueError):
        raise ValueError("Неверный номер заказа")
    async with _pool.acquire() as c, c.transaction():
        o = await c.fetchrow("SELECT * FROM orders WHERE id=$1 AND status=-1 FOR UPDATE", oid)
        if not o:
            raise ValueError("Заказ не найден или уже обработан")
        if approve:
            await c.execute("UPDATE orders SET status=0 WHERE id=$1", oid)
            await _ref_bonus(c, o["user_id"], o["total"])
        else:
            await c.execute("UPDATE orders SET status=-2 WHERE id=$1", oid)
            await _restock(c, o["product_id"], o["grams"])
        return {"user_id": o["user_id"], "code": order_code, "approved": approve}


async def delete_order(order_code: str) -> dict:
    """Удаление заказа админом с ВОЗВРАТОМ средств и товара, если заказ был
    активен (оплачен / в работе / в пути) и ещё не получен. Возвращает инфо для
    уведомления покупателя."""
    try:
        oid = int(order_code.split("-")[1]) - ORDER_CODE_BASE
    except (IndexError, ValueError):
        raise ValueError("Неверный номер заказа")
    async with _pool.acquire() as c, c.transaction():
        o = await c.fetchrow("SELECT * FROM orders WHERE id=$1 FOR UPDATE", oid)
        if not o:
            raise ValueError("Заказ не найден")
        st = int(o["status"])
        total = int(o["total"] or 0)
        refunded = 0
        if st in (0, 1, 2):                 # оплачен / в работе / в пути — вернуть деньги и товар
            bonus_part = int(o["bonus_part"] or 0)
            if o["pay"] == "balance":
                # зеркально оплате с баланса: возвращаем сумму и бонусную часть в locked
                await c.execute(
                    "UPDATE users SET balance=balance+$1, locked=locked+$2 WHERE tg_id=$3",
                    total, bonus_part, o["user_id"])
            else:
                # оплата картой/криптой (извне) — возвращаем как баланс магазина
                await c.execute("UPDATE users SET balance=balance+$1 WHERE tg_id=$2",
                                total, o["user_id"])
            await _restock(c, o["product_id"], o["grams"])
            refunded = total
        elif st == -1:                      # не оплачен — денег не брали, вернуть только товар
            await _restock(c, o["product_id"], o["grams"])
        await c.execute("DELETE FROM invoices WHERE order_id=$1", oid)   # снять привязанные счета
        await c.execute("DELETE FROM orders WHERE id=$1", oid)
        await c.execute("DELETE FROM ratings WHERE order_id=$1", oid)
    return {"user_id": int(o["user_id"]), "refunded": refunded,
            "total": total, "status": st}


REVIEW_MAX = 600      # максимальная длина текста отзыва


async def rate_order(tg_id: int, order_code: str, stars: int,
                     text: str | None = None, anon: int = 0,
                     name: str | None = None, avatar: str | None = None) -> dict:
    """Оценка заказа (1–5) + необязательный текстовый отзыв.
    text=None — только звёзды (текст не трогаем). anon: 0 = ник и аватар, 1 = инкогнито.
    avatar — data-URI мини-аватар; для инкогнито не сохраняем."""
    try:
        oid = int(order_code.split("-")[1]) - ORDER_CODE_BASE
    except (IndexError, ValueError):
        raise ValueError("Неверный номер заказа")
    if not 1 <= stars <= 5:
        raise ValueError("Оценка — от 1 до 5")
    anon = 1 if anon else 0
    write_text = text is not None
    review = (text or "").strip()[:REVIEW_MAX] if write_text else None
    # аватар храним только для НЕ-инкогнито и только если это data-URI картинки
    ava = avatar if (not anon and avatar and avatar.startswith("data:image/")) else None
    async with _pool.acquire() as c, c.transaction():
        await _auto_deliver(c)
        o = await c.fetchrow("SELECT * FROM orders WHERE id=$1 AND user_id=$2", oid, tg_id)
        if not o:
            raise ValueError("Заказ не найден")
        if o["status"] != 3:
            raise ValueError("Оценить можно после получения заказа")
        if not o["product_id"]:
            raise ValueError("Этот заказ нельзя оценить")
        if write_text:
            # ставим/обновляем оценку и текст сразу; имя-снимок — на момент отзыва
            await c.execute("""
                INSERT INTO ratings(order_id, user_id, product_id, stars, text, anon, name, avatar, created)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8, now())
                ON CONFLICT (order_id) DO UPDATE
                  SET stars=$4, text=$5, anon=$6, name=$7, avatar=$8, created=now()
            """, oid, tg_id, o["product_id"], stars, review, anon,
                (name or "").strip()[:64] or None, ava)
        else:
            await c.execute("""
                INSERT INTO ratings(order_id, user_id, product_id, stars) VALUES($1,$2,$3,$4)
                ON CONFLICT (order_id) DO UPDATE SET stars=$4
            """, oid, tg_id, o["product_id"], stars)
        return await snapshot(tg_id, c)


async def product_reviews(product_id: int, limit: int = 60) -> list:
    """Текстовые отзывы на товар — свежие сверху. Инкогнито имя не отдаём."""
    async with _pool.acquire() as c:
        rows = await c.fetch("""
            SELECT stars, text, anon, name, avatar, created
            FROM ratings
            WHERE product_id=$1 AND text IS NOT NULL AND text <> '' AND hidden=0
            ORDER BY created DESC NULLS LAST, order_id DESC
            LIMIT $2
        """, product_id, limit)
    out = []
    for r in rows:
        incognito = bool(r["anon"])
        out.append({
            "stars": r["stars"],
            "text": r["text"],
            "anon": 1 if incognito else 0,
            "name": None if incognito else (r["name"] or "Покупатель"),
            "avatar": None if incognito else r["avatar"],
            "created": r["created"].isoformat() if r["created"] else None,
        })
    return out


async def admin_reviews(limit: int = 200) -> list:
    """Все текстовые отзывы для модерации — включая скрытые и настоящее имя
    инкогнито (модератору важно знать автора)."""
    async with _pool.acquire() as c:
        rows = await c.fetch("""
            SELECT r.order_id, r.product_id, r.stars, r.text, r.anon, r.hidden,
                   r.name AS snap_name, r.avatar, r.created,
                   p.name AS product, u.name AS user_name, u.username
            FROM ratings r
            LEFT JOIN products p ON p.id = r.product_id
            LEFT JOIN users u    ON u.tg_id = r.user_id
            WHERE r.text IS NOT NULL AND r.text <> ''
            ORDER BY r.created DESC NULLS LAST, r.order_id DESC
            LIMIT $1
        """, limit)
    out = []
    for r in rows:
        out.append({
            "order": f"MM-{r['order_id'] + ORDER_CODE_BASE}",
            "product": r["product"] or "—",
            "stars": r["stars"],
            "text": r["text"],
            "anon": 1 if r["anon"] else 0,
            "hidden": 1 if r["hidden"] else 0,
            "avatar": r["avatar"],
            # автор: snapshot-имя или актуальное из профиля; username — в помощь модератору
            "author": r["snap_name"] or r["user_name"] or "Покупатель",
            "username": r["username"],
            "created": r["created"].isoformat() if r["created"] else None,
        })
    return out


async def admin_moderate_review(order_code: str, action: str) -> None:
    """Модерация отзыва: hide / show / delete. delete очищает текст, аватар и
    имя (звёзды-оценку оставляем — она честная)."""
    try:
        oid = int(order_code.split("-")[1]) - ORDER_CODE_BASE
    except (IndexError, ValueError):
        raise ValueError("Неверный номер заказа")
    async with _pool.acquire() as c:
        if action == "hide":
            await c.execute("UPDATE ratings SET hidden=1 WHERE order_id=$1", oid)
        elif action == "show":
            await c.execute("UPDATE ratings SET hidden=0 WHERE order_id=$1", oid)
        elif action == "delete":
            await c.execute(
                "UPDATE ratings SET text=NULL, avatar=NULL, name=NULL, hidden=0 "
                "WHERE order_id=$1", oid)
        else:
            raise ValueError("Неизвестное действие")


# ── пополнения (ручная проверка) ─────────────────────────────────────────────
async def topup_receipt(tg_id: int, amount: int, method: str, receipt: str) -> int:
    async with _pool.acquire() as c:
        n = await c.fetchval(
            "SELECT COUNT(*) FROM topups WHERE user_id=$1 AND status=0", tg_id)
        if n >= MAX_PENDING_TOPUPS:
            raise ValueError("Слишком много квитанций на проверке — дождитесь обработки")
        return await c.fetchval("""
            INSERT INTO topups(user_id, amount, method, receipt) VALUES($1,$2,$3,$4)
            RETURNING id
        """, tg_id, amount, method, receipt)


async def admin_topups() -> list:
    async with _pool.acquire() as c:
        rows = await c.fetch("""
            SELECT t.*, u.name, u.username FROM topups t
            LEFT JOIN users u ON u.tg_id = t.user_id
            WHERE t.status=0 ORDER BY t.id
        """)
    return [{
        "id": r["id"], "user_id": r["user_id"],
        "user": r["name"] or "?", "username": r["username"],
        "amount": r["amount"], "method": r["method"],
        "created": r["created"].isoformat(), "receipt": r["receipt"],
    } for r in rows]


async def admin_topup_history(limit: int = 120) -> list:
    """Все пополнения одной лентой: квитанции, крипта (только пополнения,
    без счетов за заказы) и карта. Статусы: 0 в обработке, 1 зачислено,
    2 отклонено/истекло."""
    async with _pool.acquire() as c:
        await _expire_invoices(c)
        rows = await c.fetch("""
            SELECT * FROM (
                SELECT t.id, t.user_id, t.amount, 'receipt' AS kind,
                       COALESCE(t.method, '') AS detail, NULL AS txid,
                       NULL AS ref, t.status, t.created
                FROM topups t
                UNION ALL
                SELECT i.id, i.user_id, i.amount_uah, 'crypto',
                       i.currency, i.txid, NULL, i.status, i.created
                FROM invoices i WHERE i.order_id IS NULL
                UNION ALL
                SELECT p.id, p.user_id, p.amount_uah, 'card',
                       COALESCE('•' || right(p.card, 4), ''), NULL,
                       p.payment_id, p.status, p.created
                FROM card_payments p
            ) x ORDER BY created DESC LIMIT $1
        """, limit)
        uids = list({r["user_id"] for r in rows})
        users = await c.fetch(
            "SELECT tg_id, name, username FROM users WHERE tg_id = ANY($1)", uids) if uids else []
    um = {u["tg_id"]: u for u in users}
    return [{
        "id": r["id"], "user_id": r["user_id"],
        "user": (um[r["user_id"]]["name"] if r["user_id"] in um else None) or "?",
        "username": um[r["user_id"]]["username"] if r["user_id"] in um else None,
        "amount": r["amount"], "kind": r["kind"], "detail": r["detail"],
        "txid": r["txid"], "ref": r["ref"], "status": r["status"],
        "created": r["created"],
    } for r in rows]


async def admin_payment_resolve(kind: str, pid: int, approve: bool) -> dict:
    """Ручное управление статусом платежа (карта/крипто-пополнение) в любую
    сторону. approve=True → «зачислено» (+баланс, если ещё не начисляли),
    approve=False → «отклонено» (списываем баланс, если раньше зачисляли).
    Баланс двигаем ровно по факту смены зачисления — повторное решение в тот
    же статус запрещено, поэтому двойного начисления/списания не будет."""
    tgt = 1 if approve else 2
    async with _pool.acquire() as c, c.transaction():
        if kind == "card":
            r = await c.fetchrow(
                "SELECT * FROM card_payments WHERE id=$1 FOR UPDATE", pid)
            if not r:
                raise ValueError("Платёж не найден")
            if r["status"] == tgt:
                raise ValueError("Платёж уже в этом статусе")
            await c.execute("UPDATE card_payments SET status=$2 WHERE id=$1", pid, tgt)
            uid, amount = r["user_id"], r["amount_uah"]
        elif kind == "crypto":
            r = await c.fetchrow(
                "SELECT * FROM invoices WHERE id=$1 AND order_id IS NULL FOR UPDATE", pid)
            if not r:
                raise ValueError("Счёт не найден")
            if r["status"] == tgt:
                raise ValueError("Счёт уже в этом статусе")
            await c.execute(
                "UPDATE invoices SET status=$2, txid=COALESCE(txid, 'manual') WHERE id=$1",
                pid, tgt)
            uid, amount = r["user_id"], r["amount_uah"]
        else:
            raise ValueError("Неизвестный тип платежа")
        was_credited = r["status"] == 1        # деньги уже были на балансе
        credited = reversed_ = False
        if approve and not was_credited:       # зачисляем впервые
            await c.execute("UPDATE users SET balance=balance+$1 WHERE tg_id=$2", amount, uid)
            credited = True
        elif not approve and was_credited:      # откатываем ранее зачисленное
            await c.execute("""
                UPDATE users SET balance = GREATEST(0, balance - $1),
                                 locked  = LEAST(locked, GREATEST(0, balance - $1))
                WHERE tg_id=$2
            """, amount, uid)
            reversed_ = True
    return {"user_id": uid, "amount": amount, "approved": approve,
            "credited": credited, "reversed": reversed_}


async def topup_decide(topup_id: int, approve: bool) -> dict:
    """Возвращает {user_id, amount, approved} для уведомления."""
    async with _pool.acquire() as c, c.transaction():
        t = await c.fetchrow("SELECT * FROM topups WHERE id=$1 AND status=0 FOR UPDATE", topup_id)
        if not t:
            raise ValueError("Заявка не найдена или уже обработана")
        await c.execute("UPDATE topups SET status=$2, decided=now() WHERE id=$1",
                        topup_id, 1 if approve else 2)
        if approve:
            await c.execute("UPDATE users SET balance=balance+$1 WHERE tg_id=$2",
                            t["amount"], t["user_id"])
        return {"user_id": t["user_id"], "amount": t["amount"], "approved": approve}


# ── админ: заказы и ТТН ──────────────────────────────────────────────────────
async def grow_stats() -> dict:
    """Агрегаты по долям E-grow: вложено, к выплате, выплачено."""
    async with _pool.acquire() as c:
        r = await c.fetchrow("""
            SELECT
              COUNT(*)                    FILTER (WHERE status=0)      AS active_cnt,
              COALESCE(SUM(invested) FILTER (WHERE status=0), 0)       AS active_invested,
              COALESCE(SUM(payout)   FILTER (WHERE status=0), 0)       AS payout_due,
              COUNT(*)                    FILTER (WHERE status=1)      AS paid_cnt,
              COALESCE(SUM(payout)   FILTER (WHERE status=1), 0)       AS paid_out,
              COALESCE(SUM(invested), 0)                               AS invested_total
            FROM shares
        """)
    active_inv, payout_due = int(r["active_invested"]), int(r["payout_due"])
    return {
        "active_cnt": r["active_cnt"], "active_invested": active_inv,
        "payout_due": payout_due, "profit_due": payout_due - active_inv,
        "paid_cnt": r["paid_cnt"], "paid_out": int(r["paid_out"]),
        "invested_total": int(r["invested_total"]),
    }


async def mark_bonus_part(order_code: str, amount: int | None) -> dict:
    """Ручная пометка старого заказа: какая часть была оплачена бонусами.
    Без суммы — весь заказ считается бонусным."""
    try:
        oid = int(order_code.split("-")[1]) - ORDER_CODE_BASE
    except (IndexError, ValueError):
        raise ValueError("Неверный номер заказа")
    async with _pool.acquire() as c:
        r = await c.fetchrow("""
            UPDATE orders SET bonus_part = LEAST(COALESCE($2::bigint, total), total)
            WHERE id=$1 RETURNING total, bonus_part
        """, oid, amount)
    if not r:
        raise ValueError("Заказ не найден")
    return {"total": r["total"], "bonus_part": r["bonus_part"]}


async def sales_stats() -> dict:
    """Бухгалтерия продаж. Заказ считается проданным с момента, как его взяли
    в работу (статус 1+): заказ оплачен ещё на статусе 0, а «в работе» — уже
    подтверждённая продажа, ждать ТТН для учёта не нужно. Бонусные оплаты (промо,
    приветственные) учитываются отдельно: это не живые деньги. Чистая прибыль =
    реальная выручка минус комиссия на вывод и минус фикс-расход на каждую посылку."""
    async with _pool.acquire() as c:
        await _auto_deliver(c)
        tot = await c.fetchrow("""
            SELECT COUNT(*) AS cnt, COALESCE(SUM(total),0) AS revenue,
                   COALESCE(SUM(grams),0) AS grams,
                   COALESCE(SUM(bonus_part),0) AS bonus
            FROM orders WHERE status >= 1
        """)
        by = await c.fetch("""
            SELECT product, COUNT(*) AS cnt, COALESCE(SUM(total),0) AS revenue,
                   COALESCE(SUM(grams),0) AS grams
            FROM orders WHERE status >= 1 GROUP BY product ORDER BY revenue DESC LIMIT 20
        """)
    revenue, bonus = int(tot["revenue"]), int(tot["bonus"])
    real = revenue - bonus
    fee = round(real * WITHDRAW_FEE_PCT / 100)
    shipping = int(tot["cnt"]) * PARCEL_COST
    return {
        "count": tot["cnt"], "revenue": revenue, "grams": int(tot["grams"]),
        "bonus": bonus, "real": real,
        "fee_pct": WITHDRAW_FEE_PCT, "fee": fee,
        "parcel_cost": PARCEL_COST, "shipping": shipping,
        "profit": real - fee - shipping,
        "by_product": [{"product": b["product"], "cnt": b["cnt"],
                        "revenue": int(b["revenue"]), "grams": int(b["grams"])} for b in by],
    }


async def admin_todo() -> dict:
    """Что ждёт действий админа — для красных точек в приложении.
    Заказы: -1 проверить оплату, 0 взять в работу. Пополнения/выводы: на проверке."""
    async with _pool.acquire() as c:
        o = await c.fetchval("SELECT COUNT(*) FROM orders WHERE status IN (-1, 0)")
        t = await c.fetchval("SELECT COUNT(*) FROM topups WHERE status=0")
        w = await c.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status=0")
    return {"orders": o, "topups": t, "withdrawals": w}


async def admin_orders() -> list:
    async with _pool.acquire() as c:
        await _auto_deliver(c)
        rows = await c.fetch("""
            SELECT o.*, u.name, u.username FROM orders o
            LEFT JOIN users u ON u.tg_id = o.user_id
            WHERE o.status <> 3
            ORDER BY o.id DESC LIMIT 200
        """)
    return [{
        "id": f"MM-{r['id'] + ORDER_CODE_BASE}",
        "user": r["name"] or "?", "username": r["username"], "user_id": r["user_id"],
        "product": r["product"], "grams": r["grams"], "total": r["total"],
        "status": r["status"], "ttn": r["ttn"], "date": r["date"],
        "pay": r["pay"], "delay_paid": r["delay_paid"],
        "receipt": r["receipt"] if r["status"] == -1 else None,
        "ship": json.loads(r["ship"]) if r["ship"] else None,
        "np_status_text": r["np_status_text"], "np_eta": r["np_eta"], "np_arrival": r["np_arrival"],
    } for r in rows]


async def set_ttn(order_code: str, ttn: str) -> dict:
    """Возвращает {user_id, code, ttn, old_ttn} для уведомления покупателя."""
    try:
        oid = int(order_code.split("-")[1]) - ORDER_CODE_BASE
    except (IndexError, ValueError):
        raise ValueError("Неверный номер заказа")
    async with _pool.acquire() as c:
        o = await c.fetchrow("SELECT * FROM orders WHERE id=$1", oid)
        if not o:
            raise ValueError("Заказ не найден")
        changed = o["ttn"] and o["ttn"] != ttn.strip()
        await c.execute("""
            UPDATE orders SET ttn=$2, status=GREATEST(status, 2), shipped_at=COALESCE(shipped_at, now())
            WHERE id=$1
        """, oid, ttn.strip())
        if changed:  # старый статус НП относится к прежнему ТТН — сбрасываем, трекер подтянет заново
            await c.execute("""
                UPDATE orders SET np_status=NULL, np_status_text=NULL, np_eta=NULL, np_arrival=NULL
                WHERE id=$1
            """, oid)
        return {"user_id": o["user_id"], "code": order_code,
                "ttn": ttn.strip(), "old_ttn": o["ttn"]}


async def order_to_work(order_code: str) -> dict:
    try:
        oid = int(order_code.split("-")[1]) - ORDER_CODE_BASE
    except (IndexError, ValueError):
        raise ValueError("Неверный номер заказа")
    async with _pool.acquire() as c:
        o = await c.fetchrow("SELECT user_id FROM orders WHERE id=$1 AND status=0", oid)
        if not o:
            raise ValueError("Заказ не найден или уже в работе")
        # worked_at — точка отсчёта дедлайна отправки (компенсация за просрочку)
        await c.execute(
            "UPDATE orders SET status=1, worked_at=COALESCE(worked_at, now()) WHERE id=$1", oid)
        return {"user_id": o["user_id"], "code": order_code}


async def compensate_delayed_orders(amount: int) -> list:
    """Компенсация за просрочку отправки. Девиз — отправка в день заказа, поэтому
    дедлайн считаем от ДАТЫ СОЗДАНИЯ заказа (created): за КАЖДЫЕ просроченные сутки
    (полночь Киева, к которой оплаченный заказ без ТТН так и не отправлен) начисляем
    `amount` ₴ БОНУСОМ (в locked, с вейджером — как остальные бонусы). Берём заказы
    оплаченные/в работе (status 0 или 1) без ТТН — сюда попадают и уже висящие в
    работе. Идемпотентно по delay_paid: повтор за те же сутки не начислит дважды,
    пропущенный день догоняется. Прикрепили ТТН (status≥2) — заказ выпал из выборки.
    Возвращает список начислений для уведомления покупателей."""
    if amount <= 0:
        return []
    out = []
    async with _pool.acquire() as c, c.transaction():
        rows = await c.fetch("""
            SELECT id, user_id, delay_paid,
                   ((now() AT TIME ZONE 'Europe/Kyiv')::date
                    - (created AT TIME ZONE 'Europe/Kyiv')::date) AS overdue
            FROM orders
            WHERE status IN (0, 1) AND ttn IS NULL
            FOR UPDATE
        """)
        for r in rows:
            overdue = int(r["overdue"] or 0)
            new_days = overdue - int(r["delay_paid"] or 0)
            if new_days <= 0:
                continue
            add = amount * new_days
            # бонус: в locked + вейджер, как в остальных начислениях
            await c.execute(
                f"UPDATE users SET balance = balance + $1, locked = locked + $1, "
                f"wager_req = (CASE WHEN locked <= 0 THEN 0 ELSE wager_req END) "
                f"+ $1 * {BONUS_WAGER_X} WHERE tg_id = $2", add, r["user_id"])
            await c.execute("UPDATE orders SET delay_paid = $2 WHERE id = $1",
                            r["id"], overdue)
            out.append({"user_id": r["user_id"],
                        "code": f"MM-{r['id'] + ORDER_CODE_BASE}",
                        "new_days": new_days, "total_days": overdue,
                        "amount": add, "each": amount})
    return out


async def shipped_orders() -> list:
    """Заказы «В пути» с ТТН — для трекера Новой Почты."""
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id, user_id, ttn, np_status FROM orders "
            "WHERE status=2 AND ttn IS NOT NULL LIMIT 100")
    return [{"id": r["id"], "user_id": r["user_id"], "ttn": r["ttn"],
             "np_status": r["np_status"],
             "code": f"MM-{r['id'] + ORDER_CODE_BASE}"} for r in rows]


async def update_order_np(oid: int, code, text: str, eta: str, arrival: str):
    """Сохраняет живой статус НП по заказу (без смены статуса заказа)."""
    async with _pool.acquire() as c:
        await c.execute(
            "UPDATE orders SET np_status=$2, np_status_text=$3, np_eta=$4, np_arrival=$5 WHERE id=$1",
            oid, code, text or None, eta or None, arrival or None)


async def mark_delivered(oid: int) -> bool:
    async with _pool.acquire() as c:
        tag = await c.execute("UPDATE orders SET status=3, ship=NULL WHERE id=$1 AND status=2", oid)
    return tag == "UPDATE 1"


# ── крипто-счета (автопроверка оплаты) ───────────────────────────────────────
async def _cancel_user_pending_invoices(c, tg_id: int, exclude_order_id: int | None = None):
    """Гасит все неоплаченные счета юзера, отменяя привязанные заказы с возвратом стока."""
    q = ("""SELECT o.id, o.product_id, o.grams FROM orders o
            JOIN invoices i ON i.order_id=o.id
            WHERE i.user_id=$1 AND i.status=0 AND o.status=-1""")
    args = [tg_id]
    if exclude_order_id is not None:
        q += " AND o.id<>$2"
        args.append(exclude_order_id)
    for r in await c.fetch(q, *args):
        await _restock(c, r["product_id"], r["grams"])
    if exclude_order_id is not None:
        await c.execute("""UPDATE orders SET status=-2 WHERE status=-1 AND id<>$2 AND id IN
            (SELECT order_id FROM invoices WHERE user_id=$1 AND status=0 AND order_id IS NOT NULL)
        """, tg_id, exclude_order_id)
    else:
        await c.execute("""UPDATE orders SET status=-2 WHERE status=-1 AND id IN
            (SELECT order_id FROM invoices WHERE user_id=$1 AND status=0 AND order_id IS NOT NULL)
        """, tg_id)
    await c.execute("UPDATE invoices SET status=2 WHERE user_id=$1 AND status=0", tg_id)


async def _expire_invoices(c):
    # истёкший счёт отменяет привязанный неоплаченный заказ и возвращает остаток
    rows = await c.fetch("""
        SELECT o.id, o.product_id, o.grams FROM orders o
        JOIN invoices i ON i.order_id = o.id
        WHERE i.status=0 AND i.expires < now() AND o.status=-1
    """)
    for r in rows:
        await _restock(c, r["product_id"], r["grams"])
    await c.execute("""
        UPDATE orders SET status=-2 WHERE status=-1 AND id IN
            (SELECT order_id FROM invoices
             WHERE status=0 AND expires < now() AND order_id IS NOT NULL)
    """)
    await c.execute("UPDATE invoices SET status=2 WHERE status=0 AND expires < now()")


async def pending_amounts(currency: str) -> set:
    # берём суммы не только активных счетов, но и всех за последние 3 часа —
    # чтобы метку не переиспользовать, пока поздний платёж по старому счёту
    # ещё может попасть в окно проверки (защита от кросс-зачисления)
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT amount_crypto FROM invoices WHERE currency=$1 "
            "AND (status=0 OR created > now() - interval '3 hours')", currency)
    return {r["amount_crypto"] for r in rows}


async def create_invoice(tg_id: int, amount_uah: int, currency: str,
                         amount_crypto: str, address: str) -> dict:
    async with _pool.acquire() as c, c.transaction():
        # новый счёт отменяет прежний неоплаченный (включая счёт-заказ — с возвратом стока)
        await _cancel_user_pending_invoices(c, tg_id)
        row = await c.fetchrow("""
            INSERT INTO invoices(user_id, amount_uah, currency, amount_crypto, address, expires)
            VALUES($1,$2,$3,$4,$5, now() + make_interval(secs => $6))
            RETURNING *
        """, tg_id, amount_uah, currency, amount_crypto, address, INVOICE_TTL)
    return dict(row)


async def active_invoice(tg_id: int) -> dict | None:
    async with _pool.acquire() as c:
        await _expire_invoices(c)
        r = await c.fetchrow(
            "SELECT * FROM invoices WHERE user_id=$1 AND status=0 ORDER BY id DESC LIMIT 1", tg_id)
    return dict(r) if r else None


async def invoice_get(inv_id: int, tg_id: int) -> dict | None:
    async with _pool.acquire() as c:
        await _expire_invoices(c)
        r = await c.fetchrow(
            "SELECT * FROM invoices WHERE id=$1 AND user_id=$2", inv_id, tg_id)
    return dict(r) if r else None


async def pending_invoices() -> list:
    async with _pool.acquire() as c:
        await _expire_invoices(c)
        rows = await c.fetch("SELECT * FROM invoices WHERE status=0 ORDER BY id LIMIT 100")
    return [dict(r) for r in rows]


async def invoice_cancel(inv_id: int, tg_id: int):
    async with _pool.acquire() as c, c.transaction():
        inv = await c.fetchrow(
            "SELECT * FROM invoices WHERE id=$1 AND user_id=$2 AND status=0", inv_id, tg_id)
        if not inv:
            return
        await c.execute("UPDATE invoices SET status=2 WHERE id=$1", inv_id)
        if inv["order_id"]:
            tag = await c.execute(
                "UPDATE orders SET status=-2 WHERE id=$1 AND status=-1", inv["order_id"])
            if tag == "UPDATE 1":
                o = await c.fetchrow(
                    "SELECT product_id, grams FROM orders WHERE id=$1", inv["order_id"])
                await _restock(c, o["product_id"], o["grams"])


async def invoice_paid(inv_id: int, txid: str) -> dict | None:
    """Помечает счёт оплаченным: пополнение баланса или оплата заказа."""
    async with _pool.acquire() as c, c.transaction():
        inv = await c.fetchrow(
            "SELECT * FROM invoices WHERE id=$1 AND status=0 FOR UPDATE", inv_id)
        if not inv:
            return None
        await c.execute("UPDATE invoices SET status=1, txid=$2 WHERE id=$1", inv_id, txid)
        res = {"user_id": inv["user_id"], "amount": inv["amount_uah"], "order_code": None}
        if inv["order_id"]:
            upd = await c.execute(
                "UPDATE orders SET status=0 WHERE id=$1 AND status=-1", inv["order_id"])
            if upd.endswith("1"):
                await _ref_bonus(c, inv["user_id"], inv["amount_uah"])
            res["order_code"] = f"MM-{inv['order_id'] + ORDER_CODE_BASE}"
        else:
            await c.execute("UPDATE users SET balance=balance+$1 WHERE tg_id=$2",
                            inv["amount_uah"], inv["user_id"])
        return res


async def delete_account(tg_id: int):
    """Полное удаление аккаунта со всей историей. Необратимо."""
    async with _pool.acquire() as c, c.transaction():
        await c.execute("DELETE FROM orders WHERE user_id=$1", tg_id)
        await c.execute("DELETE FROM topups WHERE user_id=$1", tg_id)
        await c.execute("DELETE FROM invoices WHERE user_id=$1", tg_id)
        await c.execute("DELETE FROM ratings WHERE user_id=$1", tg_id)
        await c.execute("DELETE FROM grows WHERE user_id=$1", tg_id)
        await c.execute("DELETE FROM shares WHERE user_id=$1", tg_id)
        await c.execute("DELETE FROM withdrawals WHERE user_id=$1", tg_id)
        await c.execute("DELETE FROM transfers WHERE user_id=$1", tg_id)
        await c.execute("UPDATE users SET ref_by=NULL WHERE ref_by=$1", tg_id)
        await c.execute("DELETE FROM users WHERE tg_id=$1", tg_id)


async def touch_device(tg_id: int, device: str, ip: str,
                       platform: str = "", premium=None, has_photo=None):
    """Запоминаем отпечаток устройства, IP и сигналы качества аккаунта
    (платформа iPhone/Android, премиум, наличие фото) — для антиабьюза реф-гонки.
    Пустые/None значения не затирают уже сохранённые."""
    async with _pool.acquire() as c:
        await c.execute("""
            UPDATE users SET device_hash = COALESCE(NULLIF($2, ''), device_hash),
                             last_ip     = COALESCE(NULLIF($3, ''), last_ip),
                             platform    = COALESCE(NULLIF($4, ''), platform),
                             tg_premium  = COALESCE($5, tg_premium),
                             tg_photo    = COALESCE($6, tg_photo)
            WHERE tg_id=$1
        """, tg_id, device[:64], ip[:64], (platform or "")[:48], premium, has_photo)


async def set_ban(tg_id: int, banned: bool, reason: str = "") -> None:
    """Универсальный бан на общей базе. Аккаунта в магазине может ещё не быть
    (человек только в казино/чате) — создаём заготовку, чтобы флаг не потерялся."""
    async with _pool.acquire() as c:
        await c.execute("""
            INSERT INTO users(tg_id, banned, banned_at, ban_reason)
            VALUES($1, $2, CASE WHEN $2 THEN now() ELSE NULL END, $3)
            ON CONFLICT (tg_id) DO UPDATE
                SET banned = $2,
                    banned_at = CASE WHEN $2 THEN COALESCE(users.banned_at, now()) ELSE NULL END,
                    ban_reason = CASE WHEN $2 THEN $3 ELSE NULL END
        """, int(tg_id), bool(banned), (reason or "")[:200] or None)


async def banned_ids() -> list[int]:
    """Все забаненные — для наполнения in-memory гейта при старте."""
    async with _pool.acquire() as c:
        rows = await c.fetch("SELECT tg_id FROM users WHERE banned")
        return [int(r["tg_id"]) for r in rows]


async def is_banned(tg_id: int) -> bool:
    async with _pool.acquire() as c:
        return bool(await c.fetchval("SELECT banned FROM users WHERE tg_id=$1", int(tg_id)))


async def accept_rules(tg_id: int) -> None:
    """Отметка о принятии правил чата (кнопка у guard-бота). Аккаунта в
    магазине может ещё не быть — тогда создаём заготовку, чтобы отметка
    не потерялась до первого входа в апп."""
    async with _pool.acquire() as c:
        await c.execute("""
            INSERT INTO users(tg_id, rules_accepted) VALUES($1, now())
            ON CONFLICT (tg_id) DO UPDATE
                SET rules_accepted = COALESCE(users.rules_accepted, now())
        """, tg_id)


async def rules_accepted(tg_id: int) -> bool:
    async with _pool.acquire() as c:
        return bool(await c.fetchval(
            "SELECT rules_accepted FROM users WHERE tg_id=$1", tg_id))


async def claim_bonus(tg_id: int, amount: int, device: str, ip: str):
    """Одноразовый бонус: раз на аккаунт и устройство. IP не проверяем —
    мобильные операторы сажают тысячи людей на один адрес (CGNAT), и честным
    прилетали ложные отказы; сам IP пишем в bonus_claims для разборов.
    Зачисляется в locked (нельзя вывести; тратится в магазине или отыгрывается в казино)."""
    device, ip = device[:64], ip[:64]
    if device == "na":  # заглушка старого клиента, совпадала у всех — не сравниваем
        device = ""
    async with _pool.acquire() as c, c.transaction():
        # сериализуем параллельные заявки с одного устройства
        if device:
            await c.execute("SELECT pg_advisory_xact_lock(hashtext('dev:'||$1))", device)
        row = await c.fetchrow(
            "SELECT bonus_claimed, rules_accepted FROM users WHERE tg_id=$1 FOR UPDATE",
            tg_id)
        if row["bonus_claimed"]:
            raise ValueError("Бонус уже был получен")
        if not row["rules_accepted"]:
            raise ValueError("Примите правила чата — кнопка под приветствием "
                             "(или команда /rules в чате)")
        if device and await c.fetchval(
                "SELECT 1 FROM bonus_claims WHERE device_hash=$1 LIMIT 1", device):
            raise ValueError("С этого устройства бонус уже получали")
        await c.execute(
            f"UPDATE users SET bonus_claimed=true, balance=balance+$1, locked=locked+$1, "
            f"wager_req=(CASE WHEN locked<=0 THEN 0 ELSE wager_req END)+$1*{BONUS_WAGER_X} WHERE tg_id=$2",
            amount, tg_id)
        await c.execute("""
            INSERT INTO bonus_claims(user_id, device_hash, ip) VALUES($1,$2,$3)
            ON CONFLICT (user_id) DO NOTHING
        """, tg_id, device or None, ip or None)
        # мгновенный бонус пригласившему за активацию друга (раз на друга).
        # Триггер — приветственный бонус: он уже под защитой (подписка на канал+чат,
        # правила, дедуп по устройству), так что накрутка бессмысленна.
        if REF_ACTIVATION_BONUS > 0:
            inv = await c.fetchrow(
                "SELECT ref_by, ref_activated FROM users WHERE tg_id=$1", tg_id)
            ref_by = inv["ref_by"]
            if ref_by and ref_by != tg_id and not inv["ref_activated"]:
                paid = await c.fetchval(f"""
                    UPDATE users SET balance=balance+$1, locked=locked+$1,
                                     ref_earned=ref_earned+$1,
                                     wager_req=(CASE WHEN locked<=0 THEN 0 ELSE wager_req END)+$1*{BONUS_WAGER_X}
                    WHERE tg_id=$2 RETURNING tg_id
                """, REF_ACTIVATION_BONUS, ref_by)   # NULL, если реферер удалён
                if paid:
                    await c.execute(
                        "UPDATE users SET ref_activated=true WHERE tg_id=$1", tg_id)
                    _ACTIVATION_NOTIFY.append(
                        {"ref_by": ref_by, "bonus": REF_ACTIVATION_BONUS})


# ── промокоды (одноразовые бонус-коды, деньги в locked) ──────────────────────
_PROMO_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих 0/O/1/I


def _gen_promo_code() -> str:
    return "".join(secrets.choice(_PROMO_ALPHABET) for _ in range(8))


async def promo_generate(amount: int, count: int) -> list:
    amount = max(1, int(amount))
    count = max(1, min(500, int(count)))
    codes = []
    async with _pool.acquire() as c:
        while len(codes) < count:
            code = _gen_promo_code()
            try:
                await c.execute("INSERT INTO promos(code, amount) VALUES($1,$2)", code, amount)
                codes.append(code)
            except asyncpg.UniqueViolationError:
                continue
    return codes


async def promo_redeem(tg_id: int, code: str) -> dict:
    code = (code or "").strip().upper()
    if not code:
        raise ValueError("Введите промокод")
    async with _pool.acquire() as c, c.transaction():
        p = await c.fetchrow("SELECT * FROM promos WHERE code=$1 FOR UPDATE", code)
        if not p:
            raise ValueError("Промокод не найден")
        if p["used_by"]:
            raise ValueError("Промокод уже использован")
        await c.execute("UPDATE promos SET used_by=$1, used_at=now() WHERE code=$2", tg_id, code)
        # деньги в locked — потратить в магазине можно, вывести нельзя (до отыгрыша вейджера)
        await c.execute(
            f"UPDATE users SET balance=balance+$1, locked=locked+$1, "
            f"wager_req=(CASE WHEN locked<=0 THEN 0 ELSE wager_req END)+$1*{BONUS_WAGER_X} WHERE tg_id=$2",
            p["amount"], tg_id)
        snap = await snapshot(tg_id, c)
        snap["promo_amount"] = p["amount"]
        return snap


async def admin_promos() -> dict:
    async with _pool.acquire() as c:
        rows = await c.fetch("""
            SELECT code, amount, used_by, used_at FROM promos ORDER BY created DESC LIMIT 300
        """)
        stats = await c.fetch("""
            SELECT amount, COUNT(*) AS total, COUNT(used_by) AS used FROM promos GROUP BY amount ORDER BY amount
        """)
    return {
        "codes": [{"code": r["code"], "amount": r["amount"], "used": bool(r["used_by"])} for r in rows],
        "stats": [{"amount": r["amount"], "total": r["total"], "used": r["used"]} for r in stats],
    }


# ── серверный пин-код ────────────────────────────────────────────────────────
async def pin_state(tg_id: int) -> dict:
    """Состояние аккаунта для казино: есть ли человек в магазине и стоит ли пин."""
    async with _pool.acquire() as c:
        u = await c.fetchrow(
            "SELECT pin_hash, pin_locked_until FROM users WHERE tg_id=$1", tg_id)
    if not u:
        return {"exists": False, "has_pin": False, "locked_until": None}
    locked = u["pin_locked_until"]
    now = datetime.now(timezone.utc)
    return {"exists": True, "has_pin": bool(u["pin_hash"]),
            "locked_until": locked.isoformat() if locked and locked > now else None}


async def verify_pin(tg_id: int, pin: str) -> dict:
    """Проверка пина с серверным счётчиком попыток и часовой блокировкой."""
    async with _pool.acquire() as c, c.transaction():
        u = await c.fetchrow(
            "SELECT pin_hash, pin_salt, pin_fails, pin_locked_until FROM users WHERE tg_id=$1 FOR UPDATE",
            tg_id)
        if not u or not u["pin_hash"]:
            return {"ok": True, "no_pin": True}
        now = datetime.now(timezone.utc)
        if u["pin_locked_until"] and u["pin_locked_until"] > now:
            return {"ok": False, "locked_until": u["pin_locked_until"].isoformat()}
        if _hash_pin(pin, u["pin_salt"]) == u["pin_hash"]:
            await c.execute(
                "UPDATE users SET pin_fails=0, pin_locked_until=NULL WHERE tg_id=$1", tg_id)
            return {"ok": True}
        fails = u["pin_fails"] + 1
        locked = None
        if fails >= PIN_MAX_FAILS:
            locked = now + PIN_LOCK
            fails = 0
        await c.execute(
            "UPDATE users SET pin_fails=$2, pin_locked_until=$3 WHERE tg_id=$1",
            tg_id, fails, locked)
        return {"ok": False,
                "left": 0 if locked else max(0, PIN_MAX_FAILS - fails),
                "locked_until": locked.isoformat() if locked else None}


async def set_pin(tg_id: int, pin: str, old: str) -> dict:
    """Установка/смена пина. Смена требует текущий пин (с той же блокировкой)."""
    if not (pin.isdigit() and len(pin) == 4):
        raise ValueError("Пин-код — 4 цифры")
    async with _pool.acquire() as c, c.transaction():
        u = await c.fetchrow(
            "SELECT pin_hash, pin_salt, pin_fails, pin_locked_until FROM users WHERE tg_id=$1 FOR UPDATE",
            tg_id)
        if u and u["pin_hash"]:
            now = datetime.now(timezone.utc)
            if u["pin_locked_until"] and u["pin_locked_until"] > now:
                raise ValueError("Ввод пин-кода заблокирован — попробуйте позже")
            if not old or _hash_pin(old, u["pin_salt"]) != u["pin_hash"]:
                fails = u["pin_fails"] + 1
                locked = now + PIN_LOCK if fails >= PIN_MAX_FAILS else None
                await c.execute(
                    "UPDATE users SET pin_fails=$2, pin_locked_until=$3 WHERE tg_id=$1",
                    tg_id, 0 if locked else fails, locked)
                raise ValueError("Неверный текущий пин-код")
        salt = secrets.token_hex(16)
        await c.execute("""
            UPDATE users SET pin_hash=$2, pin_salt=$3, pin_fails=0, pin_locked_until=NULL
            WHERE tg_id=$1
        """, tg_id, _hash_pin(pin, salt), salt)
        return {"ok": True}


# ── PayDome: авто-карта для пополнения ───────────────────────────────────────
CARD_PAY_TTL = 30 * 60  # 30 минут на оплату карты


async def card_payment_create(tg_id: int, payment_id: str, card: str,
                              amount_uah: int, pay_uah: int) -> dict:
    async with _pool.acquire() as c, c.transaction():
        # новый счёт отменяет прежний неоплаченный
        await c.execute(
            "UPDATE card_payments SET status=2 WHERE user_id=$1 AND status=0", tg_id)
        row = await c.fetchrow("""
            INSERT INTO card_payments(user_id, payment_id, card, amount_uah, pay_uah, expires)
            VALUES($1,$2,$3,$4,$5, now() + make_interval(secs => $6))
            RETURNING *
        """, tg_id, payment_id, card, amount_uah, pay_uah, CARD_PAY_TTL)
    return dict(row)


async def card_payment_active(tg_id: int) -> dict | None:
    async with _pool.acquire() as c:
        await c.execute(
            "UPDATE card_payments SET status=2 WHERE status=0 AND expires < now()")
        r = await c.fetchrow(
            "SELECT * FROM card_payments WHERE user_id=$1 AND status=0 ORDER BY id DESC LIMIT 1",
            tg_id)
    return dict(r) if r else None


async def card_payment_pending() -> list:
    async with _pool.acquire() as c:
        await c.execute(
            "UPDATE card_payments SET status=2 WHERE status=0 AND expires < now()")
        rows = await c.fetch("SELECT * FROM card_payments WHERE status=0 ORDER BY id LIMIT 100")
    return [dict(r) for r in rows]


async def card_payment_cancel(tg_id: int, payment_id: str):
    async with _pool.acquire() as c:
        await c.execute(
            "UPDATE card_payments SET status=2 WHERE payment_id=$1 AND user_id=$2 AND status=0",
            payment_id, tg_id)


async def card_payment_paid(payment_id: str) -> dict | None:
    """Помечает оплаченным и зачисляет баланс. None — уже обработан/нет такого."""
    async with _pool.acquire() as c, c.transaction():
        p = await c.fetchrow(
            "SELECT * FROM card_payments WHERE payment_id=$1 AND status=0 FOR UPDATE", payment_id)
        if not p:
            return None
        await c.execute("UPDATE card_payments SET status=1 WHERE id=$1", p["id"])
        # на баланс — желаемая сумма (amount_uah); разница pay_uah−amount_uah = сервисный сбор
        await c.execute("UPDATE users SET balance=balance+$1 WHERE tg_id=$2",
                        p["amount_uah"], p["user_id"])
        return {"user_id": p["user_id"], "amount": p["amount_uah"]}


# ── вывод баланса (ручная выплата) ───────────────────────────────────────────
MIN_WITHDRAW = 100


async def create_withdrawal(tg_id: int, amount: int, method: str, requisites: str) -> dict:
    if amount < MIN_WITHDRAW:
        raise ValueError(f"Минимальная сумма вывода — {MIN_WITHDRAW} ₴")
    if not requisites.strip():
        raise ValueError("Укажите реквизиты для выплаты")
    async with _pool.acquire() as c, c.transaction():
        u = await c.fetchrow("SELECT balance, locked FROM users WHERE tg_id=$1 FOR UPDATE", tg_id)
        withdrawable = u["balance"] - (u["locked"] or 0)
        if withdrawable < amount:
            raise ValueError("Доступно к выводу меньше — бонусные средства можно только потратить в магазине")
        await c.execute("UPDATE users SET balance=balance-$1 WHERE tg_id=$2", amount, tg_id)
        await c.execute("""
            INSERT INTO withdrawals(user_id, amount, method, requisites) VALUES($1,$2,$3,$4)
        """, tg_id, amount, method, requisites.strip()[:200])
        return await snapshot(tg_id, c)


async def admin_withdrawals() -> list:
    async with _pool.acquire() as c:
        rows = await c.fetch("""
            SELECT w.*, u.name, u.username FROM withdrawals w
            LEFT JOIN users u ON u.tg_id = w.user_id
            WHERE w.status=0 ORDER BY w.id
        """)
    return [{
        "id": r["id"], "user_id": r["user_id"],
        "user": r["name"] or "?", "username": r["username"],
        "amount": r["amount"], "method": r["method"], "requisites": r["requisites"],
        "created": r["created"].isoformat(),
    } for r in rows]


async def withdrawal_decide(wid: int, approve: bool) -> dict:
    """Выплачено — баланс уже списан; отклонено — возврат на баланс."""
    async with _pool.acquire() as c, c.transaction():
        w = await c.fetchrow(
            "SELECT * FROM withdrawals WHERE id=$1 AND status=0 FOR UPDATE", wid)
        if not w:
            raise ValueError("Заявка не найдена или уже обработана")
        await c.execute("UPDATE withdrawals SET status=$2, decided=now() WHERE id=$1",
                        wid, 1 if approve else 2)
        if not approve:
            await c.execute("UPDATE users SET balance=balance+$1 WHERE tg_id=$2",
                            w["amount"], w["user_id"])
        return {"user_id": w["user_id"], "amount": w["amount"], "approved": approve}


# ── E-growing (доли в кустах, стадии роста) ──────────────────────────────────
# стадии: 0 семечко, 1 саженец, 2 вегетативная, 3 предцвет, 4 цветение, 5 сбор урожая
GROW_STAGES_DEFAULT = [
    {"d": 7, "p": 35}, {"d": 14, "p": 28}, {"d": 45, "p": 22},
    {"d": 20, "p": 15}, {"d": 28, "p": 8}, {"d": 14, "p": 0},
]
HARVEST = 5  # на сборе урожая вход закрыт


def _plan_stages(r) -> list:
    try:
        s = json.loads(r["stages"]) if r["stages"] else None
    except (ValueError, TypeError):
        s = None
    return s if s and len(s) == 6 else [dict(x) for x in GROW_STAGES_DEFAULT]


def _plan_row(r) -> dict:
    return {
        "id": r["id"], "name": r["name"], "sub": r["sub"] or "",
        "genetics": r["genetics"] or "",
        "price": r["price"], "slots": r["slots"],
        "stages": _plan_stages(r),
        "stage": r["stage"], "stage_at": r["stage_at"].isoformat(),
        "start_at": r["start_at"].isoformat(),
        "sold_pct": r["sold_pct"], "done": r["done"],
        "active": r["active"],
        "photo": bool(r["photo"]),
        "pv": len(r["photo"] or ""),
    }


async def get_grow_plans(include_inactive: bool = False, conn=None) -> list:
    c = conn or _pool
    q = "SELECT * FROM grow_plans" + ("" if include_inactive else " WHERE active") + " ORDER BY pos, id"
    plans = [_plan_row(r) for r in await c.fetch(q)]
    live = await c.fetch(
        "SELECT id, plan_id, note, created FROM grow_photos ORDER BY id DESC")
    by_plan: dict = {}
    for r in live:
        by_plan.setdefault(r["plan_id"], []).append(
            {"id": r["id"], "note": r["note"] or "", "created": r["created"].isoformat()})
    for p in plans:
        p["live"] = by_plan.get(p["id"], [])[:12]
    return plans


async def add_grow_photo(plan_id: int, photo: str, note: str = "") -> int:
    if not photo.startswith("data:image/") or len(photo) > MAX_PHOTO_LEN:
        raise ValueError("Приложите фото")
    data = json.dumps({"f": photo, "t": _make_thumb(photo)})
    async with _pool.acquire() as c:
        return await c.fetchval(
            "INSERT INTO grow_photos(plan_id, photo, note) VALUES($1,$2,$3) RETURNING id",
            plan_id, data, note.strip()[:200])


async def delete_grow_photo(photo_id: int):
    async with _pool.acquire() as c:
        await c.execute("DELETE FROM grow_photos WHERE id=$1", photo_id)


async def grow_live_photo(photo_id: int, size: str = "f") -> str | None:
    async with _pool.acquire() as c:
        val = await c.fetchval("SELECT photo FROM grow_photos WHERE id=$1", photo_id)
    if not val:
        return None
    try:
        d = json.loads(val)
        return d.get(size) or d.get("f")
    except (ValueError, TypeError):
        return val


async def grow_plan_photo(pid: int, size: str = "f") -> str | None:
    async with _pool.acquire() as c:
        val = await c.fetchval("SELECT photo FROM grow_plans WHERE id=$1", pid)
    if not val:
        return None
    try:
        d = json.loads(val)
        return d.get(size) or d.get("f")
    except (ValueError, TypeError):
        return val


def _parse_dt(v):
    """ISO-строку (в т.ч. с 'Z') → aware datetime; иначе None."""
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def save_grow_plan(d: dict) -> int:
    stages = d.get("stages")
    if not (isinstance(stages, list) and len(stages) == 6):
        stages = GROW_STAGES_DEFAULT
    stages = [{"d": max(0, int(s.get("d", 0))), "p": max(0, min(1000, int(s.get("p", 0))))}
              for s in stages]
    start = _parse_dt(d.get("start_at"))  # None = старт сразу / без перепланирования
    async with _pool.acquire() as c:
        photo = None
        if d.get("photo") == "keep" and d.get("id"):
            photo = await c.fetchval("SELECT photo FROM grow_plans WHERE id=$1", int(d["id"]))
        elif isinstance(d.get("photo"), str) and d["photo"].startswith("data:image/") \
                and len(d["photo"]) <= MAX_PHOTO_LEN:
            photo = json.dumps({"f": d["photo"], "t": _make_thumb(d["photo"])})
        slots = d.get("slots")
        slots = None if slots in (None, "") else max(0, int(slots))
        vals = (d["name"], d.get("sub", ""), str(d.get("genetics", "")).strip(),
                photo, int(d["price"]), slots,
                json.dumps(stages), bool(d.get("active", True)))
        if d.get("id"):
            pid = int(d["id"])
            await c.execute("""
                UPDATE grow_plans SET name=$2, sub=$3, genetics=$4, photo=$5, price=$6,
                                      slots=$7, stages=$8, active=$9 WHERE id=$1
            """, pid, *vals)
            if start is not None:
                # перепланирование старта: двигаем и отсчёт цикла, пока не пошли стадии
                await c.execute("""
                    UPDATE grow_plans
                    SET start_at=$2,
                        stage_at=CASE WHEN stage=0 AND NOT done THEN $2 ELSE stage_at END
                    WHERE id=$1
                """, pid, start)
            return pid
        # новая программа: цикл и таймер стартуют с даты старта (или сразу)
        s = start or datetime.now(timezone.utc)
        return await c.fetchval("""
            INSERT INTO grow_plans(name, sub, genetics, photo, price, slots, stages, active,
                                   stage, stage_at, start_at, pos)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8, 0, $9, $9,
                   COALESCE((SELECT MAX(pos)+1 FROM grow_plans), 0))
            RETURNING id
        """, *vals, s)


async def delete_grow_plan(pid: int):
    """Удаление программы: невыплаченные доли возвращаются на балансы."""
    async with _pool.acquire() as c, c.transaction():
        rows = await c.fetch("SELECT * FROM shares WHERE plan_id=$1 AND status=0", pid)
        for r in rows:
            await c.execute("UPDATE users SET balance=balance+$1 WHERE tg_id=$2",
                            r["invested"], r["user_id"])
        await c.execute("DELETE FROM shares WHERE plan_id=$1", pid)
        await c.execute("DELETE FROM grow_photos WHERE plan_id=$1", pid)
        await c.execute("DELETE FROM grow_plans WHERE id=$1", pid)
        return [{"user_id": r["user_id"], "amount": r["invested"]} for r in rows]


async def user_shares(tg_id: int, conn=None) -> list:
    c = conn or _pool
    rows = await c.fetch("""
        SELECT s.*, p.name AS plan_name, p.stage AS p_stage, p.stage_at AS p_stage_at,
               p.stages AS p_stages, p.done AS p_done, p.start_at AS p_start_at
        FROM shares s LEFT JOIN grow_plans p ON p.id = s.plan_id
        WHERE s.user_id=$1 ORDER BY s.id DESC
    """, tg_id)
    out = []
    for r in rows:
        try:
            st = json.loads(r["p_stages"]) if r["p_stages"] else GROW_STAGES_DEFAULT
        except (ValueError, TypeError):
            st = GROW_STAGES_DEFAULT
        out.append({
            "id": r["id"], "plan_id": r["plan_id"],
            "plan_name": r["plan_name"] or "Программа удалена",
            "pct": r["pct"], "invested": r["invested"], "payout": r["payout"],
            "profit_pct": r["profit_pct"], "entry_stage": r["stage"],
            "status": r["status"], "created": r["created"].isoformat(),
            "plan_stage": r["p_stage"] if r["p_stage"] is not None else HARVEST,
            "plan_stage_at": r["p_stage_at"].isoformat() if r["p_stage_at"] else None,
            "plan_start_at": r["p_start_at"].isoformat() if r["p_start_at"] else None,
            "plan_stages": st, "plan_done": bool(r["p_done"]),
        })
    return out


async def buy_share(tg_id: int, plan_id: int, pct: int) -> dict:
    """Покупка доли куста (кратно 10%) на текущей стадии программы."""
    if pct < 10 or pct > 100 or pct % 10:
        raise ValueError("Доля — от 10% до 100%, кратно 10")
    async with _pool.acquire() as c, c.transaction():
        p = await c.fetchrow(
            "SELECT * FROM grow_plans WHERE id=$1 AND active FOR UPDATE", plan_id)
        if not p or p["done"]:
            raise ValueError("Программа недоступна")
        if p["stage"] >= HARVEST:
            raise ValueError("Идёт сбор урожая — вход закрыт до новой программы")
        if p["slots"] is not None:
            avail = p["slots"] * 100 - p["sold_pct"]
            if pct > avail:
                raise ValueError(f"Свободно только {avail}% — выберите долю меньше"
                                 if avail > 0 else "Все доли выкуплены")
        invested = round(p["price"] * pct / 100)
        u = await c.fetchrow("SELECT balance FROM users WHERE tg_id=$1 FOR UPDATE", tg_id)
        if u["balance"] < invested:
            raise ValueError("Недостаточно средств — пополните баланс")
        profit = _plan_stages(p)[p["stage"]]["p"]
        payout = invested + round(invested * profit / 100)
        await c.execute("UPDATE users SET balance=balance-$1 WHERE tg_id=$2", invested, tg_id)
        await c.execute("UPDATE grow_plans SET sold_pct=sold_pct+$2 WHERE id=$1", plan_id, pct)
        await c.execute("""
            INSERT INTO shares(user_id, plan_id, pct, invested, profit_pct, payout, stage)
            VALUES($1,$2,$3,$4,$5,$6,$7)
        """, tg_id, plan_id, pct, invested, profit, payout, p["stage"])
        return await snapshot(tg_id, c)


async def _payout_plan(c, plan_id: int) -> list:
    """Выплата всех долей программы при сборе урожая."""
    rows = await c.fetch(
        "SELECT * FROM shares WHERE plan_id=$1 AND status=0 FOR UPDATE", plan_id)
    name = await c.fetchval("SELECT name FROM grow_plans WHERE id=$1", plan_id)
    for r in rows:
        await c.execute("UPDATE users SET balance=balance+$1 WHERE tg_id=$2",
                        r["payout"], r["user_id"])
        await c.execute("UPDATE shares SET status=1 WHERE id=$1", r["id"])
    return [{"user_id": r["user_id"], "name": name, "payout": r["payout"]} for r in rows]


async def set_grow_stage(plan_id: int, stage: int) -> list:
    """Ручное переключение стадии админом.
    stage 0..5 — просто ставит стадию; stage 6 — завершить сбор и выплатить."""
    s = max(0, min(6, int(stage)))
    async with _pool.acquire() as c, c.transaction():
        if s >= 6:
            await c.execute("""
                UPDATE grow_plans SET stage=$2, stage_at=now(), done=true WHERE id=$1
            """, plan_id, HARVEST)
            return await _payout_plan(c, plan_id)
        await c.execute("""
            UPDATE grow_plans SET stage=$2, stage_at=now(), done=false WHERE id=$1
        """, plan_id, s)
        return []


async def advance_grow_stages() -> list:
    """Авто-смена стадий по дням. Выплаты — когда стадия сбора урожая закончилась."""
    notes = []
    async with _pool.acquire() as c, c.transaction():
        plans = await c.fetch(
            "SELECT * FROM grow_plans WHERE active AND NOT done FOR UPDATE")
        now = datetime.now(timezone.utc)
        for p in plans:
            stages = _plan_stages(p)
            stage, at = p["stage"], p["stage_at"]
            changed = finished = False
            while True:
                dur = timedelta(days=max(0, stages[stage]["d"]))
                if now - at < dur:
                    break
                if stage >= HARVEST:      # сбор урожая закончился — выплата
                    finished = changed = True
                    break
                at += dur
                stage += 1
                changed = True
            if changed:
                await c.execute("""
                    UPDATE grow_plans SET stage=$2, stage_at=$3, done=$4 WHERE id=$1
                """, p["id"], stage, at, finished)
                if finished:
                    notes += await _payout_plan(c, p["id"])
    return notes


# ── активность в чате (еженедельные награды) ─────────────────────────────────
ACTIVITY_DAY_CAP = 60    # потолок очков в сутки — чтобы нельзя было нафлудить приз
ACTIVITY_DAY_BONUS = 8   # премия за каждый день, когда человек был активен
ACTIVITY_STRIKE_FINE = 5  # штраф за нарушение (предупреждение от модератора)


async def bump_activity(user_id: int, name: str, points: int = 1):
    """Плюс очко за содержательное сообщение, но не выше дневного потолка."""
    async with _pool.acquire() as c:
        # $3/$4 с явным ::int: LEAST из двух голых параметров не даёт Postgres
        # вывести тип — запрос падал на подготовке, и очки не писались вовсе
        await c.execute("""
            INSERT INTO chat_activity(user_id, day, points, msgs, name)
            VALUES($1, CURRENT_DATE, LEAST($3::int, $4::int), 1, $2)
            ON CONFLICT (user_id, day) DO UPDATE
            SET points = LEAST(chat_activity.points + $3::int, $4::int),
                msgs   = chat_activity.msgs + 1,
                name   = COALESCE(EXCLUDED.name, chat_activity.name)
        """, user_id, name, points, ACTIVITY_DAY_CAP)


async def activity_selftest(week_start) -> str:
    """Самопроверка счётчика: гоняем НАСТОЯЩИЙ bump_activity, не копию SQL —
    иначе ошибка в боевом запросе останется незамеченной."""
    async with _pool.acquire() as c:
        await c.execute("DELETE FROM chat_activity WHERE user_id=-1")
    await bump_activity(-1, "selftest")
    async with _pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT points, msgs FROM chat_activity WHERE user_id=-1 AND day=CURRENT_DATE")
        await c.execute("DELETE FROM chat_activity WHERE user_id=-1")
        stat = await c.fetchrow("""
            SELECT COUNT(DISTINCT user_id) AS users, COALESCE(SUM(points),0) AS pts,
                   COALESCE(SUM(msgs),0) AS msgs
            FROM chat_activity WHERE day >= $1 AND user_id > 0
        """, week_start)
        today_rows = await c.fetch("""
            SELECT name, points, msgs, strikes FROM chat_activity
            WHERE day = CURRENT_DATE AND user_id > 0
            ORDER BY points DESC LIMIT 15
        """)
    lines = [f"— {r['name'] or '?'}: {r['points']} очк., {r['msgs']} сообщ."
             + (f", страйков {r['strikes']}" if r['strikes'] else "")
             for r in today_rows]
    return (f"запись/чтение: ok (points={row['points']}, msgs={row['msgs']})\n"
            f"за неделю: участников {stat['users']}, очков {stat['pts']}, "
            f"сообщений засчитано {stat['msgs']}"
            + ("\n\nсегодня:\n" + "\n".join(lines) if lines else ""))


async def bump_strike(user_id: int, name: str = ""):
    """Нарушение — минус к недельному счёту, чтобы спам не приносил призов."""
    async with _pool.acquire() as c:
        await c.execute("""
            INSERT INTO chat_activity(user_id, day, strikes, name)
            VALUES($1, CURRENT_DATE, 1, $2)
            ON CONFLICT (user_id, day) DO UPDATE SET strikes = chat_activity.strikes + 1
        """, user_id, name or None)


_TOP_SQL = """
    SELECT user_id, MAX(name) AS name,
           SUM(points) AS pts, COUNT(DISTINCT day) AS days, SUM(strikes) AS strikes,
           SUM(points) + $3 * COUNT(DISTINCT day) - $4 * SUM(strikes) AS score
    FROM chat_activity
    WHERE day >= $1 AND day <= $2
    GROUP BY user_id
    HAVING SUM(points) > 0
    ORDER BY score DESC, pts DESC
    LIMIT $5
"""


async def top_activity(week_start, week_end, limit: int = 3) -> list:
    async with _pool.acquire() as c:
        rows = await c.fetch(_TOP_SQL, week_start, week_end,
                             ACTIVITY_DAY_BONUS, ACTIVITY_STRIKE_FINE, limit)
    # людей с очками показываем всегда: ушедший в минус из-за штрафов счёт
    # прижимаем к нулю, а не прячем человека из списка (призы всё равно
    # только при положительном счёте — см. award_activity_week)
    return [{"user_id": r["user_id"], "name": r["name"] or "участник",
             "points": r["pts"], "days": r["days"], "score": max(0, int(r["score"]))}
            for r in rows]


async def award_activity_week(week_start, week_end, prizes: list) -> list:
    """Начисляет призы топ-участникам (в locked — как бонус, без вывода).
    Идемпотентно: за одну неделю награждаем один раз."""
    async with _pool.acquire() as c, c.transaction():
        if await c.fetchval("SELECT 1 FROM activity_awards WHERE week_start=$1 LIMIT 1", week_start):
            return []
        rows = await c.fetch(_TOP_SQL, week_start, week_end,
                             ACTIVITY_DAY_BONUS, ACTIVITY_STRIKE_FINE, len(prizes))
        out = []
        for i, r in enumerate(rows):
            if r["score"] <= 0:
                continue
            amount = prizes[i]
            uid = r["user_id"]
            # человек мог ещё не открывать приложение — заводим ему счёт
            await c.execute("INSERT INTO users(tg_id, name) VALUES($1,$2) "
                            "ON CONFLICT (tg_id) DO NOTHING", uid, r["name"])
            await c.execute("UPDATE users SET balance=balance+$1, locked=locked+$1 "
                            "WHERE tg_id=$2", amount, uid)
            await c.execute("""
                INSERT INTO activity_awards(week_start, place, user_id, amount, score)
                VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING
            """, week_start, i + 1, uid, amount, int(r["score"]))
            out.append({"place": i + 1, "user_id": uid, "name": r["name"] or "участник",
                        "amount": amount, "score": int(r["score"]),
                        "points": r["pts"], "days": r["days"]})
        return out


# ── статистика подписок чата/канала (guard-бот /stats) ───────────────────────
async def record_member_event(chat_id: int, user_id: int, direction: str,
                              name: str | None = None) -> bool:
    """Фиксирует вход/выход участника. direction: 'join' | 'leave'.
    Дедуп: тот же (chat, user, направление) в пределах 30 c не пишем дважды —
    Telegram изредка дублирует chat_member при смене статуса."""
    if direction not in ("join", "leave"):
        return False
    async with _pool.acquire() as c:
        dup = await c.fetchval(
            "SELECT 1 FROM member_events "
            "WHERE chat_id=$1 AND user_id=$2 AND dir=$3 AND at > now()-interval '30 seconds' "
            "LIMIT 1", chat_id, user_id, direction)
        if dup:
            return False
        await c.execute(
            "INSERT INTO member_events(chat_id, user_id, dir, name) VALUES($1,$2,$3,$4)",
            chat_id, user_id, direction, (name or "")[:128] or None)
        return True


async def member_flow(chat_id: int, since) -> dict:
    """Сколько подписалось/отписалось в чате с момента `since` (datetime, UTC)."""
    async with _pool.acquire() as c:
        row = await c.fetchrow("""
            SELECT
              COUNT(*) FILTER (WHERE dir='join')  AS joined,
              COUNT(*) FILTER (WHERE dir='leave') AS left
            FROM member_events WHERE chat_id=$1 AND at >= $2
        """, chat_id, since)
    joined = int(row["joined"] or 0)
    left = int(row["left"] or 0)
    return {"joined": joined, "left": left, "net": joined - left}


async def save_member_snapshot(chat_id: int, total: int) -> None:
    async with _pool.acquire() as c:
        await c.execute(
            "INSERT INTO member_snapshots(chat_id, total) VALUES($1,$2) "
            "ON CONFLICT DO NOTHING", chat_id, int(total))


async def member_tracking_since(chat_id: int):
    """Момент, с которого по чату есть хоть какие-то данные (событие/снимок)."""
    async with _pool.acquire() as c:
        return await c.fetchval("""
            SELECT min(t) FROM (
              SELECT min(at) AS t FROM member_events    WHERE chat_id=$1
              UNION ALL
              SELECT min(at) AS t FROM member_snapshots WHERE chat_id=$1
            ) q
        """, chat_id)


# ── награды чата (викторина, реф-гонка) ──────────────────────────────────────
async def chat_reward(user_id: int, name: str, amount: int) -> None:
    """Начислить бонус участнику чата в баланс (в locked — как приз, без вывода).
    Заводим счёт, если человек ещё не открывал приложение."""
    if amount <= 0:
        return
    async with _pool.acquire() as c, c.transaction():
        await c.execute("INSERT INTO users(tg_id, name) VALUES($1,$2) "
                        "ON CONFLICT (tg_id) DO NOTHING", user_id, (name or "")[:64])
        await c.execute("UPDATE users SET balance=balance+$1, locked=locked+$1 "
                        "WHERE tg_id=$2", amount, user_id)


async def quiz_claim_day(day) -> bool:
    """Резервируем сегодняшний прогон викторины. True — прогон наш (можно вести),
    False — на этот день уже кто-то запустил (защита от дублей при рестарте)."""
    async with _pool.acquire() as c:
        got = await c.fetchval("""
            INSERT INTO quiz_days(day) VALUES($1)
            ON CONFLICT (day) DO NOTHING RETURNING day
        """, day)
    return got is not None


async def quiz_finish_day(day, theme, winner_id, winner_name, winner_score) -> None:
    async with _pool.acquire() as c:
        await c.execute("""
            UPDATE quiz_days SET theme=$2, winner_id=$3, winner_name=$4, winner_score=$5
            WHERE day=$1
        """, day, theme, winner_id, (winner_name or None), winner_score)


async def quiz_asked_hashes(theme: str) -> set:
    """Какие вопросы темы уже задавались в текущем круге."""
    async with _pool.acquire() as c:
        rows = await c.fetch("SELECT qhash FROM quiz_asked WHERE theme=$1", theme)
    return {r["qhash"] for r in rows}


async def quiz_mark_asked(theme: str, hashes: list) -> None:
    if not hashes:
        return
    async with _pool.acquire() as c:
        await c.executemany(
            "INSERT INTO quiz_asked(theme, qhash) VALUES($1,$2) ON CONFLICT DO NOTHING",
            [(theme, h) for h in hashes])


async def quiz_reset_theme(theme: str) -> None:
    """Начать новый круг — забываем, что уже задавали (вопросы можно повторять)."""
    async with _pool.acquire() as c:
        await c.execute("DELETE FROM quiz_asked WHERE theme=$1", theme)


async def quiz_used_themes() -> set:
    """Темы, уже выпадавшие в текущем круге (их прячем из голосования)."""
    async with _pool.acquire() as c:
        rows = await c.fetch("SELECT theme FROM quiz_used_themes")
    return {r["theme"] for r in rows}


async def quiz_mark_theme_used(theme: str) -> None:
    async with _pool.acquire() as c:
        await c.execute("INSERT INTO quiz_used_themes(theme) VALUES($1) "
                        "ON CONFLICT DO NOTHING", theme)


async def quiz_reset_theme_cycle() -> None:
    """Круг тем пройден — начинаем заново (снова доступны все темы)."""
    async with _pool.acquire() as c:
        await c.execute("DELETE FROM quiz_used_themes")


async def ref_race_activated(week_start, week_end) -> list:
    """Приглашённые, АКТИВИРОВАВШИЕСЯ за неделю (забрали приветственный бонус —
    запись в bonus_claims). По одной строке на друга + данные пригласившего.
    Подписку каждого проверяет уже бот (getChatMember) — здесь только БД."""
    async with _pool.acquire() as c:
        rows = await c.fetch("""
            SELECT u.ref_by AS referrer_id, u.tg_id AS friend_id,
                   ref.name AS name, ref.username AS username
            FROM users u
            JOIN bonus_claims bc ON bc.user_id = u.tg_id
            LEFT JOIN users ref  ON ref.tg_id = u.ref_by
            WHERE u.ref_by IS NOT NULL
              AND bc.created >= $1 AND bc.created < $2
        """, week_start, week_end)
    return [{"referrer_id": r["referrer_id"], "friend_id": r["friend_id"],
             "name": r["name"] or "участник", "username": r["username"]} for r in rows]


async def ref_race_award_record(week_start, user_id: int, cnt: int, prize: int) -> bool:
    """Идемпотентно за неделю начисляет приз победителю и фиксирует запись.
    True — приз начислен сейчас; False — за эту неделю уже награждали."""
    async with _pool.acquire() as c, c.transaction():
        if await c.fetchval("SELECT 1 FROM ref_race_awards WHERE week_start=$1", week_start):
            return False
        await c.execute("INSERT INTO users(tg_id) VALUES($1) "
                        "ON CONFLICT (tg_id) DO NOTHING", user_id)
        await c.execute(f"UPDATE users SET balance=balance+$1, locked=locked+$1, "
                        f"wager_req=(CASE WHEN locked<=0 THEN 0 ELSE wager_req END)+$1*{BONUS_WAGER_X} "
                        f"WHERE tg_id=$2", prize, user_id)
        await c.execute("""
            INSERT INTO ref_race_awards(week_start, user_id, cnt, amount)
            VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING
        """, week_start, user_id, cnt, prize)
    return True


# ── общий кошелёк для внешних сервисов ───────────────────────────────────────
async def wallet_balance(tg_id: int) -> dict:
    async with _pool.acquire() as c:
        u = await c.fetchrow(
            "SELECT balance, locked, wager_req FROM users WHERE tg_id=$1", tg_id)
    if not u:
        raise ValueError("Пользователь не найден")
    locked = u["locked"] or 0
    wager = u["wager_req"] or 0
    active = wager > 0 and locked > 0    # «повисший» вейджер без бонуса не показываем
    return {"balance": u["balance"], "locked": locked,
            "wager": wager if active else 0,
            "bonus_max_bet": (BONUS_MAX_BET if active else None),
            "withdrawable": max(0, u["balance"] - locked)}


async def wallet_op(tg_id: int, amount: int, op_id: str, kind: str, ref: str = None) -> dict:
    """Движение по кошельку: amount>0 — начисление, amount<0 — списание.

    Играть в казино можно на ВЕСЬ баланс, включая бонусы (locked): ставка
    списывается сначала с выводимых средств, бонус — в последнюю очередь.

    Вейджер (отыгрыш бонуса):
    • Ставка уменьшает wager_req на свою сумму (оборот). Пока wager_req>0,
      ставка не может превышать BONUS_MAX_BET — нельзя слить бонус в один спин.
    • Выигрыш при активном вейджере и игре «на бонус» (нет выводимых средств)
      липкий — идёт в locked, а не в вывод. Так удачный бонус нельзя сразу
      обналичить; его ещё надо отыграть. Выигрыш на свои деньги — выводим.
    • Как только оборот добит (wager_req дошёл до 0) — весь locked
      разблокируется в вывод: бонус отыгран.
    Вывести бонус напрямую по-прежнему нельзя: вывод считает balance-locked.
    Идемпотентно по op_id: повтор запроса вернёт прежний результат, а не спишет дважды."""
    op_id = str(op_id or "").strip()
    if not op_id or len(op_id) > 64:
        raise ValueError("Неверный op_id")
    if amount == 0:
        raise ValueError("Нулевая операция")
    is_referral = amount > 0 and (str(ref) == "referral" or str(kind) == "referral")
    # PvP-покер: op_id казино всегда начинается с "pv:" (бай-ин/кэшаут/возврат).
    # Там игроки могут «подыгрывать» друг другу, поэтому бонус в PvP не пускаем
    # и оборот PvP не засчитываем в вейджер.
    is_pvp = op_id.startswith("pv:")
    async with _pool.acquire() as c, c.transaction():
        # пользователя блокируем ПЕРВЫМ: тогда параллельный повтор с тем же op_id
        # дождётся коммита и увидит готовую запись, а не словит конфликт ключа
        u = await c.fetchrow(
            "SELECT balance, locked, wager_req FROM users WHERE tg_id=$1 FOR UPDATE", tg_id)
        if not u:
            raise ValueError("Пользователь не найден")
        prev = await c.fetchrow("SELECT balance_after FROM wallet_ops WHERE op_id=$1", op_id)
        if prev:
            return {"balance": prev["balance_after"], "duplicate": True}
        balance = u["balance"]
        locked = u["locked"] or 0
        wager = u["wager_req"] or 0
        withdrawable = balance - locked
        if amount < 0:
            if is_pvp:
                # PvP — только выводимые деньги, бонус сюда не пускаем
                if withdrawable < -amount:
                    raise ValueError("В PvP-покере бонусными деньгами играть нельзя — только выводимыми")
            else:
                if balance < -amount:
                    raise ValueError("Недостаточно средств")
                # лимит ставки, пока есть неотыгранный бонус (locked>0 отсекает
                # «повисший» вейджер после проигрыша бонуса — тогда лимит не нужен)
                if wager > 0 and locked > 0 and (-amount) > BONUS_MAX_BET:
                    raise ValueError(f"Пока бонус не отыгран, ставка не больше {BONUS_MAX_BET} ₴")

        new_balance = balance + amount
        new_locked = locked
        new_wager = wager
        if amount < 0 and not is_pvp:
            # ставка: бонус тратится последним (locked ≤ остатка), оборот уменьшается
            new_locked = min(locked, new_balance)
            new_wager = max(0, wager - (-amount))
            if wager > 0 and new_wager == 0:
                new_locked = 0            # вейджер добит — весь бонус в вывод
        elif amount > 0 and not is_referral and not is_pvp:
            # выигрыш: липкий, только если играли на бонус (нет выводимых) и вейджер жив
            playing_bonus = withdrawable <= 0
            if wager > 0 and playing_bonus:
                new_locked = min(new_balance, locked + amount)
        # PvP (бай-ин или кэшаут): locked и wager не трогаем — всё на выводимых

        await c.execute(
            "UPDATE users SET balance=$1, locked=$2, wager_req=$3 WHERE tg_id=$4",
            new_balance, new_locked, new_wager, tg_id)
        # реферальные выплаты казино приходят сюда с ref='referral' —
        # учитываем их и в ref_earned, чтобы статистика была общей
        if is_referral:
            await c.execute(
                "UPDATE users SET ref_earned = ref_earned + $1 WHERE tg_id=$2",
                amount, tg_id)
        # Δ выводимых средств — по нему казино считает РЕАЛЬНЫЙ банк
        # (бонусные ставки не считаются; отыгранный бонус входит как реальная выплата)
        real_delta = (new_balance - new_locked) - (balance - locked)
        await c.execute("""
            INSERT INTO wallet_ops(op_id, user_id, delta, kind, ref, balance_after, real_delta)
            VALUES($1,$2,$3,$4,$5,$6,$7)
        """, op_id, tg_id, amount, str(kind)[:32], (str(ref)[:64] if ref else None),
             new_balance, real_delta)
        active = new_wager > 0 and new_locked > 0
        return {"balance": new_balance, "locked": new_locked,
                "wager": new_wager if active else 0,
                "bonus_max_bet": (BONUS_MAX_BET if active else None),
                "withdrawable": max(0, new_balance - new_locked), "duplicate": False}


async def casino_real_bank() -> int:
    """Реальный банк казино = −Σ Δвыводимых по ставкам/выигрышам (kind bet/win).
    Считает движение ТОЛЬКО реальных (выводимых) денег: бонус-ставки в банк не
    идут, а отыгранный бонус (стал выводимым) входит как реальная выплата.
    Реф-выплаты исключаем — это маркетинг, не игровой оборот."""
    async with _pool.acquire() as c:
        v = await c.fetchval(
            "SELECT -COALESCE(SUM(real_delta), 0) FROM wallet_ops "
            "WHERE kind IN ('bet', 'win') AND (ref IS NULL OR ref <> 'referral')")
    return int(v or 0)


async def wallet_bonus(tg_id: int, amount: int, op_id: str,
                       kind: str = "bonus", ref: str = None) -> dict:
    """Начисление БОНУСА: идёт в locked (нельзя вывести; тратится в магазине
    или отыгрывается в казино). Используется рейкбеком из казино. Идемпотентно по op_id."""
    op_id = str(op_id or "").strip()
    if not op_id or len(op_id) > 64:
        raise ValueError("Неверный op_id")
    if amount <= 0:
        raise ValueError("Сумма бонуса должна быть > 0")
    async with _pool.acquire() as c, c.transaction():
        await c.execute("INSERT INTO users(tg_id) VALUES($1) ON CONFLICT (tg_id) DO NOTHING", tg_id)
        await c.execute("SELECT balance FROM users WHERE tg_id=$1 FOR UPDATE", tg_id)
        prev = await c.fetchrow("SELECT balance_after FROM wallet_ops WHERE op_id=$1", op_id)
        if prev:
            return {"balance": prev["balance_after"], "duplicate": True}
        r = await c.fetchrow(f"""
            UPDATE users SET balance=balance+$1, locked=locked+$1,
                             wager_req=(CASE WHEN locked<=0 THEN 0 ELSE wager_req END)+$1*{BONUS_WAGER_X} WHERE tg_id=$2
            RETURNING balance
        """, amount, tg_id)
        await c.execute("""
            INSERT INTO wallet_ops(op_id, user_id, delta, kind, ref, balance_after)
            VALUES($1,$2,$3,$4,$5,$6)
        """, op_id, tg_id, amount, str(kind)[:32], (str(ref)[:64] if ref else None), r["balance"])
        return {"balance": r["balance"], "duplicate": False}


# ── статистика реферальной программы для админки ────────────────────────────
async def admin_ref_stats() -> dict:
    """Топ рефереров: приглашённые, покупки приглашённых (всего и за 30 дней),
    заработано бонусов; плюс общие итоги программы."""
    async with _pool.acquire() as c:
        total_invited = int(await c.fetchval(
            "SELECT COUNT(*) FROM users WHERE ref_by IS NOT NULL") or 0)
        total_earned = int(await c.fetchval(
            "SELECT COALESCE(SUM(ref_earned), 0) FROM users") or 0)
        buys = await c.fetchrow("""
            SELECT COUNT(o.id) AS n, COALESCE(SUM(o.total), 0) AS total,
                   COUNT(o.id) FILTER (WHERE o.created >= now() - interval '30 days') AS n30,
                   COALESCE(SUM(o.total) FILTER (WHERE o.created >= now() - interval '30 days'), 0) AS total30
            FROM orders o
            JOIN users u ON u.tg_id = o.user_id AND u.ref_by IS NOT NULL
            WHERE o.status >= 0
        """)
        top = await c.fetch("""
            WITH inv AS (
                SELECT ref_by AS r, COUNT(*) AS invited
                FROM users WHERE ref_by IS NOT NULL GROUP BY ref_by),
            pur AS (
                SELECT u.ref_by AS r, COUNT(o.id) AS orders,
                       COALESCE(SUM(o.total), 0) AS total,
                       COUNT(o.id) FILTER (WHERE o.created >= now() - interval '30 days') AS orders30,
                       COALESCE(SUM(o.total) FILTER (WHERE o.created >= now() - interval '30 days'), 0) AS total30
                FROM orders o
                JOIN users u ON u.tg_id = o.user_id AND u.ref_by IS NOT NULL
                WHERE o.status >= 0
                GROUP BY u.ref_by)
            SELECT ref.tg_id, ref.name, ref.username, ref.ref_earned,
                   inv.invited,
                   COALESCE(pur.orders, 0) AS orders, COALESCE(pur.total, 0) AS total,
                   COALESCE(pur.orders30, 0) AS orders30, COALESCE(pur.total30, 0) AS total30
            FROM inv
            JOIN users ref ON ref.tg_id = inv.r
            LEFT JOIN pur ON pur.r = inv.r
            ORDER BY COALESCE(pur.total, 0) DESC, inv.invited DESC
            LIMIT 30
        """)
        # сколько приглашённых у реферера требуют внимания: то же подозрение, что
        # и в детализации (устройство/IP как у реферера или дубль устройства среди
        # приглашённых), но ещё НЕ забаненные — забанил, счётчик упал
        flg = await c.fetch("""
            WITH invd AS (
                SELECT u.tg_id, u.ref_by AS r, u.banned,
                       bc.device_hash AS device, bc.ip AS ip
                FROM users u
                LEFT JOIN bonus_claims bc ON bc.user_id = u.tg_id
                WHERE u.ref_by IS NOT NULL),
            refd AS (
                SELECT bc.user_id AS r, bc.device_hash AS rdevice, bc.ip AS rip
                FROM bonus_claims bc),
            dup AS (
                SELECT r, device FROM invd
                WHERE device IS NOT NULL GROUP BY r, device HAVING COUNT(*) > 1)
            SELECT i.r AS r, COUNT(*) FILTER (WHERE NOT i.banned AND (
                     (i.device IS NOT NULL AND i.device = rd.rdevice)
                  OR (i.ip     IS NOT NULL AND i.ip     = rd.rip)
                  OR (i.device IS NOT NULL AND EXISTS (
                        SELECT 1 FROM dup d WHERE d.r = i.r AND d.device = i.device))
                   )) AS flags
            FROM invd i
            LEFT JOIN refd rd ON rd.r = i.r
            GROUP BY i.r
        """)
    flag_by_ref = {int(f["r"]): int(f["flags"] or 0) for f in flg}
    return {
        "total_invited": total_invited, "total_earned": total_earned,
        "buys_n": int(buys["n"] or 0), "buys_total": int(buys["total"] or 0),
        "buys_n30": int(buys["n30"] or 0), "buys_total30": int(buys["total30"] or 0),
        "top": [{**dict(t), "pct": round(ref_percent(t["invited"]) * 100),
                 "flags": flag_by_ref.get(int(t["tg_id"]), 0)} for t in top],
    }


async def admin_ref_detail(referrer_id: int) -> dict:
    """Детализация одного реферера для админки: сам реферер + список всех его
    приглашённых с покупками каждого (кто когда пришёл, сколько заказов, на
    сколько ₴)."""
    async with _pool.acquire() as c:
        ref = await c.fetchrow("""
            SELECT u.tg_id, u.name, u.username, u.ref_earned,
                   u.platform, u.tg_premium, u.tg_photo, u.banned,
                   bc.device_hash AS device, bc.ip AS ip
            FROM users u LEFT JOIN bonus_claims bc ON bc.user_id = u.tg_id
            WHERE u.tg_id = $1
        """, referrer_id)
        rows = await c.fetch("""
            SELECT u.tg_id, u.name, u.username, u.created AS joined, u.bonus_claimed,
                   u.platform, u.tg_premium, u.tg_photo, u.banned,
                   COUNT(o.id) FILTER (WHERE o.status >= 0)                    AS orders,
                   COALESCE(SUM(o.total) FILTER (WHERE o.status >= 0), 0)      AS spent,
                   MAX(o.created) FILTER (WHERE o.status >= 0)                 AS last_order,
                   MAX(bc.device_hash)                                        AS device,
                   MAX(bc.ip)                                                 AS ip
            FROM users u
            LEFT JOIN orders o        ON o.user_id = u.tg_id
            LEFT JOIN bonus_claims bc ON bc.user_id = u.tg_id
            WHERE u.ref_by = $1
            GROUP BY u.tg_id, u.name, u.username, u.created, u.bonus_claimed,
                     u.platform, u.tg_premium, u.tg_photo, u.banned
            ORDER BY spent DESC, joined DESC
        """, referrer_id)

    ref_device = ref["device"] if ref else None
    ref_ip = ref["ip"] if ref else None
    # частота device/IP среди приглашённых — для ловли фермы с одного устройства
    dev_cnt, ip_cnt = {}, {}
    for r in rows:
        if r["device"]:
            dev_cnt[r["device"]] = dev_cnt.get(r["device"], 0) + 1
        if r["ip"]:
            ip_cnt[r["ip"]] = ip_cnt.get(r["ip"], 0) + 1

    invitees = []
    for r in rows:
        dev, ip = r["device"], r["ip"]
        reasons = []
        if dev and dev == ref_device:
            reasons.append("устройство как у реферера")
        if dev and dev_cnt.get(dev, 0) > 1:
            reasons.append("устройство дублируется у приглашённых")
        if ip and ip == ref_ip:
            reasons.append("IP как у реферера")
        invitees.append({
            "tg_id": r["tg_id"], "name": r["name"], "username": r["username"],
            "joined": r["joined"].isoformat() if r["joined"] else None,
            "orders": int(r["orders"] or 0), "spent": int(r["spent"] or 0),
            "last_order": r["last_order"].isoformat() if r["last_order"] else None,
            "bonus": bool(r["bonus_claimed"]),
            "device": dev, "ip": ip,                 # только для админа
            "platform": r["platform"], "premium": r["tg_premium"],
            "photo": r["tg_photo"], "banned": bool(r["banned"]),
            "flag": bool(reasons), "flag_reason": "; ".join(reasons),
        })
    return {
        "referrer": {
            "tg_id": referrer_id,
            "name": (ref["name"] if ref else None),
            "username": (ref["username"] if ref else None),
            "ref_earned": int(ref["ref_earned"]) if ref else 0,
            "device": ref_device, "ip": ref_ip,
            "platform": (ref["platform"] if ref else None),
            "premium": (ref["tg_premium"] if ref else None),
            "photo": (ref["tg_photo"] if ref else None),
            "banned": bool(ref["banned"]) if ref else False,
        },
        "invitees": invitees,
    }


async def my_referrals(tg_id: int) -> dict:
    """Подробности реф-программы для клиента: заработок с разбивкой (казино /
    покупки) + список своих приглашённых с суммой их покупок.
    Казино — начисления через кошелёк с kind/ref 'referral'; покупки — остаток
    от общего ref_earned (в него входит и казино-часть)."""
    async with _pool.acquire() as c:
        earned_total = int(await c.fetchval(
            "SELECT COALESCE(ref_earned, 0) FROM users WHERE tg_id=$1", tg_id) or 0)
        casino = int(await c.fetchval("""
            SELECT COALESCE(SUM(delta), 0) FROM wallet_ops
            WHERE user_id=$1 AND delta > 0 AND (kind='referral' OR ref='referral')
        """, tg_id) or 0)
        casino = min(casino, earned_total)
        shop = max(0, earned_total - casino)
        invited = int(await c.fetchval(
            "SELECT COUNT(*) FROM users WHERE ref_by=$1", tg_id) or 0)
        rows = await c.fetch("""
            SELECT u.tg_id, u.name, u.created AS joined, u.bonus_claimed,
                   COUNT(o.id) FILTER (WHERE o.status >= 0)               AS orders,
                   COALESCE(SUM(o.total) FILTER (WHERE o.status >= 0), 0) AS spent,
                   MAX(o.created) FILTER (WHERE o.status >= 0)            AS last_order,
                   MIN(o.created) FILTER (WHERE o.status >= 0)            AS first_order
            FROM users u
            LEFT JOIN orders o ON o.user_id = u.tg_id
            WHERE u.ref_by = $1
            GROUP BY u.tg_id, u.name, u.created, u.bonus_claimed
            ORDER BY spent DESC, joined DESC
        """, tg_id)
    active = sum(1 for r in rows if int(r["orders"] or 0) > 0)
    return {
        "invited": invited, "pct": round(ref_percent(invited) * 100),
        "earned_total": earned_total, "earned_casino": casino, "earned_shop": shop,
        "active": active,
        "referrals": [{
            "tg_id": r["tg_id"],
            "name": r["name"], "joined": r["joined"].isoformat() if r["joined"] else None,
            "orders": int(r["orders"] or 0), "spent": int(r["spent"] or 0),
            "avg": (int(r["spent"]) // int(r["orders"])) if int(r["orders"] or 0) else 0,
            "last_order": r["last_order"].isoformat() if r["last_order"] else None,
            "first_order": r["first_order"].isoformat() if r["first_order"] else None,
            "bonus": bool(r["bonus_claimed"]),
        } for r in rows],
    }


# ── перенос аккаунта ─────────────────────────────────────────────────────────
async def transfer_create(tg_id: int) -> str:
    code = secrets.token_hex(8).upper()  # 64 бита — не брутфорсится
    async with _pool.acquire() as c:
        await c.execute("DELETE FROM transfers WHERE user_id=$1", tg_id)
        await c.execute("INSERT INTO transfers(code, user_id, expires) VALUES($1,$2,$3)",
                        code, tg_id, time.time() + TRANSFER_TTL)
    return code


async def transfer_redeem(code: str, new_id: int) -> dict:
    async with _pool.acquire() as c, c.transaction():
        row = await c.fetchrow("SELECT * FROM transfers WHERE code=$1", code.strip().upper())
        if not row or row["expires"] < time.time():
            raise ValueError("Код не найден или истёк")
        old_id = row["user_id"]
        if old_id == new_id:
            raise ValueError("Это тот же аккаунт")
        old = await c.fetchrow("SELECT * FROM users WHERE tg_id=$1 FOR UPDATE", old_id)
        if not old:
            raise ValueError("Аккаунт для переноса не найден")
        # целевой аккаунт может ещё не существовать (перенос до первого входа)
        await c.execute("INSERT INTO users(tg_id) VALUES($1) ON CONFLICT (tg_id) DO NOTHING", new_id)
        # переносим И locked: иначе бонусные (невыводимые) деньги превратились бы
        # в выводимый баланс на новом аккаунте (отмывание бонусов)
        await c.execute("""
            UPDATE users SET balance=balance+$1, locked=locked+$2, ref_earned=ref_earned+$3,
                bonus_claimed = bonus_claimed OR $4
            WHERE tg_id=$5
        """, old["balance"], old["locked"] or 0, old["ref_earned"],
             old["bonus_claimed"], new_id)
        await c.execute("UPDATE orders SET user_id=$1 WHERE user_id=$2", new_id, old_id)
        await c.execute("UPDATE topups SET user_id=$1 WHERE user_id=$2", new_id, old_id)
        await c.execute("UPDATE invoices SET user_id=$1 WHERE user_id=$2", new_id, old_id)
        await c.execute("UPDATE ratings SET user_id=$1 WHERE user_id=$2", new_id, old_id)
        await c.execute("UPDATE grows SET user_id=$1 WHERE user_id=$2", new_id, old_id)
        await c.execute("UPDATE shares SET user_id=$1 WHERE user_id=$2", new_id, old_id)
        await c.execute("UPDATE withdrawals SET user_id=$1 WHERE user_id=$2", new_id, old_id)
        await c.execute("UPDATE promos SET used_by=$1 WHERE used_by=$2", new_id, old_id)
        # приглашённых старого аккаунта переводим на новый, но не создаём
        # самоссылку, если новый аккаунт сам был приглашён старым
        await c.execute("UPDATE users SET ref_by=$1 WHERE ref_by=$2 AND tg_id <> $1",
                        new_id, old_id)
        await c.execute("UPDATE users SET ref_by=NULL WHERE tg_id=$1 AND ref_by=$1", new_id)
        await c.execute("DELETE FROM users WHERE tg_id=$1", old_id)
        await c.execute("DELETE FROM transfers WHERE user_id=$1", old_id)
        return await snapshot(new_id, c)


# ── Розыгрыш (лотерея) ────────────────────────────────────────────────────────
async def lottery_ensure_round(days: int, prizes: str) -> dict:
    """Текущий открытый круг; если открытого нет — создаём со сроком now()+days."""
    async with _pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT * FROM lottery_rounds WHERE status='open' ORDER BY id DESC LIMIT 1")
        if not row:
            row = await c.fetchrow(
                "INSERT INTO lottery_rounds(status, deadline, prizes) "
                "VALUES('open', now() + make_interval(days => $1), $2) RETURNING *",
                int(days), str(prizes))
        return dict(row)


async def lottery_last_finished() -> dict | None:
    """Последний завершённый круг (для показа результатов/анимации)."""
    async with _pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT * FROM lottery_rounds WHERE status='finished' "
            "ORDER BY drawn_at DESC NULLS LAST, id DESC LIMIT 1")
        return dict(row) if row else None


async def lottery_new_candidates(referrer: int) -> list[int]:
    """Приглашённые данным человеком, ещё НЕ учтённые в лотерее (нет в lottery_refs).
    Их надо проверить на подписку в main и подтверждённых записать."""
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT u.tg_id FROM users u WHERE u.ref_by=$1 AND u.tg_id <> $1 "
            "AND NOT EXISTS(SELECT 1 FROM lottery_refs r WHERE r.referral=u.tg_id)",
            referrer)
        return [r["tg_id"] for r in rows]


async def lottery_confirm_refs(referrer: int, referral_ids: list[int], round_id: int) -> int:
    """Записать подтверждённых (подписавшихся) рефералов. Пара уникальна глобально —
    повтор игнорируется. Возвращает сколько реально добавлено."""
    if not referral_ids:
        return 0
    async with _pool.acquire() as c:
        n = 0
        for rid in referral_ids:
            if int(rid) == int(referrer):
                continue
            res = await c.execute(
                "INSERT INTO lottery_refs(referrer, referral, round_id) VALUES($1,$2,$3) "
                "ON CONFLICT(referrer, referral) DO NOTHING", referrer, int(rid), round_id)
            if res.endswith(" 1"):
                n += 1
        return n


async def lottery_grant_tickets(user_id: int, round_id: int, eligible: bool,
                                per_ticket: int) -> dict:
    """Выдать участнику билеты (числа) за неиспользованные подтверждённые рефералы.
    eligible — сам подписан на канал и чат. Каждые per_ticket непотраченных
    рефералов → одно число. Атомарно, под advisory-lock на (круг, пользователь)."""
    async with _pool.acquire() as c, c.transaction():
        await c.execute("SELECT pg_advisory_xact_lock(hashtext($1))",
                        f"lotgrant:{round_id}:{user_id}")
        added = []
        if eligible and per_ticket > 0:
            avail = await c.fetchval(
                "SELECT count(*) FROM lottery_refs WHERE referrer=$1 AND ticketed=false",
                user_id) or 0
            want = avail // per_ticket
            for _ in range(int(want)):
                num = None
                for _try in range(30):
                    cand = random.randint(10000, 99999)
                    if not await c.fetchval(
                            "SELECT 1 FROM lottery_tickets WHERE round_id=$1 AND number=$2",
                            round_id, cand):
                        num = cand
                        break
                if num is None:
                    break
                await c.execute(
                    "INSERT INTO lottery_tickets(round_id, number, user_id) VALUES($1,$2,$3)",
                    round_id, num, user_id)
                added.append(num)
                # пометить per_ticket рефералов потраченными (самые старые)
                await c.execute(
                    "UPDATE lottery_refs SET ticketed=true WHERE ctid IN ("
                    "  SELECT ctid FROM lottery_refs WHERE referrer=$1 AND ticketed=false "
                    "  ORDER BY counted LIMIT $2)", user_id, int(per_ticket))
        numbers = [r["number"] for r in await c.fetch(
            "SELECT number FROM lottery_tickets WHERE round_id=$1 AND user_id=$2 ORDER BY number",
            round_id, user_id)]
        pending = await c.fetchval(
            "SELECT count(*) FROM lottery_refs WHERE referrer=$1 AND ticketed=false",
            user_id) or 0
        confirmed = await c.fetchval(
            "SELECT count(*) FROM lottery_refs WHERE referrer=$1", user_id) or 0
        return {"added": added, "numbers": numbers,
                "pending": int(pending), "confirmed": int(confirmed)}


async def lottery_stats(round_id: int) -> dict:
    async with _pool.acquire() as c:
        parts = await c.fetchval(
            "SELECT count(DISTINCT user_id) FROM lottery_tickets WHERE round_id=$1", round_id)
        tix = await c.fetchval(
            "SELECT count(*) FROM lottery_tickets WHERE round_id=$1", round_id)
        return {"participants": int(parts or 0), "tickets": int(tix or 0)}


async def lottery_my_referrals(referrer: int) -> list[dict]:
    """Все приглашённые + флаги: counted (подтверждён в лотерее) и ticketed
    (уже вошёл в число). Для не-counted статус подписки добавит main."""
    async with _pool.acquire() as c:
        rows = await c.fetch("""
            SELECT u.tg_id, u.name, u.username, u.created,
                   (r.referrer IS NOT NULL)          AS counted,
                   COALESCE(r.ticketed, false)       AS ticketed
            FROM users u
            LEFT JOIN lottery_refs r ON r.referral = u.tg_id
            WHERE u.ref_by=$1 AND u.tg_id <> $1
            ORDER BY u.created DESC""", referrer)
        return [dict(x) for x in rows]


async def lottery_winners(round_id: int) -> list[dict]:
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT place, user_id, number, prize, name FROM lottery_winners "
            "WHERE round_id=$1 ORDER BY place", round_id)
        return [dict(r) for r in rows]


async def lottery_draw(round_id: int, prizes: list[int]) -> list[dict] | None:
    """Провести розыгрыш под advisory-lock: если круг ещё open — тянем случайные
    выигрышные числа, фиксируем победителей (paid=false), закрываем круг.
    Начисление призов делает main (идемпотентно). None — если уже проведён."""
    async with _pool.acquire() as c, c.transaction():
        await c.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"lotdraw:{round_id}")
        rnd = await c.fetchrow(
            "SELECT status FROM lottery_rounds WHERE id=$1 FOR UPDATE", round_id)
        if not rnd or rnd["status"] != "open":
            return None
        pool = [dict(r) for r in await c.fetch(
            "SELECT number, user_id FROM lottery_tickets WHERE round_id=$1", round_id)]
        random.shuffle(pool)
        winners = []
        for i in range(min(len(prizes), len(pool))):
            t = pool[i]
            name = await c.fetchval("SELECT name FROM users WHERE tg_id=$1", t["user_id"])
            w = {"place": i + 1, "user_id": t["user_id"], "number": t["number"],
                 "prize": int(prizes[i]), "name": name}
            winners.append(w)
            await c.execute(
                "INSERT INTO lottery_winners(round_id, place, user_id, number, prize, name) "
                "VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING",
                round_id, w["place"], w["user_id"], w["number"], w["prize"], name)
        await c.execute(
            "UPDATE lottery_rounds SET status='finished', drawn_at=now() WHERE id=$1", round_id)
        return winners


async def lottery_unpaid_winners() -> list[dict]:
    """Победители, которым приз ещё не начислен (для докатки после сбоя)."""
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT round_id, place, user_id, prize FROM lottery_winners WHERE paid=false")
        return [dict(r) for r in rows]


async def lottery_mark_paid(round_id: int, place: int) -> None:
    async with _pool.acquire() as c:
        await c.execute(
            "UPDATE lottery_winners SET paid=true WHERE round_id=$1 AND place=$2",
            round_id, place)
