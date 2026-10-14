"""
Telegram RP-бот: персонажи, кубики, приключения, дуэли, магазин, ИИ-рассказчик.
Стек: aiogram 3, aiosqlite, (опционально) anthropic.
"""
import asyncio
import html
import logging
import os
import random
import re
import time
from collections import defaultdict, deque
import subprocess
import sys

# ═══════════════════════ НАСТРОЙКИ — ВПИШИ СЮДА ═══════════════════════
BOT_TOKEN = ""      # токен от @BotFather, внутри кавычек
ANTHROPIC_API_KEY = ""               # необязательно: ключ для ИИ-рассказчика /gm
AI_MODEL = "claude-sonnet-5-5"
DB_PATH = "rp_bot.db"
# ═══════════════════════════════════════════════════════════════════════

BOT_TOKEN = os.getenv("BOT_TOKEN") or BOT_TOKEN
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or ANTHROPIC_API_KEY


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
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

logging.basicConfig(level=logging.INFO)


ai = None
if ANTHROPIC_API_KEY:
    from anthropic import AsyncAnthropic

    ai = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

router = Router()
db: aiosqlite.Connection

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
ITEMS = {
    "heal": dict(name="Зелье лечения", type="potion", price=30, heal=50),
    "heal_big": dict(name="Большое зелье", type="potion", price=80, heal=150),
    "sword": dict(name="Стальной меч", type="weapon", price=150, bonus=3),
    "great_sword": dict(name="Клинок героя", type="weapon", price=500, bonus=6),
    "leather": dict(name="Кожаная броня", type="armor", price=120, bonus=3),
    "plate": dict(name="Латные доспехи", type="armor", price=450, bonus=7),
}
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
    bio TEXT DEFAULT '', last_daily REAL DEFAULT 0, last_rest REAL DEFAULT 0, last_adv REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS inventory(
    user_id INTEGER, item TEXT, qty INTEGER, PRIMARY KEY(user_id, item)
);
CREATE TABLE IF NOT EXISTS scenes(chat_id INTEGER PRIMARY KEY, text TEXT);
"""

# ───────────────────────────── БАЗА ДАННЫХ ─────────────────────────────


async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()


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


async def gear_bonus(uid: int) -> tuple[int, int]:
    """Лучшее оружие и лучшая броня из инвентаря."""
    inv = await get_inventory(uid)
    atk = max([ITEMS[i]["bonus"] for i in inv if ITEMS.get(i, {}).get("type") == "weapon"], default=0)
    df = max([ITEMS[i]["bonus"] for i in inv if ITEMS.get(i, {}).get("type") == "armor"], default=0)
    return atk, df


async def get_scene(chat_id: int) -> str:
    async with db.execute("SELECT text FROM scenes WHERE chat_id=?", (chat_id,)) as cur:
        row = await cur.fetchone()
    return row["text"] if row else ""


async def grant(uid: int, xp: int = 0, gold: int = 0) -> str:
    """Выдаёт опыт и золото, обрабатывает повышение уровня. Возвращает текст о level up."""
    p = await get_player(uid)
    lvl, cur_xp, max_hp = p["level"], p["xp"] + xp, p["max_hp"]
    main = CLASSES[p["cls"]]["main"]
    stat = p[main]
    note = ""
    while cur_xp >= lvl * 100:
        cur_xp -= lvl * 100
        lvl += 1
        max_hp += 10
        stat += 1
        note += f"\n🎉 <b>Новый уровень: {lvl}!</b> Макс. HP +10, {main_name(main)} +1"
    fields = dict(level=lvl, xp=cur_xp, max_hp=max_hp, gold=p["gold"] + gold, **{main: stat})
    if note:
        fields["hp"] = max_hp
    await update_player(uid, **fields)
    return note


# ───────────────────────────── УТИЛИТЫ ─────────────────────────────


def esc(s: str) -> str:
    return html.escape(s or "")


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


async def need_player(m: Message):
    p = await get_player(m.from_user.id)
    if not p:
        await m.answer("У тебя ещё нет персонажа. Напиши мне в личку: /create")
    return p


async def is_admin(m: Message) -> bool:
    if m.chat.type == "private":
        return True
    member = await m.bot.get_chat_member(m.chat.id, m.from_user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)


async def profile_text(p) -> str:
    atk, df = await gear_bonus(p["user_id"])
    c = CLASSES[p["cls"]]
    text = (
        f"{c['emoji']} <b>{esc(p['name'])}</b> — {p['race']} {p['cls']}\n"
        f"Ур. {p['level']} · XP {p['xp']}/{p['level'] * 100}\n"
        f"❤️ {bar(p['hp'], p['max_hp'])} {p['hp']}/{p['max_hp']}\n"
        f"💪 {p['strength']}  🏹 {p['agility']}  🧠 {p['intellect']}\n"
        f"🗡 Атака +{atk}  🛡 Защита +{df}\n"
        f"💰 {p['gold']} золота\n"
        f"🏆 Побед: {p['wins']} · Поражений: {p['losses']}"
    )
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
/top — рейтинг героев"""


