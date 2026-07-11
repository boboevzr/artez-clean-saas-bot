import os
import asyncpg
import logging
from datetime import datetime, timezone

DB_URL = os.getenv("DATABASE_URL", "")

pool = None

async def init_db():
    global pool
    if not DB_URL:
        logging.warning("DATABASE_URL not set, DB disabled")
        return
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=5)
    await create_tables()
    logging.info("✅ Database connected")

async def create_tables():
    async with pool.acquire() as conn:
        await conn.execute("""
        -- ══════════════════════════════════════
        --  КЛИЕНТЫ
        -- ══════════════════════════════════════
        CREATE TABLE IF NOT EXISTS clients (
            id              SERIAL PRIMARY KEY,
            tg_id           BIGINT UNIQUE NOT NULL,   -- Telegram user ID
            tg_username     VARCHAR(100),              -- @username если есть
            first_name      VARCHAR(100),
            last_name       VARCHAR(100),
            phone           VARCHAR(20),
            lang            VARCHAR(5) DEFAULT 'ru',
            total_orders    INT DEFAULT 0,
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW()
        );

        -- ══════════════════════════════════════
        --  СОТРУДНИКИ
        -- ══════════════════════════════════════
        CREATE TABLE IF NOT EXISTS staff (
            id              SERIAL PRIMARY KEY,
            tg_id           BIGINT UNIQUE NOT NULL,
            tg_username     VARCHAR(100),
            first_name      VARCHAR(100),
            last_name       VARCHAR(100),
            role            VARCHAR(20) NOT NULL
                            CHECK (role IN ('admin','manager','washer','packer','driver')),
            branch          VARCHAR(20) CHECK (branch IN ('zarafshan','navoi')),
            is_active       BOOLEAN DEFAULT TRUE,
            created_at      TIMESTAMP DEFAULT NOW()
        );

        -- ══════════════════════════════════════
        --  ЗАКАЗЫ
        -- ══════════════════════════════════════
        CREATE TABLE IF NOT EXISTS orders (
            id                  SERIAL PRIMARY KEY,
            order_num           VARCHAR(20) UNIQUE NOT NULL,  -- ARTEZ-1001

            -- Клиент
            client_tg_id        BIGINT NOT NULL,
            client_tg_username  VARCHAR(100),
            client_first_name   VARCHAR(100),
            client_last_name    VARCHAR(100),
            client_phone        VARCHAR(20),

            -- Заявка
            source              VARCHAR(20) DEFAULT 'bot'
                                CHECK (source IN ('bot','site','phone','walkin')),
            branch              VARCHAR(30),
            city                VARCHAR(100),
            address             TEXT,
            location            VARCHAR(100),
            service             VARCHAR(200),
            pickup_date         VARCHAR(50),
            pickup_time         VARCHAR(100),
            note                TEXT,

            -- Статус
            status              VARCHAR(30) DEFAULT 'new'
                                CHECK (status IN (
                                    'new',        -- Новый
                                    'confirmed',  -- Подтверждён
                                    'pickup',     -- Вывоз
                                    'received',   -- В мастерской
                                    'washing',    -- Мойка
                                    'drying',     -- Сушка
                                    'packing',    -- Упаковка
                                    'ready',      -- Готов
                                    'delivery',   -- Доставка
                                    'delivered',  -- Доставлен
                                    'cancelled'   -- Отменён
                                )),

            -- Оператор (кто принял заявку)
            operator_tg_id      BIGINT,
            operator_username   VARCHAR(100),
            operator_first_name VARCHAR(100),
            operator_last_name  VARCHAR(100),
            accepted_at         TIMESTAMP,

            -- Мойщик / исполнитель
            washer_tg_id        BIGINT,
            washer_username     VARCHAR(100),
            washer_first_name   VARCHAR(100),
            washer_last_name    VARCHAR(100),
            washing_started_at  TIMESTAMP,
            washing_done_at     TIMESTAMP,

            -- Водитель вывоза
            driver_pickup_tg_id         BIGINT,
            driver_pickup_username      VARCHAR(100),
            driver_pickup_first_name    VARCHAR(100),
            driver_pickup_last_name     VARCHAR(100),
            pickup_at                   TIMESTAMP,

            -- Водитель доставки
            driver_delivery_tg_id       BIGINT,
            driver_delivery_username    VARCHAR(100),
            driver_delivery_first_name  VARCHAR(100),
            driver_delivery_last_name   VARCHAR(100),
            delivered_at                TIMESTAMP,

            -- Время
            created_at          TIMESTAMP DEFAULT NOW(),
            updated_at          TIMESTAMP DEFAULT NOW()
        );

        -- ══════════════════════════════════════
        --  ИСТОРИЯ СТАТУСОВ
        -- ══════════════════════════════════════
        CREATE TABLE IF NOT EXISTS order_status_history (
            id              SERIAL PRIMARY KEY,
            order_num       VARCHAR(20) NOT NULL,
            old_status      VARCHAR(30),
            new_status      VARCHAR(30) NOT NULL,
            changed_by_tg_id      BIGINT,
            changed_by_name       VARCHAR(200),
            note            TEXT,
            created_at      TIMESTAMP DEFAULT NOW()
        );

        -- ══════════════════════════════════════
        --  ЕДИНИЦЫ ИЗМЕРЕНИЯ
        -- ══════════════════════════════════════
        CREATE TABLE IF NOT EXISTS units (
            id          SERIAL PRIMARY KEY,
            key         VARCHAR(20) UNIQUE NOT NULL,  -- m2, m, pcs, cm, cm2, kg
            name_ru     VARCHAR(50) NOT NULL,          -- м², м, шт, см, см², кг
            name_uz     VARCHAR(50) NOT NULL,          -- m², m, dona, sm, sm², kg
            symbol_ru   VARCHAR(10) NOT NULL,          -- м²
            symbol_uz   VARCHAR(10) NOT NULL,          -- m²
            created_at  TIMESTAMP DEFAULT NOW()
        );

        -- ══════════════════════════════════════
        --  ЦЕНЫ НА УСЛУГИ
        -- ══════════════════════════════════════
        CREATE TABLE IF NOT EXISTS prices (
            id              SERIAL PRIMARY KEY,
            service_key     VARCHAR(30) NOT NULL,
            type_key        VARCHAR(20) NOT NULL,
            price           INT NOT NULL,
            unit            VARCHAR(20) DEFAULT 'sum/m2',
            unit_key        VARCHAR(20) DEFAULT 'm2',
            min_order       NUMERIC(10,2) DEFAULT NULL,
            updated_at      TIMESTAMP DEFAULT NOW(),
            UNIQUE(service_key, type_key)
        );

        -- Добавляем новые колонки если их нет (для существующих таблиц)
        ALTER TABLE prices ADD COLUMN IF NOT EXISTS unit_key VARCHAR(20) DEFAULT 'm2';
        ALTER TABLE prices ADD COLUMN IF NOT EXISTS min_order NUMERIC(10,2) DEFAULT NULL;
        ALTER TABLE order_payments ADD COLUMN IF NOT EXISTS driver_tg_id BIGINT;

        -- Снять старый CHECK на status (чтобы добавить drying)
        DO $$ DECLARE r RECORD;
        BEGIN
          FOR r IN SELECT conname FROM pg_constraint
                   WHERE conrelid='orders'::regclass AND contype='c' AND conname LIKE '%status%'
          LOOP EXECUTE format('ALTER TABLE orders DROP CONSTRAINT %I', r.conname);
          END LOOP;
        END $$;
        -- Добавить CHECK со всеми статусами включая drying
        ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_status_check;
        ALTER TABLE orders ADD CONSTRAINT orders_status_check CHECK (status IN (
          'new','confirmed','pickup','received','washing','drying','packing','ready','delivery','delivered','cancelled'
        ));

        -- Индексы
        CREATE INDEX IF NOT EXISTS idx_orders_client   ON orders(client_tg_id);
        CREATE INDEX IF NOT EXISTS idx_orders_status   ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_orders_branch   ON orders(branch);
        CREATE INDEX IF NOT EXISTS idx_orders_created  ON orders(created_at);
        CREATE INDEX IF NOT EXISTS idx_clients_tg_id   ON clients(tg_id);
        """)

        # Дефолтные единицы измерения
        units_count = await conn.fetchval("SELECT COUNT(*) FROM units")
        if units_count == 0:
            default_units = [
                ("m2",  "Квадратный метр", "kvadrat metr",  "м²",  "m²"),
                ("m",   "Метр",            "metr",          "м",   "m"),
                ("pcs", "Штука",           "dona",          "шт",  "dona"),
                ("cm",  "Сантиметр",       "santimetr",     "см",  "sm"),
                ("cm2", "Кв. сантиметр",   "kv. santimetr", "см²", "sm²"),
                ("kg",  "Килограмм",       "kilogramm",     "кг",  "kg"),
            ]
            await conn.executemany("""
                INSERT INTO units (key, name_ru, name_uz, symbol_ru, symbol_uz)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (key) DO NOTHING
            """, default_units)

        # Дефолтные цены — добавляются только если таблица prices ещё пуста
        count = await conn.fetchval("SELECT COUNT(*) FROM prices")
        if count == 0:
            defaults = [
                ("carpet",      "standard", 12000, "sum/m2", "m2",  10.0),
                ("carpet",      "express",  16000, "sum/m2", "m2",  10.0),
                ("carpet_home", "standard", 14000, "sum/m2", "m2",  10.0),
                ("carpet_home", "express",  18000, "sum/m2", "m2",  10.0),
                ("sofa",        "standard", 16000, "sum/m2", "m2",  None),
                ("sofa",        "express",  20000, "sum/m2", "m2",  None),
                ("mattress",    "standard", 16000, "sum/m2", "m2",  None),
                ("mattress",    "express",  20000, "sum/m2", "m2",  None),
                ("curtains",    "standard", 14000, "sum/m2", "m2",  None),
                ("curtains",    "express",  18000, "sum/m2", "m2",  None),
            ]
            await conn.executemany("""
                INSERT INTO prices (service_key, type_key, price, unit, unit_key, min_order)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (service_key, type_key) DO NOTHING
            """, defaults)

    # Миграции для существующих БД
    migrations = [
        "ALTER TABLE clients ADD COLUMN IF NOT EXISTS tg_phone VARCHAR(20) DEFAULT NULL",
        "ALTER TABLE clients ADD COLUMN IF NOT EXISTS language VARCHAR(5) DEFAULT 'ru'",
    ]
    async with pool.acquire() as conn:
        for sql in migrations:
            try:
                await conn.execute(sql)
            except Exception:
                pass

    logging.info("✅ Tables created/verified")


