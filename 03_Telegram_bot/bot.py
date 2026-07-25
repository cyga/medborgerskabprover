#!/usr/bin/env python3
"""Telegram-бот для подготовки к Medborgerskabsprøven.

Режимы:
  /quiz    – вечерняя тренировка: 10 вопросов (сначала новые, потом ошибки)
  /exam    – пробный экзамен: 25 случайных вопросов, как на настоящем
  /mistakes – повторить свои ошибки
  /stats   – статистика
  /reminder – ежедневное напоминание в 20:00

Данные: JSON-файлы прошлых экзаменов в ../02_Tidligere_proever/
Прогресс хранится в progress.json рядом с ботом.
Незавершённые сессии — в ptb_state.pickle (переживают перезапуск).
"""
import asyncio
import json
import os
import random
from datetime import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, PicklePersistence)

BASE = Path(__file__).parent
DATA_DIR = BASE.parent / "02_Tidligere_proever"
PROGRESS_FILE = BASE / "progress.json"
# Незавершённые сессии (context.user_data) — переживают перезапуск бота.
STATE_FILE = BASE / "ptb_state.pickle"
TOKEN = os.environ.get("BOT_TOKEN") or (BASE / "token.txt").read_text().strip()

QUIZ_LEN = 10
EXAM_LEN = 25
PASS_SCORE = 20

# ---------- загрузка вопросов ----------
QUESTIONS = {}  # id -> question dict
RU = {}
ru_file = BASE / "ru.json"
if ru_file.exists():
    RU = json.loads(ru_file.read_text(encoding="utf-8"))

for f in sorted(DATA_DIR.glob("*.json")):
    exam = json.loads(f.read_text(encoding="utf-8"))
    for q in exam["questions"]:
        qid = f'{exam["exam"]}:{q["n"]}'
        q["id"] = qid
        q["exam"] = exam["exam"]
        q["ru"] = RU.get(qid, "")
        QUESTIONS[qid] = q

# ---------- прогресс ----------
# Файл общий для всех пользователей, а цикл «прочитать → изменить → записать»
# не атомарен: без блокировки два одновременных ответа затрут друг друга.
PROGRESS_LOCK = asyncio.Lock()

def load_progress():
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    return {}

def save_progress(p):
    """Атомарная запись: пишем во временный файл и подменяем им основной.

    os.replace на одной ФС атомарен, поэтому при падении/убийстве процесса
    progress.json остаётся либо старым, либо новым, но не обрезанным.
    """
    tmp = PROGRESS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(p, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(PROGRESS_FILE)

def user_data(p, uid):
    return p.setdefault(str(uid), {"seen": {}, "wrong": [], "sessions": []})

# ---------- выбор вопросов ----------
def pick_questions(ud, k, exam_mode=False):
    all_ids = list(QUESTIONS)
    if exam_mode:
        return random.sample(all_ids, k)
    unseen = [i for i in all_ids if i not in ud["seen"]]
    wrong = [i for i in ud["wrong"] if i in QUESTIONS]
    random.shuffle(unseen)
    random.shuffle(wrong)
    picked = wrong[: k // 3] + unseen
    if len(picked) < k:  # всё видел – добираем случайными
        rest = [i for i in all_ids if i not in picked]
        random.shuffle(rest)
        picked += rest
    return picked[:k]

# ---------- отправка вопроса ----------
async def send_question(update_or_query, context):
    s = context.user_data["session"]
    idx = s["idx"]
    q = QUESTIONS[s["ids"][idx]]
    letters = sorted(q["opts"])
    text = f'❓ {idx + 1}/{len(s["ids"])}  [{q["exam"]}]\n\n{q["q"]}\n\n'
    text += "\n".join(f"{l}: {q['opts'][l]}" for l in letters)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(l, callback_data=l)
                                for l in letters]])
    msg = update_or_query.effective_message if isinstance(
        update_or_query, Update) else update_or_query.message
    await msg.reply_text(text, reply_markup=kb)

async def start_session(update, context, ids, mode):
    context.user_data["session"] = {"ids": ids, "idx": 0, "correct": 0,
                                    "mode": mode}
    await send_question(update, context)

# ---------- команды ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🇩🇰 Подготовка к Medborgerskabsprøven\n\n"
        f"В базе {len(QUESTIONS)} вопросов из реальных экзаменов 2021–2026.\n\n"
        "/quiz – 10 вопросов (вечерняя норма)\n"
        "/exam – пробный экзамен, 25 вопросов (проходной: 20)\n"
        "/mistakes – работа над ошибками\n"
        "/stats – статистика\n"
        "/reminder – напоминание каждый вечер в 20:00")

async def cmd_quiz(update, context):
    p = load_progress()
    ud = user_data(p, update.effective_user.id)
    await start_session(update, context,
                        pick_questions(ud, QUIZ_LEN), "quiz")

async def cmd_exam(update, context):
    await update.message.reply_text(
        "📝 Пробный экзамен: 25 вопросов. На реальном экзамене – 30 минут. "
        f"Проходной: {PASS_SCORE}/25. Держи темп ~1 мин/вопрос!")
    p = load_progress()
    ud = user_data(p, update.effective_user.id)
    await start_session(update, context,
                        pick_questions(ud, EXAM_LEN, exam_mode=True), "exam")

