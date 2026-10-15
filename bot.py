"""
Telegram parduotuvės botas (aiogram 3)
- Fizinės prekės, pristatymas
- Mokėjimas kriptovaliuta (pervedimas į jūsų piniginę + TX hash, patvirtina adminas)
- Prekių valdymas admino komandomis bote
- Klientų kalba: lietuvių
"""
import asyncio
import html
import logging
import os
import re
import sqlite3
import time
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ───────────────────────── Konfigūracija ─────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "7547963291").replace(" ", "").split(",") if x}
SHOP_NAME = os.getenv("SHOP_NAME", "Akropolis")
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "@akropoliobot")
SHIPPING_CENTS = round(float(os.getenv("SHIPPING_EUR", "0").replace(",", ".")) * 100)
# Railway: prijunkite Volume su Mount path /data, kad duomenys nedingtų
DB_PATH = os.getenv("DB_PATH", "/data/shop.db" if os.path.isdir("/data") else "shop.db")


def parse_wallets(raw: str) -> dict[str, str]:
    wallets = {}
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        coin, addr = part.split(":", 1)
        if coin.strip() and addr.strip():
            wallets[coin.strip().upper()] = addr.strip()
    return wallets


# Pvz.: WALLETS=BTC:bc1q...,USDT-TRC20:TXyz...,ETH:0xabc...
WALLETS = parse_wallets(os.getenv("WALLETS", "SOL:Hcmgr8XKFkHaZ1gDRWficrvkUWQJ41pzgRvqThnYnHQw"))

