import asyncio
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SOLANA_WALLET = os.getenv("SOLANA_WALLET", "").strip()
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
DB_PATH = Path(os.getenv("DB_PATH", "shop.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
ORDER_LIFETIME = 3600
POLL_INTERVAL = 10
LAMPORTS_PER_SOL = 1_000_000_000

if not BOT_TOKEN:
    raise RuntimeError("Trūksta BOT_TOKEN .env faile")
if not SOLANA_WALLET:
    raise RuntimeError("Trūksta SOLANA_WALLET .env faile")
if not HELIUS_API_KEY:
    raise RuntimeError("Trūksta HELIUS_API_KEY .env faile")
if not ADMIN_IDS:
    raise RuntimeError("Nurodyk bent vieną ADMIN_IDS reikšmę")

RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("solana_shop")
router = Router()


def connect_db():
    import sqlite3
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect_db() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            price_lamports INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(category_id) REFERENCES categories(id)
        );
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'available',
            order_id INTEGER,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            amount_lamports INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            tx_signature TEXT UNIQUE,
            video_id INTEGER,
            FOREIGN KEY(product_id) REFERENCES products(id),
            FOREIGN KEY(video_id) REFERENCES videos(id)
        );
        CREATE TABLE IF NOT EXISTS used_transactions (
            signature TEXT PRIMARY KEY,
            order_id INTEGER,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scanned_signatures (
            signature TEXT PRIMARY KEY,
            scanned_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id, active);
        CREATE INDEX IF NOT EXISTS idx_videos_inventory ON videos(product_id, status);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, expires_at);
        """)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def sol_to_lamports(value: str) -> int:
    amount = Decimal(value.strip())
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Kaina turi būti didesnė už nulį.")
    lamports = amount * LAMPORTS_PER_SOL
    if lamports != lamports.to_integral_value():
        raise ValueError("SOL kaina gali turėti ne daugiau kaip 9 skaitmenis po kablelio.")
    return int(lamports)


def format_sol(lamports: int) -> str:
    return f"{Decimal(lamports) / Decimal(LAMPORTS_PER_SOL):.9f}".rstrip("0").rstrip(".")


def create_order(user_id: int, product_id: int):
    now = int(time.time())
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        product = db.execute("""
            SELECT p.*, c.name AS category_name
            FROM products p JOIN categories c ON c.id = p.category_id
            WHERE p.id = ? AND p.active = 1
        """, (product_id,)).fetchone()
        if not product:
            db.rollback()
            return None, "Prekė nerasta."
        stock = db.execute("SELECT COUNT(*) AS total FROM videos WHERE product_id = ? AND status = 'available'", (product_id,)).fetchone()["total"]
        if stock < 1:
            db.rollback()
            return None, "Šios prekės vaizdo įrašai išparduoti."

        # Unique exact amount helps distinguish payments sent to the same wallet.
        base_amount = product["price_lamports"]
        amount = None
        for offset in range(1, 999001):
            candidate = base_amount + offset
            exists = db.execute("""
                SELECT 1 FROM orders
                WHERE amount_lamports = ? AND status = 'pending' AND expires_at > ?
                LIMIT 1
            """, (candidate, now)).fetchone()
            if not exists:
                amount = candidate
                break
        if amount is None:
            db.rollback()
            return None, "Nepavyko sukurti unikalios mokėjimo sumos. Bandyk dar kartą."
        cur = db.execute("""
            INSERT INTO orders(user_id, product_id, amount_lamports, status, created_at, expires_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
        """, (user_id, product_id, amount, now, now + ORDER_LIFETIME))
        order_id = cur.lastrowid
        db.commit()
        return {"id": order_id, "name": product["name"], "description": product["description"], "amount": amount, "expires_at": now + ORDER_LIFETIME}, None


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Katalogas", callback_data="catalog")],
        [InlineKeyboardButton(text="📦 Mano užsakymai", callback_data="myorders")],
        [InlineKeyboardButton(text="ℹ️ Pagalba", callback_data="help")],
    ])


def categories_keyboard():
    with connect_db() as db:
        categories = db.execute("""
            SELECT c.id, c.name FROM categories c
            WHERE EXISTS (SELECT 1 FROM products p WHERE p.category_id = c.id AND p.active = 1)
            ORDER BY c.name
        """).fetchall()
    rows = [[InlineKeyboardButton(text=r["name"], callback_data=f"cat:{r['id']}")] for r in categories]
    rows.append([InlineKeyboardButton(text="⬅️ Meniu", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def products_keyboard(category_id: int):
    with connect_db() as db:
        products = db.execute("SELECT id, name, price_lamports FROM products WHERE category_id = ? AND active = 1 ORDER BY id DESC", (category_id,)).fetchall()
    rows = [[InlineKeyboardButton(text=f"{p['name']} — {format_sol(p['price_lamports'])} SOL", callback_data=f"product:{p['id']}")] for p in products]
    rows.append([InlineKeyboardButton(text="⬅️ Kategorijos", callback_data="catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_keyboard(product_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Pirkti už SOL", callback_data=f"buy:{product_id}")],
        [InlineKeyboardButton(text="⬅️ Katalogas", callback_data="catalog")],
    ])


@router.message(CommandStart())
async def start(message: Message):
    await message.answer("Sveiki atvykę į parduotuvę!\n\nPasirinkite veiksmą:", reply_markup=main_keyboard())


@router.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    await callback.message.edit_text("Pasirinkite veiksmą:", reply_markup=main_keyboard())
    await callback.answer()


@router.callback_query(F.data == "catalog")
async def catalog(callback: CallbackQuery):
    await callback.message.edit_text("🛍 Pasirinkite kategoriją:", reply_markup=categories_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("cat:"))
async def category_open(callback: CallbackQuery):
    category_id = int(callback.data.split(":", 1)[1])
    with connect_db() as db:
        category = db.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
    if not category:
        await callback.answer("Kategorija nerasta.", show_alert=True)
        return
    await callback.message.edit_text(f"📂 Kategorija: {category['name']}\n\nPasirinkite prekę:", reply_markup=products_keyboard(category_id))
    await callback.answer()


@router.callback_query(F.data.startswith("product:"))
async def product_open(callback: CallbackQuery):
    product_id = int(callback.data.split(":", 1)[1])
    with connect_db() as db:
        product = db.execute("""
            SELECT p.*, c.name AS category_name FROM products p
            JOIN categories c ON c.id = p.category_id
            WHERE p.id = ? AND p.active = 1
        """, (product_id,)).fetchone()
        if product:
            stock = db.execute("SELECT COUNT(*) AS total FROM videos WHERE product_id = ? AND status = 'available'", (product_id,)).fetchone()["total"]
        else:
            stock = 0
    if not product:
        await callback.answer("Prekė nerasta.", show_alert=True)
        return
    text = f"📦 {product['name']}\n\n{product['description'] or 'Aprašymo nėra.'}\n\n💰 Kaina: {format_sol(product['price_lamports'])} SOL\n🎞 Likutis: {stock}"
    await callback.message.edit_text(text, reply_markup=product_keyboard(product_id))
    await callback.answer()


@router.callback_query(F.data.startswith("buy:"))
async def buy_product(callback: CallbackQuery):
    product_id = int(callback.data.split(":", 1)[1])
    order, error = create_order(callback.from_user.id, product_id)
    if error:
        await callback.answer(error, show_alert=True)
        return
    await callback.message.answer(
        f"🧾 Užsakymas #{order['id']}\n📦 Prekė: {order['name']}\n\n"
        f"💰 Tiksliai perveskite: {format_sol(order['amount'])} SOL\n"
        f"👛 Gavėjo piniginė:\n{SOLANA_WALLET}\n\n"
        "⏳ Užsakymas galioja 60 minučių. Mokėjimas tikrinamas automatiškai.\n"
        "⚠️ Siųskite tik SOL per Solana tinklą. Nesiųskite kitos valiutos ar žetono."
    )
    await callback.answer("Užsakymas sukurtas.")


@router.callback_query(F.data == "myorders")
async def my_orders(callback: CallbackQuery):
    with connect_db() as db:
        orders = db.execute("""
            SELECT o.id, o.amount_lamports, o.status, p.name AS product_name
            FROM orders o JOIN products p ON p.id = o.product_id
            WHERE o.user_id = ? ORDER BY o.id DESC LIMIT 10
        """, (callback.from_user.id,)).fetchall()
    if not orders:
        text = "Dar neturite užsakymų."
    else:
        lines = ["📦 Jūsų paskutiniai užsakymai:"]
        for order in orders:
            lines.append(f"\n#{order['id']} — {order['product_name']}\n{format_sol(order['amount_lamports'])} SOL — {order['status']}")
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=main_keyboard())
    await callback.answer()


@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery):
    await callback.message.edit_text(
        "ℹ️ Pagalba\n\n1. Pasirinkite prekę.\n2. Perveskite tiksliai nurodytą SOL sumą.\n"
        "3. Palaukite automatinio patvirtinimo.\n4. Vaizdo įrašas bus išsiųstas šiame pokalbyje.\n\n"
        "Jei kilo mokėjimo problema, susisiekite su administratoriumi.",
        reply_markup=main_keyboard(),
    )
    await callback.answer()


class AdminVideo(StatesGroup):
    waiting_for_video = State()


@router.message(Command("addcategory"))
async def add_category(message: Message):
    if not is_admin(message.from_user.id):
        return
    name = message.text.partition(" ")[2].strip()
    if not name:
        await message.answer("Naudojimas: /addcategory Kategorijos pavadinimas")
        return
    try:
        with connect_db() as db:
            db.execute("INSERT INTO categories(name) VALUES (?)", (name,))
        await message.answer(f"✅ Kategorija sukurta: {name}")
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            await message.answer("Tokia kategorija jau egzistuoja.")
        else:
            log.exception("Nepavyko sukurti kategorijos")
            await message.answer("Nepavyko sukurti kategorijos.")


@router.message(Command("addproduct"))
async def add_product(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = [part.strip() for part in message.text.partition(" ")[2].split("|", 3)]
    if len(parts) < 4:
        await message.answer("Naudojimas:\n/addproduct kategorijos_ID | pavadinimas | kaina_SOL | aprašymas\n\nPavyzdys:\n/addproduct 1 | Premium video | 0.05 | Skaitmeninė prekė")
        return
    try:
        category_id = int(parts[0])
        name = parts[1]
        price_lamports = sol_to_lamports(parts[2])
        description = parts[3]
        with connect_db() as db:
            if not db.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone():
                await message.answer("Tokios kategorijos nėra.")
                return
            cur = db.execute("INSERT INTO products(category_id, name, description, price_lamports) VALUES (?, ?, ?, ?)", (category_id, name, description, price_lamports))
            product_id = cur.lastrowid
        await message.answer(f"✅ Prekė sukurta.\nID: {product_id}\nPavadinimas: {name}\nKaina: {format_sol(price_lamports)} SOL\n\nVaizdo įrašams pridėti: /addvideo {product_id}")
    except (ValueError, InvalidOperation) as exc:
        await message.answer(f"Neteisingi duomenys: {exc}")


@router.message(Command("products"))
async def admin_products(message: Message):
    if not is_admin(message.from_user.id):
        return
    with connect_db() as db:
        rows = db.execute("SELECT p.id, p.name, p.price_lamports, c.name AS category FROM products p JOIN categories c ON c.id = p.category_id ORDER BY p.id").fetchall()
    if not rows:
        await message.answer("Prekių dar nėra.")
        return
    await message.answer("📦 Prekių sąrašas:\n" + "\n".join(f"ID {r['id']} | {r['name']} | {format_sol(r['price_lamports'])} SOL | {r['category']}" for r in rows))


@router.message(Command("addvideo"))
async def add_video_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = message.text.partition(" ")[2].strip()
    if not raw.isdigit():
        await message.answer("Naudojimas: /addvideo PREKĖS_ID")
        return
    product_id = int(raw)
    with connect_db() as db:
        product = db.execute("SELECT id, name FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        await message.answer("Prekė nerasta.")
        return
    await state.set_state(AdminVideo.waiting_for_video)
    await state.update_data(product_id=product_id)
    await message.answer(f"Siųsk vaizdo įrašus prekei „{product['name']}“. Kiekvienas įrašas bus įtrauktas į jos inventorių. Baigęs parašyk /done.")


@router.message(AdminVideo.waiting_for_video, F.video)
async def save_product_video(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    product_id = data.get("product_id")
    if not product_id:
        await state.clear()
        await message.answer("Nepavyko nustatyti prekės. Pradėk iš naujo.")
        return
    with connect_db() as db:
        if not db.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone():
            await state.clear()
            await message.answer("Prekė nerasta.")
            return
        db.execute("INSERT INTO videos(product_id, file_id, status, created_at) VALUES (?, ?, 'available', ?)", (product_id, message.video.file_id, int(time.time())))
        count = db.execute("SELECT COUNT(*) AS total FROM videos WHERE product_id = ? AND status = 'available'", (product_id,)).fetchone()["total"]
    await message.answer(f"✅ Vaizdo įrašas pridėtas.\nPrekės ID: {product_id}\nGalimų vaizdo įrašų: {count}\nSiųsk kitą arba parašyk /done.")


@router.message(Command("done"))
async def finish_video_upload(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("✅ Vaizdo įrašų įkėlimas baigtas.")


@router.message(Command("stock"))
async def stock_command(message: Message):
    if not is_admin(message.from_user.id):
        return
    with connect_db() as db:
        rows = db.execute("""
            SELECT p.id, p.name,
            SUM(CASE WHEN v.status = 'available' THEN 1 ELSE 0 END) AS available,
            SUM(CASE WHEN v.status = 'reserved' THEN 1 ELSE 0 END) AS reserved,
            SUM(CASE WHEN v.status = 'sent' THEN 1 ELSE 0 END) AS sent
            FROM products p LEFT JOIN videos v ON v.product_id = p.id
            GROUP BY p.id, p.name ORDER BY p.id
        """).fetchall()
    if not rows:
        await message.answer("Prekių nėra.")
        return
    await message.answer("📊 Vaizdo įrašų inventorius:\n\n" + "\n\n".join(f"ID {r['id']} — {r['name']}\nLaisvi: {r['available'] or 0} | Rezervuoti: {r['reserved'] or 0} | Išsiųsti: {r['sent'] or 0}" for r in rows))


@router.message(Command("orders"))
async def admin_orders(message: Message):
    if not is_admin(message.from_user.id):
        return
    with connect_db() as db:
        rows = db.execute("""
            SELECT o.id, o.user_id, o.amount_lamports, o.status, p.name AS product_name, o.tx_signature
            FROM orders o JOIN products p ON p.id = o.product_id
            ORDER BY o.id DESC LIMIT 20
        """).fetchall()
    if not rows:
        await message.answer("Užsakymų dar nėra.")
        return
    await message.answer("🧾 Paskutiniai užsakymai:\n\n" + "\n\n".join(f"#{r['id']} | {r['product_name']}\nVartotojas: {r['user_id']}\nSuma: {format_sol(r['amount_lamports'])} SOL\nBūsena: {r['status']}\nTX: {r['tx_signature'] or '—'}" for r in rows))


async def rpc_call(session: aiohttp.ClientSession, method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as response:
        response.raise_for_status()
        result = await response.json()
    if result.get("error"):
        raise RuntimeError(f"Solana RPC klaida: {result['error']}")
    return result.get("result")


def extract_instructions(tx: dict):
    message = ((tx.get("transaction") or {}).get("message") or {})
    instructions = list(message.get("instructions") or [])
    for group in ((tx.get("meta") or {}).get("innerInstructions") or []):
        instructions.extend(group.get("instructions") or [])
    return instructions


def find_transfers_to_wallet(tx: dict):
    transfers = []
    for instruction in extract_instructions(tx):
        if instruction.get("program") != "system":
            continue
        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("type") != "transfer":
            continue
        info = parsed.get("info") or {}
        if info.get("destination") == SOLANA_WALLET and isinstance(info.get("lamports"), int):
            transfers.append(info["lamports"])
    return transfers


def process_confirmed_transfer(signature: str, amount_lamports: int, block_time: int):
    now = int(time.time())
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM used_transactions WHERE signature = ?", (signature,)).fetchone():
            db.execute("INSERT OR IGNORE INTO scanned_signatures VALUES (?, ?)", (signature, now))
            db.commit()
            return None
        rows = db.execute("""
            SELECT id, user_id, product_id, amount_lamports FROM orders
            WHERE status = 'pending' AND amount_lamports = ? AND created_at <= ? AND expires_at >= ?
        """, (amount_lamports, block_time, block_time)).fetchall()
        if len(rows) != 1:
            db.execute("INSERT OR IGNORE INTO scanned_signatures VALUES (?, ?)", (signature, now))
            db.commit()
            return None
        order = rows[0]
        db.execute("INSERT INTO used_transactions(signature, order_id, created_at) VALUES (?, ?, ?)", (signature, order["id"], now))
        db.execute("UPDATE orders SET status = 'paid', tx_signature = ? WHERE id = ? AND status = 'pending'", (signature, order["id"]))
        db.execute("INSERT OR IGNORE INTO scanned_signatures VALUES (?, ?)", (signature, now))
        db.commit()
        return {"order_id": order["id"], "user_id": order["user_id"], "product_id": order["product_id"]}


async def scan_solana_payments(bot: Bot, session: aiohttp.ClientSession):
    while True:
        try:
            signatures = await rpc_call(session, "getSignaturesForAddress", [SOLANA_WALLET, {"limit": 100, "commitment": "finalized"}])
            for item in reversed(signatures or []):
                signature = item.get("signature")
                if not signature:
                    continue
                with connect_db() as db:
                    seen = db.execute("SELECT 1 FROM scanned_signatures WHERE signature = ?", (signature,)).fetchone()
                if seen:
                    continue
                if item.get("err") is not None:
                    with connect_db() as db:
                        db.execute("INSERT OR IGNORE INTO scanned_signatures VALUES (?, ?)", (signature, int(time.time())))
                    continue
                tx = await rpc_call(session, "getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
                if not tx:
                    continue
                meta = tx.get("meta") or {}
                if meta.get("err") is not None:
                    with connect_db() as db:
                        db.execute("INSERT OR IGNORE INTO scanned_signatures VALUES (?, ?)", (signature, int(time.time())))
                    continue
                block_time = tx.get("blockTime")
                if not isinstance(block_time, int):
                    continue
                for amount in find_transfers_to_wallet(tx):
                    result = process_confirmed_transfer(signature, amount, block_time)
                    if result:
                        log.info("Mokėjimas patvirtintas: order=%s", result["order_id"])
                        await bot.send_message(result["user_id"], f"✅ Mokėjimas užsakymui #{result['order_id']} patvirtintas! Paruošiame vaizdo įrašą.")
                        break
                with connect_db() as db:
                    db.execute("INSERT OR IGNORE INTO scanned_signatures VALUES (?, ?)", (signature, int(time.time())))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Klaida tikrinant Solana mokėjimus")
        await asyncio.sleep(POLL_INTERVAL)


def reserve_videos_for_paid_orders():
    """
    Atomically assign one unique available video to each paid order.
    A video is assigned at most once because assignment happens inside
    BEGIN IMMEDIATE and only from status='available'.
    """
    assignments = []
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        orders = db.execute("""
            SELECT id, user_id, product_id
            FROM orders
            WHERE status = 'paid' AND video_id IS NULL
            ORDER BY id
        """).fetchall()

        for order in orders:
            video = db.execute("""
                SELECT id, file_id
                FROM videos
                WHERE product_id = ? AND status = 'available'
                ORDER BY id
                LIMIT 1
            """, (order["product_id"],)).fetchone()
            if not video:
                log.error(
                    "Užsakymas #%s apmokėtas, bet prekei %s nebėra video",
                    order["id"], order["product_id"]
                )
                continue

            # Conditional update is an extra safeguard against double assignment.
            updated = db.execute("""
                UPDATE videos
                SET status = 'reserved', order_id = ?
                WHERE id = ? AND status = 'available'
            """, (order["id"], video["id"])).rowcount
            if updated != 1:
                continue

            order_updated = db.execute("""
                UPDATE orders SET video_id = ?
                WHERE id = ? AND status = 'paid' AND video_id IS NULL
            """, (video["id"], order["id"])).rowcount
            if order_updated != 1:
                db.execute("""
                    UPDATE videos SET status = 'available', order_id = NULL
                    WHERE id = ? AND status = 'reserved' AND order_id = ?
                """, (video["id"], order["id"]))
                continue

            assignments.append({
                "order_id": order["id"],
                "user_id": order["user_id"],
                "video_id": video["id"],
                "file_id": video["file_id"],
            })
        db.commit()
    return assignments


def get_reserved_deliveries():
    """
    Return previously reserved deliveries as well as newly assigned ones.
    This is what allows retries after a temporary Telegram/network failure.
    """
    with connect_db() as db:
        rows = db.execute("""
            SELECT o.id AS order_id, o.user_id, o.video_id,
                   v.file_id
            FROM orders o
            JOIN videos v ON v.id = o.video_id
            WHERE o.status = 'paid'
              AND v.status = 'reserved'
              AND v.order_id = o.id
            ORDER BY o.id
        """).fetchall()
    return [dict(row) for row in rows]


async def deliver_paid_orders(bot: Bot):
    while True:
        try:
            # First assign inventory to newly paid orders, then reload all
            # pending reservations so failed deliveries are retried.
            reserve_videos_for_paid_orders()
            for item in get_reserved_deliveries():
                try:
                    await bot.send_video(
                        chat_id=item["user_id"],
                        video=item["file_id"],
                        caption=f"🎉 Užsakymas #{item['order_id']} įvykdytas!"
                    )
                    with connect_db() as db:
                        db.execute("BEGIN IMMEDIATE")
                        db.execute("""
                            UPDATE videos SET status = 'sent'
                            WHERE id = ? AND status = 'reserved' AND order_id = ?
                        """, (item["video_id"], item["order_id"]))
                        db.execute("""
                            UPDATE orders SET status = 'delivered'
                            WHERE id = ? AND status = 'paid' AND video_id = ?
                        """, (item["order_id"], item["video_id"]))
                        db.commit()
                    log.info(
                        "Video pristatytas: order=%s video=%s",
                        item["order_id"], item["video_id"]
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Keep reservation and retry next loop. Never release it
                    # to inventory, otherwise another customer could receive it.
                    log.exception(
                        "Nepavyko pristatyti užsakymo #%s; bus bandoma dar kartą",
                        item["order_id"]
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Klaida pristatant vaizdo įrašus")
        await asyncio.sleep(5)


async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    async with aiohttp.ClientSession() as session:
        payment_task = asyncio.create_task(scan_solana_payments(bot, session))
        delivery_task = asyncio.create_task(deliver_paid_orders(bot))
        try:
            log.info("Telegram botas paleistas")
            await dp.start_polling(bot)
        finally:
            payment_task.cancel()
            delivery_task.cancel()
            await asyncio.gather(payment_task, delivery_task, return_exceptions=True)
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