# ══════════════════════════════════════
#  КЛИЕНТЫ
# ══════════════════════════════════════
async def upsert_client(tg_id, username, first_name, last_name, phone=None, lang="ru"):
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO clients (tg_id, tg_username, first_name, last_name, phone, lang, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, NOW())
            ON CONFLICT (tg_id) DO UPDATE SET
                tg_username  = EXCLUDED.tg_username,
                first_name   = EXCLUDED.first_name,
                last_name    = EXCLUDED.last_name,
                lang         = EXCLUDED.lang,
                phone        = COALESCE(EXCLUDED.phone, clients.phone),
                updated_at   = NOW()
        """, tg_id, username, first_name, last_name, phone, lang)


# ══════════════════════════════════════
#  ЗАКАЗЫ
# ══════════════════════════════════════
async def get_next_order_num(prefix: str = "ARTEZ") -> str:
    """Возвращает следующий номер заказа на основе данных в БД (переживает редеплои)"""
    if not pool:
        return f"{prefix}-1001"
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT order_num FROM orders
            WHERE order_num LIKE $1
            ORDER BY id DESC
            LIMIT 1
        """, f"{prefix}-%")
        if row and row["order_num"]:
            try:
                last_num = int(row["order_num"].split("-")[-1])
            except (ValueError, IndexError):
                last_num = 1000
        else:
            last_num = 1000
        return f"{prefix}-{last_num + 1}"