# ───────────────────────────── КНОПКИ И КОНТЕКСТ ─────────────────────────────

NO_CHAR = "У тебя ещё нет персонажа. Напиши мне в личку и нажми /create"

MENU_BUTTONS = {
    "👤 Профиль": "profile", "⚔️ Приключение": "adventure", "🏕 Отдых": "rest",
    "🎁 Награда": "daily", "🛒 Магазин": "shop", "🎒 Инвентарь": "inventory",
    "🏆 Рейтинг": "top", "❓ Помощь": "help",
}
NAV_LABELS = {v: k for k, v in MENU_BUTTONS.items()}
DICE_BUTTONS = ["d4", "d6", "d8", "d10", "d12", "d20", "d100", "2d6"]


class Ctx:
    """Одинаково работает и для сообщений, и для нажатий inline-кнопок."""

    def __init__(self, user, chat_id, send):
        self.uid, self.name, self.chat_id, self.send = user.id, user.full_name, chat_id, send


def ctx_msg(m: Message) -> Ctx:
    return Ctx(m.from_user, m.chat.id, m.answer)


def ctx_cb(cb: CallbackQuery) -> Ctx:
    return Ctx(cb.from_user, cb.message.chat.id, cb.message.answer)


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


@router.message(Command("start", "help", "menu"))
async def cmd_start(m: Message):
    await m.answer(HELP, reply_markup=reply_menu())


# ───────────────────────────── СОЗДАНИЕ ПЕРСОНАЖА# ───────────────────────────── СОЗДАНИЕ ПЕРСОНАЖА ─────────────────────────────


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
    p = await get_player(cb.from_user.id)
    await cb.message.edit_text("✅ Персонаж создан! Стартовый набор: 100 💰 и 2 зелья лечения.\n\n" + await profile_text(p))
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
    atk, df = await gear_bonus(p["user_id"])
    main = p[CLASSES[p["cls"]]["main"]]
    roll = random.randint(1, 20)
    score, need = roll + main + atk, 10 + p["level"] * 2 + mod
    await update_player(p["user_id"], last_adv=time.time())
    kb = nav_kb("adventure", "rest", "inventory", "profile")

    header = f"{name}\n🎲 d20: {roll} + навык {main} + оружие {atk} = <b>{score}</b> (нужно {need})\n\n"
    if score >= need or roll == 20:
        gold = random.randint(g_lo, g_hi) + p["level"] * 3
        gain = xp + p["level"] * 5
        await update_player(p["user_id"], wins=p["wins"] + 1)
        note = await grant(p["user_id"], xp=gain, gold=gold)
        loot = ""
        if random.random() < 0.15:
            await add_item(p["user_id"], "heal")
            loot = "\n🧪 Найдено: Зелье лечения!"
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
    if it["type"] == "potion":
        return f"восст. {it['heal']} HP"
    return f"{'атака' if it['type'] == 'weapon' else 'защита'} +{it['bonus']}"


