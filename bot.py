#!/usr/bin/env python3
"""
@mytaskprogress_bot — трекер идей/дел/планов
Фичи: текст + голос, пагинация, вкладки Открытые/Выполненные,
спринты по датам, перенос задач на любой день, NLU-команды.
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

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7653823001"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
PAGE_SIZE = 6
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

    m = re.search(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?", text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    return None


def fmt_date(due: str | None) -> str:
    """'YYYY-MM-DD' → 'ДД.ММ' или 'сегодня'/'завтра'"""
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
    # миграция: колонка due_date
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


def get_ideas(done: int, page: int = 0) -> tuple[list[tuple], int]:
    """(id, text, due_date) по статусу с пагинацией."""
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute(
        "SELECT COUNT(*) FROM ideas WHERE done = ?", (done,)
    ).fetchone()[0]
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    rows = conn.execute(
        "SELECT id, text, due_date FROM ideas WHERE done = ? "
        "ORDER BY COALESCE(due_date, '9999-12-31') ASC, created_at DESC "
        "LIMIT ? OFFSET ?",
        (done, PAGE_SIZE, page * PAGE_SIZE),
    ).fetchall()
    conn.close()
    return rows, total_pages


def get_sprint(due_date: str) -> list[tuple]:
    """Задачи на конкретную дату: [(id, text, done)]"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, text, done FROM ideas WHERE due_date = ? ORDER BY done ASC, created_at ASC",
        (due_date,),
    ).fetchall()
    conn.close()
    return rows


def get_sprint_dates() -> list[str]:
    """Все даты, на которые есть задачи (включая выполненные)"""
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

    # 4. Спринт: «спринт», «что на завтра», «план на пятницу», «задачи на 05.08», «на сегодня»
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

    # 6. Добавить с датой: «добавь купить хлеб на завтра», «запиши ... на пятницу»
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

def _page_nav(tab: str, page: int, total_pages: int):
    btns = []
    if page > 0:
        btns.append(InlineKeyboardButton(text="◀️", callback_data=f"page:{tab}:{page - 1}"))
    btns.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        btns.append(InlineKeyboardButton(text="▶️", callback_data=f"page:{tab}:{page + 1}"))
    return btns


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Спринт на сегодня", callback_data="sprint:today")],
        [InlineKeyboardButton(text="📚 Все задачи", callback_data="tab:open")],
        [InlineKeyboardButton(text="✅ Выполненные", callback_data="tab:done")],
        [InlineKeyboardButton(text="➕ Добавить задачу", callback_data="add_prompt")],
    ])


def _task_buttons(idea_id: int, page: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text="📅", callback_data=f"move_pick:{idea_id}:{page}"),
        InlineKeyboardButton(text="🗑", callback_data=f"delete:{idea_id}:{page}"),
    ]