async def save_order(data: dict) -> str:
    if not pool: return data.get("order_num","")
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO orders (
                order_num, source,
                client_tg_id, client_tg_username, client_first_name, client_last_name, client_phone,
                branch, city, address, location, service, pickup_date, pickup_time, note,
                status
            ) VALUES (
                $1, $2,
                $3, $4, $5, $6, $7,
                $8, $9, $10, $11, $12, $13, $14, $15,
                'new'
            )
            ON CONFLICT (order_num) DO NOTHING
        """,
            data.get("order_num"),
            data.get("source","bot"),
            data.get("client_tg_id"),
            data.get("client_tg_username"),
            data.get("client_first_name"),
            data.get("client_last_name"),
            data.get("phone"),
            data.get("branch"),
            data.get("city"),
            data.get("address"),
            data.get("location"),
            data.get("service"),
            data.get("pickup_date"),
            data.get("pickup_time"),
            data.get("note"),
        )
        # Увеличиваем счётчик заказов клиента
        await conn.execute("""
            UPDATE clients SET total_orders = total_orders + 1, updated_at = NOW()
            WHERE tg_id = $1
        """, data.get("client_tg_id"))
        # Пишем в историю
        await conn.execute("""
            INSERT INTO order_status_history (order_num, new_status, note)
            VALUES ($1, 'new', 'Заявка создана через бот')
        """, data.get("order_num"))
    return data.get("order_num")


async def update_order_status(order_num: str, new_status: str,
                               by_tg_id=None, by_name=None, note=None,
                               extra: dict = None):
    """Обновить статус заказа и записать в историю"""
    if not pool: return
    async with pool.acquire() as conn:
        # Берём старый статус
        old = await conn.fetchrow("SELECT status FROM orders WHERE order_num=$1", order_num)
        old_status = old["status"] if old else None

        # Базовое обновление
        set_parts = ["status=$1", "updated_at=NOW()"]
        vals = [new_status]

        # Дополнительные поля в зависимости от статуса
        if extra:
            i = 2
            for k, v in extra.items():
                set_parts.append(f"{k}=${i}")
                vals.append(v)
                i += 1

        vals.append(order_num)
        await conn.execute(
            f"UPDATE orders SET {', '.join(set_parts)} WHERE order_num=${len(vals)}",
            *vals
        )
        # История
        await conn.execute("""
            INSERT INTO order_status_history
            (order_num, old_status, new_status, changed_by_tg_id, changed_by_name, note)
            VALUES ($1,$2,$3,$4,$5,$6)
        """, order_num, old_status, new_status, by_tg_id, by_name, note)


async def get_order(order_num: str):
    if not pool: return None
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM orders WHERE order_num=$1", order_num)


async def get_order_by_id(order_id: int):
    if not pool: return None
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)


async def update_order_status_by_id(order_id: int, new_status: str, by_tg_id=None, by_name=None, note=None):
    if not pool: return
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status FROM orders WHERE id=$1", order_id)
        if not row: return
        old_status = row["status"]
        await conn.execute("UPDATE orders SET status=$1 WHERE id=$2", new_status, order_id)
        await conn.execute("""
            INSERT INTO order_activity (order_id, staff_name, action, details)
            VALUES ($1,$2,$3,$4)
        """, order_id, by_name or "Водитель (TG)", "status_change",
            f"{old_status} → {new_status}" + (f": {note}" if note else ""))


async def get_prices_for_services(service_keys: list, type_key: str) -> dict:
    """Возвращает {service_key: price_int} из таблицы prices."""
    if not pool: return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT service_key, price FROM prices WHERE service_key = ANY($1::varchar[]) AND type_key=$2",
            service_keys, type_key)
        return {r["service_key"]: float(r["price"]) for r in rows}


async def delete_order_items(order_id: int) -> int:
    if not pool: return 0
    async with pool.acquire() as conn:
        res = await conn.execute("DELETE FROM order_items WHERE order_id=$1", order_id)
        return int(res.split()[-1]) if res else 0


async def create_pickup_items(order_id: int, service_items: list, price_map: dict) -> int:
    """Создаёт позиции: service_items = [(service_key, qty, label)]."""
    if not pool or not service_items: return 0
    count = 0
    async with pool.acquire() as conn:
        for svc_key, qty, label in service_items:
            price = price_map.get(svc_key, 0)
            for _ in range(qty):
                await conn.execute(
                    "INSERT INTO order_items (order_id, service, sqm, price_per_sqm) VALUES ($1, $2, 0, $3)",
                    order_id, label, price)
                count += 1
    return count


async def get_order_activity_by_id(order_id: int) -> list:
    if not pool: return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action, details, staff_name, created_at FROM order_activity WHERE order_id=$1 ORDER BY created_at DESC",
            order_id)
        return [dict(r) for r in rows]


async def set_route_stop_status(order_id: int, status: str) -> bool:
    """Ставит stop_status (pending/done/skipped) для последней точки маршрута заказа."""
    if not pool: return False
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT route_id FROM route_orders WHERE order_id=$1 ORDER BY id DESC LIMIT 1", order_id)
        if not row: return False
        await conn.execute(
            "UPDATE route_orders SET stop_status=$1 WHERE route_id=$2 AND order_id=$3",
            status, row["route_id"], order_id)
        return True


async def add_order_activity(order_id: int, action: str, details: str, staff_name: str = "Водитель (TG)"):
    """Запись в историю заказа без смены статуса."""
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO order_activity (order_id, staff_name, action, details) VALUES ($1,$2,$3,$4)",
            order_id, staff_name, action, details)


async def get_route_delivery_info(order_id: int):
    """Возвращает (branch, msg_ids_dict) маршрута содержащего этот заказ."""
    if not pool: return None, {}
    import json as _j
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT r.branch, r.tg_delivery_msg_ids
            FROM route_orders ro
            JOIN routes r ON r.id = ro.route_id
            WHERE ro.order_id = $1
            ORDER BY ro.id DESC LIMIT 1
        """, order_id)
        if not row: return None, {}
        raw = row["tg_delivery_msg_ids"]
        try: msg_ids = _j.loads(raw) if isinstance(raw, str) else (raw or {})
        except: msg_ids = {}
        return row["branch"], msg_ids


