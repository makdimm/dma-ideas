#!/usr/bin/env python3
"""
@mytaskprogress_bot — трекер идей/дел/планов
Фичи: текст + голос, спринты по датам, перенос задач, NLU-команды.
Интерфейс: /start = спринт на сегодня (включает все открытые задачи), минимум кнопок.
"""

import asyncio
import io
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
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

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7653823001"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
DB_PATH = "/app/data/ideas.db"

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY and _openai_available else None

# Ожидание ввода даты для переноса: {user_id: idea_id}
_pending_move: dict[int, int] = {}


# ─── ДАТЫ ──────────────────────────────────────────────────────

_WEEKDAYS = {
    "понедельник": 0, "пн": 0,
    "вторник": 1, "вт": 1,
    "среда": 2, "ср": 2, "среду": 2,
    "четверг": 3, "чт": 3,
    "пятница": 4, "пт": 4,
    "суббота": 5, "сб": 5,
    "воскресенье": 6, "вс": 6,
}


def parse_date(text: str) -> str | None:
    """Распознаёт дату → 'YYYY-MM-DD' или None.
    Понимает: сегодня/завтра/послезавтра, дни недели, ДД.ММ[.ГГГГ], ISO."""
    if not text:
        return None
    t = text.strip().lower().replace("ё", "е")
    today = date.today()

    if t in ("сегодня", "сег", "сейчас"):
        return today.isoformat()
    if t in ("завтра", "завтрашний"):
        return (today + timedelta(days=1)).isoformat()
    if t in ("послезавтра", "после завтра"):
        return (today + timedelta(days=2)).isoformat()

    for name, wd in _WEEKDAYS.items():
        if t.startswith(name):
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return (today + timedelta(days=delta)).isoformat()

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None

    m = re.search(r"(?<!\d)(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?", text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def fmt_date(due: str | None) -> str:
    """'YYYY-MM-DD' → 'ДД.ММ' / 'сегодня' / 'завтра'"""
    if not due:
        return ""
    try:
        d = date.fromisoformat(due)
    except ValueError:
        return due
    today = date.today()
    if d == today:
        return "сегодня"
    if d == today + timedelta(days=1):
        return "завтра"
    return d.strftime("%d.%m")


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
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ideas)")]
    if "due_date" not in cols:
        conn.execute("ALTER TABLE ideas ADD COLUMN due_date TEXT")
    conn.commit()
    conn.close()


def add_idea(text: str, due_date: str | None = None) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO ideas (text, done, created_at, due_date) VALUES (?, 0, ?, ?)",
        (text, datetime.now().isoformat(), due_date),
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


def set_done(idea_id: int, done: bool) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("UPDATE ideas SET done = ? WHERE id = ?", (1 if done else 0, idea_id))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def set_due_date(idea_id: int, due_date: str | None) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("UPDATE ideas SET due_date = ? WHERE id = ?", (due_date, idea_id))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def get_idea(idea_id: int) -> tuple | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, text, done, due_date FROM ideas WHERE id = ?", (idea_id,)
    ).fetchone()
    conn.close()
    return row


def get_sprint(due: str) -> list[tuple]:
    """Задачи для спринта на конкретную дату: [(id, text, done, due_date)]
    Только задачи с этой датой — общий список (без даты) в спринт не попадает."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, text, done, due_date FROM ideas "
        "WHERE due_date = ? ORDER BY done ASC, created_at ASC",
        (due,),
    ).fetchall()
    conn.close()
    return rows


def get_ideas(done: int) -> list[tuple]:
    """Все задачи по статусу: [(id, text, due_date)]"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, text, due_date FROM ideas WHERE done = ? "
        "ORDER BY COALESCE(due_date, '9999-12-31') ASC, created_at DESC",
        (done,),
    ).fetchall()
    conn.close()
    return rows


def get_sprint_dates() -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT due_date FROM ideas WHERE due_date IS NOT NULL ORDER BY due_date"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def count_ideas() -> tuple[int, int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT done, COUNT(*) FROM ideas GROUP BY done")
    counts = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()
    return counts.get(0, 0), counts.get(1, 0)