COINGECKO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "USDT": "tether", "USDC": "usd-coin",
    "LTC": "litecoin", "TON": "the-open-network", "SOL": "solana", "TRX": "tron",
    "XMR": "monero", "DOGE": "dogecoin", "BNB": "binancecoin",
}
DECIMALS = {
    "BTC": 8, "ETH": 6, "USDT": 2, "USDC": 2, "LTC": 6, "TON": 3,
    "SOL": 5, "TRX": 2, "XMR": 6, "DOGE": 2, "BNB": 6,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("shopbot")

# ───────────────────────── Duomenų bazė ─────────────────────────
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price_cents INTEGER NOT NULL,
    stock INTEGER NOT NULL DEFAULT 0,
    photo_id TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS cart (
    user_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL,
    PRIMARY KEY (user_id, product_id)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    address TEXT NOT NULL,
    comment TEXT,
    shipping_cents INTEGER NOT NULL DEFAULT 0,
    total_cents INTEGER NOT NULL,
    coin TEXT NOT NULL,
    crypto_amount TEXT,
    wallet TEXT NOT NULL,
    tx_hash TEXT,
    status TEXT NOT NULL,
    tracking TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_items (
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    qty INTEGER NOT NULL
);
"""


def init_db():
    conn.executescript(SCHEMA)
    conn.commit()


def q(sql, params=()):
    return conn.execute(sql, params).fetchall()


def q1(sql, params=()):
    return conn.execute(sql, params).fetchone()


def ex(sql, params=()):
    cur = conn.execute(sql, params)
    conn.commit()
    return cur


# ───────────────────────── Pagalbinės ─────────────────────────
def esc(s) -> str:
    return html.escape(str(s or ""))


def fmt(cents: int) -> str:
    return f"{cents / 100:.2f} €".replace(".", ",")


def parse_price(s: str) -> int | None:
    try:
        v = float(s.strip().replace(",", ".").replace("€", ""))
    except ValueError:
        return None
    return round(v * 100) if v >= 0 else None


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


STATUS = {
    "awaiting_payment": "⏳ Laukiama apmokėjimo",
    "checking": "🔎 Tikrinamas mokėjimas",
    "paid": "✅ Apmokėta, ruošiama siunta",
    "shipped": "🚚 Išsiųsta",
    "cancelled": "❌ Atšaukta",
}

_rate_cache: dict[str, tuple[float, float]] = {}


async def get_rate_eur(coin: str) -> float | None:
    """1 monetos kaina eurais (CoinGecko, 5 min. talpykla)."""
    base = coin.split("-")[0]
    cg = COINGECKO_IDS.get(base)
    if not cg:
        return None
    cached = _rate_cache.get(cg)
    if cached and time.time() - cached[1] < 300:
        return cached[0]
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={cg}&vs_currencies=eur"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(url) as r:
                data = await r.json()
        rate = float(data[cg]["eur"])
        _rate_cache[cg] = (rate, time.time())
        return rate
    except Exception as e:  # noqa: BLE001
        log.warning("Nepavyko gauti %s kurso: %s", coin, e)
        return None


async def crypto_amount(coin: str, cents: int) -> str | None:
    rate = await get_rate_eur(coin)
    if not rate:
        return None
    base = coin.split("-")[0]
    d = DECIMALS.get(base, 6)
    return f"{(cents / 100) / rate:.{d}f}"


async def notify_admins(bot: Bot, text: str, kb: InlineKeyboardMarkup | None = None):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, reply_markup=kb)
        except Exception as e:  # noqa: BLE001
            log.warning("Nepavyko pranešti adminui %s: %s", aid, e)


async def safe_edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        await cb.message.answer(text, reply_markup=kb)


# ───────────────────────── Klaviatūros ─────────────────────────
BTN_CATALOG = "🛍 Katalogas"
BTN_CART = "🛒 Krepšelis"
BTN_ORDERS = "📦 Mano užsakymai"
BTN_HELP = "ℹ️ Pagalba"
BTN_CANCEL = "❌ Atšaukti"
BTN_SKIP = "⏭ Praleisti"
BTN_CONTACT = "📱 Siųsti mano numerį"

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_CATALOG), KeyboardButton(text=BTN_CART)],
        [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)
cancel_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)
phone_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_CONTACT, request_contact=True)], [KeyboardButton(text=BTN_CANCEL)]],
    resize_keyboard=True,
)
skip_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_SKIP)], [KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True
)


def admin_order_kb(oid: int, status: str) -> InlineKeyboardMarkup | None:
    b = InlineKeyboardBuilder()
    if status in ("awaiting_payment", "checking"):
        b.button(text="✅ Patvirtinti apmokėjimą", callback_data=f"a_ok:{oid}")
        b.button(text="❌ Atšaukti užsakymą", callback_data=f"a_rej:{oid}")
    elif status == "paid":
        b.button(text="🚚 Pažymėti išsiųstu", callback_data=f"a_ship:{oid}")
        b.button(text="❌ Atšaukti užsakymą", callback_data=f"a_rej:{oid}")
    else:
        return None
    b.adjust(1)
    return b.as_markup()


def customer_order_kb(oid: int, status: str) -> InlineKeyboardMarkup | None:
    if status != "awaiting_payment":
        return None
    b = InlineKeyboardBuilder()
    b.button(text="✅ Sumokėjau", callback_data=f"paid:{oid}")
    b.button(text="❌ Atšaukti užsakymą", callback_data=f"ucancel:{oid}")
    b.adjust(1)
    return b.as_markup()


# ───────────────────────── Užsakymų logika ─────────────────────────
def order_text(o, for_admin: bool = False) -> str:
    items = q("SELECT * FROM order_items WHERE order_id=?", (o["id"],))
    lines = [f"🧾 <b>Užsakymas #{o['id']}</b> — {STATUS.get(o['status'], o['status'])}", f"Data: {o['created_at']}", ""]
    for it in items:
        lines.append(f"• {esc(it['name'])} × {it['qty']} = {fmt(it['price_cents'] * it['qty'])}")
    if o["shipping_cents"]:
        lines.append(f"• Pristatymas: {fmt(o['shipping_cents'])}")
    lines.append(f"<b>Iš viso: {fmt(o['total_cents'])}</b>")
    pay = f"Mokėjimas: {esc(o['coin'])}"
    if o["crypto_amount"]:
        pay += f" — <code>{esc(o['crypto_amount'])}</code> {esc(o['coin'])}"
    lines.append(pay)
    if o["tx_hash"]:
        lines.append(f"TX: <code>{esc(o['tx_hash'])}</code>")
    if o["tracking"]:
        lines.append(f"Siuntos sekimo nr.: <code>{esc(o['tracking'])}</code>")
    if for_admin:
        user_link = f'<a href="tg://user?id={o["user_id"]}">{esc(o["full_name"])}</a>'
        if o["username"]:
            user_link += f" (@{esc(o['username'])})"
        lines += [
            "",
            f"👤 Pirkėjas: {user_link}",
            f"📞 Tel.: {esc(o['phone'])}",
            f"📍 Adresas: {esc(o['address'])}",
        ]
        if o["comment"]:
            lines.append(f"💬 Pastaba: {esc(o['comment'])}")
        lines.append(f"Piniginė: <code>{esc(o['wallet'])}</code>")
    return "\n".join(lines)


def get_order(oid: int):
    return q1("SELECT * FROM orders WHERE id=?", (oid,))


def cancel_order(oid: int) -> bool:
    o = get_order(oid)
    if not o or o["status"] in ("cancelled", "shipped"):
        return False
    for it in q("SELECT product_id, qty FROM order_items WHERE order_id=?", (oid,)):
        conn.execute("UPDATE products SET stock = stock + ? WHERE id=?", (it["qty"], it["product_id"]))
    conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
    conn.commit()
    return True


def cart_rows(uid: int):
    return q(
        "SELECT c.product_id, c.qty, p.name, p.price_cents, p.stock, p.active "
        "FROM cart c JOIN products p ON p.id=c.product_id WHERE c.user_id=? ORDER BY p.name",
        (uid,),
    )


def render_cart(uid: int) -> tuple[str, InlineKeyboardMarkup | None]:
    rows = cart_rows(uid)
    if not rows:
        return "🛒 Jūsų krepšelis tuščias.", None
    lines = ["🛒 <b>Jūsų krepšelis</b>", ""]
    subtotal = 0
    b = InlineKeyboardBuilder()
    for r in rows:
        s = r["price_cents"] * r["qty"]
        subtotal += s
        warn = "" if r["active"] and r["qty"] <= r["stock"] else " ⚠️ nepakanka sandėlyje"
        lines.append(f"• {esc(r['name'])} × {r['qty']} = {fmt(s)}{warn}")
        b.button(text="➖", callback_data=f"cq:{r['product_id']}:-1")
        b.button(text=f"{r['name'][:18]} × {r['qty']}", callback_data="noop")
        b.button(text="➕", callback_data=f"cq:{r['product_id']}:1")
    lines.append("")
    if SHIPPING_CENTS:
        lines.append(f"Prekės: {fmt(subtotal)}")
        lines.append(f"Pristatymas: {fmt(SHIPPING_CENTS)}")
    lines.append(f"<b>Iš viso: {fmt(subtotal + SHIPPING_CENTS)}</b>")
    b.button(text="🗑 Išvalyti", callback_data="cart_clear")
    b.button(text="✅ Pateikti užsakymą", callback_data="checkout")
    b.adjust(*([3] * len(rows)), 2)
    return "\n".join(lines), b.as_markup()


def cart_problem(uid: int) -> str | None:
    rows = cart_rows(uid)
    if not rows:
        return "Jūsų krepšelis tuščias."
    for r in rows:
        if not r["active"] or r["qty"] > r["stock"]:
            return f"Prekės „{r['name']}“ nebėra pakankamai sandėlyje. Pakoreguokite krepšelį."
    return None


# ───────────────────────── Filtrai ir būsenos ─────────────────────────
class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(event.from_user) and event.from_user.id in ADMIN_IDS


class Checkout(StatesGroup):
    name = State()
    phone = State()
    address = State()
    comment = State()
    coin = State()


class PayTx(StatesGroup):
    tx = State()


class AddProduct(StatesGroup):
    category = State()
    name = State()
    description = State()
    price = State()
    stock = State()
    photo = State()


class AdminShip(StatesGroup):
    tracking = State()


class AdminPhoto(StatesGroup):
    photo = State()


common = Router(name="common")
admin = Router(name="admin")
admin.message.filter(IsAdmin())
admin.callback_query.filter(IsAdmin())
shop = Router(name="shop")


# ───────────────────────── Bendri (meniu, atšaukimas) ─────────────────────────
@common.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    text = f"Sveiki atvykę į <b>{esc(SHOP_NAME)}</b>! 👋\nPasirinkite veiksmą meniu apačioje."
    if m.from_user.id in ADMIN_IDS:
        text += "\n\n🔧 Jūs esate administratorius — komandų sąrašas: /admin"
    await m.answer(text, reply_markup=main_kb)


@common.message(Command("cancel"))
@common.message(F.text == BTN_CANCEL)
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Veiksmas atšauktas.", reply_markup=main_kb)


@common.message(F.text == BTN_CATALOG)
async def menu_catalog(m: Message, state: FSMContext):
    await state.clear()
    text, kb = render_categories()
    await m.answer(text, reply_markup=kb)


@common.message(F.text == BTN_CART)
async def menu_cart(m: Message, state: FSMContext):
    await state.clear()
    text, kb = render_cart(m.from_user.id)
    await m.answer(text, reply_markup=kb)


@common.message(F.text == BTN_ORDERS)
async def menu_orders(m: Message, state: FSMContext):
    await state.clear()
    rows = q("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (m.from_user.id,))
    if not rows:
        await m.answer("Jūs dar neturite užsakymų.", reply_markup=main_kb)
        return
    b = InlineKeyboardBuilder()
    lines = ["📦 <b>Jūsų užsakymai</b>", ""]
    for o in rows:
        lines.append(f"#{o['id']} · {o['created_at']} · {fmt(o['total_cents'])} · {STATUS.get(o['status'])}")
        b.button(text=f"Užsakymas #{o['id']}", callback_data=f"my:{o['id']}")
    b.adjust(2)
    await m.answer("\n".join(lines), reply_markup=b.as_markup())


@common.message(F.text == BTN_HELP)
@common.message(Command("help"))
async def menu_help(m: Message, state: FSMContext):
    await state.clear()
    text = (
        "ℹ️ <b>Kaip pirkti</b>\n\n"
        "1. Atidarykite „Katalogas“ ir pridėkite prekes į krepšelį.\n"
        "2. Krepšelyje paspauskite „Pateikti užsakymą“ ir nurodykite pristatymo duomenis.\n"
        "3. Pasirinkite kriptovaliutą ir perveskite nurodytą sumą į pateiktą adresą.\n"
        "4. Paspauskite „Sumokėjau“ ir atsiųskite transakcijos ID (hash).\n"
        "5. Patvirtinus mokėjimą prekę išsiųsime ir atsiųsime siuntos sekimo numerį."
    )
    if SUPPORT_CONTACT:
        text += f"\n\nKlausimai: {esc(SUPPORT_CONTACT)}"
    await m.answer(text, reply_markup=main_kb)


# ───────────────────────── Katalogas ─────────────────────────
def render_categories() -> tuple[str, InlineKeyboardMarkup | None]:
    cats = q(
        "SELECT c.id, c.name, COUNT(p.id) AS n FROM categories c "
        "LEFT JOIN products p ON p.category_id=c.id AND p.active=1 "
        "GROUP BY c.id ORDER BY c.name"
    )
    cats = [c for c in cats if c["n"] > 0]
    if not cats:
        return "Katalogas kol kas tuščias. Užsukite vėliau!", None
    b = InlineKeyboardBuilder()
    for c in cats:
        b.button(text=f"{c['name']} ({c['n']})", callback_data=f"cat:{c['id']}")
    b.adjust(1)
    return "🛍 <b>Pasirinkite kategoriją:</b>", b.as_markup()


@shop.callback_query(F.data == "cats")
async def cb_cats(cb: CallbackQuery):
    text, kb = render_categories()
    await safe_edit(cb, text, kb)
    await cb.answer()


@shop.callback_query(F.data.startswith("cat:"))
async def cb_category(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    cat = q1("SELECT * FROM categories WHERE id=?", (cid,))
    prods = q("SELECT * FROM products WHERE category_id=? AND active=1 ORDER BY name", (cid,))
    b = InlineKeyboardBuilder()
    for p in prods:
        sold = " (išparduota)" if p["stock"] <= 0 else ""
        b.button(text=f"{p['name']} — {fmt(p['price_cents'])}{sold}", callback_data=f"prod:{p['id']}")
    b.button(text="⬅️ Atgal", callback_data="cats")
    b.adjust(1)
    title = esc(cat["name"]) if cat else "Kategorija"
    text = f"<b>{title}</b>\n\nPasirinkite prekę:" if prods else f"<b>{title}</b>\n\nŠioje kategorijoje prekių nėra."
    await safe_edit(cb, text, b.as_markup())
    await cb.answer()


@shop.callback_query(F.data.startswith("prod:"))
async def cb_product(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    p = q1("SELECT * FROM products WHERE id=? AND active=1", (pid,))
    if not p:
        await cb.answer("Prekė nebepasiekiama.", show_alert=True)
        return
    stock = f"Sandėlyje: {p['stock']} vnt." if p["stock"] > 0 else "❗ Išparduota"
    desc = p["description"] or ""
    if len(desc) > 700:
        desc = desc[:700] + "…"
    text = f"<b>{esc(p['name'])}</b>\n\n{esc(desc)}\n\nKaina: <b>{fmt(p['price_cents'])}</b>\n{stock}"
    b = InlineKeyboardBuilder()
    if p["stock"] > 0:
        b.button(text="🛒 Į krepšelį", callback_data=f"add:{pid}")
    b.button(text="⬅️ Atgal", callback_data=f"cat:{p['category_id']}")
    b.adjust(1)
    if p["photo_id"]:
        await cb.message.answer_photo(p["photo_id"], caption=text, reply_markup=b.as_markup())
    else:
        await cb.message.answer(text, reply_markup=b.as_markup())
    await cb.answer()


@shop.callback_query(F.data.startswith("add:"))
async def cb_add(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    uid = cb.from_user.id
    p = q1("SELECT * FROM products WHERE id=? AND active=1", (pid,))
    if not p:
        await cb.answer("Prekė nebepasiekiama.", show_alert=True)
        return
    row = q1("SELECT qty FROM cart WHERE user_id=? AND product_id=?", (uid, pid))
    cur = row["qty"] if row else 0
    if cur + 1 > p["stock"]:
        await cb.answer("Daugiau šios prekės sandėlyje nėra.", show_alert=True)
        return
    ex(
        "INSERT INTO cart(user_id, product_id, qty) VALUES(?,?,1) "
        "ON CONFLICT(user_id, product_id) DO UPDATE SET qty = qty + 1",
        (uid, pid),
    )
    await cb.answer(f"Pridėta į krepšelį ({cur + 1} vnt.)")


# ───────────────────────── Krepšelis ─────────────────────────
@shop.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@shop.callback_query(F.data.startswith("cq:"))
async def cb_cart_qty(cb: CallbackQuery):
    _, pid, delta = cb.data.split(":")
    pid, delta = int(pid), int(delta)
    uid = cb.from_user.id
    row = q1(
        "SELECT c.qty, p.stock FROM cart c JOIN products p ON p.id=c.product_id WHERE c.user_id=? AND c.product_id=?",
        (uid, pid),
    )
    if not row:
        await cb.answer()
        return
    new = row["qty"] + delta
    if delta > 0 and new > row["stock"]:
        await cb.answer("Daugiau šios prekės sandėlyje nėra.", show_alert=True)
        return
    if new <= 0:
        ex("DELETE FROM cart WHERE user_id=? AND product_id=?", (uid, pid))
    else:
        ex("UPDATE cart SET qty=? WHERE user_id=? AND product_id=?", (new, uid, pid))
    text, kb = render_cart(uid)
    await safe_edit(cb, text, kb)
    await cb.answer()


@shop.callback_query(F.data == "cart_clear")
async def cb_cart_clear(cb: CallbackQuery):
    ex("DELETE FROM cart WHERE user_id=?", (cb.from_user.id,))
    await safe_edit(cb, "🗑 Krepšelis išvalytas.")
    await cb.answer()


# ───────────────────────── Užsakymo pateikimas ─────────────────────────
@shop.callback_query(F.data == "checkout")
async def cb_checkout(cb: CallbackQuery, state: FSMContext):
    if not WALLETS:
        await cb.answer("Mokėjimai dar nesukonfigūruoti. Susisiekite su pardavėju.", show_alert=True)
        return
    problem = cart_problem(cb.from_user.id)
    if problem:
        await cb.answer(problem, show_alert=True)
        return
    await state.set_state(Checkout.name)
    await cb.message.answer("👤 Įveskite vardą ir pavardę:", reply_markup=cancel_kb)
    await cb.answer()


@shop.message(Checkout.name, F.text)
async def st_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not 2 <= len(name) <= 100:
        await m.answer("Netinkamas vardas, bandykite dar kartą.")
        return
    await state.update_data(full_name=name)
    await state.set_state(Checkout.phone)
    await m.answer("📞 Įveskite telefono numerį arba paspauskite mygtuką žemiau:", reply_markup=phone_kb)


@shop.message(Checkout.phone, F.contact)
@shop.message(Checkout.phone, F.text)
async def st_phone(m: Message, state: FSMContext):
    phone = m.contact.phone_number if m.contact else m.text.strip()
    if not re.fullmatch(r"\+?[\d\s\-()]{6,20}", phone):
        await m.answer("Netinkamas telefono numeris, bandykite dar kartą (pvz. +37060000000).")
        return
    await state.update_data(phone=phone)
    await state.set_state(Checkout.address)
    await m.answer(
        "📍 Įveskite pristatymo adresą (gatvė, namo nr., miestas, pašto kodas, šalis) arba paštomato adresą:",
        reply_markup=cancel_kb,
    )


@shop.message(Checkout.address, F.text)
async def st_address(m: Message, state: FSMContext):
    addr = m.text.strip()
    if not 5 <= len(addr) <= 300:
        await m.answer("Adresas per trumpas arba per ilgas, bandykite dar kartą.")
        return
    await state.update_data(address=addr)
    await state.set_state(Checkout.comment)
    await m.answer("💬 Pastabos užsakymui? Parašykite arba paspauskite „Praleisti“.", reply_markup=skip_kb)


@shop.message(Checkout.comment, F.text)
async def st_comment(m: Message, state: FSMContext):
    comment = "" if m.text == BTN_SKIP else m.text.strip()[:500]
    await state.update_data(comment=comment)
    await state.set_state(Checkout.coin)
    b = InlineKeyboardBuilder()
    for coin in WALLETS:
        b.button(text=coin, callback_data=f"coin:{coin}")
    b.adjust(2)
    await m.answer("Beveik baigta! ✅", reply_markup=main_kb)
    await m.answer("💰 Pasirinkite kriptovaliutą mokėjimui:", reply_markup=b.as_markup())


@shop.callback_query(Checkout.coin, F.data.startswith("coin:"))
async def cb_coin(cb: CallbackQuery, state: FSMContext):
    coin = cb.data.split(":", 1)[1]
    wallet = WALLETS.get(coin)
    if not wallet:
        await cb.answer("Ši valiuta nepasiekiama.", show_alert=True)
        return
    uid = cb.from_user.id
    problem = cart_problem(uid)
    if problem:
        await state.clear()
        await cb.answer(problem, show_alert=True)
        return
    data = await state.get_data()
    rows = cart_rows(uid)
    subtotal = sum(r["price_cents"] * r["qty"] for r in rows)
    total = subtotal + SHIPPING_CENTS
    amount = await crypto_amount(coin, total)

    # Rezervuojame prekes ir sukuriame užsakymą vienoje transakcijoje
    try:
        for r in rows:
            cur = conn.execute(
                "UPDATE products SET stock = stock - ? WHERE id=? AND stock >= ?",
                (r["qty"], r["product_id"], r["qty"]),
            )
            if cur.rowcount == 0:
                raise ValueError(r["name"])
        cur = conn.execute(
            "INSERT INTO orders(user_id, username, full_name, phone, address, comment, shipping_cents, "
            "total_cents, coin, crypto_amount, wallet, status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                uid, cb.from_user.username, data["full_name"], data["phone"], data["address"],
                data.get("comment", ""), SHIPPING_CENTS, total, coin, amount, wallet,
                "awaiting_payment", now_str(),
            ),
        )
        oid = cur.lastrowid
        for r in rows:
            conn.execute(
                "INSERT INTO order_items(order_id, product_id, name, price_cents, qty) VALUES(?,?,?,?,?)",
                (oid, r["product_id"], r["name"], r["price_cents"], r["qty"]),
            )
        conn.execute("DELETE FROM cart WHERE user_id=?", (uid,))
        conn.commit()
    except ValueError as e:
        conn.rollback()
        await state.clear()
        await cb.answer(f"Prekės „{e}“ nebėra pakankamai sandėlyje.", show_alert=True)
        return

    await state.clear()
    if amount:
        pay_line = (
            f"Mokėti: <code>{amount}</code> <b>{esc(coin)}</b>\n"
            f"<i>Suma apskaičiuota pagal dabartinį kursą.</i>"
        )
    else:
        pay_line = (
            f"Mokėti: {fmt(total)} vertės <b>{esc(coin)}</b>\n"
            f"<i>Kurso gauti nepavyko — tikslią sumą patikslins pardavėjas.</i>"
        )
    text = (
        f"🧾 <b>Užsakymas #{oid} sukurtas</b>\n\n"
        f"Suma: <b>{fmt(total)}</b>\n{pay_line}\n\n"
        f"Piniginės adresas ({esc(coin)}):\n<code>{esc(wallet)}</code>\n\n"
        f"⚠️ Siųskite tik <b>{esc(coin)}</b> į šį adresą. Atlikę pervedimą paspauskite „Sumokėjau“ "
        f"ir atsiųskite transakcijos ID (hash)."
    )
    await safe_edit(cb, text, customer_order_kb(oid, "awaiting_payment"))
    await cb.answer()
    o = get_order(oid)
    await notify_admins(cb.bot, "🆕 <b>Naujas užsakymas</b>\n\n" + order_text(o, True), admin_order_kb(oid, o["status"]))


@shop.callback_query(F.data.startswith("my:"))
async def cb_my_order(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    o = get_order(oid)
    if not o or o["user_id"] != cb.from_user.id:
        await cb.answer("Užsakymas nerastas.", show_alert=True)
        return
    text = order_text(o)
    if o["status"] == "awaiting_payment":
        text += f"\n\nPiniginės adresas ({esc(o['coin'])}):\n<code>{esc(o['wallet'])}</code>"
    await cb.message.answer(text, reply_markup=customer_order_kb(oid, o["status"]))
    await cb.answer()


@shop.callback_query(F.data.startswith("paid:"))
async def cb_paid(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[1])
    o = get_order(oid)
    if not o or o["user_id"] != cb.from_user.id or o["status"] != "awaiting_payment":
        await cb.answer("Šio užsakymo apmokėti nebegalima.", show_alert=True)
        return
    await state.set_state(PayTx.tx)
    await state.update_data(order_id=oid)
    await cb.message.answer(f"🔗 Atsiųskite užsakymo #{oid} transakcijos ID (hash):", reply_markup=cancel_kb)
    await cb.answer()


@shop.message(PayTx.tx, F.text)
async def st_tx(m: Message, state: FSMContext):
    tx = m.text.strip()
    if not re.fullmatch(r"[A-Za-z0-9:_\-]{10,200}", tx):
        await m.answer("Tai neatrodo kaip transakcijos ID. Nukopijuokite jį iš piniginės ir atsiųskite dar kartą.")
        return
    oid = (await state.get_data())["order_id"]
    await state.clear()
    o = get_order(oid)
    if not o or o["status"] != "awaiting_payment":
        await m.answer("Šio užsakymo apmokėti nebegalima.", reply_markup=main_kb)
        return
    ex("UPDATE orders SET tx_hash=?, status='checking' WHERE id=?", (tx, oid))
    await m.answer(
        f"Ačiū! 🙏 Užsakymo #{oid} mokėjimas tikrinamas. Pranešime, kai jis bus patvirtintas.",
        reply_markup=main_kb,
    )
    o = get_order(oid)
    await notify_admins(m.bot, "💸 <b>Pirkėjas pranešė apie mokėjimą</b>\n\n" + order_text(o, True), admin_order_kb(oid, o["status"]))


@shop.callback_query(F.data.startswith("ucancel:"))
async def cb_user_cancel(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    o = get_order(oid)
    if not o or o["user_id"] != cb.from_user.id or o["status"] != "awaiting_payment":
        await cb.answer("Šio užsakymo atšaukti nebegalima.", show_alert=True)
        return
    cancel_order(oid)
    await safe_edit(cb, f"❌ Užsakymas #{oid} atšauktas.")
    await cb.answer()
    await notify_admins(cb.bot, f"❌ Pirkėjas atšaukė užsakymą #{oid}.")


# ───────────────────────── Administravimas ─────────────────────────
ADMIN_HELP = """🔧 <b>Administratoriaus komandos</b>

<b>Kategorijos</b>
/addcat Pavadinimas — nauja kategorija
/cats — kategorijų sąrašas
/delcat ID — ištrinti tuščią kategoriją

<b>Prekės</b>
/addproduct — pridėti prekę (žingsnis po žingsnio)
/products — visų prekių sąrašas
/setprice ID kaina — pvz. <code>/setprice 3 19,99</code>
/setstock ID kiekis — pvz. <code>/setstock 3 25</code>
/setphoto ID — pakeisti nuotrauką
/hide ID · /show ID — paslėpti / rodyti kataloge

<b>Užsakymai</b>
/orders — paskutiniai užsakymai
/orders checking — tik laukiantys patvirtinimo
/order ID — užsakymo informacija ir veiksmai

/cancel — nutraukti vykdomą veiksmą"""


@admin.message(Command("admin"))
async def a_help(m: Message):
    await m.answer(ADMIN_HELP)


@admin.message(Command("addcat"))
async def a_addcat(m: Message, command: CommandObject):
    name = (command.args or "").strip()
    if not name:
        await m.answer("Naudojimas: /addcat Pavadinimas")
        return
    cid = ex("INSERT INTO categories(name) VALUES(?)", (name,)).lastrowid
    await m.answer(f"✅ Kategorija sukurta: #{cid} {esc(name)}")


@admin.message(Command("cats"))
async def a_cats(m: Message):
    rows = q(
        "SELECT c.id, c.name, COUNT(p.id) AS n FROM categories c "
        "LEFT JOIN products p ON p.category_id=c.id GROUP BY c.id ORDER BY c.id"
    )
    if not rows:
        await m.answer("Kategorijų nėra. Sukurkite: /addcat Pavadinimas")
        return
    await m.answer("\n".join(f"#{r['id']} {esc(r['name'])} — prekių: {r['n']}" for r in rows))


@admin.message(Command("delcat"))
async def a_delcat(m: Message, command: CommandObject):
    try:
        cid = int((command.args or "").strip())
    except ValueError:
        await m.answer("Naudojimas: /delcat ID")
        return
    if q1("SELECT 1 FROM products WHERE category_id=?", (cid,)):
        await m.answer("Kategorijoje yra prekių — pirmiausia jas perkelkite arba paslėpkite.")
        return
    cur = ex("DELETE FROM categories WHERE id=?", (cid,))
    await m.answer("✅ Ištrinta." if cur.rowcount else "Kategorija nerasta.")


@admin.message(Command("addproduct"))
async def a_addproduct(m: Message, state: FSMContext):
    cats = q("SELECT * FROM categories ORDER BY name")
    if not cats:
        await m.answer("Pirmiausia sukurkite kategoriją: /addcat Pavadinimas")
        return
    b = InlineKeyboardBuilder()
    for c in cats:
        b.button(text=c["name"], callback_data=f"ap_cat:{c['id']}")
    b.adjust(2)
    await state.set_state(AddProduct.category)
    await m.answer("Pasirinkite kategoriją naujai prekei:", reply_markup=b.as_markup())


@admin.callback_query(AddProduct.category, F.data.startswith("ap_cat:"))
async def a_ap_cat(cb: CallbackQuery, state: FSMContext):
    await state.update_data(category_id=int(cb.data.split(":")[1]))
    await state.set_state(AddProduct.name)
    await cb.message.answer("Prekės pavadinimas:", reply_markup=cancel_kb)
    await cb.answer()


@admin.message(AddProduct.name, F.text)
async def a_ap_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()[:100])
    await state.set_state(AddProduct.description)
    await m.answer("Aprašymas (arba „-“, jei nereikia):")


@admin.message(AddProduct.description, F.text)
async def a_ap_desc(m: Message, state: FSMContext):
    desc = "" if m.text.strip() == "-" else m.text.strip()
    await state.update_data(description=desc)
    await state.set_state(AddProduct.price)
    await m.answer("Kaina eurais (pvz. 19,99):")


@admin.message(AddProduct.price, F.text)
async def a_ap_price(m: Message, state: FSMContext):
    cents = parse_price(m.text)
    if cents is None or cents == 0:
        await m.answer("Netinkama kaina. Pvz.: 19,99")
        return
    await state.update_data(price_cents=cents)
    await state.set_state(AddProduct.stock)
    await m.answer("Kiekis sandėlyje (sveikas skaičius):")


@admin.message(AddProduct.stock, F.text)
async def a_ap_stock(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        await m.answer("Įveskite sveiką skaičių, pvz. 10")
        return
    await state.update_data(stock=int(m.text.strip()))
    await state.set_state(AddProduct.photo)
    await m.answer("Atsiųskite prekės nuotrauką (arba „-“, jei be nuotraukos):")


@admin.message(AddProduct.photo, F.photo | F.text)
async def a_ap_photo(m: Message, state: FSMContext):
    if m.photo:
        photo_id = m.photo[-1].file_id
    elif m.text and m.text.strip() == "-":
        photo_id = None
    else:
        await m.answer("Atsiųskite nuotrauką arba „-“.")
        return
    d = await state.get_data()
    await state.clear()
    pid = ex(
        "INSERT INTO products(category_id, name, description, price_cents, stock, photo_id) VALUES(?,?,?,?,?,?)",
        (d["category_id"], d["name"], d["description"], d["price_cents"], d["stock"], photo_id),
    ).lastrowid
    await m.answer(
        f"✅ Prekė pridėta: #{pid} {esc(d['name'])} — {fmt(d['price_cents'])}, {d['stock']} vnt.",
        reply_markup=main_kb,
    )


@admin.message(Command("products"))
async def a_products(m: Message):
    rows = q(
        "SELECT p.*, c.name AS cat FROM products p JOIN categories c ON c.id=p.category_id ORDER BY c.name, p.name"
    )
    if not rows:
        await m.answer("Prekių nėra. Pridėkite: /addproduct")
        return
    lines = []
    for p in rows:
        hidden = " 🙈 paslėpta" if not p["active"] else ""
        lines.append(f"#{p['id']} [{esc(p['cat'])}] {esc(p['name'])} — {fmt(p['price_cents'])} — {p['stock']} vnt.{hidden}")
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await m.answer(text[i:i + 4000])


def _two_args(command: CommandObject) -> tuple[int, str] | None:
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0]), parts[1]


@admin.message(Command("setprice"))
async def a_setprice(m: Message, command: CommandObject):
    args = _two_args(command)
    cents = parse_price(args[1]) if args else None
    if not args or not cents:
        await m.answer("Naudojimas: /setprice ID kaina (pvz. /setprice 3 19,99)")
        return
    cur = ex("UPDATE products SET price_cents=? WHERE id=?", (cents, args[0]))
    await m.answer(f"✅ Kaina pakeista: {fmt(cents)}" if cur.rowcount else "Prekė nerasta.")


@admin.message(Command("setstock"))
async def a_setstock(m: Message, command: CommandObject):
    args = _two_args(command)
    if not args or not args[1].isdigit():
        await m.answer("Naudojimas: /setstock ID kiekis (pvz. /setstock 3 25)")
        return
    cur = ex("UPDATE products SET stock=? WHERE id=?", (int(args[1]), args[0]))
    await m.answer(f"✅ Likutis pakeistas: {args[1]} vnt." if cur.rowcount else "Prekė nerasta.")


@admin.message(Command("hide", "show"))
async def a_hide_show(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await m.answer(f"Naudojimas: /{command.command} ID")
        return
    active = 1 if command.command == "show" else 0
    cur = ex("UPDATE products SET active=? WHERE id=?", (active, int(arg)))
    if active == 0:
        ex("DELETE FROM cart WHERE product_id=?", (int(arg),))
    if not cur.rowcount:
        await m.answer("Prekė nerasta.")
    else:
        await m.answer("✅ Prekė rodoma kataloge." if active else "✅ Prekė paslėpta.")


@admin.message(Command("setphoto"))
async def a_setphoto(m: Message, command: CommandObject, state: FSMContext):
    arg = (command.args or "").strip()
    if not arg.isdigit() or not q1("SELECT 1 FROM products WHERE id=?", (int(arg),)):
        await m.answer("Naudojimas: /setphoto ID")
        return
    await state.set_state(AdminPhoto.photo)
    await state.update_data(pid=int(arg))
    await m.answer("Atsiųskite naują nuotrauką (arba „-“, kad pašalintumėte):", reply_markup=cancel_kb)


@admin.message(AdminPhoto.photo, F.photo | F.text)
async def a_setphoto_recv(m: Message, state: FSMContext):
    if m.photo:
        photo_id = m.photo[-1].file_id
    elif m.text and m.text.strip() == "-":
        photo_id = None
    else:
        await m.answer("Atsiųskite nuotrauką arba „-“.")
        return
    pid = (await state.get_data())["pid"]
    await state.clear()
    ex("UPDATE products SET photo_id=? WHERE id=?", (photo_id, pid))
    await m.answer("✅ Nuotrauka atnaujinta.", reply_markup=main_kb)


@admin.message(Command("orders"))
async def a_orders(m: Message, command: CommandObject):
    status = (command.args or "").strip()
    if status and status not in STATUS:
        await m.answer("Galimos būsenos: " + ", ".join(STATUS))
        return
    if status:
        rows = q("SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT 20", (status,))
    else:
        rows = q("SELECT * FROM orders ORDER BY id DESC LIMIT 20")
    if not rows:
        await m.answer("Užsakymų nėra.")
        return
    b = InlineKeyboardBuilder()
    lines = []
    for o in rows:
        lines.append(f"#{o['id']} · {o['created_at']} · {esc(o['full_name'])} · {fmt(o['total_cents'])} · {STATUS[o['status']]}")
        b.button(text=f"#{o['id']}", callback_data=f"a_view:{o['id']}")
    b.adjust(5)
    await m.answer("\n".join(lines), reply_markup=b.as_markup())


async def _send_admin_order(target: Message, oid: int):
    o = get_order(oid)
    if not o:
        await target.answer("Užsakymas nerastas.")
        return
    await target.answer(order_text(o, True), reply_markup=admin_order_kb(oid, o["status"]))


@admin.message(Command("order"))
async def a_order(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await m.answer("Naudojimas: /order ID")
        return
    await _send_admin_order(m, int(arg))


@admin.callback_query(F.data.startswith("a_view:"))
async def a_view(cb: CallbackQuery):
    await _send_admin_order(cb.message, int(cb.data.split(":")[1]))
    await cb.answer()


@admin.callback_query(F.data.startswith("a_ok:"))
async def a_ok(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    o = get_order(oid)
    if not o or o["status"] not in ("awaiting_payment", "checking"):
        await cb.answer("Užsakymo būsena jau pasikeitė.", show_alert=True)
        return
    ex("UPDATE orders SET status='paid' WHERE id=?", (oid,))
    o = get_order(oid)
    await safe_edit(cb, order_text(o, True), admin_order_kb(oid, o["status"]))
    await cb.answer("Apmokėjimas patvirtintas")
    try:
        await cb.bot.send_message(o["user_id"], f"✅ Užsakymo #{oid} apmokėjimas patvirtintas! Ruošiame siuntą.")
    except Exception as e:  # noqa: BLE001
        log.warning("Nepavyko pranešti pirkėjui: %s", e)


@admin.callback_query(F.data.startswith("a_rej:"))
async def a_rej(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    if not cancel_order(oid):
        await cb.answer("Šio užsakymo atšaukti negalima.", show_alert=True)
        return
    o = get_order(oid)
    await safe_edit(cb, order_text(o, True))
    await cb.answer("Užsakymas atšauktas")
    msg = f"❌ Užsakymas #{oid} atšauktas."
    if SUPPORT_CONTACT:
        msg += f"\nJei turite klausimų: {esc(SUPPORT_CONTACT)}"
    try:
        await cb.bot.send_message(o["user_id"], msg)
    except Exception as e:  # noqa: BLE001
        log.warning("Nepavyko pranešti pirkėjui: %s", e)


@admin.callback_query(F.data.startswith("a_ship:"))
async def a_ship(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[1])
    o = get_order(oid)
    if not o or o["status"] != "paid":
        await cb.answer("Išsiųsti galima tik apmokėtą užsakymą.", show_alert=True)
        return
    await state.set_state(AdminShip.tracking)
    await state.update_data(order_id=oid)
    await cb.message.answer(f"Įveskite užsakymo #{oid} siuntos sekimo numerį (arba „-“):", reply_markup=cancel_kb)
    await cb.answer()


@admin.message(AdminShip.tracking, F.text)
async def a_ship_tracking(m: Message, state: FSMContext):
    oid = (await state.get_data())["order_id"]
    await state.clear()
    tracking = None if m.text.strip() == "-" else m.text.strip()[:100]
    ex("UPDATE orders SET status='shipped', tracking=? WHERE id=?", (tracking, oid))
    o = get_order(oid)
    await m.answer(f"✅ Užsakymas #{oid} pažymėtas išsiųstu.", reply_markup=main_kb)
    msg = f"🚚 Jūsų užsakymas #{oid} išsiųstas!"
    if tracking:
        msg += f"\nSiuntos sekimo numeris: <code>{esc(tracking)}</code>"
    try:
        await m.bot.send_message(o["user_id"], msg)
    except Exception as e:  # noqa: BLE001
        log.warning("Nepavyko pranešti pirkėjui: %s", e)


# ───────────────────────── Kita ─────────────────────────
@shop.message(StateFilter(None))
async def fallback(m: Message):
    await m.answer("Naudokite meniu mygtukus apačioje 👇", reply_markup=main_kb)


@shop.message()
async def fallback_state(m: Message):
    await m.answer("Atsiųskite tekstą arba paspauskite „❌ Atšaukti“.")


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Nenurodytas BOT_TOKEN. Užpildykite .env failą (žr. .env.example).")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS nenurodyti — niekas negalės valdyti parduotuvės.")
    if not WALLETS:
        log.warning("WALLETS nenurodyti — pirkėjai negalės pateikti užsakymų.")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(common, admin, shop)
    await bot.set_my_commands([
        BotCommand(command="start", description="Pradėti"),
        BotCommand(command="help", description="Pagalba"),
        BotCommand(command="cancel", description="Atšaukti veiksmą"),
    ])
    log.info("Botas paleistas")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