async def get_orders_by_status(status: str, branch: str = None):
    if not pool: return []
    async with pool.acquire() as conn:
        if branch:
            return await conn.fetch(
                "SELECT * FROM orders WHERE status=$1 AND branch=$2 ORDER BY created_at DESC",
                status, branch
            )
        return await conn.fetch(
            "SELECT * FROM orders WHERE status=$1 ORDER BY created_at DESC", status
        )


async def get_client_by_tg_id(tg_id: int) -> dict | None:
    if not pool: return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM clients WHERE tg_id=$1", tg_id)
        return dict(row) if row else None

async def update_client_tg_phone(tg_id: int, tg_phone: str):
    """Сохраняет верифицированный Telegram-номер (из contact share) в clients.tg_phone."""
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE clients SET tg_phone=$2, updated_at=NOW() WHERE tg_id=$1",
            tg_id, tg_phone)

async def get_client_tg_phone(tg_id: int) -> str | None:
    """Возвращает сохранённый верифицированный номер клиента (tg_phone или phone)."""
    if not pool: return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tg_phone, phone FROM clients WHERE tg_id=$1", tg_id)
    if not row: return None
    return row["tg_phone"] or row["phone"] or None

async def get_last_lead_info(tg_id: int) -> dict | None:
    """Returns client_name and address from the most recent lead by tg_id."""
    if not pool: return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT client_name, address FROM leads
            WHERE client_tg_id=$1 AND address IS NOT NULL AND address != ''
            ORDER BY created_at DESC LIMIT 1
        """, tg_id)
    return dict(row) if row else None

async def get_client_orders(tg_id: int):
    if not pool: return []
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM orders WHERE client_tg_id=$1 ORDER BY created_at DESC LIMIT 10",
            tg_id
        )

async def get_stats(branch: str = None):
    """Статистика заказов"""
    if not pool: return {}
    async with pool.acquire() as conn:
        where = f"WHERE branch='{branch}'" if branch else ""
        row = await conn.fetchrow(f"""
            SELECT
                COUNT(*) FILTER (WHERE status='new')       AS new_count,
                COUNT(*) FILTER (WHERE status='delivered') AS done_count,
                COUNT(*) FILTER (WHERE status='cancelled') AS cancel_count,
                COUNT(*)                                    AS total
            FROM orders {where}
        """)
        return dict(row) if row else {}


# ══════════════════════════════════════
#  УСЛУГИ (названия RU/UZ)
# ══════════════════════════════════════
async def get_services() -> list:
    if not pool:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM services ORDER BY order_idx, key")
        return [dict(r) for r in rows]

# ══════════════════════════════════════
#  ЦЕНЫ НА УСЛУГИ
# ══════════════════════════════════════
async def get_all_prices() -> dict:
    """Возвращает все цены в виде {service_key: {type_key: {"price":.., "unit":.., "unit_key":.., "min_order":..}}}"""
    if not pool:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT service_key, type_key, price, unit, unit_key, min_order FROM prices")
    result = {}
    for r in rows:
        result.setdefault(r["service_key"], {})[r["type_key"]] = {
            "price": r["price"],
            "unit": r["unit"],
            "unit_key": r["unit_key"],
            "min_order": float(r["min_order"]) if r["min_order"] is not None else None,
        }
    return result


async def get_price(service_key: str, type_key: str):
    """Возвращает цену (int) для конкретной услуги и типа, либо None если не найдено"""
    if not pool:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT price FROM prices WHERE service_key=$1 AND type_key=$2",
            service_key, type_key
        )
    return row["price"] if row else None


async def set_price(service_key: str, type_key: str, price: int, unit: str = None,
                     unit_key: str = None, min_order=None) -> bool:
    """Устанавливает (или создаёт) цену для услуги/типа. Возвращает True при успехе."""
    if not pool:
        return False
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO prices (service_key, type_key, price, unit, unit_key, min_order, updated_at)
            VALUES ($1, $2, $3,
                    COALESCE($4, 'sum/m2'),
                    COALESCE($5, 'm2'),
                    $6, NOW())
            ON CONFLICT (service_key, type_key) DO UPDATE SET
                price      = EXCLUDED.price,
                unit       = COALESCE($4, prices.unit),
                unit_key   = COALESCE($5, prices.unit_key),
                min_order  = $6,
                updated_at = NOW()
        """, service_key, type_key, price, unit, unit_key, min_order)
    return True


# ══════════════════════════════════════
#  ЕДИНИЦЫ ИЗМЕРЕНИЯ
# ══════════════════════════════════════
async def get_all_units():
    """Возвращает список всех единиц измерения"""
    if not pool:
        return []
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM units ORDER BY id")


async def get_unit(key: str):
    if not pool:
        return None
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM units WHERE key=$1", key)


async def add_unit(key: str, name_ru: str, name_uz: str, symbol_ru: str, symbol_uz: str) -> bool:
    if not pool:
        return False
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO units (key, name_ru, name_uz, symbol_ru, symbol_uz)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (key) DO UPDATE SET
                name_ru = EXCLUDED.name_ru,
                name_uz = EXCLUDED.name_uz,
                symbol_ru = EXCLUDED.symbol_ru,
                symbol_uz = EXCLUDED.symbol_uz
        """, key, name_ru, name_uz, symbol_ru, symbol_uz)
    return True


async def delete_unit(key: str) -> bool:
    if not pool:
        return False
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM units WHERE key=$1", key)
    return result != "DELETE 0"


# ══════════════════════════════════════
#  СОТРУДНИКИ (водители и т.п.)
# ══════════════════════════════════════
async def add_staff(tg_id: int, first_name: str, role: str = "driver", last_name: str = "", tg_username: str = "") -> bool:
    """Добавляет или обновляет сотрудника. Возвращает True при успехе."""
    if not pool:
        return False
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO staff (tg_id, tg_username, first_name, last_name, role, is_active)
            VALUES ($1, $2, $3, $4, $5, TRUE)
            ON CONFLICT (tg_id) DO UPDATE SET
                first_name  = EXCLUDED.first_name,
                last_name   = EXCLUDED.last_name,
                tg_username = EXCLUDED.tg_username,
                role        = EXCLUDED.role,
                is_active   = TRUE
        """, tg_id, tg_username, first_name, last_name, role)
    return True


