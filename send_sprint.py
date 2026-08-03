#!/usr/bin/env python3
"""
send_sprint.py — отправить спринт в @mytaskprogress_bot (dma-ideas).

1. Добавляет задачи в БД бота (ideas.db в контейнере dma-ideas).
2. Отправляет Диме сообщение со спринтом в чат с ботом (sendMessage от имени бота).

Примеры:
  python3 send_sprint.py --title "Спринт 03.08" "Задача 1" "Задача 2"
  python3 send_sprint.py --title "Спринт" --file tasks.txt
  python3 send_sprint.py --title "Спринт" --file tasks.json
  python3 send_sprint.py --title "Простое сообщение без задач"   # только уведомление
"""

import argparse
import html
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
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
    """сегодня/завтра/послезавтра/день недели/ДД.ММ[.ГГГГ] → YYYY-MM-DD"""
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


def load_env() -> dict:
    env = {}
    for line in (HERE / ".env").read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env()
TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = ENV.get("ADMIN_ID", "7653823001")


def add_tasks_to_db(tasks: list[str], due_date: str | None = None) -> list[int]:
    """Insert tasks into the bot's DB (inside container) via stdin JSON."""
    code = (
        "import json, sqlite3, sys\n"
        "from datetime import datetime\n"
        "tasks = json.loads(sys.stdin.read())\n"
        "conn = sqlite3.connect('/app/data/ideas.db')\n"
        "now = datetime.now().isoformat()\n"
        "ids = []\n"
        "for t in tasks:\n"
        "    cur = conn.execute('INSERT INTO ideas (text, done, created_at, due_date) VALUES (?,0,?,?)', (t, now, due_date))\n"
        "    ids.append(cur.lastrowid)\n"
        "conn.commit()\n"
        "conn.close()\n"
        "print(json.dumps(ids))\n"
    )
    payload = {"tasks": tasks, "due_date": due_date}
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", "-c", code],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"DB insert failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout.strip())


def send_message(text: str) -> dict:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"}
    ).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def load_tasks(args) -> list[str]:
    tasks = list(args.tasks)
    if args.file:
        data = Path(args.file).read_text(encoding="utf-8")
        try:
            tasks.extend(json.loads(data))
        except json.JSONDecodeError:
            tasks.extend(line.strip() for line in data.splitlines() if line.strip())
    return tasks


def main():
    parser = argparse.ArgumentParser(description="Send sprint to @mytaskprogress_bot")
    parser.add_argument("--title", default="Спринт")
    parser.add_argument("--date", help="due date: сегодня/завтра/пятница/12.08/2026-08-03")
    parser.add_argument("--file", help="file with tasks: one per line or JSON list")
    parser.add_argument("tasks", nargs="*", help="tasks to add")
    args = parser.parse_args()

    tasks = load_tasks(args)
    due = parse_date(args.date) if args.date else None

    ids = []
    if tasks:
        ids = add_tasks_to_db(tasks, due)
        body = "\n".join(f"⬜ {html.escape(t)}" for t in tasks)
        title = args.title
        if due:
            title = f"{title} · {due}"
        text = f"📋 <b>{html.escape(title)}</b>\n\n{body}"
    else:
        text = args.title  # notification only, no tasks

    result = send_message(text)
    if not result.get("ok"):
        sys.exit(f"sendMessage failed: {result}")

    print(json.dumps({
        "ok": True,
        "added_ids": ids,
        "due_date": due,
        "message_id": result.get("result", {}).get("message_id"),
        "title": args.title,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