async def do_shop(ctx: Ctx):
    p = await ctx_player(ctx)
    if not p:
        return
    kb = InlineKeyboardBuilder()
    lines = []
    for key, it in ITEMS.items():
        lines.append(f"• <b>{it['name']}</b> — {it['price']} 💰 ({item_desc(key)})")
        kb.button(text=f"{it['name']} · {it['price']}💰", callback_data=f"buy:{ctx.uid}:{key}")
    kb.button(text=NAV_LABELS["inventory"], callback_data="go:inventory")
    kb.adjust(1)
    await ctx.send(f"🛒 <b>Магазин</b> (у тебя {p['gold']} 💰)\n\n" + "\n".join(lines), reply_markup=kb.as_markup())


@router.message(Command("shop"))
async def cmd_shop(m: Message):
    await do_shop(ctx_msg(m))


@router.callback_query(F.data.startswith("buy:"))
async def buy_cb(cb: CallbackQuery):
    _, uid, key = cb.data.split(":")
    if cb.from_user.id != int(uid):
        return await cb.answer("Это чужой магазин, открой свой: /shop", show_alert=True)
    p, it = await get_player(cb.from_user.id), ITEMS.get(key)
    if not p or not it:
        return await cb.answer()
    if p["gold"] < it["price"]:
        return await cb.answer("Не хватает золота 💸", show_alert=True)
    if it["type"] != "potion" and key in await get_inventory(p["user_id"]):
        return await cb.answer("Это у тебя уже есть", show_alert=True)
    await update_player(p["user_id"], gold=p["gold"] - it["price"])
    await add_item(p["user_id"], key)
    await cb.answer(f"Куплено: {it['name']}! Осталось {p['gold'] - it['price']} 💰", show_alert=True)


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
        if it["type"] == "potion":
            kb.button(text=f"Выпить: {it['name']}", callback_data=f"use:{ctx.uid}:{key}")
    kb.button(text=NAV_LABELS["shop"], callback_data="go:shop")
    kb.button(text=NAV_LABELS["profile"], callback_data="go:profile")
    kb.adjust(1)
    await ctx.send("🎒 <b>Инвентарь</b>\n\n" + "\n".join(lines) +
                   "\n\n<i>Оружие и броня работают автоматически — берётся лучшее.</i>",
                   reply_markup=kb.as_markup())


@router.message(Command("inventory", "inv"))
async def cmd_inventory(m: Message):
    await do_inventory(ctx_msg(m))


@router.callback_query(F.data.startswith("use:"))
async def use_cb(cb: CallbackQuery):
    _, uid, key = cb.data.split(":")
    if cb.from_user.id != int(uid):
        return await cb.answer("Это не твой инвентарь", show_alert=True)
    p, it = await get_player(cb.from_user.id), ITEMS.get(key)
    if not p or not it or it["type"] != "potion":
        return await cb.answer()
    if p["hp"] >= p["max_hp"]:
        return await cb.answer("Здоровье и так полное", show_alert=True)
    if not await remove_item(p["user_id"], key):
        return await cb.answer("Такого предмета больше нет", show_alert=True)
    new_hp = min(p["max_hp"], p["hp"] + it["heal"])
    await update_player(p["user_id"], hp=new_hp)
    await cb.answer(f"❤️ Здоровье: {new_hp}/{p['max_hp']}", show_alert=True)


# ───────────────────────────── ОБРАБОТКА КНОПОК ─────────────────────────────

ACTIONS = {
    "profile": do_profile, "adventure": do_adventure, "rest": do_rest, "daily": do_daily,
    "shop": do_shop, "inventory": do_inventory, "top": do_top, "dice": do_dice, "help": do_help,
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
    atk, df = await gear_bonus(p["user_id"])
    return dict(name=p["name"], hp=p["max_hp"], power=p[CLASSES[p["cls"]]["main"]] + atk,
                defense=df + p["agility"] // 2, crit=min(40, p["agility"] * 2))


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


# ───────────────────────────── ЗАПУСК ─────────────────────────────


async def main():
    if not BOT_TOKEN or "ВСТАВЬ" in BOT_TOKEN:
        raise SystemExit("Впиши токен бота в строку BOT_TOKEN в начале main.py")
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
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