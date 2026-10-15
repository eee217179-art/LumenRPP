"""
Telegram RP-бот: персонажи, кубики, приключения, дуэли, магазин, ИИ-рассказчик,
донаты за Telegram Stars, рефералки, промокоды и админ-панель.
Стек: aiogram 3, aiosqlite, (опционально) anthropic.
"""
import asyncio
import html
import logging
import os
import random
import re
import sqlite3
import time
from collections import defaultdict, deque
from urllib.parse import quote
import subprocess
import sys

# ═══════════════════════ НАСТРОЙКИ — ВПИШИ СЮДА ═══════════════════════
BOT_TOKEN = ""      # токен от @BotFather, внутри кавычек
ANTHROPIC_API_KEY = ""               # необязательно: ключ для ИИ-рассказчика /gm
AI_MODEL = "claude-sonnet-5-5"
DB_PATH = "rp_bot.db"
ADMIN_IDS = {7759191824}             # Telegram ID админов бота (можно добавить ещё через запятую)
# ═══════════════════════════════════════════════════════════════════════

BOT_TOKEN = os.getenv("BOT_TOKEN") or BOT_TOKEN
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or ANTHROPIC_API_KEY
if os.getenv("ADMIN_IDS"):
    ADMIN_IDS |= {int(x) for x in os.getenv("ADMIN_IDS").split(",") if x.strip().isdigit()}


def _ensure(pkg, mod):
    """Ставит библиотеку сам, если хостинг её не установил."""
    try:
        __import__(mod)
        return
    except ImportError:
        pass
    for extra in ([], ["--break-system-packages"], ["--user"]):
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, *extra])
            return
        except Exception:
            continue


_ensure("aiogram>=3.13", "aiogram")
_ensure("aiosqlite", "aiosqlite")
if ANTHROPIC_API_KEY:
    _ensure("anthropic", "anthropic")


import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

logging.basicConfig(level=logging.INFO)


ai = None
if ANTHROPIC_API_KEY:
    from anthropic import AsyncAnthropic

    ai = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

router = Router()
db: aiosqlite.Connection
BOT_USERNAME = ""
BANNED: set[int] = set()

# ───────────────────────────── ДАННЫЕ ИГРЫ ─────────────────────────────

CLASSES = {
    "Воин": dict(emoji="⚔️", hp=120, strength=8, agility=4, intellect=2, main="strength"),
    "Маг": dict(emoji="🔮", hp=80, strength=2, agility=4, intellect=8, main="intellect"),
    "Плут": dict(emoji="🗡", hp=95, strength=4, agility=8, intellect=3, main="agility"),
}
# бонусы рас: (сила, ловкость, интеллект)
RACES = {
    "Человек": (1, 1, 1),
    "Эльф": (0, 2, 1),
    "Гном": (2, 0, 1),
    "Орк": (3, 0, 0),
}

# Типы предметов: potion (лечит), weapon (атака), armor (защита), accessory (удача),
# scroll (даёт опыт), boost (сбрасывает кулдауны), box (сундук с наградой).
# lvl — минимальный уровень для покупки (по умолчанию 1).
ITEMS = {
    # ── зелья
    "heal": dict(name="Зелье лечения", type="potion", price=30, heal=50),
    "heal_mid": dict(name="Среднее зелье", type="potion", price=55, heal=90),
    "heal_big": dict(name="Большое зелье", type="potion", price=80, heal=150),
    "elixir": dict(name="Эликсир жизни", type="potion", price=220, heal=500, lvl=4),
    # ── оружие
    "dagger": dict(name="Ржавый кинжал", type="weapon", price=60, bonus=1),
    "sword": dict(name="Стальной меч", type="weapon", price=150, bonus=3),
    "axe": dict(name="Боевой топор", type="weapon", price=260, bonus=4, lvl=3),
    "staff": dict(name="Посох арканиста", type="weapon", price=340, bonus=5, lvl=4),
    "great_sword": dict(name="Клинок героя", type="weapon", price=500, bonus=6, lvl=5),
    "dragon_blade": dict(name="Клинок дракона", type="weapon", price=1300, bonus=9, lvl=10),
    # ── броня
    "cloth": dict(name="Тканевая роба", type="armor", price=50, bonus=1),
    "leather": dict(name="Кожаная броня", type="armor", price=120, bonus=3),
    "chain": dict(name="Кольчуга", type="armor", price=260, bonus=5, lvl=3),
    "plate": dict(name="Латные доспехи", type="armor", price=450, bonus=7, lvl=5),
    "dragon_armor": dict(name="Драконья чешуя", type="armor", price=1200, bonus=10, lvl=10),
    # ── аксессуары (удача: прибавка к броску приключения и шанс крита в дуэли)
    "ring_luck": dict(name="Кольцо удачи", type="accessory", price=300, luck=1),
    "amulet": dict(name="Амулет странника", type="accessory", price=700, luck=2, lvl=3),
    "crown": dict(name="Корона теней", type="accessory", price=2000, luck=4, lvl=8),
    # ── свитки и расходники
    "scroll_xp": dict(name="Свиток опыта", type="scroll", price=120, xp=60),
    "scroll_xp_big": dict(name="Великий свиток опыта", type="scroll", price=450, xp=300, lvl=3),
    "hourglass": dict(name="Песочные часы", type="boost", price=200),
    # ── сундуки
    "lootbox": dict(name="Сундук сокровищ", type="box", price=250, gold=(80, 350), chance=0.35,
                    pool=["heal", "heal_mid", "scroll_xp", "hourglass", "dagger", "cloth"]),
    "lootbox_epic": dict(name="Эпический сундук", type="box", price=900, gold=(300, 1200), chance=0.6, lvl=3,
                         pool=["elixir", "scroll_xp_big", "hourglass", "ring_luck", "amulet", "axe", "chain"]),
}
GEAR_TYPES = ("weapon", "armor", "accessory")
USABLE_TYPES = ("potion", "scroll", "boost", "box")

SHOP_CATS = {
    "potion": ("🧪 Зелья", ("potion",)),
    "weapon": ("⚔️ Оружие", ("weapon",)),
    "armor": ("🛡 Броня", ("armor",)),
    "acc": ("💍 Аксессуары", ("accessory",)),
    "misc": ("📜 Свитки и сундуки", ("scroll", "boost", "box")),
}

# ── Донаты за Telegram Stars (XTR). stars — цена в звёздах.
DONATE = {
    "g1": dict(title="Мешочек золота", stars=25, gold=300, desc="300 💰"),
    "g2": dict(title="Кошель золота", stars=50, gold=700, desc="700 💰 (+17% бонус)"),
    "g3": dict(title="Сундук золота", stars=100, gold=1600, desc="1 600 💰 (+33% бонус)"),
    "g4": dict(title="Гора золота", stars=250, gold=4500, desc="4 500 💰 (+50% бонус)"),
    "g5": dict(title="Казна короля", stars=500, gold=10000, desc="10 000 💰 (+67% бонус)"),
    "vip": dict(title="👑 VIP на 30 дней", stars=150, vip_days=30, gold=500,
                desc="+25% золота и опыта на 30 дней и 500 💰 в подарок"),
    "hero": dict(title="🎁 Набор героя", stars=200, gold=1000,
                 items={"great_sword": 1, "plate": 1, "amulet": 1, "elixir": 3},
                 desc="Клинок героя, Латные доспехи, Амулет странника, 3 Эликсира жизни и 1000 💰"),
}

VIP_BONUS = 0.25            # +25% к золоту и опыту у VIP
REF_NEW_GOLD = 200          # бонус приглашённому после создания персонажа
REF_GOLD = 150              # бонус пригласившему за каждого друга
REF_DONATE_PERCENT = 10     # % золота от доната друга, который получает пригласивший
REF_MILESTONES = {3: ("lootbox", 1), 10: ("lootbox_epic", 1), 25: ("dragon_blade", 1)}

# (название, модификатор сложности, награда золотом, опыт)
ENCOUNTERS = [
    ("🐺 Стая волков", 0, (15, 35), 25),
    ("🏴‍☠️ Разбойники на тракте", 1, (20, 45), 30),
    ("🧟 Древний склеп", 3, (25, 55), 40),
    ("👻 Проклятый лес", 5, (40, 80), 60),
    ("🐉 Молодой дракон", 8, (60, 120), 90),
]

