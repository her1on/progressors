# Python-Pro: список фиксов

## 1. `stages: list` → `list[dict]`

В `supabase_client.py` три функции принимают `stages: list` без параметра типа.

```python
# Было
def update_stages(user_id: int, stages: list) -> None:
def save_liked_stages(user_id: int, goal: str, stages: list) -> None:
def save_track(..., stages: list, ...) -> None:

# Должно быть
def update_stages(user_id: int, stages: list[dict]) -> None:
def save_liked_stages(user_id: int, goal: str, stages: list[dict]) -> None:
def save_track(..., stages: list[dict], ...) -> None:
```

---

## 2. `BOT_TOKEN` assert перед `Bot()`

`os.getenv()` возвращает `str | None`. Передавать `None` в `Bot(token=...)` — тихая ошибка при старте.

```python
# bot.py ~строка 66
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
assert BOT_TOKEN, "TELEGRAM_BOT_TOKEN not set"
bot = Bot(token=BOT_TOKEN, ...)
```

---

## 3. `_get_client()` → `lru_cache`

Глобальная мутируемая переменная с ручной проверкой `None`.

```python
# llm_client.py — было
_client: OpenAI | None = None

def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(...)
    return _client

# Стало
from functools import lru_cache

@lru_cache(maxsize=1)
def _get_client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("CODEX_API_KEY"),
        base_url="https://codex.sale/v1",
        timeout=30,
        max_retries=0,
    )
```

---

## 4. Непараметризованные return types

```python
# supabase_client.py — было
def get_difficulty_bias(user_id: int) -> dict:
def get_liked_stages(user_id: int) -> list[dict]:
def get_track(user_id: int) -> dict | None:

# Стало
def get_difficulty_bias(user_id: int) -> dict[str, int]:
def get_liked_stages(user_id: int) -> list[dict[str, str]]:
def get_track(user_id: int) -> dict[str, object] | None:
```

---

## 5. FastAPI endpoints без return type (server.py)

```python
# Было
async def webapp_get_track(x_init_data: str = Header(...)):
async def webapp_update_progress(req: ProgressRequest, x_init_data: str = Header(...)):
async def webapp_stage_feedback(req: StageFeedbackRequest, x_init_data: str = Header(...)):
async def root():

# Стало
async def webapp_get_track(x_init_data: str = Header(...)) -> dict:
async def webapp_update_progress(req: ProgressRequest, x_init_data: str = Header(...)) -> dict[str, bool]:
async def webapp_stage_feedback(req: StageFeedbackRequest, x_init_data: str = Header(...)) -> dict[str, bool]:
async def root() -> FileResponse:
```

---

## 6. TypedDict для Stage (перспективный рефакторинг)

Этапы сейчас — `dict` без схемы. При росте кодовой базы стоит ввести TypedDict.

```python
from typing import TypedDict

class Stage(TypedDict):
    id: int
    title: str
    weeks: int
    topics: str
    materials: str
    outcome: str
    modified: str  # "simplified" | "advanced" | "alternative" | ""
    liked: bool
    disliked: bool
    stepik_params: dict | None
    stepik_courses: list | None
```

Требует обновления всех мест где создаётся/читается этап — делать отдельным PR.

---

## 7. `requests` → `httpx` (требует новой зависимости)

`stepik.py`, `youtube.py`, `server.py` используют синхронный `requests`, обёрнутый в `asyncio.to_thread`.
Замена на `httpx` с async-клиентом уберёт использование thread pool для HTTP.
Обсуждать отдельно — это значимый рефакторинг.
