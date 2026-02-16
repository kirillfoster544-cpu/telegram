import os
import re
import time
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# =========================
# CONFIG (Railway Variables)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "7489815425").strip())
DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty. Set Railway variable BOT_TOKEN")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

TTL_SECONDS = 15 * 60  # 15 минут на продолжение после открытия ссылки


# =========================
# DB
# =========================
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def today_key() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            code TEXT UNIQUE,
            lang TEXT DEFAULT 'ru',
            created_at INTEGER
        );
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS pending (
            from_id INTEGER PRIMARY KEY,
            to_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id INTEGER NOT NULL,
            to_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            user_id INTEGER PRIMARY KEY,
            link_clicks_total INTEGER DEFAULT 0,
            link_clicks_today INTEGER DEFAULT 0,
            msgs_total INTEGER DEFAULT 0,
            msgs_today INTEGER DEFAULT 0,
            last_day TEXT DEFAULT ''
        );
        """)
        con.commit()


def _gen_code(n: int = 10) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    import random
    return "".join(random.choice(alphabet) for _ in range(n))


def upsert_user(user_id: int, username: str, full_name: str) -> str:
    with db() as con:
        row = con.execute("SELECT code FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            con.execute(
                "UPDATE users SET username=?, full_name=? WHERE user_id=?",
                (username, full_name, user_id),
            )
            con.commit()
            return row["code"]

        while True:
            code = _gen_code(10)
            exists = con.execute("SELECT 1 FROM users WHERE code=?", (code,)).fetchone()
            if not exists:
                break

        con.execute(
            "INSERT INTO users (user_id, username, full_name, code, created_at) VALUES (?,?,?,?,?)",
            (user_id, username, full_name, code, int(time.time())),
        )
        con.commit()
        return code


def get_user(user_id: int):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def get_user_by_code(code: str):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE code=?", (code,)).fetchone()


def ensure_stats(user_id: int):
    t = today_key()
    with db() as con:
        row = con.execute("SELECT * FROM stats WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            con.execute(
                "INSERT INTO stats (user_id, link_clicks_total, link_clicks_today, msgs_total, msgs_today, last_day) "
                "VALUES (?,?,?,?,?,?)",
                (user_id, 0, 0, 0, 0, t),
            )
            con.commit()
            return
        if row["last_day"] != t:
            con.execute(
                "UPDATE stats SET link_clicks_today=0, msgs_today=0, last_day=? WHERE user_id=?",
                (t, user_id),
            )
            con.commit()


def inc_click(user_id: int):
    ensure_stats(user_id)
    with db() as con:
        con.execute(
            "UPDATE stats SET link_clicks_total=link_clicks_total+1, link_clicks_today=link_clicks_today+1 "
            "WHERE user_id=?",
            (user_id,),
        )
        con.commit()


def inc_msg(user_id: int):
    ensure_stats(user_id)
    with db() as con:
        con.execute(
            "UPDATE stats SET msgs_total=msgs_total+1, msgs_today=msgs_today+1 WHERE user_id=?",
            (user_id,),
        )
        con.commit()


def get_stats(user_id: int):
    ensure_stats(user_id)
    with db() as con:
        return con.execute("SELECT * FROM stats WHERE user_id=?", (user_id,)).fetchone()


def set_pending(from_id: int, to_id: int):
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO pending (from_id, to_id, created_at) VALUES (?,?,?)",
            (from_id, to_id, int(time.time())),
        )
        con.commit()


def get_pending(from_id: int):
    with db() as con:
        return con.execute("SELECT * FROM pending WHERE from_id=?", (from_id,)).fetchone()


def clear_pending(from_id: int):
    with db() as con:
        con.execute("DELETE FROM pending WHERE from_id=?", (from_id,))
        con.commit()


def log_message(from_id: int, to_id: int, text: str):
    with db() as con:
        con.execute(
            "INSERT INTO logs (from_id, to_id, text, created_at) VALUES (?,?,?,?)",
            (from_id, to_id, text, int(time.time())),
        )
        con.commit()


def last_logs(limit: int = 20):
    with db() as con:
        return con.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# =========================
# HELPERS / UI
# =========================
def quote_link_block(link: str) -> str:
    return f"<blockquote><code>{link}</code></blockquote>"


def extract_code_from_link(text: str) -> str | None:
    m = re.search(r"start=([a-z0-9]{6,64})", text, flags=re.I)
    return m.group(1).lower() if m else None


def format_user(u) -> str:
    """
    Красиво + кликабельно:
    @username (id) или Full Name (id)
    """
    if not u:
        return "unknown"
    uid = int(u["user_id"])
    uname = (u["username"] or "").strip()
    full = (u["full_name"] or "").strip()

    if uname:
        return f"<a href='tg://user?id={uid}'>@{uname}</a> ({uid})"
    name = full if full else "user"
    return f"<a href='tg://user?id={uid}'>{name}</a> ({uid})"


async def get_my_link(user_id: int) -> str:
    u = get_user(user_id)
    me = await bot.get_me()
    return f"https://t.me/{me.username}?start={u['code']}"


def share_url(link: str) -> str:
    text = "Напиши мне анонимно 💬"
    return f"https://t.me/share/url?url={link}&text={text}"


async def kb_home(user_id: int) -> InlineKeyboardMarkup:
    me = await bot.get_me()
    link = await get_my_link(user_id)
    group_link = f"https://t.me/{me.username}?startgroup=1"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Поделиться ссылкой", url=share_url(link))],
        [InlineKeyboardButton(text="➕ Добавить бота в группу", url=group_link)],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="ui:stats"),
            InlineKeyboardButton(text="ℹ️ Помощь", callback_data="ui:help"),
        ],
    ])


def kb_back_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="ui:home")]
    ])


def kb_write_more() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать ещё", callback_data="ui:write_more")]
    ])


def kb_reply(sender_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ответить", callback_data=f"reply:{sender_id}")]
    ])


async def send_admin_log(from_id: int, to_id: int, text: str):
    fu = get_user(from_id)
    tu = get_user(to_id)

    msg = (
        "🛡 <b>ADMIN LOG</b>\n\n"
        f"👤 От: {format_user(fu)}\n"
        f"📩 Кому: {format_user(tu)}\n\n"
        f"💬 Текст:\n{text}"
    )
    await bot.send_message(ADMIN_ID, msg)


def start_text(link: str) -> str:
    return (
        "Начните получать анонимные вопросы прямо сейчас!\n\n"
        "Ваша ссылка:\n"
        f"{quote_link_block(link)}\n\n"
        "Разместите эту ссылку ☝️ в описании своего профиля Telegram, TikTok, Instagram (stories), "
        "чтобы вам могли написать 💬\n\n"
        f"{ADMIN_NOTICE}"
    )


WRITE_TEXT = "✍️ Напишите ваше сообщение — оно будет отправлено анонимно.\n\n" + ADMIN_NOTICE


# =========================
# START + DeepLink
# =========================
@dp.message(CommandStart())
async def start(message: Message):
    init_db()

    code = upsert_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )

    parts = (message.text or "").split(maxsplit=1)
    target_code = parts[1].strip().lower() if len(parts) > 1 else ""

    # пришли по чужой ссылке -> ждём текст
    if target_code:
        target = get_user_by_code(target_code)
        if target and int(target["user_id"]) != message.from_user.id:
            inc_click(int(target["user_id"]))
            set_pending(message.from_user.id, int(target["user_id"]))
            await message.answer(WRITE_TEXT)
            return

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={code}"
    await message.answer(start_text(link), reply_markup=await kb_home(message.from_user.id))


# =========================
# UI callbacks
# =========================
@dp.callback_query(F.data == "ui:home")
async def ui_home(call: CallbackQuery):
    link = await get_my_link(call.from_user.id)
    await call.message.edit_text(start_text(link), reply_markup=await kb_home(call.from_user.id))
    await call.answer()


@dp.callback_query(F.data == "ui:stats")
async def ui_stats(call: CallbackQuery):
    st = get_stats(call.from_user.id)
    link = await get_my_link(call.from_user.id)

    text = (
        "📊 <b>Статистика</b>\n\n"
        "Сегодня:\n"
        f"💬 Сообщений: <b>{st['msgs_today']}</b>\n"
        f"👀 Переходов по ссылке: <b>{st['link_clicks_today']}</b>\n\n"
        "За всё время:\n"
        f"💬 Сообщений: <b>{st['msgs_total']}</b>\n"
        f"👀 Переходов по ссылке: <b>{st['link_clicks_total']}</b>\n\n"
        "Ваша ссылка:\n"
        f"{quote_link_block(link)}\n\n"
        f"{ADMIN_NOTICE}"
    )

    await call.message.edit_text(text, reply_markup=kb_back_home())
    await call.answer()


@dp.callback_query(F.data == "ui:help")
async def ui_help(call: CallbackQuery):
    text = (
        "ℹ️ <b>Помощь</b>\n\n"
        "Как получать сообщения:\n"
        "1) Нажмите /start и возьмите свою ссылку.\n"
        "2) Разместите ссылку в профиле/сторис.\n\n"
        "Как написать человеку:\n"
        "— откройте его ссылку и напишите сообщение.\n\n"
        f"{ADMIN_NOTICE}"
    )
    await call.message.edit_text(text, reply_markup=kb_back_home())
    await call.answer()


@dp.callback_query(F.data == "ui:write_more")
async def ui_write_more(call: CallbackQuery):
    p = get_pending(call.from_user.id)
    if p:
        await call.message.answer(WRITE_TEXT)
    else:
        await call.message.answer("Откройте ссылку человека (t.me/бот?start=код), чтобы написать ему.")
    await call.answer()


# =========================
# REPLY button flow
# =========================
@dp.callback_query(F.data.startswith("reply:"))
async def reply_start(call: CallbackQuery):
    sender_id = int(call.data.split(":", 1)[1])
    set_pending(call.from_user.id, sender_id)
    await call.message.answer("✍️ Напишите ответ — он будет отправлен анонимно.\n\n" + ADMIN_NOTICE)
    await call.answer()


# =========================
# Commands
# =========================
@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "ℹ️ <b>Помощь</b>\n\n"
        "Команды:\n"
        "/start — ваша ссылка\n"
        "/stats — статистика\n"
        "/help — помощь\n\n"
        f"{ADMIN_NOTICE}"
    )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    st = get_stats(message.from_user.id)
    link = await get_my_link(message.from_user.id)

    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        "Сегодня:\n"
        f"💬 Сообщений: <b>{st['msgs_today']}</b>\n"
        f"👀 Переходов по ссылке: <b>{st['link_clicks_today']}</b>\n\n"
        "За всё время:\n"
        f"💬 Сообщений: <b>{st['msgs_total']}</b>\n"
        f"👀 Переходов по ссылке: <b>{st['link_clicks_total']}</b>\n\n"
        "Ваша ссылка:\n"
        f"{quote_link_block(link)}\n\n"
        f"{ADMIN_NOTICE}",
        reply_markup=await kb_home(message.from_user.id),
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    rows = last_logs(25)
    if not rows:
        await message.answer("Логов пока нет.")
        return

    lines = ["🛡 <b>Последние сообщения</b>:\n"]
    for r in rows:
        fu = get_user(r["from_id"])
        tu = get_user(r["to_id"])
        lines.append(
            f"👤 {format_user(fu)}\n"
            f"➡️ {format_user(tu)}\n"
            f"💬 {r['text']}\n"
        )

    await message.answer("\n".join(lines))


# =========================
# Message sending
# =========================
@dp.message()
async def on_message(message: Message):
    init_db()

    # Если человек прислал ссылку текстом — подсказка
    code_from_link = extract_code_from_link(message.text or "")
    if code_from_link:
        await message.answer("Открой эту ссылку (нажми на неё), затем напиши сообщение в боте.")
        return

    # Должен быть pending
    p = get_pending(message.from_user.id)
    if not p:
        await message.answer("Чтобы написать человеку — открой его ссылку (t.me/бот?start=код).")
        return

    # TTL
    if int(time.time()) - int(p["created_at"]) > TTL_SECONDS:
        clear_pending(message.from_user.id)
        await message.answer("⏳ Время истекло. Открой ссылку человека заново.")
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустое сообщение не отправляю.")
        return

    to_id = int(p["to_id"])

    # Отправка получателю + кнопка "Ответить"
    await bot.send_message(
        to_id,
        "📩 Вам пришло анонимное сообщение:\n\n"
        f"{text}",
        reply_markup=kb_reply(sender_id=message.from_user.id),
    )

    # Статы получателя
    inc_msg(to_id)

    # Лог в базу + админу
    log_message(message.from_user.id, to_id, text)
    await send_admin_log(message.from_user.id, to_id, text)

    # Ответ отправителю
    await message.answer("✅ Сообщение отправлено, ожидайте ответ!", reply_markup=kb_write_more())

    # Продлим pending
    set_pending(message.from_user.id, to_id)


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