async def cmd_mistakes(update, context):
    p = load_progress()
    ud = user_data(p, update.effective_user.id)
    wrong = [i for i in ud["wrong"] if i in QUESTIONS]
    if not wrong:
        await update.message.reply_text("🎉 Нет накопленных ошибок!")
        return
    random.shuffle(wrong)
    await start_session(update, context, wrong[:QUIZ_LEN], "mistakes")

async def cmd_stats(update, context):
    p = load_progress()
    ud = user_data(p, update.effective_user.id)
    seen = len(ud["seen"])
    correct = sum(1 for v in ud["seen"].values() if v.get("last_ok"))
    exams = [s for s in ud["sessions"] if s["mode"] == "exam"]
    txt = (f"📊 Пройдено вопросов: {seen}/{len(QUESTIONS)}\n"
           f"Сейчас знаешь: {correct} ({correct * 100 // max(seen, 1)}%)\n"
           f"В работе над ошибками: {len(ud['wrong'])}\n")
    if exams:
        txt += "\nПробные экзамены:\n" + "\n".join(
            f"  {s['score']}/25 {'✅' if s['score'] >= PASS_SCORE else '❌'}"
            for s in exams[-5:])
    await update.message.reply_text(txt)

async def cmd_reminder(update, context):
    chat_id = update.effective_chat.id
    for j in context.job_queue.get_jobs_by_name(str(chat_id)):
        j.schedule_removal()
    context.job_queue.run_daily(reminder_job, time(hour=20, minute=0),
                                chat_id=chat_id, name=str(chat_id))
    await update.message.reply_text(
        "⏰ Буду напоминать каждый день в 20:00 (время сервера). /quiz!")

async def reminder_job(context):
    await context.bot.send_message(
        context.job.chat_id, "🇩🇰 Время вечерней тренировки! /quiz")

# ---------- обработка ответов ----------
async def on_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    s = context.user_data.get("session")
    if not s:
        await query.edit_message_reply_markup(None)
        return
    # Сессия могла быть восстановлена из файла после того, как база вопросов
    # изменилась — тогда продолжать её нельзя.
    if s["idx"] >= len(s["ids"]) or s["ids"][s["idx"]] not in QUESTIONS:
        context.user_data.pop("session", None)
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(
            "⚠️ Старая сессия больше не актуальна. Начни заново: /quiz")
        return
    qid = s["ids"][s["idx"]]
    q = QUESTIONS[qid]
    chosen = query.data
    ok = chosen == q["correct"]
    if ok:
        s["correct"] += 1
    last = s["idx"] + 1 >= len(s["ids"])

    # Весь цикл чтения-изменения-записи держим под одним локом, без await
    # внутри, чтобы параллельные ответы не потеряли чужой прогресс.
    async with PROGRESS_LOCK:
        p = load_progress()
        ud = user_data(p, update.effective_user.id)
        ud["seen"][qid] = {"last_ok": ok}
        if ok:
            if qid in ud["wrong"]:
                ud["wrong"].remove(qid)
        elif qid not in ud["wrong"]:
            ud["wrong"].append(qid)
        if last:
            ud["sessions"].append({"mode": s["mode"], "score": s["correct"],
                                   "total": len(s["ids"])})
        save_progress(p)

    fb = "✅ Rigtigt!" if ok else \
        f"❌ Forkert. Правильно: {q['correct']}: {q['opts'][q['correct']]}"
    if q.get("ru"):
        fb += f"\n🇷🇺 {q['ru']}"
    await query.edit_message_text(query.message.text + f"\n\n{fb}")

    s["idx"] += 1
    if not last:
        await send_question(update, context)
    else:
        score, total = s["correct"], len(s["ids"])
        if s["mode"] == "exam":
            verdict = ("🎉 BESTÅET! Ты бы сдал." if score >= PASS_SCORE
                       else f"❌ Не хватило {PASS_SCORE - score}. Ещё потренируемся.")
            await query.message.reply_text(
                f"Итог: {score}/{total}\n{verdict}")
        else:
            await query.message.reply_text(
                f"Готово: {score}/{total} ✅  Завтра ещё /quiz. /stats – прогресс")
        context.user_data.pop("session", None)

def main():
    # update_interval=15 — потолок потерь при жёстком kill; при обычной
    # остановке (Ctrl-C / SIGTERM) PTB сбрасывает состояние на диск сам.
    persistence = PicklePersistence(filepath=STATE_FILE, update_interval=15)
    app = (Application.builder().token(TOKEN)
           .persistence(persistence).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("quiz", cmd_quiz))
    app.add_handler(CommandHandler("exam", cmd_exam))
    app.add_handler(CommandHandler("mistakes", cmd_mistakes))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("reminder", cmd_reminder))
    app.add_handler(CallbackQueryHandler(on_answer))
    print(f"Бот запущен. Вопросов в базе: {len(QUESTIONS)}")
    app.run_polling()

if __name__ == "__main__":
    main()
