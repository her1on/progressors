# CLAUDE.md — Инструкция для ИИ-ассистента

## Критические правила (не нарушать никогда)

- **LLM модель:** `gpt-5.5`, base_url: `https://codex.sale/v1` — не менять без явного разрешения пользователя
- **Промпты** в `bot_prompts.py` — не менять без явного запроса
- **Всегда спрашивать подтверждение** перед любым изменением кода
- **Не добавлять новые зависимости** без явного разрешения
- **Не менять таблицы Supabase** без явного запроса — сначала дать SQL, потом код

---

## Архитектура проекта

### Файлы и их ответственность

| Файл | Ответственность |
|---|---|
| `bot.py` | aiogram хендлеры, FSM, клавиатуры, логика бота |
| `bot_prompts.py` | Все промпты для LLM — только строковые функции |
| `llm_client.py` | HTTP-клиент к Codex API, retry (3 попытки), семафор |
| `server.py` | FastAPI бэкенд Mini App, уведомления в бот |
| `supabase_client.py` | Все операции с Supabase — только через этот модуль |
| `youtube.py` | YouTube Data API v3 — поиск видео |
| `stepik.py` | Stepik Public API — поиск курсов |
| `level.py` | `parse_questions()` — парсит JSON вопросов из LLM ответа |
| `static/index.html` | Telegram Mini App (React + Babel standalone) |

### Как данные текут

```
Пользователь → bot.py (aiogram)
    ├── llm_client.py → Codex API (gpt-5.5)
    ├── youtube.py → YouTube Data API v3
    ├── stepik.py → Stepik Public API
    └── supabase_client.py → Supabase (PostgreSQL)

Mini App (static/index.html) → server.py (FastAPI)
    ├── supabase_client.py → Supabase
    └── _notify_bot() → Telegram Bot API (прямой HTTP)
```

---

## FSM — состояния и поток

```python
class Form(StatesGroup):
    goal          # Ввод цели
    specialization  # Выбор специализации (только для широких целей)
    hours         # Выбор часов в неделю
    months        # Выбор срока
    motivation    # Выбор мотивации (только для широких целей)
    format_pref   # Выбор формата материалов (только для широких целей)
    skills        # Ввод имеющихся навыков (только для широких целей)
    quiz          # 4 вопроса диагностики уровня
    track         # Активный трек — основное состояние
```

**Два флоу онбординга:**
- `goal_scope == "узкий"` → сразу hours → build_track (без мотивации/формата/квиза)
- `goal_scope == "широкий"` → полный флоу со специализацией, мотивацией, форматом, навыками, квизом

---

## Соглашения по коду

### Хендлеры aiogram

```python
# Стандартный паттерн хендлера:
@dp.callback_query(Form.track, F.data.startswith("done_"))
async def stage_done(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    data = await state.get_data()
    # ... логика ...
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
```

- Всегда вызывать `await callback.answer()` в callback хендлерах
- `try/except` вокруг `delete()` — Telegram может вернуть ошибку если сообщение старое
- Supabase обновления — fire-and-forget через `asyncio.create_task(asyncio.to_thread(...))`

### Работа с Supabase

**Только через `supabase_client.py`** — не вызывать REST API напрямую из `bot.py` или `server.py`.

Существующие функции:
- `save_track(user_id, goal, level, hours, months, goal_scope, stages, summary, skills_text)` — PATCH-then-POST upsert
- `update_progress(user_id, completed, current_stage)`
- `update_stages(user_id, stages)`
- `get_track(user_id)` → возвращает последний трек пользователя или `None`
- `save_liked_stages(user_id, goal, stages)` — сохраняет лайкнутые этапы в `liked_stages`
- `get_liked_stages(user_id)` → последние 20 лайкнутых этапов пользователя
- `get_difficulty_bias(user_id)` → `{"hard_count": N, "easy_count": N}` — история упрощений/усложнений

При добавлении новой таблицы: добавить функции в `supabase_client.py`, не писать SQL запросы в других файлах.

### Работа с LLM