def open_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    rows, total_pages = get_ideas(0, page)
    kb = []

    kb.append([
        InlineKeyboardButton(text="👉 📋 Открытые 👈", callback_data="noop"),
        InlineKeyboardButton(text="✅ Выполненные", callback_data="tab:done"),
    ])

    if not rows:
        kb.append([InlineKeyboardButton(text="➕ Добавить задачу", callback_data="add_prompt")])
    else:
        for idea_id, text, due in rows:
            short = text[:45] + ("…" if len(text) > 45 else "")
            label = f"⬜  {short}"
            if due:
                label = f"⬜  [{fmt_date(due)}] {short}"
            kb.append([
                InlineKeyboardButton(text=label, callback_data=f"toggle:{idea_id}:{page}"),
                *_task_buttons(idea_id, page),
            ])
        if total_pages > 1:
            kb.append(_page_nav("open", page, total_pages))
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
        for idea_id, text, due in rows:
            short = text[:45] + ("…" if len(text) > 45 else "")
            label = f"✅  {short}"
            if due:
                label = f"✅  [{fmt_date(due)}] {short}"
            kb.append([
                InlineKeyboardButton(text=label, callback_data=f"toggle:{idea_id}:{page}"),
                *_task_buttons(idea_id, page),
            ])
        if total_pages > 1:
            kb.append(_page_nav("done", page, total_pages))
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
    for idea_id, text, due in rows:
        short = text[:30] + ("…" if len(text) > 30 else "")
        if due:
            short = f"[{fmt_date(due)}] {short}"
        kb.append([
            InlineKeyboardButton(text=f"❌  {short}", callback_data=f"delete:{idea_id}:{page}")
        ])
    if total_pages > 1:
        kb.append(_page_nav("delete", page, total_pages))
    kb.append([InlineKeyboardButton(text="◀️ Назад к задачам", callback_data="tab:open")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def sprint_keyboard(due: str) -> InlineKeyboardMarkup:
    rows = get_sprint(due)
    kb = []
    if not rows:
        kb.append([InlineKeyboardButton(text="— пусто —", callback_data="noop")])
    else:
        for idea_id, text, done in rows:
            short = text[:45] + ("…" if len(text) > 45 else "")
            mark = "✅" if done else "⬜"
            kb.append([
                InlineKeyboardButton(
                    text=f"{mark}  {short}",
                    callback_data=f"toggle:{idea_id}:0",
                ),
                *_task_buttons(idea_id, 0),
            ])
    kb.append([
        InlineKeyboardButton(text="📅 Сегодня", callback_data="sprint:today"),
        InlineKeyboardButton(text="📅 Завтра", callback_data="sprint:tomorrow"),
    ])
    kb.append([
        InlineKeyboardButton(text="📅 Послезавтра", callback_data="sprint:dayafter"),
        InlineKeyboardButton(text="🗓 Другая дата", callback_data="sprint_pick_date"),
    ])
    kb.append([
        InlineKeyboardButton(text="📚 Все задачи", callback_data="tab:open"),
        InlineKeyboardButton(text="➕ Добавить", callback_data="add_prompt"),
        InlineKeyboardButton(text="🏠 Меню", callback_data="menu"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def sprint_pick_keyboard() -> InlineKeyboardMarkup:
    """Выбор даты из существующих спринтов + быстрое меню"""
    dates = get_sprint_dates()
    kb = []
    for d in dates:
        kb.append([InlineKeyboardButton(text=f"🗓 {fmt_date(d)}", callback_data=f"sprint:{d}")])
    kb.append([
        InlineKeyboardButton(text="📅 Сегодня", callback_data="sprint:today"),
        InlineKeyboardButton(text="📅 Завтра", callback_data="sprint:tomorrow"),
    ])
    kb.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def move_pick_keyboard(idea_id: int, page: int = 0) -> InlineKeyboardMarkup:
    today = date.today()
    kb = [
        [InlineKeyboardButton(text="📅 Сегодня", callback_data=f"move_set:{idea_id}:{today.isoformat()}")],
        [InlineKeyboardButton(text="📅 Завтра", callback_data=f"move_set:{idea_id}:{(today + timedelta(days=1)).isoformat()}")],
        [InlineKeyboardButton(text="📅 Послезавтра", callback_data=f"move_set:{idea_id}:{(today + timedelta(days=2)).isoformat()}")],
        [InlineKeyboardButton(text="✏️ Написать дату", callback_data=f"move_type:{idea_id}:{page}")],
        [InlineKeyboardButton(text="🚫 Без даты", callback_data=f"move_set:{idea_id}:none")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"move_back:{page}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ─── VIEWS ─────────────────────────────────────────────────────

def _header() -> str:
    open_c, done_c = count_ideas()
    return (
        f"📋 <b>Мои идеи / дела / планы</b>\n"
        f"└ {open_c + done_c} всего · {done_c} ✅ выполнено\n\n"
    )


async def show_tab(msg_or_call, tab: str, page: int = 0, edit: bool = False):
    header = _header()
    if tab == "open":
        text = header + "<b>📋 Открытые задачи</b>\n(нажми на задачу — отметишь ✅, 📅 — перенести)"
        markup = open_keyboard(page)
    else:
        text = header + "<b>✅ Выполненные</b>"
        markup = done_keyboard(page)

    if edit:
        await msg_or_call.edit_text(text, reply_markup=markup)
    else:
        await msg_or_call.answer(text, reply_markup=markup)


async def show_sprint(msg_or_call, due: str, edit: bool = False):
    rows = get_sprint(due)
    label = fmt_date(due)
    if rows:
        open_n = sum(1 for _, _, d in rows if not d)
        done_n = len(rows) - open_n
        text = (
            f"🏃 <b>Спринт на {label}</b>\n"
            f"└ {len(rows)} задач · {done_n} ✅ выполнено\n\n"
        )
        for idea_id, t, done in rows:
            mark = "✅" if done else "⬜"
            text += f"{mark} <b>#{idea_id}</b> {t}\n"
    else:
        text = f"🏃 <b>Спринт на {label}</b>\n\nПока пусто. Добавь задачу или перенеси сюда 📅"

    if edit:
        await msg_or_call.edit_text(text, reply_markup=sprint_keyboard(due))
    else:
        await msg_or_call.answer(text, reply_markup=sprint_keyboard(due))


# ─── HANDLERS: КОМАНДЫ ─────────────────────────────────────────

@dp.message(Command("start", "menu"))
async def cmd_start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        await msg.reply("⛔ Нет доступа")
        return
    open_c, done_c = count_ideas()
    await msg.answer(
        _header()
        + "🏠 <b>Главное меню</b>\n\n"
        "Что умею:\n"
        "• /sprint — спринт на сегодня\n"
        "• /sprint завтра — на завтра\n"
        "• /all — все задачи\n"
        "• /done 3 — отметить выполненной\n"
        "• /undone 3 — вернуть в работу\n"
        "• /move 3 на пятницу — перенести\n"
        "• Просто текст — новая задача\n"
        "• «спринт», «что на завтра», «отметь 3» — тоже понимаю",
        reply_markup=main_menu_keyboard(),
    )


@dp.message(Command("sprint"))
async def cmd_sprint(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    arg = msg.text.replace("/sprint", "", 1).strip()
    due = parse_date(arg) if arg else date.today().isoformat()
    if not due:
        due = date.today().isoformat()
    await show_sprint(msg, due)


@dp.message(Command("all", "tasks"))
async def cmd_all(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await show_tab(msg, "open")


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
    conn = sqlite3.connect(DB_PATH)
    open_list = conn.execute(
        "SELECT id, text, due_date FROM ideas WHERE done = 0 ORDER BY COALESCE(due_date, '9999-12-31') ASC, created_at DESC"
    ).fetchall()
    done_list = conn.execute(
        "SELECT id, text, due_date FROM ideas WHERE done = 1 ORDER BY created_at DESC"
    ).fetchall()
    conn.close()

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
    await show_tab(msg, "open")


@dp.message(Command("help"))
async def cmd_help(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await cmd_start(msg)


# ─── HANDLERS: CALLBACKS ───────────────────────────────────────

@dp.callback_query(lambda c: c.data == "noop")
async def noop(call: types.CallbackQuery):
    await call.answer()


@dp.callback_query(lambda c: c.data == "menu")
async def go_menu(call: types.CallbackQuery):
    open_c, done_c = count_ideas()
    await call.message.edit_text(
        _header()
        + "🏠 <b>Главное меню</b>\n\n"
        "• 📋 Спринт на сегодня\n"
        "• 📚 Все задачи\n"
        "• ✅ Выполненные\n"
        "• ➕ Добавить задачу",
        reply_markup=main_menu_keyboard(),
    )
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


@dp.callback_query(lambda c: c.data == "sprint_pick_date")
async def sprint_pick_date(call: types.CallbackQuery):
    await call.message.edit_text(
        "🗓 <b>Выбери дату спринта</b>",
        reply_markup=sprint_pick_keyboard(),
    )
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("move_pick:"))
async def move_pick(call: types.CallbackQuery):
    _, idea_id, page = call.data.split(":")
    await call.message.edit_text(
        f"📅 <b>Перенос задачи #{idea_id}</b>\nКуда?",
        reply_markup=move_pick_keyboard(int(idea_id), int(page)),
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
    _, idea_id, page = call.data.split(":")
    _pending_move[call.from_user.id] = int(idea_id)
    await call.message.edit_text(
        f"✏️ Напиши дату для задачи #{idea_id}:\n"
        "<i>пятница</i>, <i>12.08</i>, <i>завтра</i>, <i>послезавтра</i>…",
    )
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("move_back:"))
async def move_back(call: types.CallbackQuery):
    page = int(call.data.split(":")[1])
    await show_tab(call.message, "open", page, edit=True)
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
        await show_tab(call.message, tab, page, edit=True)
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
        "Можно сразу с датой: <i>купить хлеб на завтра</i>\n"
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
            logger.info("Добавлена идея #%s (голос): %s", idea_id, text[:60])
            await msg.answer(f"🎤 <b>Распознано и добавлено!</b> #{idea_id}{due_label}\n\n{intent['text']}")
        else:
            await msg.answer(f"🎤 <b>Распознано:</b> {text}")
            await _apply_intent(msg, intent)

    except Exception as e:
        logger.exception("Voice processing error")
        await msg.reply(f"❌ Ошибка обработки голоса: {e}")

    await show_tab(msg, "open")


async def _apply_intent(msg: types.Message, intent: dict):
    """Применяет NLU-интент, отвечает результатом."""
    action = intent["action"]
    if action == "sprint":
        await show_sprint(msg, intent["date"])
    elif action == "all":
        await show_tab(msg, "open")
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

    # Если ждём дату для переноса
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
