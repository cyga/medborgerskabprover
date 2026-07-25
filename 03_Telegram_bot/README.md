# Telegram-бот для Medborgerskabsprøven

275 карточек из реальных экзаменов 2021–2026 (195 уникальных вопросов)
с русскими подсказками.

## Запуск (5 минут)

1. В Telegram найди **@BotFather** → `/newbot` → придумай имя → получи токен.
2. Сохрани токен в файл `token.txt` рядом с `bot.py` (или в переменную `BOT_TOKEN`).
3. Установи зависимости и запусти:

```bash
pip install "python-telegram-bot[job-queue]>=21.0"
python bot.py
```

4. В Telegram открой своего бота → `/start` → `/reminder` (напоминание в 20:00).

## Команды

`/quiz` — 10 вопросов (вечерняя норма, ~15 мин). Сначала новые, треть — из прошлых ошибок.
`/exam` — пробный экзамен, 25 случайных вопросов, проходной 20/25.
`/mistakes` — только твои ошибки. `/stats` — прогресс и результаты пробных экзаменов.

## INSTALL — автозапуск как systemd-сервис

Так бот поднимается сам при включении компьютера и переживает падения.
Вариант для Linux с systemd; сервис пользовательский, root не нужен.

### 1. Окружение

```bash
cd 03_Telegram_bot
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Активировать venv не нужно ни здесь, ни в сервисе: прямой вызов
`venv/bin/python` даёт тот же интерпретатор и те же пакеты.

Важно: venv **не переживает переименования каталога** — внутри зашиты
абсолютные пути. Переименовал или перенёс проект — пересоздай venv.

### 2. Токен

```bash
printf '%s' 'ТОКЕН_ОТ_BOTFATHER' > token.txt
```

Файл в `.gitignore`. Альтернатива без файла — переменная `BOT_TOKEN`.

### 3. Сервис

```bash
mkdir -p logs
# подставляем актуальный путь к проекту в unit-файл
sed "s|/home/cyga/Documents/Danish/medborgerskabprøver/03_Telegram_bot|$PWD|g" \
    deploy/medborgerskab-bot.service > ~/.config/systemd/user/medborgerskab-bot.service

systemctl --user daemon-reload
systemctl --user enable --now medborgerskab-bot

# чтобы сервис стартовал при загрузке, не дожидаясь входа в систему
loginctl enable-linger "$USER"
```

Проверить, что поднялся: `systemctl --user status medborgerskab-bot` —
должно быть `active (running)`, а в `logs/bot.log` строка «Бот запущен».

### Команды на каждый день

```bash
systemctl --user status medborgerskab-bot     # что происходит
systemctl --user restart medborgerskab-bot    # после правок в bot.py
systemctl --user stop medborgerskab-bot       # перед ручным запуском
journalctl --user -u medborgerskab-bot -f     # системные события сервиса
tail -f logs/bot.log                          # лог самого бота
```

Перед ручным `python -u bot.py` **останавливай сервис**: два экземпляра
дерутся за `getUpdates`, и Telegram отдаёт обоим ошибку `Conflict`.

### Где лежат данные

| Что | Где | Зачем |
|---|---|---|
| Прогресс и незавершённые сессии | `data-quick/` | локальный диск, запись ~4 мс |
| Копия того же | `data/` → Google Drive | бэкап раз в минуту, в фоне |
| Лог | `logs/bot.log` | дописывается сервисом |

`data-quick/`, `data/`, `logs/` и `token.txt` — в `.gitignore`.

Остановка сервиса занимает ~10 секунд: бот успевает сбросить сессии
и отправить финальную копию на Drive. Это нормально, не убивай его `-9`.

## Хостинг где-то ещё

- Облако бесплатно: Oracle Cloud Free Tier (VM навсегда), либо Railway/Render (пробные тарифы).
- При переносе забери с собой `data-quick/` — там весь прогресс.

## Обновление базы вопросов

Бот читает все `*.json` из `../02_Tidligere_proever/`. Новый экзамен (например, зима 2026 появится на danskogproever.dk) — просто добавь файл в том же формате и перезапусти.
