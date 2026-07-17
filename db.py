"""Magic Market — хранилище PostgreSQL (asyncpg, Railway DATABASE_URL)."""
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import asyncpg

REF_PERCENT = 0.05          # доля реферера с покупки приглашённого
TRANSFER_TTL = 15 * 60      # код переноса живёт 15 минут
ORDER_CODE_BASE = 1000      # MM-1001, MM-1002, ...
AUTO_DELIVER_DAYS = 5       # через сколько дней после отправки заказ считается полученным

DEFAULT_TIERS = [
    {"from": 1, "k": 1.00}, {"from": 10, "k": 0.90}, {"from": 25, "k": 0.80},
    {"from": 50, "k": 0.70}, {"from": 100, "k": 0.60},
]
SEED_PRODUCTS = [
    ("Golden Reserve", "Флагманская позиция", "🏆", "ХИТ", 120),
    ("Black Label", "Тёмная классика", "🖤", "", 95),
    ("Royal Amber", "Янтарная серия", "💎", "NEW", 150),
    ("Velvet Night", "Мягкий профиль", "🌙", "", 80),
    ("Imperial Gold", "Лимитированный выпуск", "👑", "LIMIT", 210),
    ("Silk Road", "Восточная коллекция", "🐫", "", 105),
]
# ключи реквизитов оплаты в settings
PAYMENT_KEYS = ["card_number", "card_holder", "wallet_trc20", "wallet_ton", "wallet_btc"]

_pool: asyncpg.Pool | None = None


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/mm")
    return url.replace("postgres://", "postgresql://", 1)


