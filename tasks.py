#!/usr/bin/env python3
"""
tasks.py — управление задачами @mytaskprogress_bot (dma-ideas) из чата с DimClaw.

Используется оркестратором (мной) для выполнения команд Димы:
- показать спринт на день / общий список
- отметить выполненной / вернуть
- перенести на дату
- добавить задачу
- итоги дня

Примеры:
  python3 tasks.py sprint              # спринт на сегодня
  python3 tasks.py sprint завтра       # спринт на завтра
  python3 tasks.py sprint 12.08
  python3 tasks.py list                # все открытые (общий список)
  python3 tasks.py done 64
  python3 tasks.py undone 64
  python3 tasks.py move 64 на пятницу
  python3 tasks.py move 64 2026-08-05
  python3 tasks.py add "Купить молоко" --date завтра
  python3 tasks.py summary             # итоги сегодня
  python3 tasks.py summary 2026-08-03
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import date, timedelta

CONTAINER = "dma-ideas"
DB_PATH = "/app/data/ideas.db"

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
            delta = (wd - today.weekday()) % 7 or 7
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


def db_exec(code: str) -> str:
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", "-c", code],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        sys.exit(f"DB error: {proc.stderr.strip()}")
    return proc.stdout.strip()


def cmd_sprint(due: str):
    code = f"""
import sqlite3, json
conn = sqlite3.connect('{DB_PATH}')
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT id, text, done FROM ideas WHERE due_date=? ORDER BY done ASC, created_at ASC", ('{due}',)).fetchall()
print(json.dumps([dict(r) for r in rows], ensure_ascii=False))
conn.close()
"""
    rows = json.loads(db_exec(code))
    label = fmt_date(due)
    if not rows:
        print(f"Спринт на {label}: пусто")
        return
    done_n = sum(1 for r in rows if r["done"])
    print(f"🏃 Спринт на {label}: {len(rows)} задач, {done_n} ✅")
    for r in rows:
        mark = "✅" if r["done"] else "⬜"
        print(f"#{r['id']} {mark} {r['text']}")


def cmd_list():
    code = f"""
import sqlite3, json
conn = sqlite3.connect('{DB_PATH}')
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT id, text, done, due_date FROM ideas WHERE done=0 ORDER BY due_date IS NULL, due_date ASC, id").fetchall()
print(json.dumps([dict(r) for r in rows], ensure_ascii=False))
conn.close()
"""
    rows = json.loads(db_exec(code))
    if not rows:
        print("Открытых задач нет")
        return
    print(f"📚 Открытые задачи: {len(rows)}")
    for r in rows:
        tag = f"[{fmt_date(r['due_date'])}] " if r["due_date"] else ""
        print(f"#{r['id']} ⬜ {tag}{r['text']}")


def cmd_done(idea_id: int, done: bool):
    mark = 1 if done else 0
    label = "✅ Выполнено" if done else "⬜ Вернул в работу"
    code = f"""
import sqlite3, json
conn = sqlite3.connect('{DB_PATH}')
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT id, text FROM ideas WHERE id=?", ({idea_id},)).fetchone()
if not row:
    print(json.dumps({{"ok": False, "error": "not found"}}))
else:
    conn.execute("UPDATE ideas SET done=? WHERE id=?", ({mark}, {idea_id}))
    conn.commit()
    print(json.dumps({{"ok": True, "id": row['id'], "text": row['text']}}, ensure_ascii=False))
conn.close()
"""
    res = json.loads(db_exec(code))
    if not res["ok"]:
        print(f"❌ Задача #{idea_id} не найдена")
        return
    print(f"{label} #{res['id']}: {res['text']}")


def cmd_move(idea_id: int, due: str):
    code = f"""
import sqlite3, json
conn = sqlite3.connect('{DB_PATH}')
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT id, text FROM ideas WHERE id=?", ({idea_id},)).fetchone()
if not row:
    print(json.dumps({{"ok": False, "error": "not found"}}))
else:
    conn.execute("UPDATE ideas SET due_date=? WHERE id=?", ('{due}', {idea_id}))
    conn.commit()
    print(json.dumps({{"ok": True, "id": row['id'], "text": row['text']}}, ensure_ascii=False))
conn.close()
"""
    res = json.loads(db_exec(code))
    if not res["ok"]:
        print(f"❌ Задача #{idea_id} не найдена")
        return
    print(f"📅 Перенесено #{res['id']} на {fmt_date(due)}: {res['text']}")


def cmd_add(text: str, due: str | None):
    code = f"""
import sqlite3, json
from datetime import datetime
conn = sqlite3.connect('{DB_PATH}')
cur = conn.execute("INSERT INTO ideas (text, done, created_at, due_date) VALUES (?,0,?,?)", ({json.dumps(text)}, datetime.now().isoformat(), {json.dumps(due)}))
conn.commit()
print(json.dumps({{"id": cur.lastrowid}}, ensure_ascii=False))
conn.close()
"""
    res = json.loads(db_exec(code))
    due_label = f" на {fmt_date(due)}" if due else ""
    print(f"✅ Добавлено #{res['id']}{due_label}: {text}")


def cmd_summary(due: str):
    code = f"""
import sqlite3, json
conn = sqlite3.connect('{DB_PATH}')
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT id, text, done FROM ideas WHERE due_date=? ORDER BY done ASC, id", ('{due}',)).fetchall()
print(json.dumps([dict(r) for r in rows], ensure_ascii=False))
conn.close()
"""
    rows = json.loads(db_exec(code))
    label = fmt_date(due)
    if not rows:
        print(f"Итоги {label}: задач не было")
        return
    done_n = sum(1 for r in rows if r["done"])
    total = len(rows)
    print(f"📊 Итоги {label}: {done_n}/{total} выполнено ({round(done_n/total*100)}%)")
    for r in rows:
        mark = "✅" if r["done"] else "⬜"
        print(f"#{r['id']} {mark} {r['text']}")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sprint", help="спринт на дату")
    p.add_argument("date", nargs="?", default="сегодня")

    p = sub.add_parser("list", help="все открытые задачи")
    p = sub.add_parser("done", help="отметить выполненной")
    p.add_argument("id", type=int)
    p = sub.add_parser("undone", help="вернуть в работу")
    p.add_argument("id", type=int)
    p = sub.add_parser("move", help="перенести на дату")
    p.add_argument("id", type=int)
    p.add_argument("date", help="куда: завтра/пятница/12.08/2026-08-05")
    p = sub.add_parser("add", help="добавить задачу")
    p.add_argument("text")
    p.add_argument("--date", default=None, help="дата")
    p = sub.add_parser("summary", help="итоги дня")
    p.add_argument("date", nargs="?", default="сегодня")

    args = parser.parse_args()

    if args.cmd == "sprint":
        due = parse_date(args.date) or date.today().isoformat()
        cmd_sprint(due)
    elif args.cmd == "list":
        cmd_list()
    elif args.cmd == "done":
        cmd_done(args.id, True)
    elif args.cmd == "undone":
        cmd_done(args.id, False)
    elif args.cmd == "move":
        due = parse_date(args.date)
        if not due:
            sys.exit(f"❌ Не понял дату: {args.date}")
        cmd_move(args.id, due)
    elif args.cmd == "add":
        due = parse_date(args.date) if args.date else None
        cmd_add(args.text, due)
    elif args.cmd == "summary":
        due = parse_date(args.date) or date.today().isoformat()
        cmd_summary(due)


if __name__ == "__main__":
    main()
