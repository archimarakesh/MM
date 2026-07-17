"""Magic Market — хранилище PostgreSQL (asyncpg, Railway DATABASE_URL)."""
import base64
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO

import asyncpg
from PIL import Image

# уровни рефералки: (приглашено от, доля с покупок рефералов)
REF_TIERS = [(0, 0.05), (10, 0.10), (50, 0.15)]
TRANSFER_TTL = 15 * 60      # код переноса живёт 15 минут
ORDER_CODE_BASE = 1000      # MM-1001, MM-1002, ...
AUTO_DELIVER_DAYS = 5       # через сколько дней после отправки заказ считается полученным

DEFAULT_TIERS = [
    {"from": 1, "k": 1.00}, {"from": 10, "k": 0.90}, {"from": 25, "k": 0.80},
    {"from": 50, "k": 0.70}, {"from": 100, "k": 0.60}, {"from": 250, "k": 0.55},
    {"from": 500, "k": 0.50},
]
MAX_PHOTO_LEN = 400_000   # ~300 КБ картинки в base64
MAX_PHOTOS = 4
SEED_PRODUCTS = [
    ("Golden Reserve", "Флагманская позиция", "🏆", "ХИТ", 120),
    ("Black Label", "Тёмная классика", "🖤", "", 95),
    ("Royal Amber", "Янтарная серия", "💎", "NEW", 150),
    ("Velvet Night", "Мягкий профиль", "🌙", "", 80),
    ("Imperial Gold", "Лимитированный выпуск", "👑", "LIMIT", 210),
    ("Silk Road", "Восточная коллекция", "🐫", "", 105),
]
# ключи реквизитов оплаты в settings
PAYMENT_KEYS = ["card_number", "card_holder", "wallet_trc20", "wallet_btc"]
INVOICE_TTL = 30 * 60       # крипто-счёт живёт 30 минут

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
            ALTER TABLE products ADD COLUMN IF NOT EXISTS photos TEXT;
            ALTER TABLE products ADD COLUMN IF NOT EXISTS stock INT;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS product_id BIGINT;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipped_at TIMESTAMPTZ;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS pay TEXT;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS receipt TEXT;
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
        "tag": r["tag"], "base": r["base"],
        "tiers": json.loads(r["tiers"]) if r["tiers"] else DEFAULT_TIERS,
        "active": r["active"], "photos": photos,
        "pv": len(r["photos"] or ""),  # версия фото для кэш-бастинга
        "stock": r["stock"],           # None = не ограничено
        "rating": rating.get(r["id"], {"avg": 0, "count": 0}),
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
        "SELECT product_id, AVG(stars) AS avg, COUNT(*) AS cnt FROM ratings GROUP BY product_id")
    rating = {r["product_id"]: {"avg": round(float(r["avg"]), 1), "count": r["cnt"]} for r in rrows}
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
        if d.get("id"):
            await c.execute("""
                UPDATE products SET name=$2, sub=$3, emoji=$4, tag=$5, base=$6, tiers=$7,
                                    active=$8, photos=$9, stock=$10
                WHERE id=$1
            """, int(d["id"]), d["name"], d.get("sub", ""), d.get("emoji", "📦"),
                d.get("tag", ""), int(d["base"]), tiers, bool(d.get("active", True)), pj, stock)
            return int(d["id"])
        return await c.fetchval("""
            INSERT INTO products(name, sub, emoji, tag, base, tiers, photos, stock, pos)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,
                   COALESCE((SELECT MAX(pos)+1 FROM products), 0))
            RETURNING id
        """, d["name"], d.get("sub", ""), d.get("emoji", "📦"),
            d.get("tag", ""), int(d["base"]), tiers, pj, stock)


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
    return {
        "balance": u["balance"], "ref_count": cnt, "ref_earned": u["ref_earned"],
        "ref_percent": round(ref_percent(cnt) * 100),
        "orders": orders,
        "products": await get_products(conn=c),
        "payment": payment_public(await get_settings(conn=c)),
        "payments": await payments_history(tg_id, c),
    }