ADV_COOLDOWN, REST_COOLDOWN, DAILY_COOLDOWN, GM_COOLDOWN = 300, 600, 22 * 3600, 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS players(
    user_id INTEGER PRIMARY KEY, name TEXT, race TEXT, cls TEXT,
    level INTEGER DEFAULT 1, xp INTEGER DEFAULT 0, hp INTEGER, max_hp INTEGER,
    strength INTEGER, agility INTEGER, intellect INTEGER,
    gold INTEGER DEFAULT 100, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
    bio TEXT DEFAULT '', last_daily REAL DEFAULT 0, last_rest REAL DEFAULT 0, last_adv REAL DEFAULT 0,
    vip_until REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS inventory(
    user_id INTEGER, item TEXT, qty INTEGER, PRIMARY KEY(user_id, item)
);
CREATE TABLE IF NOT EXISTS scenes(chat_id INTEGER PRIMARY KEY, text TEXT);
CREATE TABLE IF NOT EXISTS referrals(
    user_id INTEGER PRIMARY KEY, referrer_id INTEGER, rewarded INTEGER DEFAULT 0, created REAL
);
CREATE TABLE IF NOT EXISTS promos(
    code TEXT PRIMARY KEY, gold INTEGER DEFAULT 0, xp INTEGER DEFAULT 0,
    item TEXT DEFAULT '', qty INTEGER DEFAULT 1, vip_days INTEGER DEFAULT 0,
    max_uses INTEGER DEFAULT 0, uses INTEGER DEFAULT 0, expires REAL DEFAULT 0, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS promo_uses(code TEXT, user_id INTEGER, PRIMARY KEY(code, user_id));
CREATE TABLE IF NOT EXISTS payments(
    charge_id TEXT PRIMARY KEY, user_id INTEGER, pack TEXT, stars INTEGER, created REAL, refunded INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bans(user_id INTEGER PRIMARY KEY);
"""

# ───────────────────────────── БАЗА ДАННЫХ ─────────────────────────────


async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    # миграция старых баз: добавляем недостающие колонки
    async with db.execute("PRAGMA table_info(players)") as cur:
        cols = {r["name"] for r in await cur.fetchall()}
    if "vip_until" not in cols:
        await db.execute("ALTER TABLE players ADD COLUMN vip_until REAL DEFAULT 0")
    await db.commit()
    async with db.execute("SELECT user_id FROM bans") as cur:
        BANNED.update(r["user_id"] for r in await cur.fetchall())


async def get_player(uid: int):
    async with db.execute("SELECT * FROM players WHERE user_id=?", (uid,)) as cur:
        return await cur.fetchone()


async def update_player(uid: int, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    await db.execute(f"UPDATE players SET {cols} WHERE user_id=?", (*fields.values(), uid))
    await db.commit()


async def add_item(uid: int, item: str, qty: int = 1):
    await db.execute(
        "INSERT INTO inventory(user_id,item,qty) VALUES(?,?,?) "
        "ON CONFLICT(user_id,item) DO UPDATE SET qty=qty+excluded.qty",
        (uid, item, qty),
    )
    await db.commit()


async def remove_item(uid: int, item: str) -> bool:
    async with db.execute("SELECT qty FROM inventory WHERE user_id=? AND item=?", (uid, item)) as cur:
        row = await cur.fetchone()
    if not row:
        return False
    if row["qty"] <= 1:
        await db.execute("DELETE FROM inventory WHERE user_id=? AND item=?", (uid, item))
    else:
        await db.execute("UPDATE inventory SET qty=qty-1 WHERE user_id=? AND item=?", (uid, item))
    await db.commit()
    return True


async def get_inventory(uid: int) -> dict:
    async with db.execute("SELECT item, qty FROM inventory WHERE user_id=?", (uid,)) as cur:
        return {r["item"]: r["qty"] for r in await cur.fetchall()}


async def gear_bonus(uid: int) -> tuple[int, int, int]:
    """Лучшее оружие, лучшая броня и суммарная удача от аксессуаров."""
    inv = await get_inventory(uid)
    atk = max([ITEMS[i]["bonus"] for i in inv if ITEMS.get(i, {}).get("type") == "weapon"], default=0)
    df = max([ITEMS[i]["bonus"] for i in inv if ITEMS.get(i, {}).get("type") == "armor"], default=0)
    luck = sum(ITEMS[i]["luck"] for i in inv if ITEMS.get(i, {}).get("type") == "accessory")
    return atk, df, luck


async def get_scene(chat_id: int) -> str:
    async with db.execute("SELECT text FROM scenes WHERE chat_id=?", (chat_id,)) as cur:
        row = await cur.fetchone()
    return row["text"] if row else ""


async def grant(uid: int, xp: int = 0, gold: int = 0) -> str:
    """Выдаёт опыт и золото (с VIP-бонусом), обрабатывает повышение уровня. Возвращает текст-примечание."""
    p = await get_player(uid)
    note = ""
    if (p["vip_until"] or 0) > time.time():
        bx, bg = int(xp * VIP_BONUS), int(gold * VIP_BONUS)
        xp, gold = xp + bx, gold + bg
        if bx or bg:
            note += f"\n👑 VIP-бонус: +{bx} XP, +{bg} 💰"
    lvl, cur_xp, max_hp = p["level"], p["xp"] + xp, p["max_hp"]
    main = CLASSES[p["cls"]]["main"]
    stat = p[main]
    leveled = False
    while cur_xp >= lvl * 100:
        cur_xp -= lvl * 100
        lvl += 1
        max_hp += 10
        stat += 1
        leveled = True
        note += f"\n🎉 <b>Новый уровень: {lvl}!</b> Макс. HP +10, {main_name(main)} +1"
    fields = dict(level=lvl, xp=cur_xp, max_hp=max_hp, gold=p["gold"] + gold, **{main: stat})
    if leveled:
        fields["hp"] = max_hp
    await update_player(uid, **fields)
    return note


async def add_vip(uid: int, days: int):
    p = await get_player(uid)
    base = max(time.time(), p["vip_until"] or 0)
    await update_player(uid, vip_until=base + days * 86400)


async def give_reward(uid: int, gold: int = 0, xp: int = 0, items: dict | None = None, vip_days: int = 0) -> str:
    """Единая выдача наград (промокоды, донаты, сундуки, рефералки, админ). Возвращает текст."""
    parts = []
    if gold:
        p = await get_player(uid)
        await update_player(uid, gold=max(0, p["gold"] + gold))
        parts.append(f"{gold:+d} 💰")
    for key, qty in (items or {}).items():
        await add_item(uid, key, qty)
        parts.append(f"{ITEMS[key]['name']} ×{qty}")
    if vip_days:
        await add_vip(uid, vip_days)
        parts.append(f"👑 VIP +{vip_days} дн.")
    note = ""
    if xp:
        note = await grant(uid, xp=xp)
        parts.append(f"+{xp} XP")
    return (", ".join(parts) or "ничего") + note


# ───────────────────────────── УТИЛИТЫ ─────────────────────────────


def esc(s: str) -> str:
    return html.escape(s or "")


def is_owner(uid: int) -> bool:
    return uid in ADMIN_IDS


def main_name(stat: str) -> str:
    return {"strength": "сила", "agility": "ловкость", "intellect": "интеллект"}[stat]


def bar(cur: int, mx: int, n: int = 10) -> str:
    filled = max(0, min(n, round(n * cur / mx))) if mx else 0
    return "█" * filled + "░" * (n - filled)


def fmt_time(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return " ".join(x for x in (f"{h}ч" if h else "", f"{m}м" if m else "", f"{s}с" if s and not h else "") if x)


def fmt_date(ts: float) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(ts))


async def safe_edit(msg: Message, text: str, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


async def need_player(m: Message):
    p = await get_player(m.from_user.id)
    if not p:
        await m.answer("У тебя ещё нет персонажа. Напиши мне в личку: /create")
    return p


async def is_admin(m: Message) -> bool:
    """Админ чата (не путать с админом бота — is_owner)."""
    if m.chat.type == "private":
        return True
    member = await m.bot.get_chat_member(m.chat.id, m.from_user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)


async def profile_text(p) -> str:
    atk, df, luck = await gear_bonus(p["user_id"])
    c = CLASSES[p["cls"]]
    text = (
        f"{c['emoji']} <b>{esc(p['name'])}</b> — {p['race']} {p['cls']}\n"
        f"Ур. {p['level']} · XP {p['xp']}/{p['level'] * 100}\n"
        f"❤️ {bar(p['hp'], p['max_hp'])} {p['hp']}/{p['max_hp']}\n"
        f"💪 {p['strength']}  🏹 {p['agility']}  🧠 {p['intellect']}\n"
        f"🗡 Атака +{atk}  🛡 Защита +{df}  🍀 Удача +{luck}\n"
        f"💰 {p['gold']} золота\n"
        f"🏆 Побед: {p['wins']} · Поражений: {p['losses']}"
    )
    if (p["vip_until"] or 0) > time.time():
        text += f"\n👑 VIP до {fmt_date(p['vip_until'])}"
    if p["bio"]:
        text += f"\n\n📜 {esc(p['bio'])}"
    return text


# ───────────────────────────── СТАРТ И ПОМОЩЬ ─────────────────────────────

HELP = """<b>🎭 LumenRP-бот</b>
<b>присоединяйся к телеграм каналу @LumenRPofficially</b>
<b>Персонаж</b>
/create — создать персонажа (только тут)
/profile — профиль (ответом на сообщение — чужой)
/bio текст — описание персонажа
/reset — удалить персонажа

<b>Ролевая игра</b>
/me действие — действие от третьего лица
/do описание — описание обстановки/результата
/roll 2d6+3 — бросок кубиков
/scene текст — задать сцену чата (админы)
/gmreset — сбросить память рассказчика (админы)

<b>Экономика и приключения</b>
/adventure — отправиться в приключение
/rest — восстановить здоровье
/daily — ежедневная награда
/shop — магазин
/inventory — инвентарь и предметы
/give сумма — передать золото (ответом на сообщение)
/duel — вызвать на дуэль (ответом на сообщение)
/top — рейтинг героев

<b>Бонусы</b>
/promo КОД — активировать промокод
/ref — твоя реферальная ссылка и награды за друзей
/donate — поддержать проект звёздами ⭐ (золото, VIP, наборы)"""


# ───────────────────────────── КНОПКИ И КОНТЕКСТ ─────────────────────────────

NO_CHAR = "У тебя ещё нет персонажа. Напиши мне в личку и нажми /create"

MENU_BUTTONS = {
    "👤 Профиль": "profile", "⚔️ Приключение": "adventure", "🏕 Отдых": "rest",
    "🎁 Награда": "daily", "🛒 Магазин": "shop", "🎒 Инвентарь": "inventory",
    "🏆 Рейтинг": "top", "👥 Друзья": "ref", "💎 Донат": "donate", "❓ Помощь": "help",
}
NAV_LABELS = {v: k for k, v in MENU_BUTTONS.items()}
DICE_BUTTONS = ["d4", "d6", "d8", "d10", "d12", "d20", "d100", "2d6"]


class Ctx:
    """Одинаково работает и для сообщений, и для нажатий inline-кнопок."""

    def __init__(self, user, chat_id, send, bot=None):
        self.uid, self.name, self.chat_id, self.send, self.bot = user.id, user.full_name, chat_id, send, bot


def ctx_msg(m: Message) -> Ctx:
    return Ctx(m.from_user, m.chat.id, m.answer, m.bot)


def ctx_cb(cb: CallbackQuery) -> Ctx:
    return Ctx(cb.from_user, cb.message.chat.id, cb.message.answer, cb.bot)


async def ctx_player(ctx: Ctx):
    p = await get_player(ctx.uid)
    if not p:
        await ctx.send(NO_CHAR)
    return p


def reply_menu():
    """Постоянная клавиатура внизу экрана."""
    kb = ReplyKeyboardBuilder()
    for text in MENU_BUTTONS:
        kb.button(text=text)
    kb.adjust(3)
    return kb.as_markup(resize_keyboard=True, input_field_placeholder="Выбери действие кнопкой")


def nav_kb(*actions):
    """Кнопки под сообщением."""
    kb = InlineKeyboardBuilder()
    for a in actions:
        kb.button(text=NAV_LABELS[a], callback_data=f"go:{a}")
    kb.adjust(2)
    return kb.as_markup()


async def register_referral(m: Message, arg: str):
    """Запоминает, кто пригласил пользователя (только для новичков без персонажа)."""
    try:
        ref = int(arg[4:])
    except ValueError:
        return
    uid = m.from_user.id
    if ref == uid or await get_player(uid):
        return
    async with db.execute("SELECT 1 FROM referrals WHERE user_id=?", (uid,)) as cur:
        if await cur.fetchone():
            return
    rp = await get_player(ref)
    if not rp:
        return
    await db.execute("INSERT INTO referrals(user_id,referrer_id,created) VALUES(?,?,?)", (uid, ref, time.time()))
    await db.commit()
    await m.answer(f"🤝 Тебя пригласил герой <b>{esc(rp['name'])}</b>!\n"
                   f"Создай персонажа — /create — и получи бонус <b>{REF_NEW_GOLD} 💰</b>.")


@router.message(Command("start", "help", "menu"))
async def cmd_start(m: Message, command: CommandObject):
    if command.command == "start" and m.chat.type == "private" \
            and command.args and command.args.startswith("ref_"):
        await register_referral(m, command.args)
    await m.answer(HELP, reply_markup=reply_menu())


# ───────────────────────────── СОЗДАНИЕ ПЕРСОНАЖА ─────────────────────────────


class Create(StatesGroup):
    name = State()
    race = State()
    cls = State()


@router.message(Command("create"))
async def cmd_create(m: Message, state: FSMContext):
    if m.chat.type != "private":
        return await m.answer("Создавать персонажа лучше в личке со мной 🙂")
    if await get_player(m.from_user.id):
        return await m.answer("Персонаж уже есть. Чтобы начать заново — /reset")
    await state.set_state(Create.name)
    await m.answer("Как зовут твоего героя? (2–24 символа)")


@router.message(StateFilter(Create.name), F.text)
async def create_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not 2 <= len(name) <= 24:
        return await m.answer("Имя должно быть от 2 до 24 символов. Попробуй ещё раз.")
    await state.update_data(name=name)
    kb = InlineKeyboardBuilder()
    for r, (s, a, i) in RACES.items():
        kb.button(text=f"{r} (💪{s:+d} 🏹{a:+d} 🧠{i:+d})", callback_data=f"race:{r}")
    kb.adjust(1)
    await state.set_state(Create.race)
    await m.answer("Выбери расу:", reply_markup=kb.as_markup())


@router.callback_query(StateFilter(Create.race), F.data.startswith("race:"))
async def create_race(cb: CallbackQuery, state: FSMContext):
    race = cb.data.split(":", 1)[1]
    if race not in RACES:
        return await cb.answer()
    await state.update_data(race=race)
    kb = InlineKeyboardBuilder()
    for name, c in CLASSES.items():
        kb.button(text=f"{c['emoji']} {name} (❤️{c['hp']})", callback_data=f"class:{name}")
    kb.adjust(1)
    await state.set_state(Create.cls)
    await cb.message.edit_text("Выбери класс:", reply_markup=kb.as_markup())
    await cb.answer()


async def reward_referral(bot: Bot, uid: int) -> str:
    """Награда новичку и пригласившему после создания персонажа."""
    async with db.execute("SELECT * FROM referrals WHERE user_id=? AND rewarded=0", (uid,)) as cur:
        row = await cur.fetchone()
    if not row:
        return ""
    await db.execute("UPDATE referrals SET rewarded=1 WHERE user_id=?", (uid,))
    await db.commit()
    await give_reward(uid, gold=REF_NEW_GOLD)
    ref = row["referrer_id"]
    if await get_player(ref):
        txt = await give_reward(ref, gold=REF_GOLD, items={"heal": 1})
        async with db.execute("SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND rewarded=1", (ref,)) as cur:
            cnt = (await cur.fetchone())["c"]
        msg = f"👥 По твоей ссылке пришёл новый герой! Награда: {txt}"
        if cnt in REF_MILESTONES:
            key, qty = REF_MILESTONES[cnt]
            await add_item(ref, key, qty)
            msg += f"\n🏅 Приглашено друзей: {cnt}! Бонус: {ITEMS[key]['name']} ×{qty}"
        try:
            await bot.send_message(ref, msg)
        except Exception:
            pass
    return f"\n🤝 Бонус за приглашение: +{REF_NEW_GOLD} 💰"


@router.callback_query(StateFilter(Create.cls), F.data.startswith("class:"))
async def create_class(cb: CallbackQuery, state: FSMContext):
    cls = cb.data.split(":", 1)[1]
    if cls not in CLASSES:
        return await cb.answer()
    data = await state.get_data()
    c, (bs, ba, bi) = CLASSES[cls], RACES[data["race"]]
    await db.execute(
        "INSERT INTO players(user_id,name,race,cls,hp,max_hp,strength,agility,intellect) VALUES(?,?,?,?,?,?,?,?,?)",
        (cb.from_user.id, data["name"], data["race"], cls, c["hp"], c["hp"],
         c["strength"] + bs, c["agility"] + ba, c["intellect"] + bi),
    )
    await db.commit()
    await add_item(cb.from_user.id, "heal", 2)
    await state.clear()
    bonus = await reward_referral(cb.bot, cb.from_user.id)
    p = await get_player(cb.from_user.id)
    await cb.message.edit_text("✅ Персонаж создан! Стартовый набор: 100 💰 и 2 зелья лечения." + bonus
                               + "\n\n" + await profile_text(p))
    await cb.message.answer("🎮 Управление — кнопки внизу экрана 👇", reply_markup=reply_menu())
    await cb.answer()


@router.message(Command("reset"))
async def cmd_reset(m: Message):
    if not await need_player(m):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить", callback_data=f"reset:yes:{m.from_user.id}")
    kb.button(text="Отмена", callback_data=f"reset:no:{m.from_user.id}")
    await m.answer("⚠️ Удалить персонажа и весь прогресс?", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("reset:"))
async def reset_cb(cb: CallbackQuery):
    _, ans, uid = cb.data.split(":")
    if cb.from_user.id != int(uid):
        return await cb.answer("Это не твоя кнопка", show_alert=True)
    if ans == "yes":
        await db.execute("DELETE FROM players WHERE user_id=?", (cb.from_user.id,))
        await db.execute("DELETE FROM inventory WHERE user_id=?", (cb.from_user.id,))
        await db.commit()
        await cb.message.edit_text("Персонаж удалён. Создай нового: /create")
    else:
        await cb.message.edit_text("Отменено.")
    await cb.answer()


# ───────────────────────────── ПРОФИЛЬ ─────────────────────────────


async def do_profile(ctx: Ctx, target_id=None):
    p = await get_player(target_id or ctx.uid)
    if not p:
        return await ctx.send("У этого пользователя нет персонажа." if target_id else NO_CHAR)
    await ctx.send(await profile_text(p), reply_markup=nav_kb("adventure", "inventory", "shop", "top"))


@router.message(Command("profile"))
async def cmd_profile(m: Message):
    target = m.reply_to_message.from_user.id if m.reply_to_message else None
    await do_profile(ctx_msg(m), target)


@router.message(Command("bio"))
async def cmd_bio(m: Message, command: CommandObject):
    if not await need_player(m):
        return
    if not command.args:
        return await m.answer("Использование: /bio описание вашего героя (до 300 символов)")
    await update_player(m.from_user.id, bio=command.args[:300])
    await m.answer("📜 Описание обновлено.")


async def do_top(ctx: Ctx):
    async with db.execute("SELECT * FROM players ORDER BY level DESC, xp DESC, wins DESC LIMIT 10") as cur:
        rows = await cur.fetchall()
    if not rows:
        return await ctx.send("Пока нет ни одного героя.")
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    lines = [f"{medals[i]} <b>{esc(r['name'])}</b> — ур. {r['level']} ({r['cls']}), побед: {r['wins']}"
             for i, r in enumerate(rows)]
    await ctx.send("🏆 <b>Лучшие герои</b>\n\n" + "\n".join(lines), reply_markup=nav_kb("profile", "adventure"))


async def do_help(ctx: Ctx):
    await ctx.send(HELP)


@router.message(Command("top"))
async def cmd_top(m: Message):
    await do_top(ctx_msg(m))


# ───────────────────────────── ROLEPLAY-ИНСТРУМЕНТЫ ─────────────────────────────

DICE_RE = re.compile(r"^(\d{0,2})d(\d{1,4})([+-]\d{1,4})?$", re.I)


def roll_text(name: str, expr: str) -> str:
    expr = expr.replace(" ", "")
    match = DICE_RE.match(expr)
    if not match:
        return "Формат: /roll 2d6+3, /roll d20, /roll 4d8-1"
    count, sides, mod = int(match[1] or 1), int(match[2]), int(match[3] or 0)
    if not (1 <= count <= 50 and 2 <= sides <= 1000):
        return "Максимум 50 кубиков, граней от 2 до 1000."
    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls) + mod
    detail = ", ".join(map(str, rolls)) if count <= 15 else f"{count} бросков"
    extra = ""
    if count == 1 and sides == 20:
        extra = " 🔥 <b>КРИТ!</b>" if rolls[0] == 20 else " 💀 <b>Провал!</b>" if rolls[0] == 1 else ""
    return (f"🎲 <b>{esc(name)}</b> бросает {expr}: [{detail}]"
            f"{f' {mod:+d}' if mod else ''} = <b>{total}</b>{extra}")


async def do_dice(ctx: Ctx):
    kb = InlineKeyboardBuilder()
    for d in DICE_BUTTONS:
        kb.button(text=f"🎲 {d}", callback_data=f"roll:{d}")
    kb.adjust(4)
    await ctx.send("Какой кубик бросаем?\n<i>Свой бросок: /roll 3d6+2</i>", reply_markup=kb.as_markup())


@router.message(Command("roll"))
async def cmd_roll(m: Message, command: CommandObject):
    await m.answer(roll_text(m.from_user.full_name, command.args or "d20"))


@router.callback_query(F.data.startswith("roll:"))
async def roll_cb(cb: CallbackQuery):
    await cb.message.answer(roll_text(cb.from_user.full_name, cb.data.split(":", 1)[1]))
    await cb.answer()


async def rp_say(m: Message, command: CommandObject, template: str, usage: str):
    if not command.args:
        return await m.answer(usage)
    p = await get_player(m.from_user.id)
    name = esc(p["name"]) if p else esc(m.from_user.full_name)
    try:
        await m.delete()
    except Exception:
        pass
    await m.answer(template.format(name=name, text=esc(command.args)))


@router.message(Command("me"))
async def cmd_me(m: Message, command: CommandObject):
    await rp_say(m, command, "✨ <i><b>{name}</b> {text}</i>", "Использование: /me достаёт меч")


@router.message(Command("do"))
async def cmd_do(m: Message, command: CommandObject):
    await rp_say(m, command, "🎭 <i>{text}</i> <b>({name})</b>", "Использование: /do вокруг сгущается туман")


@router.message(Command("scene"))
async def cmd_scene(m: Message, command: CommandObject):
    if not command.args:
        scene = await get_scene(m.chat.id)
        return await m.answer(f"🎬 <b>Текущая сцена:</b>\n{esc(scene)}" if scene else "Сцена не задана. /scene описание")
    if not await is_admin(m):
        return await m.answer("Менять сцену могут только админы.")
    await db.execute("INSERT INTO scenes(chat_id,text) VALUES(?,?) "
                     "ON CONFLICT(chat_id) DO UPDATE SET text=excluded.text", (m.chat.id, command.args[:1000]))
    await db.commit()
    HISTORY.pop(m.chat.id, None)
    await m.answer(f"🎬 <b>Новая сцена:</b>\n{esc(command.args[:1000])}")


# ───────────────────────────── ИИ-РАССКАЗЧИК ─────────────────────────────

HISTORY: dict[int, deque] = defaultdict(lambda: deque(maxlen=12))
CHAT_LOCKS: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
LAST_GM: dict[int, float] = {}


@router.message(Command("gm"))
async def cmd_gm(m: Message, command: CommandObject):
    if not ai:
        return await m.answer("ИИ-рассказчик выключен: в .env не задан ANTHROPIC_API_KEY.")
    p = await need_player(m)
    if not p:
        return
    if not command.args:
        return await m.answer("Использование: /gm я осторожно открываю дверь склепа")
    wait = GM_COOLDOWN - (time.time() - LAST_GM.get(m.from_user.id, 0))
    if wait > 0:
        return await m.answer(f"⏳ Подожди ещё {int(wait) + 1}с.")
    LAST_GM[m.from_user.id] = time.time()

    scene = await get_scene(m.chat.id)
    system = (
        "Ты — мастер игры (рассказчик) в текстовой фэнтези-RP в Telegram-чате. "
        "Описывай мир и последствия действий игроков живо и атмосферно, 2–5 абзацев максимум. "
        "Не решай за игроков, что они делают или чувствуют; заканчивай ситуацией, на которую они могут ответить. "
        "Соблюдай логику мира и характеристики персонажей. Отвечай на русском. "
        f"Текущая сцена: {scene or 'не задана, придумай уместную фэнтези-обстановку.'}"
    )
    entry = (f"[{p['name']} — {p['race']} {p['cls']}, ур. {p['level']}]: {command.args[:500]}")
    async with CHAT_LOCKS[m.chat.id]:
        msgs = list(HISTORY[m.chat.id]) + [{"role": "user", "content": entry}]
        await m.bot.send_chat_action(m.chat.id, "typing")
        try:
            resp = await ai.messages.create(model=AI_MODEL, max_tokens=800, system=system, messages=msgs)
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
        except Exception:
            logging.exception("AI error")
            return await m.answer("Рассказчик задумался и потерял нить... Попробуй ещё раз.")
        HISTORY[m.chat.id].append({"role": "user", "content": entry})
        HISTORY[m.chat.id].append({"role": "assistant", "content": text})
    await m.answer(f"📖 {esc(text)}")


@router.message(Command("gmreset"))
async def cmd_gmreset(m: Message):
    if not await is_admin(m):
        return await m.answer("Только для админов.")
    HISTORY.pop(m.chat.id, None)
    await m.answer("🧹 Память рассказчика очищена.")


# ───────────────────────────── ЭКОНОМИКА И ПРИКЛЮЧЕНИЯ ─────────────────────────────


async def do_daily(ctx: Ctx):
    p = await ctx_player(ctx)
    if not p:
        return
    wait = DAILY_COOLDOWN - (time.time() - p["last_daily"])
    if wait > 0:
        return await ctx.send(f"⏳ Награда будет доступна через {fmt_time(wait)}.", reply_markup=nav_kb("profile", "adventure"))
    reward = 50 + p["level"] * 10
    await update_player(ctx.uid, last_daily=time.time())
    note = await grant(ctx.uid, xp=10, gold=reward)
    await ctx.send(f"🎁 Ежедневная награда: <b>{reward} 💰</b> и 10 XP.{note}",
                   reply_markup=nav_kb("shop", "adventure", "profile"))


async def do_rest(ctx: Ctx):
    p = await ctx_player(ctx)
    if not p:
        return
    wait = REST_COOLDOWN - (time.time() - p["last_rest"])
    if wait > 0:
        return await ctx.send(f"⏳ Ты ещё не готов отдыхать: {fmt_time(wait)}.", reply_markup=nav_kb("inventory", "profile"))
    await update_player(ctx.uid, hp=p["max_hp"], last_rest=time.time())
    await ctx.send("🏕 Ты отдохнул у костра. Здоровье полностью восстановлено.",
                   reply_markup=nav_kb("adventure", "profile"))


async def do_adventure(ctx: Ctx):
    p = await ctx_player(ctx)
    if not p:
        return
    wait = ADV_COOLDOWN - (time.time() - p["last_adv"])
    if wait > 0:
        return await ctx.send(f"⏳ Герой переводит дух. Следующее приключение через {fmt_time(wait)}.",
                              reply_markup=nav_kb("rest", "inventory", "profile"))
    if p["hp"] < p["max_hp"] * 0.3:
        return await ctx.send("💔 Ты слишком ранен. Выпей зелье или отдохни.",
                              reply_markup=nav_kb("inventory", "rest"))

    name, mod, (g_lo, g_hi), xp = random.choice(ENCOUNTERS)
    atk, df, luck = await gear_bonus(p["user_id"])
    main = p[CLASSES[p["cls"]]["main"]]
    roll = random.randint(1, 20)
    score, need = roll + main + atk + luck, 10 + p["level"] * 2 + mod
    await update_player(p["user_id"], last_adv=time.time())
    kb = nav_kb("adventure", "rest", "inventory", "profile")

    luck_part = f" + удача {luck}" if luck else ""
    header = (f"{name}\n🎲 d20: {roll} + навык {main} + оружие {atk}{luck_part} = "
              f"<b>{score}</b> (нужно {need})\n\n")
    if score >= need or roll == 20:
        gold = random.randint(g_lo, g_hi) + p["level"] * 3
        gain = xp + p["level"] * 5
        await update_player(p["user_id"], wins=p["wins"] + 1)
        note = await grant(p["user_id"], xp=gain, gold=gold)
        loot = ""
        if random.random() < 0.15:
            await add_item(p["user_id"], "heal")
            loot = "\n🧪 Найдено: Зелье лечения!"
        elif random.random() < 0.03 + luck * 0.01:
            await add_item(p["user_id"], "lootbox")
            loot = "\n🎁 Найдено: Сундук сокровищ!"
        await ctx.send(f"{header}✅ <b>Победа!</b> +{gold} 💰, +{gain} XP{loot}{note}", reply_markup=kb)
    else:
        dmg = max(5, random.randint(10, 25) + p["level"] * 2 - df // 2)
        new_hp = max(1, p["hp"] - dmg)
        await update_player(p["user_id"], hp=new_hp, losses=p["losses"] + 1)
        await ctx.send(f"{header}❌ <b>Поражение.</b> Ты потерял {dmg} ❤️ (осталось {new_hp}/{p['max_hp']}).",
                       reply_markup=kb)


@router.message(Command("daily"))
async def cmd_daily(m: Message):
    await do_daily(ctx_msg(m))


@router.message(Command("rest"))
async def cmd_rest(m: Message):
    await do_rest(ctx_msg(m))


@router.message(Command("adventure"))
async def cmd_adventure(m: Message):
    await do_adventure(ctx_msg(m))


@router.message(Command("give"))
async def cmd_give(m: Message, command: CommandObject):
    p = await need_player(m)
    if not p:
        return
    if not m.reply_to_message or not (command.args or "").isdigit():
        return await m.answer("Ответь на сообщение игрока: /give 50")
    target, amount = m.reply_to_message.from_user, int(command.args)
    if target.id == m.from_user.id or target.is_bot:
        return await m.answer("Так не получится.")
    tp = await get_player(target.id)
    if not tp:
        return await m.answer("У получателя нет персонажа.")
    if not 1 <= amount <= p["gold"]:
        return await m.answer("Некорректная сумма или не хватает золота.")
    await update_player(p["user_id"], gold=p["gold"] - amount)
    await update_player(tp["user_id"], gold=tp["gold"] + amount)
    await m.answer(f"💸 <b>{esc(p['name'])}</b> передаёт <b>{amount} 💰</b> герою <b>{esc(tp['name'])}</b>.")


# ───────────────────────────── МАГАЗИН И ИНВЕНТАРЬ ─────────────────────────────


def item_desc(key: str) -> str:
    it = ITEMS[key]
    t = it["type"]
    if t == "potion":
        return f"восст. {it['heal']} HP"
    if t == "weapon":
        return f"атака +{it['bonus']}"
    if t == "armor":
        return f"защита +{it['bonus']}"
    if t == "accessory":
        return f"удача +{it['luck']}"
    if t == "scroll":
        return f"+{it['xp']} XP"
    if t == "boost":
        return "сбрасывает кулдаун приключения и отдыха"
    lo, hi = it["gold"]
    return f"{lo}–{hi} 💰 и шанс на предмет"


def cat_of(key: str) -> str:
    t = ITEMS[key]["type"]
    return next(c for c, (_, types) in SHOP_CATS.items() if t in types)


def shop_main_view(p):
    kb = InlineKeyboardBuilder()
    for c, (label, _) in SHOP_CATS.items():
        kb.button(text=label, callback_data=f"sc:{c}")
    kb.button(text="💎 Донат (⭐ Stars)", callback_data="go:donate")
    kb.button(text=NAV_LABELS["inventory"], callback_data="go:inventory")
    kb.adjust(2, 2, 1, 1, 1)
    return f"🛒 <b>Магазин</b>\n💰 У тебя {p['gold']} золота\n\nВыбери раздел:", kb.as_markup()


async def shop_cat_view(uid: int, cat: str):
    p = await get_player(uid)
    inv = await get_inventory(uid)
    label, types = SHOP_CATS[cat]
    kb = InlineKeyboardBuilder()
    lines = []
    for key, it in ITEMS.items():
        if it["type"] not in types:
            continue
        lvl = it.get("lvl", 1)
        locked = p["level"] < lvl
        owned = it["type"] in GEAR_TYPES and key in inv
        tags = (f" 🔒 ур.{lvl}" if locked else "") + (" ✅ есть" if owned else "")
        lines.append(f"• <b>{it['name']}</b> — {it['price']} 💰 ({item_desc(key)}){tags}")
        icon = "✅" if owned else "🔒" if locked else "🛒"
        kb.button(text=f"{icon} {it['name']} · {it['price']}💰", callback_data=f"buy:{uid}:{key}")
    kb.button(text="⬅️ Разделы", callback_data="sc:main")
    kb.adjust(1)
    text = f"🛒 <b>{label}</b> (у тебя {p['gold']} 💰)\n\n" + "\n".join(lines)
    return text, kb.as_markup()


async def do_shop(ctx: Ctx):
    p = await ctx_player(ctx)
    if not p:
        return
    text, kb = shop_main_view(p)
    await ctx.send(text, reply_markup=kb)


@router.message(Command("shop"))
async def cmd_shop(m: Message):
    await do_shop(ctx_msg(m))


@router.callback_query(F.data.startswith("sc:"))
async def shop_cat_cb(cb: CallbackQuery):
    p = await get_player(cb.from_user.id)
    if not p:
        return await cb.answer(NO_CHAR, show_alert=True)
    cat = cb.data.split(":", 1)[1]
    if cat == "main":
        text, kb = shop_main_view(p)
    elif cat in SHOP_CATS:
        text, kb = await shop_cat_view(cb.from_user.id, cat)
    else:
        return await cb.answer()
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("buy:"))
async def buy_cb(cb: CallbackQuery):
    _, uid, key = cb.data.split(":")
    if cb.from_user.id != int(uid):
        return await cb.answer("Это чужой магазин, открой свой: /shop", show_alert=True)
    p, it = await get_player(cb.from_user.id), ITEMS.get(key)
    if not p or not it:
        return await cb.answer()
    if p["level"] < it.get("lvl", 1):
        return await cb.answer(f"Нужен уровень {it['lvl']} 🔒", show_alert=True)
    if p["gold"] < it["price"]:
        return await cb.answer("Не хватает золота 💸", show_alert=True)
    if it["type"] in GEAR_TYPES and key in await get_inventory(p["user_id"]):
        return await cb.answer("Это у тебя уже есть", show_alert=True)
    await update_player(p["user_id"], gold=p["gold"] - it["price"])
    await add_item(p["user_id"], key)
    await cb.answer(f"Куплено: {it['name']}! Осталось {p['gold'] - it['price']} 💰", show_alert=True)
    text, kb = await shop_cat_view(p["user_id"], cat_of(key))
    await safe_edit(cb.message, text, kb)


async def do_inventory(ctx: Ctx):
    p = await ctx_player(ctx)
    if not p:
        return
    inv = await get_inventory(p["user_id"])
    if not inv:
        return await ctx.send("🎒 Инвентарь пуст.", reply_markup=nav_kb("shop", "profile"))
    kb = InlineKeyboardBuilder()
    lines = []
    for key, qty in inv.items():
        if key not in ITEMS:
            continue
        it = ITEMS[key]
        lines.append(f"• <b>{it['name']}</b> ×{qty} ({item_desc(key)})")
        if it["type"] in USABLE_TYPES:
            verb = "Открыть" if it["type"] == "box" else "Выпить" if it["type"] == "potion" else "Использовать"
            kb.button(text=f"{verb}: {it['name']}", callback_data=f"use:{ctx.uid}:{key}")
    kb.button(text=NAV_LABELS["shop"], callback_data="go:shop")
    kb.button(text=NAV_LABELS["profile"], callback_data="go:profile")
    kb.adjust(1)
    await ctx.send("🎒 <b>Инвентарь</b>\n\n" + "\n".join(lines) +
                   "\n\n<i>Оружие, броня и аксессуары работают автоматически.</i>",
                   reply_markup=kb.as_markup())


@router.message(Command("inventory", "inv"))
async def cmd_inventory(m: Message):
    await do_inventory(ctx_msg(m))


async def open_box(uid: int, key: str) -> str:
    it = ITEMS[key]
    gold = random.randint(*it["gold"])
    items = {}
    if random.random() < it["chance"]:
        k = random.choice(it["pool"])
        if ITEMS[k]["type"] in GEAR_TYPES and k in await get_inventory(uid):
            gold += 100  # дубликат снаряжения — компенсация золотом
        else:
            items[k] = 1
    return await give_reward(uid, gold=gold, items=items)


@router.callback_query(F.data.startswith("use:"))
async def use_cb(cb: CallbackQuery):
    _, uid, key = cb.data.split(":")
    if cb.from_user.id != int(uid):
        return await cb.answer("Это не твой инвентарь", show_alert=True)
    p, it = await get_player(cb.from_user.id), ITEMS.get(key)
    if not p or not it or it["type"] not in USABLE_TYPES:
        return await cb.answer()
    t = it["type"]
    if t == "potion" and p["hp"] >= p["max_hp"]:
        return await cb.answer("Здоровье и так полное", show_alert=True)
    if not await remove_item(p["user_id"], key):
        return await cb.answer("Такого предмета больше нет", show_alert=True)
    if t == "potion":
        new_hp = min(p["max_hp"], p["hp"] + it["heal"])
        await update_player(p["user_id"], hp=new_hp)
        return await cb.answer(f"❤️ Здоровье: {new_hp}/{p['max_hp']}", show_alert=True)
    if t == "scroll":
        res = await give_reward(p["user_id"], xp=it["xp"])
        text = f"📜 {it['name']}: {res}"
    elif t == "boost":
        await update_player(p["user_id"], last_adv=0, last_rest=0)
        text = "⏳ Песок времени потёк вспять — кулдауны приключения и отдыха сброшены!"
    else:
        text = f"🎁 {it['name']} открыт! Получено: {await open_box(p['user_id'], key)}"
    await cb.message.answer(text)
    await cb.answer()


# ───────────────────────────── ДОНАТЫ (TELEGRAM STARS) ─────────────────────────────


async def do_donate(ctx: Ctx):
    kb = InlineKeyboardBuilder()
    lines = []
    for key, d in DONATE.items():
        lines.append(f"• <b>{d['title']}</b> — {d['stars']} ⭐\n  <i>{d['desc']}</i>")
        kb.button(text=f"{d['title']} · {d['stars']}⭐", callback_data=f"don:{key}")
    kb.adjust(1)
    await ctx.send("💎 <b>Поддержать проект</b>\n\nОплата — Telegram Stars ⭐. "
                   "Покупка приходит мгновенно, а ты помогаешь развитию бота ❤️\n\n" + "\n".join(lines),
                   reply_markup=kb.as_markup())


@router.message(Command("donate", "premium"))
async def cmd_donate(m: Message):
    await do_donate(ctx_msg(m))


@router.callback_query(F.data.startswith("don:"))
async def don_cb(cb: CallbackQuery):
    key = cb.data.split(":", 1)[1]
    d = DONATE.get(key)
    if not d:
        return await cb.answer()
    if not await get_player(cb.from_user.id):
        return await cb.answer("Сначала создай персонажа: /create", show_alert=True)
    try:
        await cb.bot.send_invoice(
            chat_id=cb.from_user.id,
            title=d["title"][:32],
            description=d["desc"][:255],
            payload=f"pack:{key}",
            currency="XTR",
            prices=[LabeledPrice(label=d["title"][:32], amount=d["stars"])],
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        return await cb.answer("Напиши мне в личку /start и повтори покупку.", show_alert=True)
    await cb.answer("Счёт отправлен тебе в личные сообщения 💫")


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    key = q.invoice_payload.split(":", 1)[-1]
    if key not in DONATE or not await get_player(q.from_user.id):
        return await q.answer(ok=False, error_message="Товар недоступен или у тебя нет персонажа.")
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment
    uid = m.from_user.id
    key = sp.invoice_payload.split(":", 1)[-1]
    d = DONATE.get(key)
    async with db.execute("INSERT OR IGNORE INTO payments(charge_id,user_id,pack,stars,created) VALUES(?,?,?,?,?)",
                          (sp.telegram_payment_charge_id, uid, key, sp.total_amount, time.time())) as cur:
        fresh = cur.rowcount > 0
    await db.commit()
    if not fresh:
        return  # повторное уведомление о том же платеже
    if not d:
        logging.error("Платёж за неизвестный товар: %s", sp)
        for aid in ADMIN_IDS:
            try:
                await m.bot.send_message(aid, f"⚠️ Платёж за неизвестный товар {esc(key)} от {uid}\n"
                                              f"<code>{esc(sp.telegram_payment_charge_id)}</code>")
            except Exception:
                pass
        return await m.answer("Платёж получен, но товар не найден. Напиши администратору.")
    res = await give_reward(uid, gold=d.get("gold", 0), items=d.get("items"), vip_days=d.get("vip_days", 0))
    await m.answer(f"💎 Спасибо за поддержку! Получено: {res}")
    # бонус пригласившему
    async with db.execute("SELECT referrer_id FROM referrals WHERE user_id=?", (uid,)) as cur:
        row = await cur.fetchone()
    if row and d.get("gold") and await get_player(row["referrer_id"]):
        bonus = max(10, d["gold"] * REF_DONATE_PERCENT // 100)
        await give_reward(row["referrer_id"], gold=bonus)
        try:
            await m.bot.send_message(row["referrer_id"], f"🤝 Твой друг сделал донат — тебе <b>+{bonus} 💰</b>!")
        except Exception:
            pass
    for aid in ADMIN_IDS:
        try:
            await m.bot.send_message(aid, f"💎 Донат: {esc(m.from_user.full_name)} (<code>{uid}</code>) — "
                                          f"{d['title']} за {sp.total_amount} ⭐")
        except Exception:
            pass


# ───────────────────────────── ПРОМОКОДЫ ─────────────────────────────

PROMO_LOCK = asyncio.Lock()
LAST_PROMO: dict[int, float] = {}


def promo_desc(pr) -> str:
    parts = []
    if pr["gold"]:
        parts.append(f"{pr['gold']} 💰")
    if pr["xp"]:
        parts.append(f"{pr['xp']} XP")
    if pr["item"] and pr["item"] in ITEMS:
        parts.append(f"{ITEMS[pr['item']]['name']} ×{pr['qty']}")
    if pr["vip_days"]:
        parts.append(f"👑 VIP {pr['vip_days']} дн.")
    return ", ".join(parts)


async def redeem(uid: int, code: str) -> str:
    code = code.strip().upper()
    async with PROMO_LOCK:
        async with db.execute("SELECT * FROM promos WHERE code=?", (code,)) as cur:
            pr = await cur.fetchone()
        if not pr or not pr["active"]:
            return "❌ Такого промокода нет."
        if pr["expires"] and pr["expires"] < time.time():
            return "⌛ Срок действия промокода истёк."
        if pr["max_uses"] and pr["uses"] >= pr["max_uses"]:
            return "😔 Промокод уже использован максимальное число раз."
        async with db.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, uid)) as cur:
            if await cur.fetchone():
                return "Ты уже активировал этот промокод."
        await db.execute("INSERT INTO promo_uses(code,user_id) VALUES(?,?)", (code, uid))
        await db.execute("UPDATE promos SET uses=uses+1 WHERE code=?", (code,))
        await db.commit()
    items = {pr["item"]: pr["qty"]} if pr["item"] in ITEMS else None
    res = await give_reward(uid, gold=pr["gold"], xp=pr["xp"], items=items, vip_days=pr["vip_days"])
    return f"🎟 Промокод <b>{esc(code)}</b> активирован!\nПолучено: {res}"


@router.message(Command("promo"))
async def cmd_promo(m: Message, command: CommandObject):
    if not await need_player(m):
        return
    if not command.args:
        return await m.answer("Использование: /promo КОД")
    if time.time() - LAST_PROMO.get(m.from_user.id, 0) < 3:
        return await m.answer("⏳ Не так быстро, подожди пару секунд.")
    LAST_PROMO[m.from_user.id] = time.time()
    await m.answer(await redeem(m.from_user.id, command.args.split()[0]))


# ───────────────────────────── РЕФЕРАЛКИ ─────────────────────────────


async def do_ref(ctx: Ctx):
    p = await ctx_player(ctx)
    if not p:
        return
    async with db.execute("SELECT COUNT(*) c, COALESCE(SUM(rewarded),0) r FROM referrals WHERE referrer_id=?",
                          (ctx.uid,)) as cur:
        row = await cur.fetchone()
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{ctx.uid}"
    nxt = next((n for n in sorted(REF_MILESTONES) if n > row["r"]), None)
    nxt_text = ""
    if nxt:
        key, qty = REF_MILESTONES[nxt]
        nxt_text = f"\n🏅 До награды «{ITEMS[key]['name']} ×{qty}»: ещё {nxt - row['r']} друзей"
    text = (
        "👥 <b>Приглашай друзей!</b>\n\n"
        f"Твоя личная ссылка:\n<code>{link}</code>\n\n"
        f"За каждого друга, создавшего персонажа, ты получаешь <b>{REF_GOLD} 💰</b> и зелье, "
        f"а друг — <b>{REF_NEW_GOLD} 💰</b>. Плюс {REF_DONATE_PERCENT}% золота от его донатов.\n\n"
        f"Перешли по ссылке: {row['c']} · Создали персонажа: {row['r']}{nxt_text}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Поделиться ссылкой",
              url=f"https://t.me/share/url?url={quote(link)}&text={quote('Го играть в RP-бота! Держи бонус 🎁')}")
    kb.button(text=NAV_LABELS["profile"], callback_data="go:profile")
    kb.adjust(1)
    await ctx.send(text, reply_markup=kb.as_markup())


@router.message(Command("ref", "referral"))
async def cmd_ref(m: Message):
    await do_ref(ctx_msg(m))


# ───────────────────────────── АДМИН-ПАНЕЛЬ ─────────────────────────────


class AdminS(StatesGroup):
    wait = State()


class AdminError(Exception):
    pass


async def adm_target(arg: str):
    try:
        uid = int(arg)
    except ValueError:
        raise AdminError("ID должен быть числом")
    p = await get_player(uid)
    if not p:
        raise AdminError("Игрок с таким ID не найден (у него нет персонажа)")
    return p


async def a_gold(bot, raw):
    a = raw.split()
    p, amt = await adm_target(a[0]), int(a[1])
    new = max(0, p["gold"] + amt)
    await update_player(p["user_id"], gold=new)
    return f"💰 {esc(p['name'])}: {amt:+d} золота (теперь {new})"


async def a_xp(bot, raw):
    a = raw.split()
    p, amt = await adm_target(a[0]), int(a[1])
    if amt <= 0:
        raise AdminError("Опыт должен быть положительным")
    res = await give_reward(p["user_id"], xp=amt)
    return f"✨ {esc(p['name'])}: {res}"


async def a_item(bot, raw):
    a = raw.split()
    p = await adm_target(a[0])
    key, qty = a[1], int(a[2]) if len(a) > 2 else 1
    if key not in ITEMS:
        raise AdminError("Нет такого предмета. Список ID — кнопка «📦 ID предметов».")
    if qty < 1:
        raise AdminError("Количество должно быть ≥ 1")
    await add_item(p["user_id"], key, qty)
    return f"🎁 {esc(p['name'])}: {ITEMS[key]['name']} ×{qty}"


async def a_vip(bot, raw):
    a = raw.split()
    p, days = await adm_target(a[0]), int(a[1])
    if days <= 0:
        await update_player(p["user_id"], vip_until=0)
        return f"👑 VIP у {esc(p['name'])} снят"
    await add_vip(p["user_id"], days)
    return f"👑 {esc(p['name'])}: VIP +{days} дн."


async def a_heal(bot, raw):
    p = await adm_target(raw.split()[0])
    await update_player(p["user_id"], hp=p["max_hp"], last_adv=0, last_rest=0, last_daily=0)
    return f"❤️ {esc(p['name'])}: здоровье восстановлено, кулдауны сброшены"


async def a_find(bot, raw):
    p = await adm_target(raw.split()[0])
    inv = await get_inventory(p["user_id"])
    inv_text = ", ".join(f"{ITEMS[k]['name']} ×{q}" for k, q in inv.items() if k in ITEMS) or "пусто"
    ban = " 🚫 ЗАБАНЕН" if p["user_id"] in BANNED else ""
    return f"🔍 ID <code>{p['user_id']}</code>{ban}\n\n{await profile_text(p)}\n\n🎒 {inv_text}"


async def a_delchar(bot, raw):
    p = await adm_target(raw.split()[0])
    await db.execute("DELETE FROM players WHERE user_id=?", (p["user_id"],))
    await db.execute("DELETE FROM inventory WHERE user_id=?", (p["user_id"],))
    await db.commit()
    return f"🗑 Персонаж {esc(p['name'])} удалён"


async def a_ban(bot, raw):
    uid = int(raw.split()[0])
    if is_owner(uid):
        raise AdminError("Нельзя банить админа")
    await db.execute("INSERT OR IGNORE INTO bans(user_id) VALUES(?)", (uid,))
    await db.commit()
    BANNED.add(uid)
    return f"🚫 Пользователь {uid} заблокирован"


async def a_unban(bot, raw):
    uid = int(raw.split()[0])
    await db.execute("DELETE FROM bans WHERE user_id=?", (uid,))
    await db.commit()
    BANNED.discard(uid)
    return f"✅ Пользователь {uid} разблокирован"


async def a_promo(bot, raw):
    a = raw.split()

    def arg(i, default="0"):
        return a[i] if len(a) > i else default

    code = a[0].upper()
    if not re.fullmatch(r"[A-Z0-9_\-]{3,32}", code):
        raise AdminError("Код: 3–32 символа, латиница, цифры, _ и -")
    gold, xp, max_uses, days = int(arg(1)), int(arg(2)), int(arg(3)), int(arg(4))
    item, qty, vip = arg(5, "-"), int(arg(6, "1")), int(arg(7))
    if item in ("-", "0", ""):
        item = ""
    elif item not in ITEMS:
        raise AdminError("Нет такого предмета. Список ID — кнопка «📦 ID предметов».")
    if min(gold, xp, max_uses, days, vip) < 0 or qty < 1:
        raise AdminError("Числа не могут быть отрицательными")
    if not (gold or xp or item or vip):
        raise AdminError("Промокод ничего не даёт")
    expires = time.time() + days * 86400 if days else 0
    try:
        await db.execute(
            "INSERT INTO promos(code,gold,xp,item,qty,vip_days,max_uses,expires) VALUES(?,?,?,?,?,?,?,?)",
            (code, gold, xp, item, qty, vip, max_uses, expires))
        await db.commit()
    except sqlite3.IntegrityError:
        raise AdminError("Такой промокод уже существует")
    lim = f"{max_uses} активаций" if max_uses else "без лимита активаций"
    exp = f"до {fmt_date(expires)}" if expires else "бессрочно"
    row = await (await db.execute("SELECT * FROM promos WHERE code=?", (code,))).fetchone()
    return f"🎟 Промокод <code>{code}</code> создан\nНаграда: {promo_desc(row)}\n{lim}, {exp}"


async def a_delpromo(bot, raw):
    code = raw.split()[0].upper()
    async with db.execute("DELETE FROM promos WHERE code=?", (code,)) as cur:
        n = cur.rowcount
    await db.execute("DELETE FROM promo_uses WHERE code=?", (code,))
    await db.commit()
    if not n:
        raise AdminError("Такого промокода нет")
    return f"🗑 Промокод {esc(code)} удалён"


async def a_togglepromo(bot, raw):
    code = raw.split()[0].upper()
    async with db.execute("UPDATE promos SET active=1-active WHERE code=?", (code,)) as cur:
        n = cur.rowcount
    await db.commit()
    if not n:
        raise AdminError("Такого промокода нет")
    row = await (await db.execute("SELECT active FROM promos WHERE code=?", (code,))).fetchone()
    return f"🎟 Промокод {esc(code)}: {'включён ✅' if row['active'] else 'выключен ⏸'}"


async def a_refund(bot, raw):
    a = raw.split()
    uid, charge = int(a[0]), a[1]
    row = await (await db.execute("SELECT * FROM payments WHERE charge_id=?", (charge,))).fetchone()
    if not row or row["user_id"] != uid:
        raise AdminError("Платёж не найден (проверь ID и номер платежа)")
    if row["refunded"]:
        raise AdminError("Этот платёж уже возвращён")
    try:
        await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge)
    except Exception as e:
        raise AdminError(f"Telegram отклонил возврат: {esc(str(e))}")
    await db.execute("UPDATE payments SET refunded=1 WHERE charge_id=?", (charge,))
    await db.commit()
    return f"↩️ Возвращено {row['stars']} ⭐ пользователю {uid}.\nВыданные товары автоматически НЕ забираются — при необходимости спиши золото/предметы вручную."


async def a_goldall(bot, raw):
    amt = int(raw.split()[0])
    async with db.execute("UPDATE players SET gold=MAX(0, gold+?)", (amt,)) as cur:
        n = cur.rowcount
    await db.commit()
    return f"💰 Всем игрокам ({n}): {amt:+d} золота"


async def a_cast(bot, raw):
    async with db.execute("SELECT user_id FROM players") as cur:
        ids = [r["user_id"] for r in await cur.fetchall()]
    ok = fail = 0
    for uid in ids:
        try:
            try:
                await bot.send_message(uid, raw)
            except TelegramBadRequest:
                await bot.send_message(uid, raw, parse_mode=None)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    return f"📢 Рассылка завершена: доставлено {ok}, не доставлено {fail}"


# ключ: (текст кнопки, подсказка для ввода, функция)
ADMIN_INPUT = {
    "gold": ("💰 Выдать золото", "💰 <b>Выдать золото</b>\nФормат: <code>ID сумма</code>\nПример: <code>123456789 500</code>\n(отрицательное число — забрать)", a_gold),
    "xp": ("✨ Выдать опыт", "✨ <b>Выдать опыт</b>\nФормат: <code>ID количество</code>", a_xp),
    "item": ("🎁 Выдать предмет", "🎁 <b>Выдать предмет</b>\nФормат: <code>ID предмет [кол-во]</code>\nПример: <code>123456789 lootbox 3</code>", a_item),
    "vip": ("👑 Выдать VIP", "👑 <b>VIP</b>\nФормат: <code>ID дней</code> (0 — снять VIP)", a_vip),
    "heal": ("❤️ Вылечить/сбросить КД", "❤️ Отправь <code>ID</code> игрока", a_heal),
    "find": ("🔍 Найти игрока", "🔍 Отправь <code>ID</code> игрока", a_find),
    "delchar": ("🗑 Удалить персонажа", "🗑 Отправь <code>ID</code> игрока — его персонаж будет удалён безвозвратно", a_delchar),
    "ban": ("🚫 Бан", "🚫 Отправь <code>ID</code> пользователя для блокировки", a_ban),
    "unban": ("✅ Разбан", "✅ Отправь <code>ID</code> пользователя для разблокировки", a_unban),
    "promo": ("🎟 Создать промокод",
              "🎟 <b>Новый промокод</b>\nФормат:\n<code>КОД золото [опыт] [макс_активаций] [дней_действия] [предмет] [кол-во] [vip_дней]</code>\n\n"
              "0 — «нет/без лимита», «-» вместо предмета — без предмета.\n"
              "Примеры:\n<code>START 500</code> — 500 золота, без лимита\n"
              "<code>SUMMER 300 100 50 7 lootbox 2 3</code> — 300 💰, 100 XP, 50 активаций, 7 дней, 2 сундука, VIP на 3 дня", a_promo),
    "delpromo": ("🗑 Удалить промокод", "🗑 Отправь <code>КОД</code> промокода", a_delpromo),
    "togglepromo": ("⏸ Вкл/выкл промокод", "⏸ Отправь <code>КОД</code> — промокод переключится (вкл/выкл)", a_togglepromo),
    "refund": ("↩️ Вернуть звёзды", "↩️ <b>Возврат Stars</b>\nФормат: <code>ID_пользователя номер_платежа</code>\nНомер платежа — в кнопке «💳 Платежи»", a_refund),
    "goldall": ("🌍 Золото всем", "🌍 <b>Золото всем игрокам</b>\nОтправь сумму, например <code>100</code>", a_goldall),
    "cast": ("📢 Рассылка", "📢 <b>Рассылка всем игрокам</b>\nОтправь текст сообщения (можно HTML-разметку)", a_cast),
}


async def scalar(q: str, *args):
    async with db.execute(q, args) as cur:
        row = await cur.fetchone()
    return row[0] or 0


async def v_stats() -> str:
    now = time.time()
    return (
        "📊 <b>Статистика</b>\n\n"
        f"👤 Игроков: {await scalar('SELECT COUNT(*) FROM players')}\n"
        f"💰 Золота в обороте: {await scalar('SELECT SUM(gold) FROM players')}\n"
        f"👑 Активных VIP: {await scalar('SELECT COUNT(*) FROM players WHERE vip_until>?', now)}\n"
        f"💎 Платежей: {await scalar('SELECT COUNT(*) FROM payments WHERE refunded=0')} "
        f"на {await scalar('SELECT SUM(stars) FROM payments WHERE refunded=0')} ⭐\n"
        f"👥 Рефералов (всего/активных): {await scalar('SELECT COUNT(*) FROM referrals')}/"
        f"{await scalar('SELECT COUNT(*) FROM referrals WHERE rewarded=1')}\n"
        f"🎟 Промокодов: {await scalar('SELECT COUNT(*) FROM promos')}\n"
        f"🚫 Банов: {len(BANNED)}"
    )


async def v_promos() -> str:
    async with db.execute("SELECT * FROM promos ORDER BY rowid DESC LIMIT 30") as cur:
        rows = await cur.fetchall()
    if not rows:
        return "🎟 Промокодов пока нет."
    lines = []
    for r in rows:
        lim = f"{r['uses']}/{r['max_uses']}" if r["max_uses"] else f"{r['uses']}/∞"
        exp = f", до {fmt_date(r['expires'])}" if r["expires"] else ""
        state = "✅" if r["active"] and not (r["expires"] and r["expires"] < time.time()) else "⏸"
        lines.append(f"{state} <code>{r['code']}</code> — {promo_desc(r)} · {lim}{exp}")
    return "🎟 <b>Промокоды</b>\n\n" + "\n".join(lines)


async def v_payments() -> str:
    async with db.execute("SELECT * FROM payments ORDER BY created DESC LIMIT 10") as cur:
        rows = await cur.fetchall()
    if not rows:
        return "💳 Платежей пока нет."
    lines = [f"{fmt_date(r['created'])} · <code>{r['user_id']}</code> · {r['pack']} · {r['stars']}⭐"
             f"{' ↩️' if r['refunded'] else ''}\n<code>{esc(r['charge_id'])}</code>" for r in rows]
    return "💳 <b>Последние платежи</b>\n\n" + "\n\n".join(lines)


async def v_items() -> str:
    lines = [f"<code>{k}</code> — {it['name']} ({it['price']}💰)" for k, it in ITEMS.items()]
    return "📦 <b>ID предметов</b>\n\n" + "\n".join(lines)


ADMIN_VIEWS = {
    "stats": ("📊 Статистика", v_stats),
    "promos": ("📋 Список промокодов", v_promos),
    "payments": ("💳 Платежи", v_payments),
    "items": ("📦 ID предметов", v_items),
}


def admin_kb():
    kb = InlineKeyboardBuilder()
    for key, (label, _) in ADMIN_VIEWS.items():
        kb.button(text=label, callback_data=f"adm:{key}")
    for key, (label, _, _) in ADMIN_INPUT.items():
        kb.button(text=label, callback_data=f"adm:{key}")
    kb.adjust(2)
    return kb.as_markup()


def admin_cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data="adm:cancel")
    return kb.as_markup()


@router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    if not is_owner(m.from_user.id):
        return
    if m.chat.type != "private":
        return await m.answer("Админ-панель открывается только в личке со мной.")
    await state.clear()
    await m.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_kb())


@router.callback_query(F.data.startswith("adm:"))
async def adm_cb(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    act = cb.data.split(":", 1)[1]
    await cb.answer()
    if act == "cancel":
        await state.clear()
        return await safe_edit(cb.message, "🛠 <b>Админ-панель</b>", admin_kb())
    if act in ADMIN_VIEWS:
        text = (await ADMIN_VIEWS[act][1]())[:3800]
        return await safe_edit(cb.message, text, admin_kb())
    if act in ADMIN_INPUT:
        await state.set_state(AdminS.wait)
        await state.update_data(act=act)
        await cb.message.answer(ADMIN_INPUT[act][1], reply_markup=admin_cancel_kb())


@router.message(StateFilter(AdminS.wait), F.text, ~F.text.startswith("/"))
async def admin_input(m: Message, state: FSMContext):
    if not is_owner(m.from_user.id):
        return await state.clear()
    act = (await state.get_data()).get("act")
    await state.clear()
    entry = ADMIN_INPUT.get(act)
    if not entry:
        return
    try:
        result = await entry[2](m.bot, m.text.strip())
    except AdminError as e:
        result = f"⚠️ {e}"
    except (ValueError, IndexError):
        result = "⚠️ Неверный формат. Проверь пример и попробуй снова."
    except Exception:
        logging.exception("Admin action failed")
        result = "⚠️ Ошибка при выполнении, подробности в логах."
    await m.answer(result, reply_markup=admin_kb())


# ───────────────────────────── ОБРАБОТКА КНОПОК ─────────────────────────────

ACTIONS = {
    "profile": do_profile, "adventure": do_adventure, "rest": do_rest, "daily": do_daily,
    "shop": do_shop, "inventory": do_inventory, "top": do_top, "dice": do_dice, "help": do_help,
    "ref": do_ref, "donate": do_donate,
}


@router.message(F.text.in_(list(MENU_BUTTONS)))
async def menu_text(m: Message):
    await ACTIONS[MENU_BUTTONS[m.text]](ctx_msg(m))


@router.callback_query(F.data.startswith("go:"))
async def go_cb(cb: CallbackQuery):
    fn = ACTIONS.get(cb.data.split(":", 1)[1])
    if fn:
        await fn(ctx_cb(cb))
    await cb.answer()


# ───────────────────────────── ДУЭЛИ ─────────────────────────────

DUELS: dict[str, tuple[int, int]] = {}


@router.message(Command("duel"))
async def cmd_duel(m: Message):
    p = await need_player(m)
    if not p:
        return
    if not m.reply_to_message or m.reply_to_message.from_user.id == m.from_user.id \
            or m.reply_to_message.from_user.is_bot:
        return await m.answer("Ответь командой /duel на сообщение того, кого вызываешь.")
    target = m.reply_to_message.from_user
    tp = await get_player(target.id)
    if not tp:
        return await m.answer("У соперника нет персонажа.")
    duel_id = f"{m.chat.id}{m.message_id}"
    DUELS[duel_id] = (m.from_user.id, target.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="⚔️ Принять", callback_data=f"duel:yes:{duel_id}")
    kb.button(text="🏳 Отказаться", callback_data=f"duel:no:{duel_id}")
    await m.answer(f"⚔️ <b>{esc(p['name'])}</b> вызывает <b>{esc(tp['name'])}</b> на дуэль!",
                   reply_markup=kb.as_markup())


async def make_fighter(p) -> dict:
    atk, df, luck = await gear_bonus(p["user_id"])
    return dict(name=p["name"], hp=p["max_hp"], power=p[CLASSES[p["cls"]]["main"]] + atk,
                defense=df + p["agility"] // 2, crit=min(50, p["agility"] * 2 + luck * 2))


def hit(att: dict, dfn: dict) -> tuple[int, bool]:
    dmg = max(1, att["power"] + random.randint(1, 8) - dfn["defense"] // 2)
    crit = random.randint(1, 100) <= att["crit"]
    return (dmg * 2 if crit else dmg), crit


@router.callback_query(F.data.startswith("duel:"))
async def duel_cb(cb: CallbackQuery):
    _, ans, duel_id = cb.data.split(":")
    duel = DUELS.get(duel_id)
    if not duel:
        return await cb.answer("Дуэль устарела", show_alert=True)
    challenger_id, target_id = duel
    if cb.from_user.id != target_id:
        return await cb.answer("Эта дуэль не для тебя", show_alert=True)
    DUELS.pop(duel_id, None)
    if ans == "no":
        return await cb.message.edit_text("🏳 Вызов отклонён.")

    pa, pb = await get_player(challenger_id), await get_player(target_id)
    if not pa or not pb:
        return await cb.message.edit_text("Один из персонажей исчез.")
    a, b = await make_fighter(pa), await make_fighter(pb)
    log, turn = [], random.choice([0, 1])
    fighters = [a, b]
    for rnd in range(1, 31):
        att, dfn = fighters[turn], fighters[1 - turn]
        dmg, crit = hit(att, dfn)
        dfn["hp"] -= dmg
        log.append(f"{rnd}. {esc(att['name'])} → {esc(dfn['name'])}: -{dmg}{' 💥' if crit else ''}")
        if dfn["hp"] <= 0:
            break
        turn = 1 - turn
    winner = a if a["hp"] > b["hp"] else b
    win_p, lose_p = (pa, pb) if winner is a else (pb, pa)
    await update_player(win_p["user_id"], wins=win_p["wins"] + 1)
    await update_player(lose_p["user_id"], losses=lose_p["losses"] + 1)
    note = await grant(win_p["user_id"], xp=25, gold=10)
    shown = "\n".join(log[-8:])
    await cb.message.edit_text(
        f"⚔️ <b>{esc(a['name'])}</b> vs <b>{esc(b['name'])}</b>\n\n{shown}\n\n"
        f"🏆 Победитель: <b>{esc(winner['name'])}</b> (+25 XP, +10 💰){note}"
    )
    await cb.answer()


# ───────────────────────────── БАН-МИДЛВАРЬ И ЗАПУСК ─────────────────────────────


class BanMW(BaseMiddleware):
    """Игнорирует сообщения и нажатия кнопок от заблокированных пользователей."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and user.id in BANNED and not is_owner(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Ты заблокирован.", show_alert=True)
            return
        return await handler(event, data)


async def main():
    global BOT_USERNAME
    if not BOT_TOKEN or "ВСТАВЬ" in BOT_TOKEN:
        raise SystemExit("Впиши токен бота в строку BOT_TOKEN в начале main.py")
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(BanMW())
    dp.callback_query.outer_middleware(BanMW())
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="menu", description="Открыть меню с кнопками"),
        BotCommand(command="create", description="Создать персонажа"),
        BotCommand(command="profile", description="Профиль героя"),
        BotCommand(command="adventure", description="Приключение"),
        BotCommand(command="rest", description="Отдых"),
        BotCommand(command="daily", description="Ежедневная награда"),
        BotCommand(command="shop", description="Магазин"),
        BotCommand(command="inventory", description="Инвентарь"),
        BotCommand(command="promo", description="Активировать промокод"),
        BotCommand(command="ref", description="Реферальная ссылка"),
        BotCommand(command="donate", description="Поддержать проект ⭐"),
        BotCommand(command="duel", description="Дуэль (ответом)"),
        BotCommand(command="roll", description="Бросок кубиков"),
        BotCommand(command="me", description="Действие персонажа"),
        BotCommand(command="do", description="Описание обстановки"),
        BotCommand(command="scene", description="Сцена чата"),
        BotCommand(command="gm", description="ИИ-рассказчик"),
        BotCommand(command="top", description="Рейтинг"),
        BotCommand(command="help", description="Помощь"),
    ])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
