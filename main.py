import os
import re
import time
import sqlite3

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

# =========================
# CONFIG (Railway Variables)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "7489815425").strip())
DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty. Set Railway variable BOT_TOKEN")


# =========================
# DATABASE
# =========================
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            code TEXT UNIQUE,
            created_at INTEGER
        );
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS pending (
            from_id INTEGER PRIMARY KEY,
            to_id INTEGER,
            created_at INTEGER
        );
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id INTEGER,
            to_id INTEGER,
            text TEXT,
            created_at INTEGER
        );
        """)
        con.commit()


def _gen_code(n: int) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    import random
    return "".join(random.choice(alphabet) for _ in range(n))


def upsert_user(user_id: int, username: str, full_name: str) -> str:
    with db() as con:
        row = con.execute(
            "SELECT code FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if row:
            con.execute(
                "UPDATE users SET username=?, full_name=? WHERE user_id=?",
                (username, full_name, user_id)
            )
            con.commit()
            return row["code"]

        while True:
            code = _gen_code(8)
            exists = con.execute(
                "SELECT 1 FROM users WHERE code=?",
                (code,)
            ).fetchone()
            if not exists:
                break

        con.execute(
            "INSERT INTO users (user_id, username, full_name, code, created_at) VALUES (?,?,?,?,?)",
            (user_id, username, full_name, code, int(time.time()))
        )
        con.commit()
        return code


def get_user_by_code(code: str):
    with db() as con:
        return con.execute(
            "SELECT * FROM users WHERE code=?",
            (code,)
        ).fetchone()


def get_user(user_id: int):
    with db() as con:
        return con.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()


def set_pending(from_id: int, to_id: int):
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO pending (from_id, to_id, created_at) VALUES (?,?,?)",
            (from_id, to_id, int(time.time()))
        )
        con.commit()


def get_pending_to(from_id: int):
    with db() as con:
        return con.execute(
            "SELECT * FROM pending WHERE from_id=?",
            (from_id,)
        ).fetchone()


def clear_pending(from_id: int):
    with db() as con:
        con.execute(
            "DELETE FROM pending WHERE from_id=?",
            (from_id,)
        )
        con.commit()


def log_message(from_id: int, to_id: int, text: str):
    with db() as con:
        con.execute(
            "INSERT INTO logs (from_id, to_id, text, created_at) VALUES (?,?,?,?)",
            (from_id, to_id, text, int(time.time()))
        )
        con.commit()


def last_logs(limit: int = 20):
    with db() as con:
        return con.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()


# =========================
# UI
# =========================
def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✉️ Написать"),
             KeyboardButton(text="🔗 Моя ссылка")],
            [KeyboardButton(text="ℹ️ Правила")],
        ],
        resize_keyboard=True
    )


RULES_TEXT = (
    "✅ Получатель видит сообщение как анонимное.\n"
)


def format_user(u) -> str:
    if not u:
        return "unknown"
    uname = (u["username"] or "").strip()
    full = (u["full_name"] or "").strip()
    if uname:
        return f"@{uname} ({u['user_id']})"
    return f"{full} ({u['user_id']})"


async def send_admin_copy(bot: Bot, from_id: int, to_id: int, text: str):
    fu = get_user(from_id)
    tu = get_user(to_id)

    msg = (
        "🛡 ADMIN LOG\n"
        f"От: {format_user(fu)}\n"
        f"Кому: {format_user(tu)}\n"
        f"Текст: {text}"
    )
    await bot.send_message(ADMIN_ID, msg)


def extract_code_from_link(text: str):
    m = re.search(r"start=([a-z0-9]{6,32})", text, flags=re.I)
    return m.group(1).lower() if m else None


# =========================
# BOT
# =========================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def on_start(message: Message):
    init_db()

    username = (message.from_user.username or "").strip()
    full_name = (message.from_user.full_name or "").strip()
    code = upsert_user(message.from_user.id, username, full_name)

    # Warning всегда при старте
    await message.answer(RULES_TEXT, reply_markup=main_kb())

    # Deep-link /start CODE
    parts = (message.text or "").split(maxsplit=1)
    target_code = parts[1].strip() if len(parts) > 1 else ""

    if target_code:
        target = get_user_by_code(target_code)
        if target and target["user_id"] != message.from_user.id:
            set_pending(message.from_user.id, int(target["user_id"]))
            await message.answer(
                "✉️ Напиши ОДНО сообщение текстом — я доставлю его анонимно этому человеку."
            )
        else:
            await message.answer("Это твоя ссылка 🙂")
        return

    await message.answer(
        "Нажми «🔗 Моя ссылка» чтобы получить свою ссылку.",
        reply_markup=main_kb()
    )


@dp.message(F.text == "🔗 Моя ссылка")
async def my_link(message: Message):
    init_db()
    u = get_user(message.from_user.id)

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={u['code']}"
    await message.answer(
        f"🔗 Твоя ссылка:\n{link}\n\nОтправь её друзьям."
    )


@dp.message(F.text == "ℹ️ Правила")
async def rules(message: Message):
    await message.answer(RULES_TEXT)


@dp.message(F.text == "✉️ Написать")
async def how_to(message: Message):
    await message.answer(
        "Чтобы написать кому-то:\n"
        "1) Попроси у человека его ссылку.\n"
        "2) Открой её и напиши сообщение."
    )


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    rows = last_logs(20)
    if not rows:
        await message.answer("Логов пока нет.")
        return

    lines = ["🛡 Последние сообщения:"]
    for r in rows:
        fu = get_user(r["from_id"])
        tu = get_user(r["to_id"])
        lines.append(f"— {format_user(fu)} -> {format_user(tu)}: {r['text']}")

    await message.answer("\n".join(lines))


@dp.message()
async def on_text(message: Message):
    init_db()

    # Если прислали ссылку — подсказка
    code = extract_code_from_link(message.text or "")
    if code:
        await message.answer(
            "Открой эту ссылку (нажми на неё), потом напиши сообщение."
        )
        return

    p = get_pending_to(message.from_user.id)
    if not p:
        await message.answer(
            "Чтобы отправить сообщение, открой ссылку человека.",
            reply_markup=main_kb()
        )
        return

    # TTL 15 минут
    if int(time.time()) - int(p["created_at"]) > 15 * 60:
        clear_pending(message.from_user.id)
        await message.answer("Окно отправки истекло. Открой ссылку заново.")
        return

    to_id = int(p["to_id"])
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустое сообщение не отправляю.")
        return

    # Отправка получателю (анонимно)
    await bot.send_message(to_id, f"📩 Тебе пришло сообщение:\n\n{text}")

    # Лог админу
    log_message(message.from_user.id, to_id, text)
    await send_admin_copy(bot, message.from_user.id, to_id, text)

    clear_pending(message.from_user.id)
    await message.answer("✅ Отправлено.", reply_markup=main_kb())


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())