async def remove_staff(tg_id: int) -> bool:
    """Деактивирует сотрудника (is_active=FALSE)."""
    if not pool:
        return False
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE staff SET is_active=FALSE WHERE tg_id=$1", tg_id
        )
    return result != "UPDATE 0"


async def get_staff_by_role(role: str):
    """Возвращает список активных сотрудников с указанной ролью."""
    if not pool:
        return []
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM staff WHERE role=$1 AND is_active=TRUE ORDER BY first_name",
            role
        )


async def is_client_blocked(tg_id: int) -> bool:
    if not pool: return False
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT blocked FROM clients WHERE tg_id=$1", tg_id)
    return bool(row and row.get("blocked"))

async def get_client_lang(tg_id: int):
    """Возвращает сохранённый язык клиента ('ru'/'uz') или None, если клиент не найден."""
    if not pool:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT lang FROM clients WHERE tg_id=$1", tg_id)
    return row["lang"] if row else None


async def set_client_lang(tg_id: int, lang: str):
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE clients SET lang=$1, updated_at=NOW() WHERE tg_id=$2",
            lang, tg_id
        )


# ══════════════════════════════════════
#  CRM SYNC (shared crm_clients table)
# ══════════════════════════════════════
# ══════════════════════════════════════
#  ЛИДЫ (для обработки кнопки "Взять лид" в боте)
# ══════════════════════════════════════
async def get_staff_by_tg_id_for_lead(tg_id: int):
    """Возвращает сотрудника artez_api по tg_id из таблицы staff."""
    if not pool: return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, first_name, last_name, gender, role, login FROM staff WHERE tg_id=$1 AND active=TRUE",
            int(tg_id))
        return dict(row) if row else None

async def take_lead(lead_id: int, staff_id: int, staff_name: str):
    """Назначает лид на сотрудника. Возвращает ('ok'|'already_mine'|'taken', taker_name, lead_code)."""
    if not pool: return ('error', '', '')
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT assigned_to, lead_code FROM leads WHERE id=$1", lead_id)
        if not row:
            return ('not_found', '', '')
        if row['assigned_to'] and row['assigned_to'] != staff_id:
            taker = await conn.fetchrow(
                "SELECT first_name, last_name, gender FROM staff WHERE id=$1", row['assigned_to'])
            taker_name = (f"{taker['first_name'] or ''} {taker['last_name'] or ''}".strip()
                          if taker else 'другой сотрудник')
            taker_verb = 'Взяла' if taker and taker.get('gender') == 'F' else 'Взял'
            return ('taken', taker_name, taker_verb)
        if row['assigned_to'] == staff_id:
            return ('already_mine', '', '')
        await conn.execute("UPDATE leads SET assigned_to=$1, updated_at=NOW() WHERE id=$2", staff_id, lead_id)
        try:
            await conn.execute("""
                INSERT INTO lead_calls (lead_id, staff_id, action, note, created_at)
                VALUES ($1,$2,'note',$3,NOW())
            """, lead_id, staff_id, f"Лид взят через Telegram: {staff_name}")
        except Exception:
            pass
        return ('ok', '', '')


async def upsert_crm_client(phone: str, first_name: str = "", last_name: str = "",
                             tg_id: int = None, tg_username: str = None,
                             source: str = "bot"):
    """Синхронизирует клиента в общую CRM-таблицу crm_clients."""
    if not pool or not phone:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO crm_clients (phone, first_name, last_name, tg_id, tg_username, source)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (phone) DO UPDATE SET
                    first_name  = CASE WHEN $2 != '' THEN $2 ELSE crm_clients.first_name END,
                    last_name   = CASE WHEN $3 != '' THEN $3 ELSE crm_clients.last_name END,
                    tg_id       = COALESCE($4, crm_clients.tg_id),
                    tg_username = CASE WHEN $5 IS NOT NULL AND $5 != ''
                                       THEN $5 ELSE crm_clients.tg_username END,
                    orders_count = (SELECT COUNT(*) FROM orders WHERE client_phone = $1),
                    last_order_at = NOW(),
                    updated_at  = NOW()
            """, phone, first_name or "", last_name or "", tg_id, tg_username, source)
    except Exception as e:
        logging.warning(f"upsert_crm_client error: {e}")


async def get_order_debt(order_id: int) -> float:
    """Долг по заказу: К оплате − уже оплачено (только подтверждённые + наличные)."""
    if not pool: return 0.0
    async with pool.acquire() as conn:
        order = await conn.fetchrow("""
            SELECT total_price, discount_sum, delivery_discount, manual_discount,
                   COALESCE((SELECT SUM(COALESCE(price_per_sqm,0)*COALESCE(sqm,0))
                              FROM order_items WHERE order_id=$1), 0) AS items_total
            FROM orders WHERE id=$1
        """, order_id)
        if not order: return 0.0
        base = float(order["total_price"] or 0) or float(order["items_total"] or 0)
        net = (base
               - float(order["discount_sum"] or 0)
               - float(order["delivery_discount"] or 0)
               - float(order["manual_discount"] or 0))
        paid_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(amount),0) AS paid FROM order_payments "
            "WHERE order_id=$1 AND NOT (confirmed=FALSE AND confirmed_at IS NOT NULL)",
            order_id)
        paid = float(paid_row["paid"]) if paid_row else 0.0
        return max(0.0, net - paid)


async def add_payment_by_driver(order_id: int, amount: float, method: str,
                                 driver_name: str, receipt_file_id: str | None = None,
                                 driver_tg_id: int | None = None,
                                 driver_staff_id: int | None = None) -> int:
    """Создаёт платёж от водителя. Наличные — confirmed=True, остальные — pending."""
    if not pool: return 0
    confirmed = (method == "cash")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO order_payments
                (order_id, amount, method, purpose, note, created_by, confirmed, receipt_file_id, driver_tg_id, created_by_staff_id)
            VALUES ($1,$2,$3,'final',$4,$5,$6,$7,$8,$9)
            RETURNING id
        """, order_id, amount, method,
            f"Оплата при доставке (Telegram)",
            driver_name, confirmed, receipt_file_id, driver_tg_id, driver_staff_id)
        # Пересчёт payment_status
        order = await conn.fetchrow(
            "SELECT total_price, discount_sum, delivery_discount, manual_discount FROM orders WHERE id=$1",
            order_id)
        if order:
            net = (float(order["total_price"] or 0)
                   - float(order["discount_sum"] or 0)
                   - float(order["delivery_discount"] or 0)
                   - float(order["manual_discount"] or 0))
            paid_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(amount),0) AS paid FROM order_payments "
                "WHERE order_id=$1 AND NOT (confirmed=FALSE AND confirmed_at IS NOT NULL)",
                order_id)
            paid = float(paid_row["paid"]) if paid_row else 0.0
            status = "paid" if paid >= net and net > 0 else ("partial" if paid > 0 else "unpaid")
            await conn.execute("UPDATE orders SET payment_status=$1 WHERE id=$2", status, order_id)
        return row["id"] if row else 0


