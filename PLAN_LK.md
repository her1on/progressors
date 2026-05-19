# План: Личный кабинет + Supabase

## Контекст
Хакатон System Hack Tomsk 2026. Дедлайн — ~24 часа от 2026-05-19.
Суть идеи: единый личный кабинет для сайта и Telegram-бота, одна база данных Supabase.

## Идея (из переписки)
- Добавить небольшой ЛК на сайт
- Связать сайт с Telegram-ботом
- Один ЛК на два сервиса (сайт + бот)
- Лимит: максимум 3 активных трека одновременно (чтобы не нагружать БД)
- Авторизация через telegram_user_id как единый ключ

## БД: Supabase (PostgreSQL)

### Схема таблиц
```sql
-- Пользователи
users:
  telegram_user_id  bigint PRIMARY KEY
  username          text
  created_at        timestamptz DEFAULT now()

-- Треки
tracks:
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid()
  user_id     bigint REFERENCES users(telegram_user_id)
  goal        text
  level       text
  stages      jsonb       -- весь массив этапов
  created_at  timestamptz DEFAULT now()
  is_active   boolean DEFAULT true
```

## Порядок реализации

1. **Схема в Supabase** (~30 мин)
   - Создать проект на supabase.com
   - Создать таблицы users + tracks
   - Получить URL и anon key → добавить в .env

2. **Бот → Supabase** (~2-3 ч)
   - Установить `supabase` (Python client) в requirements.txt
   - При генерации трека: сохранять в tracks
   - При /start: проверять есть ли активные треки → предлагать продолжить
   - Лимит 3 трека: проверять count перед генерацией

3. **ЛК на сайте** (~3-4 ч)
   - Авторизация: Telegram Login Widget (кнопка, вставляется в HTML)
   - После входа: запрашивать треки пользователя из Supabase по telegram_user_id
   - Страница ЛК: список треков с прогрессом, кнопка продолжить/удалить

4. **Лимит 3 трека** (~30 мин)
   - В боте: перед build_track проверять SELECT count(*) WHERE user_id=X AND is_active=true
   - Если >= 3: предложить удалить старый трек

## Переменные окружения (добавить в .env и Railway)
```
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_KEY=eyJ...  # anon public key
```

## Ветки
- `telegram-bot` — текущая ветка бота (здесь и работаем)
- `main` — веб-приложение
- `combined` — итоговая ветка: бот + сайт + ЛК

## Текущий статус
- [x] Бот написан (bot.py, bot_prompts.py)
- [x] aiogram==3.13.1 задеплоен на Railway (ветка telegram-bot)
- [ ] Supabase: схема не создана
- [ ] Бот: интеграция с Supabase не написана
- [ ] Сайт: ЛК и авторизация не написаны