```python
# Все вызовы LLM через call_llm — блокирующие, оборачивать в asyncio.to_thread:
result = await asyncio.to_thread(call_llm, some_prompt(args))

# С кастомным таймаутом (для генерации трека используется 90с):
result = await asyncio.to_thread(call_llm, prompt, timeout=90)

# Для фоновых задач:
task = asyncio.create_task(asyncio.to_thread(call_llm, prompt))
```

- Семафор `threading.Semaphore(3)` в `llm_client.py` — не обходить, не менять
- Таймаут по умолчанию: 30с. Для генерации трека: 90с (передаётся явно)
- `max_retries=0` в SDK — ретраи реализованы вручную в `call_llm` (3 попытки)
- `_fetch_track` имеет fallback на `gpt-5.4` при ошибке — это намеренно

### Типизация показателей прогресса

В FSM data и Supabase:
- `completed: list[int]` — индексы пройденных этапов (0-based)
- `current_stage: int` — индекс текущего этапа (0-based)
- `stages: list[dict]` — список этапов, каждый с ключами: `id`, `title`, `weeks`, `topics`, `materials`, `outcome`

---

## Таблицы Supabase

### `user_tracks`

| Колонка | Тип | Описание |
|---|---|---|
| `user_id` | bigint | Telegram user_id |
| `goal` | text | Цель обучения |
| `level` | text | Полный новичок / Базовые знания / Средний уровень / Продвинутый |
| `hours` | int | Часов в неделю |
| `months` | int | Срок в месяцах |
| `goal_scope` | text | узкий / широкий |
| `stages` | jsonb | Массив этапов |
| `summary` | text | Итоговый блок трека |
| `skills_text` | text | Имеющиеся навыки |
| `completed` | jsonb | Список пройденных индексов |
| `current_stage` | int | Текущий индекс |
| `updated_at` | timestamp | Дата обновления |

### `liked_stages`

| Колонка | Тип | Описание |
|---|---|---|
| `user_id` | bigint | Telegram user_id |
| `goal` | text | Цель трека |
| `stage_title` | text | Название этапа |
| `topics` | text | Темы этапа |
| `materials` | text | Материалы этапа |

---

## Среда и деплой

### Production (Render)
- Переменная `RAILWAY_PUBLIC_DOMAIN` задана → webhook режим
- Порт: `PORT` из env (default 8080)
- Два сервиса: бот (`bot.py`) и Mini App (`server.py`)

### Локальная разработка
- `RAILWAY_PUBLIC_DOMAIN` не задана → автоматически polling режим
- `REDIS_URL` — опционально. Без него FSM в памяти (сбрасывается при перезапуске)
- `WEBAPP_URL=http://localhost:8000`

### Переменные окружения

```env
TELEGRAM_BOT_TOKEN=...
CODEX_API_KEY=...          # Codex API (OpenAI-совместимый)
YOUTUBE_API_KEY=...        # YouTube Data API v3
SUPABASE_URL=...
SUPABASE_SECRET_KEY=...
WEBAPP_URL=...             # URL Mini App (для кнопки в боте)
WEBHOOK_SECRET=...         # Секрет для верификации webhook
REDIS_URL=...              # Опционально
RAILWAY_PUBLIC_DOMAIN=...  # Только production
```

---

## Что делать осторожно

- **`format_stage()`** — вызывается для каждого этапа, изменение сломает отображение у всех пользователей
- **`parse_track()`** — парсит ответ LLM регулярками, хрупкий код. Изменять только если LLM стал возвращать другой формат
- **`_resolve_youtube_links()`** — заменяет search URL на прямые видео ссылки. Лимит: max_videos=2 на этап, используется `used_ids` чтобы не повторять видео
- **`kb_stage()`** — клавиатура этапа, используется везде. Изменение меняет UX всего трека
- **`webapp_continue` callback** — точка входа из Mini App в бота. Синхронизирует FSM из Supabase

---

## Стиль кода

- Без лишних комментариев — код должен говорить сам за себя
- Минимум try/except — только там где внешний сервис может упасть (LLM, YouTube, Stepik, Supabase)
- Логирование через `logger.warning()` / `logger.error()` — не `print()`
- f-strings для форматирования строк
- Type hints на всех публичных функциях
