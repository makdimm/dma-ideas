#!/usr/bin/env python3
"""
@mytaskprogress_bot — трекер идей/дел/планов
Фичи: текст + голос, пагинация, вкладки Открытые/Выполненные, навигация
"""

import asyncio
import io
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
try:
    from openai import AsyncOpenAI
    _openai_available = True
except ImportError:
    AsyncOpenAI = None
    _openai_available = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7653823001"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
PAGE_SIZE = 6
DB_PATH = "/app/data/ideas.db"

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY and _openai_available else None


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


def get_ideas(done: int, page: int = 0) -> tuple[list[tuple[int, str]], int]:
    """Returns (rows, total_pages)"""
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute(
        "SELECT COUNT(*) FROM ideas WHERE done = ?", (done,)
    ).fetchone()[0]
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    rows = conn.execute(
        "SELECT id, text FROM ideas WHERE done = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (done, PAGE_SIZE, page * PAGE_SIZE),
    ).fetchall()
    conn.close()
    return rows, total_pages


def count_ideas() -> tuple[int, int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT done, COUNT(*) FROM ideas GROUP BY done")
    counts = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()
    return counts.get(0, 0), counts.get(1, 0)


# ─── КЛАВИАТУРЫ ───────────────────────────────────────────────

def _page_nav(tab: str, page: int, total_pages: int):
    """Кнопки навигации по страницам"""
    btns = []
    if page > 0:
        btns.append(InlineKeyboardButton(text="◀️", callback_data=f"page:{tab}:{page - 1}"))
    btns.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        btns.append(InlineKeyboardButton(text="▶️", callback_data=f"page:{tab}:{page + 1}"))
    return btns


def open_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    rows, total_pages = get_ideas(0, page)
    kb = []

    # Вкладки
    kb.append([
        InlineKeyboardButton(text="👉 📋 Открытые 👈", callback_data="noop"),
        InlineKeyboardButton(text="✅ Выполненные", callback_data="tab:done"),
    ])

    if not rows:
        kb.append([InlineKeyboardButton(text="➕ Добавить задачу", callback_data="add_prompt")])
    else:
        for idea_id, text in rows:
            short = text[:50] + ("…" if len(text) > 50 else "")
            kb.append([
                InlineKeyboardButton(
                    text=f"⬜  {short}",
                    callback_data=f"toggle:{idea_id}:{page}",
                )
            ])
        # Навигация по страницам
        if total_pages > 1:
            kb.append(_page_nav("open", page, total_pages))
        # Нижняя панель
        kb.append([
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh:{page}"),
            InlineKeyboardButton(text="➕ Добавить", callback_data="add_prompt"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_mode:{page}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def done_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    rows, total_pages = get_ideas(1, page)
    kb = []

    kb.append([
        InlineKeyboardButton(text="📋 Открытые", callback_data="tab:open"),
        InlineKeyboardButton(text="👉 ✅ Выполненные 👈", callback_data="noop"),
    ])

    if not rows:
        kb.append([InlineKeyboardButton(text="— пока ничего нет —", callback_data="noop")])
    else:
        for idea_id, text in rows:
            short = text[:50] + ("…" if len(text) > 50 else "")
            kb.append([
                InlineKeyboardButton(
                    text=f"✅  {short}",
                    callback_data=f"toggle:{idea_id}:{page}",
                )
            ])
        if total_pages > 1:
            kb.append(_page_nav("done", page, total_pages))
        # Нижняя панель
        kb.append([
            InlineKeyboardButton(text="🗑 Очистить все", callback_data="clear_done"),
            InlineKeyboardButton(text="◀️ Назад", callback_data="tab:open"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def delete_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    rows, total_pages = get_ideas(0, page)
    if not rows:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад к задачам", callback_data="tab:open")]
            ]
        )

    kb = [
        [InlineKeyboardButton(text="◀️ Назад к задачам", callback_data="tab:open")]
    ]
    for idea_id, text in rows:
        short = text[:35] + ("…" if len(text) > 35 else "")
        kb.append([
            InlineKeyboardButton(text=f"❌  {short}", callback_data=f"delete:{idea_id}:{page}")
        ])
    if total_pages > 1:
        kb.append(_page_nav("delete", page, total_pages))
    kb.append([InlineKeyboardButton(text="◀️ Назад к задачам", callback_data="tab:open")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ─── HANDLERS ──────────────────────────────────────────────────

async def show_tab(msg_or_call, tab: str, page: int = 0, edit: bool = False):
    """Показать вкладку (новое сообщение или редактировать текущее)"""
    open_c, done_c = count_ideas()
    header = (
        f"📋 <b>Мои идеи / дела / планы</b>\n"
        f"└ {open_c + done_c} всего · {done_c} ✅ выполнено\n\n"
    )

    if tab == "open":
        text = header + "<b>📋 Открытые задачи</b>"
        markup = open_keyboard(page)
    else:
        text = header + "<b>✅ Выполненные</b>"
        markup = done_keyboard(page)

    if edit:
        await msg_or_call.edit_text(text, reply_markup=markup)
    else:
        await msg_or_call.answer(text, reply_markup=markup)


@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        await msg.reply("⛔ Нет доступа")
        return
    await show_tab(msg, "open")


@dp.callback_query(lambda c: c.data == "noop")
async def noop(call: types.CallbackQuery):
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("refresh:"))
async def handle_refresh(call: types.CallbackQuery):
    page = int(call.data.split(":")[1])
    await show_tab(call.message, "open", page, edit=True)
    await call.answer("🔄 Обновлено")


@dp.callback_query(lambda c: c.data.startswith("tab:"))
async def switch_tab(call: types.CallbackQuery):
    tab = call.data.split(":")[1]
    await show_tab(call.message, tab, page=0, edit=True)
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("page:"))
async def handle_page(call: types.CallbackQuery):
    _, tab, page = call.data.split(":")
    page = int(page)
    if tab == "delete":
        await call.message.edit_reply_markup(reply_markup=delete_keyboard(page))
    else:
        open_c, done_c = count_ideas()
        header = (
            f"📋 <b>Мои идеи / дела / планы</b>\n"
            f"└ {open_c + done_c} всего · {done_c} ✅ выполнено\n\n"
        )
        if tab == "open":
            text = header + "<b>📋 Открытые задачи</b>"
            markup = open_keyboard(page)
        else:
            text = header + "<b>✅ Выполненные</b>"
            markup = done_keyboard(page)
        await call.message.edit_text(text, reply_markup=markup)
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("toggle:"))
async def handle_toggle(call: types.CallbackQuery):
    parts = call.data.split(":")
    idea_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    result = toggle_idea(idea_id)
    if result is None:
        await call.answer("❌ Задача не найдена", show_alert=True)
        return
    await call.message.delete()
    tab = "done" if result else "open"
    await show_tab(call.message, tab, page)
    await call.answer()


@dp.callback_query(lambda c: c.data == "add_prompt")
async def ask_add(call: types.CallbackQuery):
    await call.message.answer(
        "✏️ <b>Напиши текст</b> или отправь <b>голосовое</b>\n\n"
        "Обычное сообщение или голосовое — и оно станет новой задачей.\n"
        "Нажми /cancel чтобы отменить."
    )
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("delete_mode:"))
async def enter_delete_mode(call: types.CallbackQuery):
    page = int(call.data.split(":")[1])
    await call.message.edit_text(
        "🗑 <b>Режим удаления</b>\n\nНажми на задачу, чтобы удалить её.",
        reply_markup=delete_keyboard(page),
    )
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("delete:"))
async def handle_delete(call: types.CallbackQuery):
    parts = call.data.split(":")
    idea_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    delete_idea(idea_id)
    await call.answer("✅ Удалено", show_alert=False)

    rows, _ = get_ideas(0, page)
    if rows:
        await call.message.edit_reply_markup(reply_markup=delete_keyboard(page))
    else:
        total_open, _ = count_ideas()
        if total_open > 0 and page > 0:
            new_page = page - 1
            rows, _ = get_ideas(0, new_page)
            if rows:
                await call.message.edit_reply_markup(reply_markup=delete_keyboard(new_page))
            else:
                await call.message.delete()
                await show_tab(call.message, "open")
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


@dp.message(lambda msg: msg.voice is not None)
async def handle_voice(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    if not ai_client:
        await msg.reply("❌ Голосовые не поддерживаются — нет OpenAI API ключа")
        return

    await bot.send_chat_action(msg.chat.id, "typing")

    try:
        file = await bot.get_file(msg.voice.file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        buf.seek(0)
        buf.name = "voice.ogg"

        transcript = await ai_client.audio.transcriptions.create(
            model="whisper-1",
            file=buf,
            language="ru",
        )
        text = transcript.text.strip()
        logger.info("Голос распознан: %r", text[:80])

        if not text:
            await msg.reply("❌ Не удалось распознать речь. Попробуй ещё раз.")
            return

        idea_id = add_idea(text)
        logger.info("Добавлена идея #%s (голос): %s", idea_id, text[:60])
        await msg.answer(f"🎤 <b>Распознано и добавлено!</b>\n\n{text}")

    except Exception as e:
        logger.exception("Voice processing error")
        await msg.reply(f"❌ Ошибка обработки голоса: {e}")

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

@dp.message(Command("remind"))
async def cmd_remind(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect(DB_PATH)
    open_list = conn.execute(
        "SELECT id, text FROM ideas WHERE done = 0 ORDER BY created_at DESC"
    ).fetchall()
    done_list = conn.execute(
        "SELECT id, text FROM ideas WHERE done = 1 ORDER BY created_at DESC"
    ).fetchall()
    conn.close()

    if not open_list and not done_list:
        await msg.answer("📋 Пока ни одной задачи. Добавь через /start")
        return

    parts = []
    if open_list:
        items = "\n".join(f"⬜ {t}" for _, t in open_list)
        parts.append(f"📋 <b>Нужно сделать:</b>\n{items}")
    if done_list:
        items = "\n".join(f"✅ {t}" for _, t in done_list)
        parts.append(f"\n✅ <b>Выполнено:</b>\n{items}")

    await msg.answer("\n\n".join(parts))


# ─── MAIN ──────────────────────────────────────────────────────

async def main():
    init_db()
    if ai_client:
        logger.info("🎤 Голосовые сообщения включены (OpenAI Whisper)")
    elif not _openai_available:
        logger.warning("🎤 Голосовые отключены — пакет openai не установлен")
    else:
        logger.warning("🎤 Голосовые отключены — нет OPENAI_API_KEY")
    logger.info("🤖 @mytaskprogress_bot запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлен")
