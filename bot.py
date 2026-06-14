#!/usr/bin/env python3
"""
@idea_dma_bot — трекер идей/дел/планов
Две вкладки: 📋 Открытые | ✅ Выполненные
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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
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


def get_ideas(done: int) -> list[tuple[int, str]]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, text FROM ideas WHERE done = ? ORDER BY created_at DESC",
        (done,),
    ).fetchall()
    conn.close()
    return rows


def count_ideas() -> tuple[int, int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT done, COUNT(*) FROM ideas GROUP BY done")
    counts = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()
    return counts.get(0, 0), counts.get(1, 0)


# ─── КЛАВИАТУРЫ ───────────────────────────────────────────────

def tab_bar(active: str) -> list[list[InlineKeyboardButton]]:
    """Верхняя строка-табы."""
    open_lbl = f"▎📋 Открытые ▎"
    done_lbl = f"▎✅ Выполненные ▎"
    return [
        [
            InlineKeyboardButton(
                text=f"👉 {open_lbl}" if active == "open" else open_lbl,
                callback_data="tab:open",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"👉 {done_lbl}" if active == "done" else done_lbl,
                callback_data="tab:done",
            ),
        ],
    ]


def open_keyboard() -> InlineKeyboardMarkup:
    rows = get_ideas(0)
    kb = tab_bar("open")

    if not rows:
        kb.append([InlineKeyboardButton(text="➕ Добавить задачу", callback_data="add_prompt")])
    else:
        for idea_id, text in rows:
            short = text[:55] + ("…" if len(text) > 55 else "")
            kb.append([
                InlineKeyboardButton(
                    text=f"⬜  {short}",
                    callback_data=f"toggle:{idea_id}",
                )
            ])
        kb.append([
            InlineKeyboardButton(text="➕ Добавить", callback_data="add_prompt"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data="delete_mode"),
        ])

    return InlineKeyboardMarkup(inline_keyboard=kb)


def done_keyboard() -> InlineKeyboardMarkup:
    rows = get_ideas(1)
    kb = tab_bar("done")

    if not rows:
        kb.append([InlineKeyboardButton(text="— пока ничего нет —", callback_data="noop")])
    else:
        for idea_id, text in rows:
            short = text[:55] + ("…" if len(text) > 55 else "")
            kb.append([
                InlineKeyboardButton(
                    text=f"✅  {short}",
                    callback_data=f"toggle:{idea_id}",
                )
            ])
        kb.append([InlineKeyboardButton(text="🗑 Очистить все", callback_data="clear_done")])

    return InlineKeyboardMarkup(inline_keyboard=kb)


def delete_keyboard() -> InlineKeyboardMarkup:
    rows = get_ideas(0)
    if not rows:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="tab:open")]
            ]
        )

    kb = []
    for idea_id, text in rows:
        short = text[:40] + ("…" if len(text) > 40 else "")
        kb.append([
            InlineKeyboardButton(text=f"❌  {short}", callback_data=f"delete:{idea_id}")
        ])
    kb.append([InlineKeyboardButton(text="◀️ Назад", callback_data="tab:open")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ─── HANDLERS ──────────────────────────────────────────────────

async def show_tab(msg: types.Message, tab: str):
    open_c, done_c = count_ideas()
    header = (
        f"📋 <b>Мои идеи / дела / планы</b>\n"
        f"└ {open_c + done_c} всего · {done_c} ✅ выполнено\n\n"
    )

    if tab == "open":
        text = header + "<b>📋 Открытые задачи</b>"
        await msg.answer(text, reply_markup=open_keyboard())
    else:
        text = header + "<b>✅ Выполненные</b>"
        await msg.answer(text, reply_markup=done_keyboard())


@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        await msg.reply("⛔ Нет доступа")
        return
    await show_tab(msg, "open")


@dp.callback_query(lambda c: c.data == "noop")
async def noop(call: types.CallbackQuery):
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("tab:"))
async def switch_tab(call: types.CallbackQuery):
    tab = call.data.split(":")[1]
    await call.message.delete()
    await show_tab(call.message, tab)
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("toggle:"))
async def handle_toggle(call: types.CallbackQuery):
    idea_id = int(call.data.split(":")[1])
    result = toggle_idea(idea_id)
    if result is None:
        await call.answer("❌ Задача не найдена", show_alert=True)
        return
    await call.message.delete()

    # Определяем текущую вкладку — уходим на ту же
    if result is True:
        # была открытая, стала выполненной — показываем done
        await show_tab(call.message, "done")
    else:
        # была выполненной, стала открытой — показываем open
        await show_tab(call.message, "open")

    await call.answer()


@dp.callback_query(lambda c: c.data == "add_prompt")
async def ask_add(call: types.CallbackQuery):
    await call.message.answer(
        "✏️ <b>Напиши текст</b>\n\n"
        "Просто отправь сообщение — оно станет новой задачей.\n"
        "Или нажми /cancel чтобы отменить."
    )
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
    await call.answer("✅ Удалено", show_alert=False)

    rows = get_ideas(0)
    if rows:
        await call.message.edit_reply_markup(reply_markup=delete_keyboard())
    else:
        await call.message.delete()
        await show_tab(call.message, "open")


@dp.callback_query(lambda c: c.data == "clear_done")
async def handle_clear_done(call: types.CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM ideas WHERE done = 1")
    conn.commit()
    conn.close()
    await call.answer("✅ Все выполненные удалены", show_alert=True)
    await call.message.delete()
    await show_tab(call.message, "open")


@dp.message(Command("cancel"))
async def cmd_cancel(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await show_tab(msg, "open")


@dp.message()
async def handle_text(msg: types.Message):
    if msg.from_user.id != ADMIN_ID or not msg.text:
        return
    if msg.text.startswith("/"):
        return

    text = msg.text.strip()
    idea_id = add_idea(text)
    logger.info("Добавлена идея #%s: %s", idea_id, text[:60])

    await msg.answer(f"✅ <b>Добавлено!</b>\n\n{text}")
    await show_tab(msg, "open")


# ─── НАПОМИНАНИЕ ──────────────────────────────────────────────

def format_reminder() -> str | None:
    open_list = get_ideas(0)
    done_list = get_ideas(1)
    if not open_list and not done_list:
        return None

    parts = []
    if open_list:
        items = "\n".join(f"⬜ {t}" for _, t in open_list)
        parts.append(f"📋 <b>Нужно сделать:</b>\n{items}")
    if done_list:
        items = "\n".join(f"✅ {t}" for _, t in done_list)
        parts.append(f"\n✅ <b>Выполнено:</b>\n{items}")

    return "\n\n".join(parts)


@dp.message(Command("remind"))
async def cmd_remind(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    text = format_reminder()
    if not text:
        await msg.answer("📋 Пока ни одной задачи. Добавь через /start")
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