async def save_payment_receipt_file(payment_id: int, file_id: str):
    """Сохраняет Telegram file_id квитанции к платежу."""
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE order_payments SET receipt_file_id=$2 WHERE id=$1",
            payment_id, file_id)


async def get_route_channel_info_for_order(order_id: int) -> dict | None:
    """Возвращает данные для обновления сообщения в канале после записи оплаты."""
    import json as _j
    if not pool: return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT r.id AS route_id, r.branch, r.tg_delivery_msg_ids,
                   ro.sort_order, ro.id AS ro_id,
                   o.order_num, o.client_first_name, o.client_last_name, o.client_phone,
                   o.address, o.short_address, o.location, o.location_address,
                   COALESCE(o.total_price, 0) AS total_price,
                   COALESCE(o.discount_sum, 0) AS discount_sum,
                   COALESCE(o.delivery_discount, 0) AS delivery_discount,
                   COALESCE(o.manual_discount, 0) AS manual_discount,
                   COALESCE((SELECT SUM(COALESCE(price_per_sqm,0)*COALESCE(sqm,0))
                              FROM order_items WHERE order_id=o.id), 0) AS items_total,
                   COALESCE((SELECT COUNT(*) FROM order_items WHERE order_id=o.id), 0)::int AS item_count,
                   COALESCE((SELECT SUM(amount) FROM order_payments
                              WHERE order_id=o.id
                                AND NOT (confirmed=FALSE AND confirmed_at IS NOT NULL)), 0) AS paid_amount
            FROM route_orders ro
            JOIN routes r ON r.id = ro.route_id
            JOIN orders o ON o.id = ro.order_id
            WHERE ro.order_id = $1
            ORDER BY r.created_at DESC LIMIT 1
        """, order_id)
        if not row: return None
        d = dict(row)
        raw = d.get("tg_delivery_msg_ids") or "{}"
        try: msg_ids = _j.loads(raw) if isinstance(raw, str) else (raw or {})
        except: msg_ids = {}
        d["msg_id"] = msg_ids.get(str(order_id))
        # Берём channel_id из __channel__ (записывается при отправке маршрута)
        stored_ch = msg_ids.get("__channel__")
        if stored_ch:
            d["channel_id"] = int(stored_ch)
        else:
            # Фолбек: читаем из конфига (для старых маршрутов без __channel__)
            branch = d.get("branch", "")
            key_ch = "delivery_channel_navoi_id" if branch == "navoi" else "delivery_channel_zarafshan_id"
            key_gr = "delivery_group_navoi_id"   if branch == "navoi" else "delivery_group_zarafshan_id"
            for key in (key_ch, key_gr, "delivery_group_id"):
                cfg = await conn.fetchrow("SELECT value FROM config WHERE key=$1", key)
                if cfg and cfg["value"]:
                    d["channel_id"] = int(cfg["value"])
                    break
            else:
                d["channel_id"] = 0
        num_row = await conn.fetchrow(
            "SELECT COUNT(*)+1 AS num FROM route_orders WHERE route_id=$1 AND sort_order < $2",
            d["route_id"], d["sort_order"])
        d["stop_num"] = int(num_row["num"]) if num_row else 1
        return d

async def get_debt_approvers_bot() -> list:
    """Сотрудники с can_approve_debt=true и заполненным tg_id."""
    if not pool: return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, first_name, last_name, tg_id FROM staff "
            "WHERE can_approve_debt=TRUE AND active=TRUE AND tg_id IS NOT NULL")
        return [dict(r) for r in rows]

async def approve_debt_close(order_id: int, responsible_staff_id: int, due_date_str: str | None) -> bool:
    """Одобрить закрытие заказа с долгом."""
    if not pool: return False
    from datetime import date, timedelta
    due = None
    if due_date_str:
        try: due = date.fromisoformat(due_date_str)
        except Exception: pass
    if due is None:
        due = date.today() + timedelta(days=7)
    async with pool.acquire() as conn:
        order = await conn.fetchrow("SELECT status, order_num FROM orders WHERE id=$1", order_id)
        if not order or order["status"] != "delivery":
            return False
        resp = await conn.fetchrow(
            "SELECT COALESCE(last_name||' '||first_name, login) AS name FROM staff WHERE id=$1", responsible_staff_id)
        by_name = resp["name"] if resp else "Менеджер"
        await conn.execute("""
            UPDATE orders SET status='delivered', debt_responsible_id=$2,
                   debt_due_date=$3, debt_approved_at=NOW() WHERE id=$1
        """, order_id, responsible_staff_id, due)
        await conn.execute(
            "INSERT INTO order_status_history(order_num, new_status, note) VALUES($1,'delivered',$2)",
            order["order_num"], f"Закрыт с долгом через бот (ответственный: {by_name})")
        await conn.execute(
            "UPDATE route_orders SET stop_status='done' WHERE order_id=$1 AND stop_status='pending'", order_id)
    return True

async def set_driver_confirmed(order_id: int, confirmed: bool = True):
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE route_orders SET driver_confirmed=$2 WHERE order_id=$1", order_id, confirmed)

async def get_order_items_for_driver(order_id: int) -> list:
    """Позиции заказа для отображения водителю."""
    if not pool: return []
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT i.service AS name, i.width_cm, i.length_cm, i.sqm, i.price_per_sqm,
                   COALESCE(i.sqm * i.price_per_sqm, 0) AS line_total
            FROM order_items i
            WHERE i.order_id = $1
            ORDER BY i.id
        """, order_id)
        return [dict(r) for r in rows]