async def init():
    global _pool
    _pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=5)
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
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS product_id BIGINT;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipped_at TIMESTAMPTZ;
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
            CREATE TABLE IF NOT EXISTS ratings(
                order_id   BIGINT PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                product_id BIGINT NOT NULL,
                stars      INT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(
                key   TEXT PRIMARY KEY,
                value TEXT);
            CREATE TABLE IF NOT EXISTS transfers(
                code    TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                expires DOUBLE PRECISION NOT NULL);
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
        UPDATE orders SET status=3
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
def _product_row(r, rating) -> dict:
    return {
        "id": r["id"], "name": r["name"], "sub": r["sub"], "emoji": r["emoji"],
        "tag": r["tag"], "base": r["base"],
        "tiers": json.loads(r["tiers"]) if r["tiers"] else DEFAULT_TIERS,
        "active": r["active"],
        "rating": rating.get(r["id"], {"avg": 0, "count": 0}),
    }


async def get_products(include_inactive: bool = False, conn=None) -> list:
    c = conn or _pool
    q = "SELECT * FROM products" + ("" if include_inactive else " WHERE active") + " ORDER BY pos, id"
    rows = await c.fetch(q)
    rrows = await c.fetch(
        "SELECT product_id, AVG(stars) AS avg, COUNT(*) AS cnt FROM ratings GROUP BY product_id")
    rating = {r["product_id"]: {"avg": round(float(r["avg"]), 1), "count": r["cnt"]} for r in rrows}
    return [_product_row(r, rating) for r in rows]


async def save_product(d: dict) -> int:
    tiers = json.dumps(d.get("tiers") or DEFAULT_TIERS)
    async with _pool.acquire() as c:
        if d.get("id"):
            await c.execute("""
                UPDATE products SET name=$2, sub=$3, emoji=$4, tag=$5, base=$6, tiers=$7, active=$8
                WHERE id=$1
            """, int(d["id"]), d["name"], d.get("sub", ""), d.get("emoji", "📦"),
                d.get("tag", ""), int(d["base"]), tiers, bool(d.get("active", True)))
            return int(d["id"])
        return await c.fetchval("""
            INSERT INTO products(name, sub, emoji, tag, base, tiers, pos)
            VALUES($1,$2,$3,$4,$5,$6,
                   COALESCE((SELECT MAX(pos)+1 FROM products), 0))
            RETURNING id
        """, d["name"], d.get("sub", ""), d.get("emoji", "📦"),
            d.get("tag", ""), int(d["base"]), tiers)


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
    stars = {r["order_id"]: r["stars"] for r in await c.fetch(
        "SELECT order_id, stars FROM ratings WHERE user_id=$1", tg_id)}
    orders = [{
        "id": f"MM-{r['id'] + ORDER_CODE_BASE}",
        "product": r["product"], "grams": r["grams"], "total": r["total"],
        "status": r["status"], "ttn": r["ttn"], "date": r["date"],
        "ship": json.loads(r["ship"]) if r["ship"] else None,
        "stars": stars.get(r["id"]),
    } for r in rows]
    pending = await c.fetch("""
        SELECT amount, method, created FROM topups
        WHERE user_id=$1 AND status=0 ORDER BY id DESC
    """, tg_id)
    return {
        "balance": u["balance"], "ref_count": cnt, "ref_earned": u["ref_earned"],
        "orders": orders,
        "products": await get_products(conn=c),
        "payment": payment_public(await get_settings(conn=c)),
        "topups_pending": [{
            "amount": p["amount"], "method": p["method"],
            "created": p["created"].isoformat(),
        } for p in pending],
    }


# ── заказы ───────────────────────────────────────────────────────────────────
async def create_order(tg_id: int, product_id: int, grams: int, pay: str, ship: dict) -> dict:
    async with _pool.acquire() as c, c.transaction():
        p = await c.fetchrow("SELECT * FROM products WHERE id=$1 AND active", product_id)
        if not p:
            raise ValueError("Товар не найден")
        product = _product_row(p, {})
        total = price_for(product, grams)
        u = await c.fetchrow("SELECT * FROM users WHERE tg_id=$1 FOR UPDATE", tg_id)
        if pay == "balance":
            if u["balance"] < total:
                raise ValueError("Недостаточно средств — пополните баланс")
            await c.execute("UPDATE users SET balance=balance-$1 WHERE tg_id=$2", total, tg_id)
        oid = await c.fetchval("""
            INSERT INTO orders(user_id, product_id, product, grams, total, status, ship, date)
            VALUES($1,$2,$3,$4,$5,0,$6,$7) RETURNING id
        """, tg_id, product_id, p["name"], grams, total,
            json.dumps(ship, ensure_ascii=False),
            datetime.now().strftime("%d.%m.%Y"))
        if u["ref_by"]:
            bonus = round(total * REF_PERCENT)
            await c.execute("""
                UPDATE users SET balance=balance+$1, ref_earned=ref_earned+$1 WHERE tg_id=$2
            """, bonus, u["ref_by"])
        snap = await snapshot(tg_id, c)
        snap["order_code"] = f"MM-{oid + ORDER_CODE_BASE}"
        snap["order_total"] = total
        return snap


async def rate_order(tg_id: int, order_code: str, stars: int) -> dict:
    try:
        oid = int(order_code.split("-")[1]) - ORDER_CODE_BASE
    except (IndexError, ValueError):
        raise ValueError("Неверный номер заказа")
    if not 1 <= stars <= 5:
        raise ValueError("Оценка — от 1 до 5")
    async with _pool.acquire() as c, c.transaction():
        await _auto_deliver(c)
        o = await c.fetchrow("SELECT * FROM orders WHERE id=$1 AND user_id=$2", oid, tg_id)
        if not o:
            raise ValueError("Заказ не найден")
        if o["status"] != 3:
            raise ValueError("Оценить можно после получения заказа")
        if not o["product_id"]:
            raise ValueError("Этот заказ нельзя оценить")
        await c.execute("""
            INSERT INTO ratings(order_id, user_id, product_id, stars) VALUES($1,$2,$3,$4)
            ON CONFLICT (order_id) DO UPDATE SET stars=$4
        """, oid, tg_id, o["product_id"], stars)
        return await snapshot(tg_id, c)


# ── пополнения (ручная проверка) ─────────────────────────────────────────────
async def topup_receipt(tg_id: int, amount: int, method: str, receipt: str) -> int:
    async with _pool.acquire() as c:
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
async def admin_orders() -> list:
    async with _pool.acquire() as c:
        await _auto_deliver(c)
        rows = await c.fetch("""
            SELECT o.*, u.name, u.username FROM orders o
            LEFT JOIN users u ON u.tg_id = o.user_id
            ORDER BY o.id DESC LIMIT 200
        """)
    return [{
        "id": f"MM-{r['id'] + ORDER_CODE_BASE}",
        "user": r["name"] or "?", "username": r["username"], "user_id": r["user_id"],
        "product": r["product"], "grams": r["grams"], "total": r["total"],
        "status": r["status"], "ttn": r["ttn"], "date": r["date"],
        "ship": json.loads(r["ship"]) if r["ship"] else None,
    } for r in rows]


async def set_ttn(order_code: str, ttn: str) -> dict:
    """Возвращает {user_id, code, ttn} для уведомления покупателя."""
    try:
        oid = int(order_code.split("-")[1]) - ORDER_CODE_BASE
    except (IndexError, ValueError):
        raise ValueError("Неверный номер заказа")
    async with _pool.acquire() as c:
        o = await c.fetchrow("SELECT * FROM orders WHERE id=$1", oid)
        if not o:
            raise ValueError("Заказ не найден")
        await c.execute("""
            UPDATE orders SET ttn=$2, status=GREATEST(status, 1), shipped_at=COALESCE(shipped_at, now())
            WHERE id=$1
        """, oid, ttn.strip())
        return {"user_id": o["user_id"], "code": order_code, "ttn": ttn.strip()}


# ── перенос аккаунта ─────────────────────────────────────────────────────────
async def transfer_create(tg_id: int) -> str:
    code = secrets.token_hex(3).upper()
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
        await c.execute("""
            UPDATE users SET balance=balance+$1, ref_earned=ref_earned+$2 WHERE tg_id=$3
        """, old["balance"], old["ref_earned"], new_id)
        await c.execute("UPDATE orders SET user_id=$1 WHERE user_id=$2", new_id, old_id)
        await c.execute("UPDATE topups SET user_id=$1 WHERE user_id=$2", new_id, old_id)
        await c.execute("UPDATE ratings SET user_id=$1 WHERE user_id=$2", new_id, old_id)
        await c.execute("UPDATE users SET ref_by=$1 WHERE ref_by=$2", new_id, old_id)
        await c.execute("DELETE FROM users WHERE tg_id=$1", old_id)
        await c.execute("DELETE FROM transfers WHERE user_id=$1", old_id)
        return await snapshot(new_id, c)
