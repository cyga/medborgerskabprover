#!/usr/bin/env python3
"""Telegram-бот для подготовки к Medborgerskabsprøven.

Режимы:
  /quiz    – вечерняя тренировка: 10 вопросов (сначала новые, потом ошибки)
  /exam    – пробный экзамен: 25 случайных вопросов, как на настоящем
  /mistakes – повторить свои ошибки
  /stats   – статистика
  /reminder – ежедневное напоминание в 20:00

Данные: JSON-файлы прошлых экзаменов в ../02_Tidligere_proever/
Прогресс и незавершённые сессии — в data-quick/ на локальном диске
(progress.json и ptb_state.pickle), копия раз в минуту уезжает в data/,
который является симлинком на Google Drive.
"""
import asyncio
import json
import logging
import os
import random
import shutil
import unicodedata
from datetime import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, PicklePersistence)

BASE = Path(__file__).parent
DATA_DIR = BASE.parent / "02_Tidligere_proever"
# Рабочие данные — на локальном диске: запись должна быть быстрой и атомарной.
QUICK_DIR = BASE / "data-quick"
QUICK_DIR.mkdir(parents=True, exist_ok=True)
PROGRESS_FILE = QUICK_DIR / "progress.json"
# Незавершённые сессии (context.user_data) — переживают перезапуск бота.
STATE_FILE = QUICK_DIR / "ptb_state.pickle"
TOKEN = os.environ.get("BOT_TOKEN") or (BASE / "token.txt").read_text().strip()

# data/ — симлинк на Google Drive (google-drive-ocamlfuse). Запись туда стоит
# ~3.6 с против 5 мс на локальном диске, поэтому рабочие файлы остаются
# локальными, а на Drive уезжает копия — в отдельном потоке, по таймеру.
BACKUP_DIR = BASE / "data"
BACKUP_INTERVAL = 60  # секунд между проверками «есть ли что копировать»

logger = logging.getLogger(__name__)

QUIZ_LEN = 10
EXAM_LEN = 25
PASS_SCORE = 20
REMINDER_TIME = time(hour=20, minute=0)  # время сервера

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

# ---------- группировка дубликатов ----------
# Одна и та же формулировка встречается в разных экзаменах (275 карточек на
# ~195 уникальных вопросов), иногда с другими вариантами ответа и другой
# буквой правильного. Для учёта прогресса это один и тот же факт, поэтому
# карточки группируются по нормализованному тексту вопроса.
def _norm(s):
    s = unicodedata.normalize("NFC", " ".join(s.split())).lower()
    return "".join(c for c in s if c.isalnum() or c.isspace())

GROUPS = {}    # gid (самый ранний id группы) -> [id, ...]
GROUP_OF = {}  # id -> gid
_by_text = {}
for qid, q in QUESTIONS.items():
    _by_text.setdefault(_norm(q["q"]), []).append(qid)
for ids in _by_text.values():
    gid = sorted(ids)[0]
    GROUPS[gid] = sorted(ids)
    for i in ids:
        GROUP_OF[i] = gid

def group_ids(ids):
    """Множество групп, к которым относятся карточки ids."""
    return {GROUP_OF[i] for i in ids if i in GROUP_OF}

def pick_variant(gid, prefer=None):
    """Случайная формулировка из группы; prefer — если нужна конкретная."""
    if prefer in GROUPS[gid]:
        return prefer
    return random.choice(GROUPS[gid])

# ---------- прогресс ----------
# Файл общий для всех пользователей, а цикл «прочитать → изменить → записать»
# не атомарен: без блокировки два одновременных ответа затрут друг друга.
PROGRESS_LOCK = asyncio.Lock()
_backup_pending = False  # есть ли несохранённые на Drive изменения

def load_progress():
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    return {}

def backup_files():
    return [f for f in (PROGRESS_FILE, STATE_FILE) if f.exists()]

def drive_ready():
    """Смонтирован ли Drive.

    Сервис стартует при загрузке машины, а google-drive-ocamlfuse монтируется
    только при графическом входе в систему. Без этой проверки mkdir создал бы
    настоящие каталоги внутри точки монтирования: копия легла бы на локальный
    диск, а после монтирования Drive исчезла бы из виду — и всё это с бодрой
    записью об успехе в логе.
    """
    target = BACKUP_DIR.resolve()
    probe = target
    while not probe.exists():          # каталогов на Drive может ещё не быть
        probe = probe.parent
    # смонтированный fuse — это другое устройство, чем локальный диск
    return probe.stat().st_dev != QUICK_DIR.stat().st_dev