async def payments_history(tg_id: int, c) -> list:
    """История пополнений: карта (ручная проверка) + крипто-счета."""
    await c.execute("UPDATE invoices SET status=2 WHERE status=0 AND expires < now()")
    tt = await c.fetch("""
        SELECT amount, method, status, created FROM topups
        WHERE user_id=$1 ORDER BY id DESC LIMIT 30
    """, tg_id)
    inv = await c.fetch("""
        SELECT amount_uah AS amount, currency AS method, status, created FROM invoices
        WHERE user_id=$1 ORDER BY id DESC LIMIT 30
    """, tg_id)
    rows = [{**dict(r), "kind": "card"} for r in tt] + [{**dict(r), "kind": "crypto"} for r in inv]
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


async def _ref_bonus(c, buyer_id: int, total: int):
    """Процент рефереру по его уровню — начисляется только после фактической оплаты."""
    ref_by = await c.fetchval("SELECT ref_by FROM users WHERE tg_id=$1", buyer_id)
    if ref_by:
        invited = await c.fetchval("SELECT COUNT(*) FROM users WHERE ref_by=$1", ref_by)
        bonus = round(total * ref_percent(invited))
        await c.execute("""
            UPDATE users SET balance=balance+$1, ref_earned=ref_earned+$1 WHERE tg_id=$2
        """, bonus, ref_by)


async def _order_product_total(c, product_id: int, grams: int, lock: bool = False):
    q = "SELECT * FROM products WHERE id=$1 AND active" + (" FOR UPDATE" if lock else "")
    p = await c.fetchrow(q, product_id)
    if not p:
        raise ValueError("Товар не найден")
    if not 1 <= grams <= 1000:
        raise ValueError("Вес — от 1 до 1000 грамм")
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


async def _insert_order(c, tg_id, p, grams, total, status, pay, ship, receipt=None) -> int:
    return await c.fetchval("""
        INSERT INTO orders(user_id, product_id, product, grams, total, status, pay, receipt, ship, date)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id
    """, tg_id, p["id"], p["name"], grams, total, status, pay, receipt,
        json.dumps(ship, ensure_ascii=False), datetime.now().strftime("%d.%m.%Y"))


async def create_order(tg_id: int, product_id: int, grams: int, pay: str,
                       ship: dict, receipt: str | None = None) -> dict:
    """Оплата с баланса (сразу оплачен) или картой (квитанция на проверку)."""
    async with _pool.acquire() as c, c.transaction():
        p, total = await _order_product_total(c, product_id, grams, lock=True)
        u = await c.fetchrow("SELECT * FROM users WHERE tg_id=$1 FOR UPDATE", tg_id)
        if pay == "balance":
            if u["balance"] < total:
                raise ValueError("Недостаточно средств — пополните баланс")
            await c.execute("UPDATE users SET balance=balance-$1 WHERE tg_id=$2", total, tg_id)
            oid = await _insert_order(c, tg_id, p, grams, total, 0, "balance", ship)
            await _ref_bonus(c, tg_id, total)
        elif pay == "card":
            oid = await _insert_order(c, tg_id, p, grams, total, -1, "card", ship, receipt)
        else:
            raise ValueError("Неизвестный способ оплаты")
        await _take_stock(c, product_id, grams)
        snap = await snapshot(tg_id, c)
        snap["order_code"] = f"MM-{oid + ORDER_CODE_BASE}"
        snap["order_total"] = total
        return snap