# ─── NLU ───────────────────────────────────────────────────────

def parse_intent(text: str) -> dict:
    """Понимает естественные команды.
    Возвращает {'action': ...} где action: sprint|all|done|undone|move|move_prompt|add"""
    t = text.strip().lower().replace("ё", "е")
    if not t:
        return {"action": "add", "text": text}

    # 1. Перенос: «перенеси 3 на пятницу», «перенеси 3», «сдвинь 3 на 12.08»
    m = re.search(r"(?:перенеси|перенести|сдвинь|move|на другой день)\s*[#№]?\s*(\d+)(?:\s*(?:на|в)\s*(.+))?", t)
    if m:
        idea_id = int(m.group(1))
        raw = (m.group(2) or "").strip()
        if raw:
            d = parse_date(raw)
            if d:
                return {"action": "move", "id": idea_id, "date": d}
        return {"action": "move_prompt", "id": idea_id}

    # 2. Выполнено: «отметь 3», «сделано 3», «3 выполнена»
    m = re.search(r"(?:отметь|сделано|сделай|выполнено|выполнена|закрыть|закрой|✅)\s*[#№]?\s*(\d+)", t)
    if m:
        return {"action": "done", "id": int(m.group(1))}
    m = re.search(r"(\d+)\s*(?:выполнена|выполнено|сделана|сделано|готово|закрыта|закрыто)\b", t)
    if m:
        return {"action": "done", "id": int(m.group(1))}

    # 3. Не выполнено: «верни 3», «отмени 3», «3 не выполнена»
    m = re.search(r"(?:верни|отмени|разверни|не\s+выполнена|не\s+выполнено|не\s+сделана|не\s+сделано|⬜)\s*[#№]?\s*(\d+)", t)
    if m:
        return {"action": "undone", "id": int(m.group(1))}
    m = re.search(r"(\d+)\s*(?:не\s+выполнена|не\s+выполнено|не\s+сделана|не\s+сделано|обратно)\b", t)
    if m:
        return {"action": "undone", "id": int(m.group(1))}

    # 4. Спринт: «спринт», «что на завтра», «план на пятницу», «на сегодня»
    if (re.search(r"(?:спринт|что\s+на|план\s+на|задачи\s+на|дела\s+на|покажи\s+на)", t)
            or t in ("сегодня", "завтра", "послезавтра")
            or re.match(r"^на\s+(.+)$", t)):
        if "послезавтра" in t:
            return {"action": "sprint", "date": (date.today() + timedelta(days=2)).isoformat()}
        if "завтра" in t:
            return {"action": "sprint", "date": (date.today() + timedelta(days=1)).isoformat()}
        if "сегодня" in t:
            return {"action": "sprint", "date": date.today().isoformat()}
        d = parse_date(t)
        if d:
            return {"action": "sprint", "date": d}
        return {"action": "sprint", "date": date.today().isoformat()}

    # 5. Все задачи
    if re.search(r"(?:все\s+задачи|все\s+дела|покажи\s+все|список)", t):
        return {"action": "all"}

    # 6. Добавить с датой: «добавь купить хлеб на завтра»
    m = re.search(r"(?:добавь|добавить|запиши|новая\s+задача|задача)\s+(.+)", t)
    if m:
        body = m.group(1).strip()
        d = None
        dm = re.search(r"\s+на\s+(.+)$", body)
        if dm:
            dd = parse_date(dm.group(1))
            if dd:
                d = dd
                body = body[: dm.start()].strip()
        return {"action": "add", "text": body or text.strip(), "date": d}

    # 7. Иначе — просто добавить
    return {"action": "add", "text": text.strip()}


# ─── КЛАВИАТУРЫ ───────────────────────────────────────────────