def _copy_to_drive():
    """Синхронная копия на Drive. Вызывать только из отдельного потока!"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    names = []
    for src in backup_files():
        tmp = BACKUP_DIR / (src.name + ".tmp")
        shutil.copyfile(src, tmp)
        tmp.replace(BACKUP_DIR / src.name)
        names.append(src.name)
    return names

async def backup_job(context=None):
    """Копирует локальные файлы на Drive, если с прошлого раза что-то менялось."""
    global _backup_pending
    if not _backup_pending:
        return
    if not await asyncio.to_thread(drive_ready):
        # флаг не снимаем: скопируем, когда Drive появится
        logger.debug("Drive не смонтирован, копия отложена")
        return
    _backup_pending = False
    try:
        names = await asyncio.to_thread(_copy_to_drive)
        logger.info("на Drive скопировано: %s", ", ".join(names) or "нечего")
    except OSError as e:
        # Drive отвалился или размонтирован — бот продолжает работать локально
        _backup_pending = True
        logger.warning("копия на Drive не удалась (%s), повтор через %d c",
                       e, BACKUP_INTERVAL)

def restore_from_drive():
    """На чистой машине поднимает прогресс из Drive. Локальные файлы важнее."""
    if not drive_ready():
        logger.info("Drive не смонтирован — восстанавливать нечего")
        return
    for name in ("progress.json", "ptb_state.pickle"):
        local, remote = QUICK_DIR / name, BACKUP_DIR / name
        if not local.exists() and remote.exists():
            shutil.copyfile(remote, local)
            logger.info("восстановлено с Drive: %s", name)

def save_progress(p):
    """Атомарная запись: пишем во временный файл и подменяем им основной.

    os.replace на одной ФС атомарен, поэтому при падении/убийстве процесса
    progress.json остаётся либо старым, либо новым, но не обрезанным.
    """
    global _backup_pending
    tmp = PROGRESS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(p, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(PROGRESS_FILE)
    _backup_pending = True  # копию на Drive заберёт фоновый backup_job

def user_data(p, uid):
    return p.setdefault(str(uid), {"seen": {}, "wrong": [], "sessions": []})

# ---------- выбор вопросов ----------
def pick_questions(ud, k, exam_mode=False):
    """Возвращает до k карточек, не более одной из каждой группы дубликатов."""
    all_gids = list(GROUPS)
    if exam_mode:
        gids = random.sample(all_gids, min(k, len(all_gids)))
        return [pick_variant(g) for g in gids]

    seen_g = group_ids(ud["seen"])
    # ошибки переспрашиваем той же формулировкой, на которой споткнулись
    wrong_pairs, taken = [], set()
    for i in ud["wrong"]:
        g = GROUP_OF.get(i)
        if g and g not in taken:
            taken.add(g)
            wrong_pairs.append((g, i))
    unseen_g = [g for g in all_gids if g not in seen_g]
    random.shuffle(wrong_pairs)
    random.shuffle(unseen_g)

    picked, used_g = [], set()
    for g, qid in wrong_pairs[: k // 3]:
        picked.append(qid)
        used_g.add(g)
    for pool in (unseen_g, all_gids):  # всё видел – добираем остальными
        random.shuffle(pool)
        for g in pool:
            if len(picked) >= k:
                return picked
            if g not in used_g:
                picked.append(pick_variant(g))
                used_g.add(g)
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
        f"В базе {len(GROUPS)} уникальных вопросов из реальных экзаменов "
        f"2021–2026 ({len(QUESTIONS)} карточек с учётом повторов).\n\n"
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
    wrong, taken = [], set()
    for i in ud["wrong"]:  # по одной формулировке на факт
        g = GROUP_OF.get(i)
        if g and g not in taken:
            taken.add(g)
            wrong.append(i)
    if not wrong:
        await update.message.reply_text("🎉 Нет накопленных ошибок!")
        return
    random.shuffle(wrong)
    await start_session(update, context, wrong[:QUIZ_LEN], "mistakes")

async def cmd_stats(update, context):
    p = load_progress()
    ud = user_data(p, update.effective_user.id)
    # считаем по уникальным вопросам, а не по карточкам-дубликатам
    seen_g = group_ids(ud["seen"])
    wrong_g = group_ids(ud["wrong"])
    seen = len(seen_g)
    correct = len(seen_g - wrong_g)
    exams = [s for s in ud["sessions"] if s["mode"] == "exam"]
    txt = (f"📊 Пройдено вопросов: {seen}/{len(GROUPS)}\n"
           f"Сейчас знаешь: {correct} ({correct * 100 // max(seen, 1)}%)\n"
           f"В работе над ошибками: {len(wrong_g)}\n")
    if exams:
        txt += "\nПробные экзамены:\n" + "\n".join(
            f"  {s['score']}/25 {'✅' if s['score'] >= PASS_SCORE else '❌'}"
            for s in exams[-5:])
    await update.message.reply_text(txt)

def schedule_reminder(job_queue, chat_id):
    """Ставит (или переставляет) ежедневное напоминание для чата."""
    for j in job_queue.get_jobs_by_name(str(chat_id)):
        j.schedule_removal()
    job_queue.run_daily(reminder_job, REMINDER_TIME,
                        chat_id=chat_id, name=str(chat_id))

async def cmd_reminder(update, context):
    chat_id = update.effective_chat.id
    schedule_reminder(context.job_queue, chat_id)
    # JobQueue не персистится, поэтому список чатов храним сами — в bot_data,
    # который PicklePersistence сохраняет и поднимает при старте.
    context.bot_data.setdefault("reminders", set()).add(chat_id)
    await update.message.reply_text(
        "⏰ Буду напоминать каждый день в 20:00 (время сервера). /quiz!")

async def restore_reminders(app):
    """Перерегистрирует напоминания после перезапуска бота."""
    chats = sorted(app.bot_data.get("reminders", set()))
    for chat_id in chats:
        schedule_reminder(app.job_queue, chat_id)
    logger.info("восстановлено напоминаний: %d", len(chats))

async def reminder_job(context):
    chat_id = context.job.chat_id
    try:
        await context.bot.send_message(
            chat_id, "🇩🇰 Время вечерней тренировки! /quiz")
    except Forbidden:
        # бота заблокировали или чат удалён — снимаем напоминание насовсем
        context.bot_data.get("reminders", set()).discard(chat_id)
        context.job.schedule_removal()
        logger.info("напоминание снято: чат %s недоступен", chat_id)

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
        # факт учитывается целиком: ответил верно на любую формулировку —
        # закрываются все её дубликаты, ошибся — хватает одной записи
        siblings = set(GROUPS.get(GROUP_OF.get(qid, ""), [qid]))
        if ok:
            ud["wrong"] = [i for i in ud["wrong"] if i not in siblings]
        elif not siblings & set(ud["wrong"]):
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

async def final_backup(app):
    """Последняя копия на Drive после того, как PTB сбросил состояние."""
    global _backup_pending
    _backup_pending = True
    await backup_job()

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx на уровне INFO печатает URL целиком — вместе с токеном бота.
    # В лог это попадать не должно: файл переживает ротацию токена.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    restore_from_drive()  # на чистой машине поднимаем прогресс из Drive

    # update_interval=15 — потолок потерь при жёстком kill; при обычной
    # остановке (Ctrl-C / SIGTERM) PTB сбрасывает состояние на диск сам.
    persistence = PicklePersistence(filepath=STATE_FILE, update_interval=15)
    app = (Application.builder().token(TOKEN)
           .persistence(persistence)
           .post_init(restore_reminders)   # после загрузки bot_data
           .post_shutdown(final_backup).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("quiz", cmd_quiz))
    app.add_handler(CommandHandler("exam", cmd_exam))
    app.add_handler(CommandHandler("mistakes", cmd_mistakes))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("reminder", cmd_reminder))
    app.add_handler(CallbackQueryHandler(on_answer))
    app.job_queue.run_repeating(backup_job, interval=BACKUP_INTERVAL,
                                first=BACKUP_INTERVAL, name="drive-backup")
    print(f"Бот запущен. Уникальных вопросов: {len(GROUPS)} "
          f"({len(QUESTIONS)} карточек). Копия на Drive раз в "
          f"{BACKUP_INTERVAL} c.")
    app.run_polling()

if __name__ == "__main__":
    main()