async def create_order_invoice(tg_id: int, product_id: int, grams: int, currency: str,
                               ship: dict, amount_crypto: str, address: str) -> tuple:
    """Заказ с оплатой криптой: заказ «ждёт оплаты» + привязанный счёт."""
    async with _pool.acquire() as c, c.transaction():
        p, total = await _order_product_total(c, product_id, grams, lock=True)
        oid = await _insert_order(c, tg_id, p, grams, total, -1, currency, ship)
        await _take_stock(c, product_id, grams)
        # новый счёт отменяет прежний неоплаченный (и его заказ, если был) с возвратом остатка
        prev = await c.fetch("""
            SELECT o.id, o.product_id, o.grams FROM orders o
            JOIN invoices i ON i.order_id = o.id
            WHERE i.user_id=$1 AND i.status=0 AND o.status=-1 AND o.id<>$2
        """, tg_id, oid)
        for r in prev:
            await _restock(c, r["product_id"], r["grams"])
        await c.execute("""
            UPDATE orders SET status=-2 WHERE status=-1 AND id<>$2 AND id IN
                (SELECT order_id FROM invoices WHERE user_id=$1 AND status=0 AND order_id IS NOT NULL)
        """, tg_id, oid)
        await c.execute("UPDATE invoices SET status=2 WHERE user_id=$1 AND status=0", tg_id)
        inv = await c.fetchrow("""
            INSERT INTO invoices(user_id, amount_uah, currency, amount_crypto, address, order_id, expires)
            VALUES($1,$2,$3,$4,$5,$6, now() + make_interval(secs => $7))
            RETURNING *
        """, tg_id, total, currency, amount_crypto, address, oid, INVOICE_TTL)
        snap = await snapshot(tg_id, c)
        code = f"MM-{oid + ORDER_CODE_BASE}"
        snap["order_code"] = code
        snap["order_total"] = total
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
        "pay": r["pay"],
        "receipt": r["receipt"] if r["status"] == -1 else None,
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
            UPDATE orders SET ttn=$2, status=GREATEST(status, 2), shipped_at=COALESCE(shipped_at, now())
            WHERE id=$1
        """, oid, ttn.strip())
        return {"user_id": o["user_id"], "code": order_code, "ttn": ttn.strip()}


async def order_to_work(order_code: str) -> dict:
    try:
        oid = int(order_code.split("-")[1]) - ORDER_CODE_BASE
    except (IndexError, ValueError):
        raise ValueError("Неверный номер заказа")
    async with _pool.acquire() as c:
        o = await c.fetchrow("SELECT user_id FROM orders WHERE id=$1 AND status=0", oid)
        if not o:
            raise ValueError("Заказ не найден или уже в работе")
        await c.execute("UPDATE orders SET status=1 WHERE id=$1", oid)
        return {"user_id": o["user_id"], "code": order_code}


async def shipped_orders() -> list:
    """Заказы «В пути» с ТТН — для трекера Новой Почты."""
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id, user_id, ttn FROM orders WHERE status=2 AND ttn IS NOT NULL LIMIT 100")
    return [{"id": r["id"], "user_id": r["user_id"], "ttn": r["ttn"],
             "code": f"MM-{r['id'] + ORDER_CODE_BASE}"} for r in rows]


async def mark_delivered(oid: int) -> bool:
    async with _pool.acquire() as c:
        tag = await c.execute("UPDATE orders SET status=3 WHERE id=$1 AND status=2", oid)
    return tag == "UPDATE 1"


# ── крипто-счета (автопроверка оплаты) ───────────────────────────────────────
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
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT amount_crypto FROM invoices WHERE currency=$1 AND status=0", currency)
    return {r["amount_crypto"] for r in rows}


async def create_invoice(tg_id: int, amount_uah: int, currency: str,
                         amount_crypto: str, address: str) -> dict:
    async with _pool.acquire() as c:
        # новый счёт отменяет прежний неоплаченный
        await c.execute("UPDATE invoices SET status=2 WHERE user_id=$1 AND status=0", tg_id)
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
        await c.execute("DELETE FROM transfers WHERE user_id=$1", tg_id)
        await c.execute("UPDATE users SET ref_by=NULL WHERE ref_by=$1", tg_id)
        await c.execute("DELETE FROM users WHERE tg_id=$1", tg_id)


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
        await c.execute("UPDATE invoices SET user_id=$1 WHERE user_id=$2", new_id, old_id)
        await c.execute("UPDATE ratings SET user_id=$1 WHERE user_id=$2", new_id, old_id)
        await c.execute("UPDATE users SET ref_by=$1 WHERE ref_by=$2", new_id, old_id)
        await c.execute("DELETE FROM users WHERE tg_id=$1", old_id)
        await c.execute("DELETE FROM transfers WHERE user_id=$1", old_id)
        return await snapshot(new_id, c)
