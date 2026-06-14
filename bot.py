#!/usr/bin/env python3
"""
@idea_dma_bot — минималистичный трекер идей/дел/планов
Статусы: ✅ выполнено | ⬜ не выполнено
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.client.default import DefaultBotProperties

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7653823001"))
DB_PATH = "/app/data/ideas.db"

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ─── DB ────────────────────────────────────────────────────────

def init_db():
    Path("/app/data").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def add_idea(text: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO ideas (text, done, created_at) VALUES (?, 0, ?)",
        (text, datetime.now().isoformat()),
    )
    conn.commit()
    idea_id = cur.lastrowid
    conn.close()
    return idea_id


def delete_idea(idea_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM ideas WHERE id = ?", (idea_id,))
    conn.commit()
    conn.close()


def toggle_idea(idea_id: int) -> bool | None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT done FROM ideas WHERE id = ?", (idea_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return None
    new_done = 0 if row[0] else 1
    conn.execute("UPDATE ideas SET done = ? WHERE id = ?", (new_done, idea_id))
    conn.commit()
    conn.close()
    return bool(new_done)


def get_all_ideas() -> list[tuple[int, str, bool]]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, text, done FROM ideas ORDER BY done ASC, created_at DESC"
    ).fetchall()
    conn.close()
    return rows


# ─── INLINE KEYBOARD ───────────────────────────────────────────

def ideas_keyboard() -> InlineKeyboardMarkup:
    rows = get_all_ideas()
    if not rows:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📝 Добавить первую идею", callback_data="add_prompt")]
            ]
        )

    kb = []
    for idea_id, text, done in rows:
        prefix = "✅" if done else "⬜"
        short = text[:50] + ("…" if len(text) > 50 else "")
        kb.append([
            InlineKeyboardButton(
                text=f"{prefix}  {short}",
                callback_data=f"toggle:{idea_id}",
            )
        ])
    kb.append([
        InlineKeyboardButton(text="➕ Добавить", callback_data="add_prompt"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data="delete_mode"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def delete_keyboard() -> InlineKeyboardMarkup:
    rows = get_all_ideas()
    if not rows:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
            ]
        )

    kb = []
    for idea_id, text, done in rows:
        short = text[:40] + ("…" if len(text) > 40 else "")
        kb.append([
            InlineKeyboardButton(text=f"❌  {short}", callback_data=f"delete:{idea_id}")
        ])
    kb.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ─── HANDLERS ──────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        await msg.reply("⛔ Нет доступа")
        return
    await show_ideas(msg)


async def show_ideas(msg: types.Message | types.CallbackQuery):
    rows = get_all_ideas()
    if isinstance(msg, types.CallbackQuery):
        msg = msg.message

    if not rows:
        await msg.answer(
            "📋 <b>Мои идеи / дела / планы</b>\n\n"
            "Пока пусто. Нажми «Добавить» и запиши свою первую мысль.",
            reply_markup=ideas_keyboard(),
        )
        return

    done_count = sum(1 for _, _, d in rows if d)
    total = len(rows)
    text = (
        f"📋 <b>Мои идеи / дела / планы</b>\n"
        f"└ {total} всего · {done_count} ✅ выполнено\n\n"
    )
    await msg.answer(text, reply_markup=ideas_keyboard())


@dp.callback_query(lambda c: c.data == "add_prompt")
async def ask_add(call: types.CallbackQuery):
    """Говорим: напиши текст задачи"""
    await call.message.answer(
        "✏️ <b>Напиши текст</b>\n\n"
        "Просто отправь сообщение — оно станет новой задачей.\n"
        "Или нажми /cancel чтобы отменить."
    )
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("toggle:"))
async def handle_toggle(call: types.CallbackQuery):
    idea_id = int(call.data.split(":")[1])
    result = toggle_idea(idea_id)
    if result is None:
        await call.message.answer("❌ Задача не найдена")
        return
    await show_ideas(call)
    await call.answer()


@dp.callback_query(lambda c: c.data == "delete_mode")
async def enter_delete_mode(call: types.CallbackQuery):
    await call.message.answer(
        "🗑 <b>Режим удаления</b>\n\n"
        "Нажми на задачу, чтобы удалить её.",
        reply_markup=delete_keyboard(),
    )
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("delete:"))
async def handle_delete(call: types.CallbackQuery):
    idea_id = int(call.data.split(":")[1])
    delete_idea(idea_id)
    rows = get_all_ideas()
    if rows:
        await call.message.edit_text(
            "🗑 <b>Режим удаления</b>\n\nНажми на задачу, чтобы удалить её.",
            reply_markup=delete_keyboard(),
        )
    else:
        await show_ideas(call)
    await call.answer("✅ Удалено", show_alert=False)


@dp.callback_query(lambda c: c.data == "back")
async def handle_back(call: types.CallbackQuery):
    await show_ideas(call)
    await call.answer()


@dp.message(Command("cancel"))
async def cmd_cancel(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await show_ideas(msg)


@dp.message()
async def handle_text(msg: types.Message):
    if msg.from_user.id != ADMIN_ID or not msg.text:
        return
    if msg.text.startswith("/"):
        return

    text = msg.text.strip()
    idea_id = add_idea(text)
    logger.info("Добавлена идея #%s: %s", idea_id, text[:60])

    await msg.answer(f"✅ <b>Добавлено!</b>\n\n{text}", reply_markup=ideas_keyboard())


# ─── КОМАНДА НАПОМИНАНИЯ ──────────────────────────────────────

def format_ideas_for_cron() -> str | None:
    """Возвращает текст напоминания или None если нет задач."""
    rows = get_all_ideas()
    if not rows:
        return None

    lines = []
    done_lines = []
    for idea_id, text, done in rows:
        if done:
            done_lines.append(f"✅ {text}")
        else:
            lines.append(f"⬜ {text}")

    text_parts = []
    if lines:
        text_parts.append("📋 <b>Нужно сделать:</b>\n" + "\n".join(lines))
    if done_lines:
        text_parts.append("\n✅ <b>Выполнено:</b>\n" + "\n".join(done_lines))

    if not text_parts:
        return None

    return "\n\n".join(text_parts)


@dp.message(Command("remind"))
async def cmd_remind(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    text = format_ideas_for_cron()
    if not text:
        await msg.answer("📋 Пока нет ни одной задачи. Добавь через /start")
        return
    await msg.answer(text)


# ─── MAIN ──────────────────────────────────────────────────────

async def main():
    init_db()
    logger.info("🤖 @idea_dma_bot запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлен")