def sprint_keyboard(due: str) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data="add_prompt"),
            InlineKeyboardButton(text="📚 Все задачи", callback_data="all"),
        ],
        [
            InlineKeyboardButton(text="📅 Сегодня", callback_data="sprint:today"),
            InlineKeyboardButton(text="📅 Завтра", callback_data="sprint:tomorrow"),
            InlineKeyboardButton(text="📅 Послезавтра", callback_data="sprint:dayafter"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def all_keyboard(done: int = 0) -> InlineKeyboardMarkup:
    kb = []
    if done:
        kb.append([
            InlineKeyboardButton(text="🗑 Очистить все", callback_data="clear_done"),
        ])
    kb.append([
        InlineKeyboardButton(text="➕ Добавить", callback_data="add_prompt"),
        InlineKeyboardButton(text="🏠 На сегодня", callback_data="sprint:today"),
    ])
    if done:
        kb.append([InlineKeyboardButton(text="📋 Открытые", callback_data="tab:open")])
    else:
        kb.append([InlineKeyboardButton(text="✅ Выполненные", callback_data="tab:done")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def move_pick_keyboard(idea_id: int) -> InlineKeyboardMarkup:
    today = date.today()
    kb = [
        [InlineKeyboardButton(text="📅 Сегодня", callback_data=f"move_set:{idea_id}:{today.isoformat()}")],
        [InlineKeyboardButton(text="📅 Завтра", callback_data=f"move_set:{idea_id}:{(today + timedelta(days=1)).isoformat()}")],
        [InlineKeyboardButton(text="📅 Послезавтра", callback_data=f"move_set:{idea_id}:{(today + timedelta(days=2)).isoformat()}")],
        [InlineKeyboardButton(text="✏️ Написать дату", callback_data=f"move_type:{idea_id}")],
        [InlineKeyboardButton(text="🚫 Без даты", callback_data=f"move_set:{idea_id}:none")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="sprint:today")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ─── VIEWS ─────────────────────────────────────────────────────

def _sprint_text(due: str) -> tuple[str, list]:
    rows = get_sprint(due)
    label = fmt_date(due)
    if not rows:
        return f"🏃 <b>Спринт на {label}</b>\n\nПока пусто. Добавь задачу или перенеси 📅", []
    open_n = sum(1 for _, _, d, _ in rows if not d)
    done_n = len(rows) - open_n
    lines = [f"🏃 <b>Спринт на {label}</b>", f"└ {len(rows)} задач · {done_n} ✅", ""]
    for idea_id, t, done, due in rows:
        mark = "✅" if done else "⬜"
        lines.append(f"{mark} <b>#{idea_id}</b> {t}")
    return "\n".join(lines), rows


async def show_sprint(msg_or_call, due: str, edit: bool = False):
    text, rows = _sprint_text(due)
    kb = sprint_keyboard(due)
    # кнопки задач: вставляем после заголовка (в начало клавиатуры)
    task_btns = []
    for idea_id, t, done, _due in rows:
        mark = "✅" if done else "⬜"
        short = t[:42] + ("…" if len(t) > 42 else "")
        task_btns.append([
            InlineKeyboardButton(text=f"{mark} {short}", callback_data=f"toggle:{idea_id}"),
            InlineKeyboardButton(text="📅", callback_data=f"move_pick:{idea_id}"),
        ])
    full_kb = InlineKeyboardMarkup(inline_keyboard=task_btns + kb.inline_keyboard)

    if edit:
        await msg_or_call.edit_text(text, reply_markup=full_kb)
    else:
        await msg_or_call.answer(text, reply_markup=full_kb)


async def show_all(msg_or_call, done: int = 0, edit: bool = False):
    rows = get_ideas(done)
    open_c, done_c = count_ideas()
    if done:
        header = f"✅ <b>Выполненные</b>\n└ {done_c} шт\n\n"
    else:
        header = f"📚 <b>Все задачи</b>\n└ {open_c} открыто · {done_c} ✅\n\n"

    if not rows:
        text = header + "— пусто —"
        kb = all_keyboard(done)
    else:
        lines = []
        task_btns = []
        for idea_id, t, due in rows:
            mark = "✅" if done else "⬜"
            tag = f"[{fmt_date(due)}] " if due else ""
            lines.append(f"{mark} <b>#{idea_id}</b> {tag}{t}")
            short = t[:42] + ("…" if len(t) > 42 else "")
            label = f"{mark} {tag}{short}"
            task_btns.append([
                InlineKeyboardButton(text=label, callback_data=f"toggle:{idea_id}"),
                InlineKeyboardButton(text="📅", callback_data=f"move_pick:{idea_id}"),
            ])
        text = header + "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=task_btns + all_keyboard(done).inline_keyboard)

    if edit:
        await msg_or_call.edit_text(text, reply_markup=kb)
    else:
        await msg_or_call.answer(text, reply_markup=kb)


# ─── HANDLERS: КОМАНДЫ ─────────────────────────────────────────

@dp.message(Command("start", "menu", "sprint"))
async def cmd_sprint(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        await msg.reply("⛔ Нет доступа")
        return
    arg = msg.text.split(" ", 1)[1] if " " in msg.text else ""
    if msg.text.startswith("/start") or msg.text.startswith("/menu"):
        arg = ""
    due = parse_date(arg) if arg else date.today().isoformat()
    if not due:
        due = date.today().isoformat()
    await show_sprint(msg, due)


@dp.message(Command("all", "tasks"))
async def cmd_all(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await show_all(msg, 0)


@dp.message(Command("done"))
async def cmd_done(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    m = re.search(r"(\d+)", msg.text.replace("/done", "", 1))
    if not m:
        await msg.answer("Формат: /done 3")
        return
    idea_id = int(m.group(1))
    if set_done(idea_id, True):
        idea = get_idea(idea_id)
        await msg.answer(f"✅ <b>Выполнено</b> #{idea_id}: {idea[1]}")
    else:
        await msg.answer("❌ Задача не найдена")


@dp.message(Command("undone"))
async def cmd_undone(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    m = re.search(r"(\d+)", msg.text.replace("/undone", "", 1))
    if not m:
        await msg.answer("Формат: /undone 3")
        return
    idea_id = int(m.group(1))
    if set_done(idea_id, False):
        idea = get_idea(idea_id)
        await msg.answer(f"⬜ <b>Вернул в работу</b> #{idea_id}: {idea[1]}")
    else:
        await msg.answer("❌ Задача не найдена")


@dp.message(Command("move"))
async def cmd_move(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    arg = msg.text.replace("/move", "", 1).strip()
    m = re.search(r"(\d+)\s*(?:на\s*)?(.+)?", arg)
    if not m:
        await msg.answer("Формат: /move 3 на пятницу")
        return
    idea_id = int(m.group(1))
    raw = (m.group(2) or "").strip()
    if raw:
        due = parse_date(raw)
        if due:
            if set_due_date(idea_id, due):
                idea = get_idea(idea_id)
                await msg.answer(f"📅 <b>Перенесено</b> #{idea_id} на {fmt_date(due)}: {idea[1]}")
            else:
                await msg.answer("❌ Задача не найдена")
            return
    _pending_move[msg.from_user.id] = idea_id
    await msg.answer(
        f"📅 На какую дату перенести задачу #{idea_id}?\n"
        "Напиши: <i>пятница</i>, <i>12.08</i>, <i>завтра</i>…",
        reply_markup=move_pick_keyboard(idea_id),
    )


@dp.message(Command("remind"))
async def cmd_remind(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    open_list = get_ideas(0)
    done_list = get_ideas(1)
    if not open_list and not done_list:
        await msg.answer("📋 Пока ни одной задачи. Добавь через /start")
        return
    parts = []
    if open_list:
        items = []
        for i, t, due in open_list:
            prefix = f"[{fmt_date(due)}] " if due else ""
            items.append(f"⬜ #{i} {prefix}{t}")
        parts.append(f"📋 <b>Нужно сделать:</b>\n" + "\n".join(items))
    if done_list:
        items = "\n".join(f"✅ {t}" for _, t, _ in done_list)
        parts.append(f"\n✅ <b>Выполнено:</b>\n{items}")
    await msg.answer("\n\n".join(parts))


@dp.message(Command("cancel"))
async def cmd_cancel(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    _pending_move.pop(msg.from_user.id, None)
    await show_sprint(msg, date.today().isoformat())


@dp.message(Command("help"))
async def cmd_help(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(
        "🏠 <b>Как пользоваться</b>\n\n"
        "• /start — спринт на сегодня (все открытые задачи)\n"
        "• /all — все задачи, /remind — сводка\n"
        "• /done 3, /undone 3 — отметить / вернуть\n"
        "• /move 3 на пятницу — перенести\n"
        "• Просто текст — новая задача («купить хлеб на завтра»)\n"
        "• «отметь 3», «что на завтра» — тоже понимаю"
    )


# ─── HANDLERS: CALLBACKS ───────────────────────────────────────

@dp.callback_query(lambda c: c.data == "noop")
async def noop(call: types.CallbackQuery):
    await call.answer()


@dp.callback_query(lambda c: c.data == "all")
async def cb_all(call: types.CallbackQuery):
    await show_all(call.message, 0, edit=True)
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("tab:"))
async def switch_tab(call: types.CallbackQuery):
    tab = call.data.split(":")[1]
    await show_all(call.message, 1 if tab == "done" else 0, edit=True)
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("sprint:"))
async def handle_sprint(call: types.CallbackQuery):
    key = call.data.split(":", 1)[1]
    if key == "today":
        due = date.today().isoformat()
    elif key == "tomorrow":
        due = (date.today() + timedelta(days=1)).isoformat()
    elif key == "dayafter":
        due = (date.today() + timedelta(days=2)).isoformat()
    else:
        due = key
    await show_sprint(call.message, due, edit=True)
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("toggle:"))
async def handle_toggle(call: types.CallbackQuery):
    idea_id = int(call.data.split(":")[1])
    result = toggle_idea(idea_id)
    if result is None:
        await call.answer("❌ Задача не найдена", show_alert=True)
        return
    # возвращаемся на текущий экран (спринт сегодня)
    await show_sprint(call.message, date.today().isoformat(), edit=True)
    await call.answer("✅" if result else "⬜")


@dp.callback_query(lambda c: c.data.startswith("move_pick:"))
async def move_pick(call: types.CallbackQuery):
    idea_id = int(call.data.split(":")[1])
    await call.message.edit_text(
        f"📅 <b>Перенос задачи #{idea_id}</b>\nКуда?",
        reply_markup=move_pick_keyboard(idea_id),
    )
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("move_set:"))
async def move_set(call: types.CallbackQuery):
    _, idea_id, due = call.data.split(":")
    idea_id = int(idea_id)
    due = None if due == "none" else due
    if set_due_date(idea_id, due):
        idea = get_idea(idea_id)
        label = fmt_date(due) if due else "без даты"
        await call.message.edit_text(f"📅 <b>Перенесено</b> #{idea_id} на {label}: {idea[1]}")
    else:
        await call.message.edit_text("❌ Задача не найдена")
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("move_type:"))
async def move_type(call: types.CallbackQuery):
    idea_id = int(call.data.split(":")[1])
    _pending_move[call.from_user.id] = idea_id
    await call.message.edit_text(
        f"✏️ Напиши дату для задачи #{idea_id}:\n"
        "<i>пятница</i>, <i>12.08</i>, <i>завтра</i>, <i>послезавтра</i>…",
    )
    await call.answer()


@dp.callback_query(lambda c: c.data == "add_prompt")
async def ask_add(call: types.CallbackQuery):
    await call.message.answer(
        "✏️ <b>Напиши текст</b> — станет задачей.\n"
        "Можно сразу с датой: <i>купить хлеб на завтра</i>.\n"
        "Нажми /cancel чтобы отменить."
    )
    await call.answer()


@dp.callback_query(lambda c: c.data == "clear_done")
async def handle_clear_done(call: types.CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM ideas WHERE done = 1")
    conn.commit()
    conn.close()
    await call.answer("✅ Все выполненные удалены", show_alert=True)
    await show_sprint(call.message, date.today().isoformat(), edit=True)


# ─── HANDLERS: ГОЛОС И ТЕКСТ ───────────────────────────────────

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
        intent = parse_intent(text)
        if intent["action"] == "add":
            idea_id = add_idea(intent["text"], intent.get("date"))
            due_label = f" на {fmt_date(intent['date'])}" if intent.get("date") else ""
            await msg.answer(f"🎤 <b>Добавлено!</b> #{idea_id}{due_label}\n\n{intent['text']}")
        else:
            await msg.answer(f"🎤 <b>Распознано:</b> {text}")
            await _apply_intent(msg, intent)
    except Exception as e:
        logger.exception("Voice processing error")
        await msg.reply(f"❌ Ошибка обработки голоса: {e}")
    await show_sprint(msg, date.today().isoformat())


async def _apply_intent(msg: types.Message, intent: dict):
    action = intent["action"]
    if action == "sprint":
        await show_sprint(msg, intent["date"])
    elif action == "all":
        await show_all(msg, 0)
    elif action == "done":
        idea_id = intent["id"]
        if set_done(idea_id, True):
            idea = get_idea(idea_id)
            await msg.answer(f"✅ <b>Выполнено</b> #{idea_id}: {idea[1]}")
        else:
            await msg.answer(f"❌ Задача #{idea_id} не найдена")
    elif action == "undone":
        idea_id = intent["id"]
        if set_done(idea_id, False):
            idea = get_idea(idea_id)
            await msg.answer(f"⬜ <b>Вернул в работу</b> #{idea_id}: {idea[1]}")
        else:
            await msg.answer(f"❌ Задача #{idea_id} не найдена")
    elif action == "move":
        idea_id = intent["id"]
        due = intent["date"]
        if set_due_date(idea_id, due):
            idea = get_idea(idea_id)
            await msg.answer(f"📅 <b>Перенесено</b> #{idea_id} на {fmt_date(due)}: {idea[1]}")
        else:
            await msg.answer(f"❌ Задача #{idea_id} не найдена")
    elif action == "move_prompt":
        idea_id = intent["id"]
        _pending_move[msg.from_user.id] = idea_id
        await msg.answer(
            f"📅 На какую дату перенести задачу #{idea_id}?\n"
            "Напиши: <i>пятница</i>, <i>12.08</i>, <i>завтра</i>…",
            reply_markup=move_pick_keyboard(idea_id),
        )
    elif action == "add":
        idea_id = add_idea(intent["text"], intent.get("date"))
        due_label = f" на {fmt_date(intent['date'])}" if intent.get("date") else ""
        await msg.answer(f"✅ <b>Добавлено!</b> #{idea_id}{due_label}\n\n{intent['text']}")


@dp.message()
async def handle_text(msg: types.Message):
    if msg.from_user.id != ADMIN_ID or not msg.text:
        return
    if msg.text.startswith("/"):
        return
    text = msg.text.strip()

    pending_id = _pending_move.pop(msg.from_user.id, None)
    if pending_id is not None:
        due = parse_date(text)
        if due:
            if set_due_date(pending_id, due):
                idea = get_idea(pending_id)
                await msg.answer(f"📅 <b>Перенесено</b> #{pending_id} на {fmt_date(due)}: {idea[1]}")
            else:
                await msg.answer(f"❌ Задача #{pending_id} не найдена")
        else:
            await msg.answer(
                f"❌ Не понял дату «{text}». Попробуй ещё раз: <i>пятница</i>, <i>12.08</i>, <i>завтра</i>… "
                "или /cancel"
            )
            _pending_move[msg.from_user.id] = pending_id
        return

    intent = parse_intent(text)
    logger.info("Текст: %r → intent: %s", text, intent["action"])
    await _apply_intent(msg, intent)


# ─── MAIN ──────────────────────────────────────────────────────

async def main():
    init_db()
    if ai_client:
        logger.info("🎤 Голосовые сообщения включены (OpenAI Whisper)")
    else:
        logger.warning("🎤 Голосовые отключены — нет OpenAI API ключа/пакета")
    logger.info("🤖 @mytaskprogress_bot запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлен")
