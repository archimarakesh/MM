"""Magic Market — хранилище PostgreSQL (asyncpg, Railway DATABASE_URL)."""
import json
import os
import secrets
import time
from datetime import datetime

import asyncpg

REF_PERCENT = 0.05          # доля реферера с покупки приглашённого
TRANSFER_TTL = 15 * 60      # код переноса живёт 15 минут
ORDER_CODE_BASE = 1000      # MM-1001, MM-1002, ...

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
            CREATE INDEX IF NOT EXISTS orders_user_idx ON orders(user_id);
            CREATE TABLE IF NOT EXISTS transfers(
                code    TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                expires DOUBLE PRECISION NOT NULL);
        """)


async def upsert_user(tg_id: int, name: str, username: str | None, ref_by: int | None = None):
    if ref_by == tg_id:
        ref_by = None
    async with _pool.acquire() as c:
        await c.execute("""
            INSERT INTO users(tg_id, name, username, ref_by) VALUES($1,$2,$3,$4)
            ON CONFLICT (tg_id) DO UPDATE SET name=$2, username=$3
        """, tg_id, name, username, ref_by)


async def snapshot(tg_id: int, conn: asyncpg.Connection | None = None) -> dict:
    c = conn or _pool
    u = await c.fetchrow("SELECT * FROM users WHERE tg_id=$1", tg_id)
    cnt = await c.fetchval("SELECT COUNT(*) FROM users WHERE ref_by=$1", tg_id)
    rows = await c.fetch("SELECT * FROM orders WHERE user_id=$1 ORDER BY id DESC", tg_id)
    orders = [{
        "id": f"MM-{r['id'] + ORDER_CODE_BASE}",
        "product": r["product"], "grams": r["grams"], "total": r["total"],
        "status": r["status"], "ttn": r["ttn"], "date": r["date"],
        "ship": json.loads(r["ship"]) if r["ship"] else None,
    } for r in rows]
    return {"balance": u["balance"], "ref_count": cnt,
            "ref_earned": u["ref_earned"], "orders": orders}


async def create_order(tg_id: int, product: str, grams: int, total: int,
                       pay: str, ship: dict) -> dict:
    async with _pool.acquire() as c, c.transaction():
        u = await c.fetchrow("SELECT * FROM users WHERE tg_id=$1 FOR UPDATE", tg_id)
        if pay == "balance":
            if u["balance"] < total:
                raise ValueError("Недостаточно средств — пополните баланс")
            await c.execute("UPDATE users SET balance=balance-$1 WHERE tg_id=$2", total, tg_id)
        await c.execute("""
            INSERT INTO orders(user_id, product, grams, total, status, ship, date)
            VALUES($1,$2,$3,$4,0,$5,$6)
        """, tg_id, product, grams, total,
            json.dumps(ship, ensure_ascii=False),
            datetime.now().strftime("%d.%m.%Y"))
        if u["ref_by"]:
            bonus = round(total * REF_PERCENT)
            await c.execute("""
                UPDATE users SET balance=balance+$1, ref_earned=ref_earned+$1 WHERE tg_id=$2
            """, bonus, u["ref_by"])
        return await snapshot(tg_id, c)


async def topup(tg_id: int, amount: int) -> dict:
    # демо-зачисление; здесь появится реальный платёжный провайдер
    async with _pool.acquire() as c:
        await c.execute("UPDATE users SET balance=balance+$1 WHERE tg_id=$2", amount, tg_id)
        return await snapshot(tg_id, c)


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
        await c.execute("UPDATE users SET ref_by=$1 WHERE ref_by=$2", new_id, old_id)
        await c.execute("DELETE FROM users WHERE tg_id=$1", old_id)
        await c.execute("DELETE FROM transfers WHERE user_id=$1", old_id)
        return await snapshot(new_id, c)
