# Прогрессоры — ИИ-навигатор по обучению

Telegram-бот и Mini App для построения персонального трека онлайн-обучения. Пользователь называет цель — система диагностирует уровень, строит поэтапный маршрут с реальными материалами и адаптирует его по ходу прохождения.

**Хакатон:** True Tech Arena 2026
**Бот:** [@progressors_bot](https://t.me/progressors_bot)

---

## Как работает

1. **Валидация цели** — ИИ проверяет реалистичность и классифицирует масштаб:
   - *Узкий* (конкретный навык: "аккорд ля минор", "карбонара") — сразу к треку, без лишних вопросов
   - *Широкий* (область знаний: "Python", "UX-дизайн") — полный онбординг
2. **Диагностика уровня** — 4 вопроса по теме, уровень определяется по ответам
3. **Персональный трек** — этапы адаптированы под уровень, время и срок
4. **Реальные материалы** — YouTube (прямые ссылки через API) и Stepik (верифицированные курсы)
5. **Адаптация** — каждый этап можно упростить, усложнить или получить альтернативные материалы
6. **Mini App** — визуальный трек с планетарной картой прогресса

## Команды

| Команда | Описание |
|---|---|
| `/start` | Начать или перезапустить онбординг |
| `/progress` | Прогресс-бар текущего трека |
| `/export` | Экспорт трека текстом |
| `/cancel` | Сбросить текущее состояние |
| `/help` | Справка |

## Стек

| Компонент | Технология |
|---|---|
| Бот | Python 3.13, aiogram 3.13.1 |
| Mini App бэкенд | FastAPI, uvicorn |
| ИИ | Codex API (OpenAI-совместимый), модель gpt-5.5 |
| База данных | Supabase (PostgreSQL) |
| FSM / кэш | Redis |
| Видео | YouTube Data API v3 |
| Курсы | Stepik Public API |
| Деплой | Render (два сервиса) |

## Архитектура

```
Telegram
   │
   ▼
bot.py (aiogram)          ←──── Redis (FSM + кэш)
   │                                    │
   ├── llm_client.py      ←──── Codex API (gpt-5.5)
   ├── youtube.py         ←──── YouTube Data API v3
   ├── stepik.py          ←──── Stepik API
   └── supabase_client.py ←──── Supabase

server.py (FastAPI)
   │
   ├── /api/webapp/track     — загрузка трека
   ├── /api/webapp/progress  — обновление прогресса
   └── /api/webapp/stage-feedback — лайк/дизлайк этапа
   │
   └── static/index.html  — Telegram Mini App (React + Babel)
```

## Запуск локально

### Переменные окружения

Создай файл `.env`:

```env
TELEGRAM_BOT_TOKEN=...
CODEX_API_KEY=...
YOUTUBE_API_KEY=...
SUPABASE_URL=...
SUPABASE_SECRET_KEY=...
REDIS_URL=...
WEBAPP_URL=http://localhost:8000
```

### Установка и запуск

```bash
git clone https://github.com/her1on/progressors.git
cd progressors
pip install -r requirements.txt

# Бот
python bot.py

# Mini App сервер (отдельный терминал)
uvicorn server:app --reload
```

### Docker

```bash
# Бот
docker build -f Dockerfile.bot -t progressors-bot .
docker run --env-file .env progressors-bot

# Mini App
docker build -f Dockerfile.server -t progressors-server .
docker run --env-file .env -p 8000:8000 progressors-server
```

## Структура проекта

```
bot.py              — хендлеры, FSM, логика бота
bot_prompts.py      — все промпты для LLM
llm_client.py       — клиент Codex API, retry, семафор
server.py           — FastAPI бэкенд Mini App
supabase_client.py  — работа с Supabase
youtube.py          — YouTube Data API v3
stepik.py           — Stepik Public API
level.py            — парсинг вопросов диагностики
static/index.html   — Telegram Mini App (React)
Dockerfile.bot      — Docker для бота
Dockerfile.server   — Docker для сервера
```