async def get_staff_id_by_tg(tg_id: int) -> int | None:
    if not pool: return None
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "SELECT id FROM staff WHERE tg_id=$1 LIMIT 1", int(tg_id))
        except Exception:
            row = await conn.fetchrow(
                "SELECT id FROM staff WHERE tg_id::text=$1 LIMIT 1", str(tg_id))
        return row["id"] if row else None

async def confirm_cash_handover_bot(handover_id: int, confirmed_by: int) -> dict:
    """Подтверждение передачи наличных через бот. Возвращает row + confirmer_name + sender_name."""
    if not pool: return {}
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE cash_handovers SET status='confirmed', confirmed_at=NOW(), confirmed_by=$2
            WHERE id=$1 AND status='pending' RETURNING *
        """, handover_id, confirmed_by)
        if not row:
            return {}
        result = dict(row)
        confirmer = await conn.fetchrow(
            "SELECT first_name, last_name FROM staff WHERE id=$1", confirmed_by)
        if confirmer:
            result['confirmer_name'] = " ".join(filter(None, [confirmer['last_name'], confirmer['first_name']])).strip()
        if result.get('from_staff_id'):
            sender = await conn.fetchrow(
                "SELECT first_name, last_name FROM staff WHERE id=$1", result['from_staff_id'])
            if sender:
                result['sender_name'] = " ".join(filter(None, [sender['last_name'], sender['first_name']])).strip()
        return result

async def reject_cash_handover_bot(handover_id: int, rejected_by: int) -> dict:
    """Отклонение передачи наличных через бот. Возвращает row + rejector_name + sender_name."""
    if not pool: return {}
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE cash_handovers SET status='rejected', confirmed_at=NOW(), confirmed_by=$2
            WHERE id=$1 AND status='pending' RETURNING *
        """, handover_id, rejected_by)
        if not row:
            return {}
        result = dict(row)
        rejector = await conn.fetchrow(
            "SELECT first_name, last_name FROM staff WHERE id=$1", rejected_by)
        if rejector:
            result['rejector_name'] = " ".join(filter(None, [rejector['last_name'], rejector['first_name']])).strip()
        if result.get('from_staff_id'):
            sender = await conn.fetchrow(
                "SELECT first_name, last_name FROM staff WHERE id=$1", result['from_staff_id'])
            if sender:
                result['sender_name'] = " ".join(filter(None, [sender['last_name'], sender['first_name']])).strip()
        return result

async def create_discount_request(order_id: int, order_num: str, driver_tg_id: int, requested_amount: float) -> dict | None:
    if not pool: return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO discount_requests(order_id, order_num, driver_tg_id, requested_amount)
            VALUES($1,$2,$3,$4) RETURNING *
        """, order_id, order_num, driver_tg_id, requested_amount)
        return dict(row) if row else None

async def get_managers_with_push() -> list:
    """Менеджеры и админы с tg_id для уведомлений."""
    if not pool: return []
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, first_name, last_name, tg_id
            FROM staff
            WHERE role IN ('admin','manager') AND active=TRUE
              AND tg_id IS NOT NULL
        """)
        return [dict(r) for r in rows]

async def apply_auto_discount(order_id: int, amount: float) -> bool:
    """Применить авто-скидку (round-off) к заказу."""
    if not pool: return False
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET manual_discount = COALESCE(manual_discount,0) + $2 WHERE id=$1",
            order_id, amount)
    return True

async def resolve_discount_request(request_id: int, approved_amount: float, resolved_by: int) -> dict | None:
    if not pool: return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE discount_requests
               SET status='approved', approved_amount=$2, resolved_by=$3, resolved_at=NOW()
             WHERE id=$1 AND status='pending'
            RETURNING *
        """, request_id, approved_amount, resolved_by)
        if not row:
            return None
        r = dict(row)
        await conn.execute(
            "UPDATE orders SET manual_discount = COALESCE(manual_discount,0) + $2 WHERE id=$1",
            r["order_id"], approved_amount)
        return r

async def reject_discount_request(request_id: int, resolved_by: int) -> dict | None:
    if not pool: return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE discount_requests
               SET status='rejected', resolved_by=$2, resolved_at=NOW()
             WHERE id=$1 AND status='pending'
            RETURNING *
        """, request_id, resolved_by)
        return dict(row) if row else None


# ── Долговые одобрения (для детального сообщения) ────────────────────────────

async def get_order_full_for_debt(order_id: int) -> dict | None:
    """Полные данные заказа для детального TG-сообщения менеджеру."""
    if not pool: return None
    async with pool.acquire() as conn:
        order = await conn.fetchrow("""
            SELECT o.order_num, o.client_first_name, o.client_last_name, o.client_phone,
                   o.address, o.short_address, o.location, o.location_address,
                   COALESCE(o.total_price, 0) AS total_price,
                   COALESCE(o.discount_sum, 0) AS discount_sum,
                   COALESCE(o.delivery_discount, 0) AS delivery_discount,
                   COALESCE(o.manual_discount, 0) AS manual_discount,
                   COALESCE((SELECT SUM(COALESCE(sqm*price_per_sqm,0)) FROM order_items WHERE order_id=o.id), 0) AS items_total
            FROM orders o WHERE o.id = $1
        """, order_id)
        if not order: return None
        d = dict(order)
        items = await conn.fetch("""
            SELECT service, width_cm, length_cm, sqm, price_per_sqm,
                   COALESCE(sqm * price_per_sqm, 0) AS line_total
            FROM order_items WHERE order_id = $1 ORDER BY id
        """, order_id)
        d["items"] = [dict(i) for i in items]
        paid = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM order_payments "
            "WHERE order_id=$1 "
            "AND ((method='cash' AND NOT (confirmed=FALSE AND confirmed_at IS NOT NULL)) "
            "     OR (method<>'cash' AND confirmed=TRUE))",
            order_id)
        d["paid_amount"] = float(paid) if paid else 0.0
        return d

async def create_debt_approval_db(order_id: int, order_num: str, driver_tg_id: int,
                                   debt_amount: float, mgr_msgs_json: str = '{}') -> dict | None:
    """Создать запись запроса долгового одобрения в БД."""
    if not pool: return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO debt_approval_requests(order_id, order_num, driver_tg_id, debt_amount, mgr_msgs)
            VALUES($1,$2,$3,$4,$5::jsonb)
            RETURNING id, order_id, order_num, debt_amount, status
        """, order_id, order_num, driver_tg_id, debt_amount, mgr_msgs_json)
        return dict(row) if row else None

async def mark_debt_approval_resolved_by_order(order_id: int, resolution: str) -> None:
    """Пометить запрос долгового одобрения как обработанный (по order_id)."""
    if not pool: return
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE debt_approval_requests
            SET status=$2, resolution=$3, resolved_at=NOW()
            WHERE order_id=$1 AND status='pending'
        """, order_id, resolution, resolution)


# ══════════════════════════════════════
#  ПРОМО-АКЦИИ
# ══════════════════════════════════════
async def get_site_user_by_tg_id(tg_id: int) -> dict | None:
    """Зарегистрированный пользователь сайта (is_verified=TRUE), привязанный к этому Telegram ID."""
    if not pool: return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, phone, first_name FROM users WHERE tg_id=$1 AND is_verified=TRUE",
            int(tg_id))
        return dict(row) if row else None


async def _get_active_promotion(conn):
    """Текущая активная кампания (is_active=TRUE и сейчас между starts_at и ends_at)."""
    return await conn.fetchrow("""
        SELECT * FROM promotions
        WHERE is_active = TRUE AND NOW() BETWEEN starts_at AND ends_at
        ORDER BY id DESC LIMIT 1
    """)


def _promo_public_fields(promo, mode: str, expires_at) -> dict:
    return {
        "id":            promo["id"],
        "code":          promo["code"],
        "title_ru":      promo["title_ru"],
        "title_uz":      promo["title_uz"],
        "text_ru":       promo["text_ru"],
        "text_uz":       promo["text_uz"],
        "discount_pct":  float(promo["discount_pct"]) if promo["discount_pct"] is not None else 0,
        "sound_enabled": promo["sound_enabled"],
        "mode":          mode,
        "expires_at":    expires_at.isoformat() if expires_at else None,
    }


async def check_promo_eligibility(user_id: int, phone: str, channel: str = "bot") -> dict | None:
    """Единый источник правды по эквайру акции для сайта/бота — зеркало
    artez_api/database.py::check_promo_eligibility (та же БД, тот же race-safe upsert).
    Возвращает None если акции нет/клиент не подходит, иначе dict с mode: full|silent|none."""
    if not pool:
        return None
    async with pool.acquire() as conn:
        promo = await _get_active_promotion(conn)
        if not promo:
            return None

        if promo["target_new_only"]:
            has_order = await conn.fetchval(
                "SELECT 1 FROM orders WHERE client_phone=$1 LIMIT 1", phone
            )
            if has_order:
                return None

        state = await conn.fetchrow(
            "SELECT * FROM promo_user_state WHERE promotion_id=$1 AND user_id=$2",
            promo["id"], user_id
        )
        if not state:
            state = await conn.fetchrow("""
                INSERT INTO promo_user_state (promotion_id, user_id, shown_at, expires_at, channel)
                VALUES ($1, $2, NOW(), NOW() + ($3 * INTERVAL '1 hour'), $4)
                ON CONFLICT (promotion_id, user_id) DO NOTHING
                RETURNING *
            """, promo["id"], user_id, promo["window_hours"], channel)
            if state:
                mode = "full"
            else:
                # Гонка: параллельный запрос (например, с сайта) уже вставил строку — перечитываем
                state = await conn.fetchrow(
                    "SELECT * FROM promo_user_state WHERE promotion_id=$1 AND user_id=$2",
                    promo["id"], user_id
                )
                if not state:
                    return None
                mode = "silent" if (state["used_order_id"] is None and state["expires_at"]
                                     and state["expires_at"] > datetime.now(timezone.utc)) else "none"
        else:
            if state["used_order_id"] is None and state["expires_at"] and state["expires_at"] > datetime.now(timezone.utc):
                mode = "silent"
            else:
                mode = "none"

        return _promo_public_fields(promo, mode, state["expires_at"])


async def get_live_promo_id_for_user(user_id: int) -> int | None:
    """Живое (не истёкшее, не использованное) окно акции пользователя — для тега лида.
    Не потребляет окно (used_order_id не трогаем — это делает только реальный заказ на стороне artez_api)."""
    if not pool or not user_id:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT pus.promotion_id
            FROM promo_user_state pus
            JOIN promotions p ON p.id = pus.promotion_id
            WHERE pus.user_id = $1 AND pus.used_order_id IS NULL
              AND pus.expires_at > NOW() AND p.is_active = TRUE
            ORDER BY pus.created_at DESC LIMIT 1
        """, user_id)
        return row["promotion_id"] if row else None


async def set_lead_promo(lead_code: str, promo_id: int) -> None:
    """Помечает лид принадлежностью к акции (тег для сотрудников), созданный ботом."""
    if not pool or not lead_code or not promo_id:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE leads SET promo_id=$1, updated_at=NOW() WHERE lead_code=$2",
            promo_id, lead_code)
