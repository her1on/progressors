import asyncio
import json
import logging
import os
import re
import urllib.parse

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

load_dotenv()

from bot_prompts import (
    advance_prompt,
    alternative_prompt,
    questions_prompt,
    simplify_prompt,
    specialize_prompt,
    track_prompt,
    validate_prompt,
)
from llm_client import call_llm
from level import parse_questions
from stepik import search_stepik_courses
from youtube import search_youtube_video

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
_redis_url = os.getenv("REDIS_URL", "")
storage = RedisStorage.from_url(_redis_url) if _redis_url else MemoryStorage()
dp = Dispatcher(storage=storage)

LEVELS = {
    "Полный новичок": "Начинаем с нуля — объясняю всё по шагам.",
    "Базовые знания": "База есть — закрепим основы и перейдём к практике.",
    "Средний уровень": "Есть опыт — фокус на реальных проектах и углублении.",
    "Продвинутый": "Высокий уровень — работаем над экспертизой.",
}

QUIZ_OPTIONS = [
    ("A", "Никогда не пробовал / не слышал"),
    ("B", "Знаком в теории, но не практиковал"),
    ("C", "Практиковал, есть реальный опыт"),
    ("D", "Занимаюсь на продвинутом уровне"),
]

# Хранилище фоновых задач (user_id → asyncio.Task)
_questions_tasks: dict[int, asyncio.Task] = {}
_track_tasks: dict[int, asyncio.Task] = {}


async def _typing_loop(chat_id: int, stop: asyncio.Event) -> None:
    """Показывает индикатор «печатает...» каждые 4 сек пока не выставлен stop."""
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        await asyncio.sleep(4)


async def _fetch_questions(goal: str) -> list[dict]:
    try:
        raw = await asyncio.to_thread(call_llm, questions_prompt(goal))
        questions = parse_questions(raw)
        if questions:
            return questions
        logger.warning(f"parse_questions returned empty for goal='{goal}', raw={raw[:200]!r}")
    except Exception as e:
        logger.error(f"_fetch_questions failed for goal='{goal}': {e}")
    return [
        {"question": "Насколько ты знаком с этой темой в теории?"},
        {"question": "Как часто ты практикуешь навыки по этой теме?"},
        {"question": "В какой мере ты применял эти знания на практике?"},
        {"question": "Насколько ты знаком с продвинутыми аспектами этой темы?"},
    ]


async def _fetch_specializations(goal: str) -> list[str]:
    try:
        raw = await asyncio.to_thread(call_llm, specialize_prompt(goal))
        raw = re.sub(r"```[a-zA-Z]*\n?", "", raw).replace("```", "").strip()
        data = json.loads(raw)
        return [item["option"] for item in data if "option" in item][:3]
    except Exception as e:
        logger.warning(f"_fetch_specializations failed for goal={goal!r}: {e}")
        return []


async def _validate_goal(goal: str) -> tuple[str, str, str]:
    """Возвращает (status, explanation, scope). status: ok | abstract | institutional | unrealistic. scope: узкий | широкий."""
    raw = await asyncio.to_thread(call_llm, validate_prompt(goal))
    lines = raw.strip().splitlines()
    first = lines[0].strip().upper()
    if "РЕАЛИСТИЧНО" in first:
        scope = lines[1].strip().lower() if len(lines) > 1 else "широкий"
        scope = "узкий" if "узкий" in scope else "широкий"
        return "ok", "", scope
    explanation = lines[1].strip() if len(lines) > 1 else ""
    if "НЕРЕАЛИСТИЧНО" in first:
        return "unrealistic", explanation or "Цель физически невозможна.", "широкий"
    if "АБСТРАКТНО" in first:
        return "abstract", explanation or "Уточни цель — укажи конкретный навык.", "широкий"
    if "ИНСТИТУЦИОНАЛЬНЫЙ" in first:
        return "institutional", explanation, "широкий"
    return "ok", "", "широкий"


async def _fetch_track(prompt: str) -> tuple[list[dict], str]:
    last_exc: Exception | None = None
    for model in ("gpt-5.5", "gpt-5.4"):
        try:
            track_text = await asyncio.to_thread(call_llm, prompt, model)
            logger.info(f"Track raw response [{model}] (first 300): {track_text[:300]}")
            stages, summary = parse_track(track_text)
            if not stages:
                logger.error(f"parse_track empty [{model}]. Full response:\n{track_text}")
                raise ValueError("no stages")
            return stages, summary
        except Exception as e:
            logger.warning(f"_fetch_track failed with {model}: {e}")
            last_exc = e
    raise last_exc


# ── FSM States ────────────────────────────────────────────────────────────────

class Form(StatesGroup):
    goal = State()
    specialization = State()
    hours = State()
    months = State()
    motivation = State()
    format_pref = State()
    quiz = State()
    track = State()


# ── Keyboards ─────────────────────────────────────────────────────────────────

def kb_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Новый маршрут"), KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
        persistent=True,
    )


def kb_hours():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="2 ч/нед", callback_data="h_2"),
        InlineKeyboardButton(text="5 ч/нед", callback_data="h_5"),
        InlineKeyboardButton(text="10 ч/нед", callback_data="h_10"),
        InlineKeyboardButton(text="20+ ч/нед", callback_data="h_20"),
    ]])


def kb_months():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="1 мес", callback_data="m_1"),
        InlineKeyboardButton(text="3 мес", callback_data="m_3"),
        InlineKeyboardButton(text="6 мес", callback_data="m_6"),
        InlineKeyboardButton(text="12 мес", callback_data="m_12"),
    ]])



def kb_quiz():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{k}  {v}", callback_data=f"q_{k}")]
        for k, v in QUIZ_OPTIONS
    ])


def kb_motivation():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Новая профессия", callback_data="mot_career")],
        [InlineKeyboardButton(text="Текущая работа", callback_data="mot_work")],
        [InlineKeyboardButton(text="Личное обучение", callback_data="mot_personal")],
    ])


def kb_format():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Видео", callback_data="fmt_video"),
            InlineKeyboardButton(text="Статьи", callback_data="fmt_articles"),
        ],
        [
            InlineKeyboardButton(text="Курсы", callback_data="fmt_courses"),
            InlineKeyboardButton(text="Любой формат", callback_data="fmt_any"),
        ],
    ])


def kb_build():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Построить трек", callback_data="build_track"),
    ]])


def kb_stage(idx: int, is_last: bool):
    rows = [
        [InlineKeyboardButton(text="✅ Пройдено", callback_data=f"done_{idx}")],
        [
            InlineKeyboardButton(text="😕 Сложно", callback_data=f"hard_{idx}"),
            InlineKeyboardButton(text="😊 Просто", callback_data=f"easy_{idx}"),
        ],
        [InlineKeyboardButton(text="👎 Не подошло", callback_data=f"bad_{idx}")],
    ]
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"prev_{idx}"))
    if not is_last:
        nav.append(InlineKeyboardButton(text="➡️ Следующий этап", callback_data=f"next_{idx}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_restart():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Начать новый маршрут", callback_data="restart"),
    ]])


def kb_specialization(options: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=opt, callback_data=f"spec_{i}")] for i, opt in enumerate(options)]
    rows.append([InlineKeyboardButton(text="Общее направление", callback_data="spec_none")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_time_warning() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Всё равно продолжить", callback_data="time_ok")],
        [InlineKeyboardButton(text="✏️ Изменить время", callback_data="time_change")],
    ])


# ── Helpers ───────────────────────────────────────────────────────────────────

def escape_md(text: str) -> str:
    for ch in r"*_`[()":
        text = text.replace(ch, f"\\{ch}")
    return text


_SOURCE_SEARCH = {
    "youtube":  "https://www.youtube.com/results?search_query={}",
    "stepik":   "https://stepik.org/search?query={}",
    "habr":     "https://habr.com/ru/search/?q={}&target_type=posts",
    "rutube":   "https://rutube.ru/search/?query={}",
    "vk":       "https://vk.com/video?q={}",
    "vk видео": "https://vk.com/video?q={}",
}


def linkify_materials(text: str, stage_topic: str = "") -> str:
    """Превращает [YouTube] Название — Канал в кликабельную ссылку на поиск."""
    habr_query = " ".join(stage_topic.split()[:3]) if stage_topic else ""

    filtered = []
    for line in text.split("\n"):
        m = re.search(r"\[([^\]\n]+)\]", line)
        if not m or m.group(1).strip().lower() not in _SOURCE_SEARCH:
            continue
        filtered.append(line)
    text = "\n".join(filtered)

    def replace(m: re.Match) -> str:
        source = m.group(1).strip()
        rest = m.group(2).strip()
        title = rest.split(" — ")[0].strip()
        title = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
        title = re.sub(r"[\[\]\*\"\'\\]+", "", title).strip()
        base = _SOURCE_SEARCH.get(source.lower())
        if not base or not title:
            return m.group(0)
        if source.lower() == "habr":
            query = habr_query or " ".join(title.split()[:3])
        else:
            query = title
        url = base.format(urllib.parse.quote_plus(query))
        return f"[{source}: {title}]({url})"

    return re.sub(r"\[([^\]\n]+)\]\s+([^\n\[]+)", replace, text)


def calc_level(answers: list[str]) -> str:
    score = sum("ABCD".index(a) for a in answers)
    if score <= 3:
        return "Полный новичок"
    elif score <= 6:
        return "Базовые знания"
    elif score <= 9:
        return "Средний уровень"
    return "Продвинутый"


def parse_track(text: str) -> tuple[list[dict], str]:
    text = re.sub(r"```[a-zA-Z]*\n?", "", text).replace("```", "").strip()
    stages = []
    summary = ""

    # Разбиваем по любому заголовку ## уровня (включая ###)
    parts = re.split(r"\n(?=#{2,3}\s)", "\n" + text)

    for part in parts:
        first_line = part.split("\n")[0]

        # Захватываем итоговый раздел
        if re.search(r"Итог|Результат трека|Карьер", first_line, re.IGNORECASE):
            summary = "\n".join(part.split("\n")[1:]).strip()
            continue
        # Блок должен выглядеть как этап: содержит цифру или слова Этап/Шаг/Step
        if not re.search(r"(?:Этап|Шаг|Step|\d+)", first_line, re.IGNORECASE):
            continue
        # Пропускаем если нет содержимого (только заголовок)
        if len(part.strip().split("\n")) < 2:
            continue

        idx = len(stages)

        # Извлекаем название: всё после номера и двоеточия/точки
        title_m = re.search(r"(?:Этап|Шаг|Step)?\s*\d+[.:)—\s]+(.+)", first_line, re.IGNORECASE)
        if title_m:
            title = title_m.group(1).strip().lstrip("📍").strip()
        else:
            # Заголовок без явного номера — берём всё после ##
            title = re.sub(r"^#{2,3}\s*", "", first_line).strip()
        title = title or f"Этап {idx + 1}"

        weeks_m = re.search(r"\*{0,2}Длительность:?\*{0,2}\s*(\d+)", part)
        weeks = int(weeks_m.group(1)) if weeks_m else 2

        topics_m = re.search(
            r"\*{0,2}Что изучать:?\*{0,2}(.+?)(?=\n\*{0,2}Материал|\n\*{0,2}Результат|\Z)",
            part, re.DOTALL | re.IGNORECASE,
        )
        topics = topics_m.group(1).strip() if topics_m else ""

        materials_m = re.search(
            r"\n\*{0,2}Материал[ыь]:?\*{0,2}(.+?)(?=\n\*{0,2}Результат|\Z)",
            part, re.DOTALL | re.IGNORECASE,
        )
        materials = materials_m.group(1).strip() if materials_m else ""

        outcome_m = re.search(
            r"\n\*{0,2}Результат:?\*{0,2}\s*(.+?)(?=\n#{2,3}|\Z)",
            part, re.DOTALL | re.IGNORECASE,
        )
        outcome = outcome_m.group(1).strip().replace("\n", " ") if outcome_m else ""

        # Если ничего не распарсилось — берём весь текст блока как topics
        if not topics and not materials:
            body = "\n".join(part.split("\n")[1:]).strip()
            topics = body[:800]

        stages.append({
            "id": idx + 1,
            "title": title,
            "weeks": weeks,
            "topics": topics,
            "materials": materials,
            "outcome": outcome,
        })

    return stages, summary


def _remove_stepik_lines(text: str) -> str:
    lines = [line for line in text.split("\n") if not re.search(r"\[Stepik\]", line, re.IGNORECASE)]
    return "\n".join(lines).strip()


def _split_llm_response(text: str) -> tuple[str, str]:
    """Split free-text LLM response into (topics, materials) by detecting source bracket lines."""
    topic_lines, material_lines = [], []
    for line in text.split("\n"):
        if re.match(r"^\s*[-*•]?\s*\[(YouTube|Stepik|Habr|Rutube|VK)", line, re.IGNORECASE):
            material_lines.append(line.strip())
        else:
            topic_lines.append(line)
    return "\n".join(topic_lines).strip(), "\n".join(material_lines).strip()


def _limit_source_lines(text: str, source: str, max_count: int) -> str:
    count = 0
    result = []
    for line in text.split("\n"):
        if re.search(rf"\[{source}\]", line, re.IGNORECASE):
            count += 1
            if count > max_count:
                continue
        result.append(line)
    return "\n".join(result).strip()


def format_stage(stage: dict, idx: int, total: int) -> str:
    badge = {
        "simplified": "[ Этап упрощён под твой уровень ]\n\n",
        "advanced":   "[ Этап усложнён под твой уровень ]\n\n",
        "alternative": "[ Альтернативные материалы ]\n\n",
    }.get(stage.get("modified", ""), "")
    text = (
        f"{badge}"
        f"*Этап {idx + 1} из {total}: {stage['title']}*\n"
        f"{stage['weeks']} нед\n\n"
    )
    if stage.get("topics"):
        text += f"*Что изучать:*\n{stage['topics']}\n\n"
    if stage.get("materials"):
        materials = _remove_stepik_lines(stage["materials"])
        materials = _limit_source_lines(materials, "YouTube", 2)
        materials = _limit_source_lines(materials, "GitHub", 0)
        if materials:
            text += f"*Материалы:*\n{linkify_materials(materials, stage['title'])}\n\n"
    if stage.get("outcome"):
        text += f"*Результат:* _{stage['outcome']}_"
    return text[:4000]


# ── Handlers ──────────────────────────────────────────────────────────────────

def _cancel_user_tasks(user_id: int) -> None:
    for store in (_questions_tasks, _track_tasks):
        task = store.pop(user_id, None)
        if task and not task.done():
            task.cancel()


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    _cancel_user_tasks(message.from_user.id)
    await state.clear()
    await message.answer(
        "🪐 *Прогрессоры* — твой ИИ-навигатор по обучению\n\n"
        "Я помогу построить персональный трек обучения — с бесплатными материалами "
        "на русском языке, подобранными под твой уровень и цели.\n\n"
        "Скажи мне — *чему хочешь научиться?*\n"
        "_Например: Python, UX-дизайн, маркетинг, сварка, английский язык_",
        reply_markup=kb_menu(),
    )
    await state.set_state(Form.goal)


@dp.message(Command("help"))
@dp.message(F.text == "❓ Помощь")
async def cmd_help(message: Message):
    await message.answer(
        "*Прогрессоры* — ИИ-навигатор по обучению\n\n"
        "*Как это работает:*\n"
        "1. Скажи чему хочешь научиться\n"
        "2. Укажи время и срок, выбери мотивацию и формат\n"
        "3. Пройди короткую диагностику уровня\n"
        "4. Получи персональный трек с бесплатными материалами\n\n"
        "*Команды:*\n"
        "/start — начать или перезапустить\n"
        "/progress — прогресс по текущему треку\n"
        "/cancel — отменить текущий процесс\n"
        "/help — эта справка\n\n"
        "*На каждом этапе трека можно:*\n"
        "✅ Отметить как пройденное\n"
        "😕 Слишком сложно — упростить материал\n"
        "😊 Слишком просто — усложнить материал\n"
        "👎 Не подошло — получить альтернативные ресурсы\n"
        "➡️ Пропустить и перейти дальше",
        reply_markup=kb_menu(),
    )


@dp.message(F.text.in_({"🚀 Новый маршрут", "Новый маршрут"}))
async def menu_new_route(message: Message, state: FSMContext):
    await cmd_start(message, state)


@dp.message(Command("progress"))
async def cmd_progress(message: Message, state: FSMContext):
    data = await state.get_data()
    stages = data.get("stages")
    if not stages:
        await message.answer("У тебя пока нет активного трека. Начни с /start")
        return

    goal = data.get("goal", "")
    level = data.get("level", "")
    completed = data.get("completed", [])
    total = len(stages)
    done = len(completed)
    current = min(data.get("current_stage", 0), total - 1)

    bar = " ".join(
        "✅" if i in completed else ("▶️" if i == current else "⬜")
        for i in range(total)
    )

    await message.answer(
        f"*Твой прогресс*\n\n"
        f"*Цель:* {escape_md(goal)}\n"
        f"*Уровень:* {level}\n\n"
        f"{bar}\n"
        f"Пройдено: {done} из {total} этапов\n\n"
        f"Сейчас: Этап {current + 1} — _{escape_md(stages[current]['title'])}_"
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Нечего отменять. Введи /start чтобы начать.")
        return
    _cancel_user_tasks(message.from_user.id)
    await state.clear()
    await message.answer(
        "❌ Сброшено. Введи /start чтобы начать заново.",
    )


@dp.message(Form.goal)
async def got_goal(message: Message, state: FSMContext):
    goal = message.text.strip()
    if not goal or not any(c.isalpha() for c in goal):
        await message.answer(
            "Пожалуйста, напиши конкретную цель — например, _Python_, _дизайн_ или _английский язык_."
        )
        return

    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(message.chat.id, stop))
    try:
        status, explanation, scope = await _validate_goal(goal)
    except Exception as e:
        stop.set()
        typing_task.cancel()
        logger.error(f"[VALIDATE ERROR] goal={goal!r} error={e!r}")
        await message.answer("⚠️ Сервис временно недоступен. Попробуй ещё раз через несколько секунд.")
        return
    finally:
        stop.set()
        typing_task.cancel()

    if status == "unrealistic":
        await message.answer(f"❌ {explanation}\n\nПопробуй сформулировать цель иначе.")
        return

    if status == "abstract":
        await message.answer(
            f"Цель слишком размытая.\n\n{explanation}"
        )
        return

    if status == "institutional":
        await message.answer(
            f"*Это институциональная профессия*\n\n"
            f"{explanation}\n\n"
            f"Я не смогу помочь попасть туда напрямую, но могу составить трек по смежным навыкам. "
            f"Напиши конкретный навык — например, _физика_, _аэродинамика_, _лётная подготовка_.",
        )
        return

    await state.update_data(goal=goal, goal_scope=scope)

    if scope == "широкий":
        stop = asyncio.Event()
        typing_task = asyncio.create_task(_typing_loop(message.chat.id, stop))
        try:
            spec_options = await _fetch_specializations(goal)
        finally:
            stop.set()
            typing_task.cancel()

        if spec_options:
            await state.update_data(spec_options=spec_options)
            await message.answer(
                f"Отлично! Цель: *{escape_md(goal)}*\n\nУточни направление:",
                reply_markup=kb_specialization(spec_options),
            )
            await state.set_state(Form.specialization)
            return

    # Узкая цель или не удалось получить специализации — сразу к часам
    questions_task = asyncio.create_task(_fetch_questions(goal))
    _questions_tasks[message.from_user.id] = questions_task

    await message.answer(
        f"Отлично! Цель: *{escape_md(goal)}*\n\nСколько часов в неделю готов уделять учёбе?",
        reply_markup=kb_hours(),
    )
    await state.set_state(Form.hours)


@dp.callback_query(Form.specialization, F.data.startswith("spec_"))
async def got_specialization(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    data = await state.get_data()
    goal = data["goal"]

    if callback.data != "spec_none":
        idx = int(callback.data.split("_")[1])
        options = data.get("spec_options", [])
        if idx < len(options):
            goal = f"{goal} — {options[idx]}"
            await state.update_data(goal=goal)

    questions_task = asyncio.create_task(_fetch_questions(goal))
    _questions_tasks[callback.from_user.id] = questions_task

    await callback.message.answer(
        f"Цель: *{escape_md(goal)}*\n\nСколько часов в неделю готов уделять учёбе?",
        reply_markup=kb_hours(),
    )
    await state.set_state(Form.hours)


@dp.callback_query(Form.hours, F.data.startswith("h_"))
async def got_hours(callback: CallbackQuery, state: FSMContext):
    hours = int(callback.data.split("_")[1])
    await state.update_data(hours=hours)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        f"*{hours} ч/нед* — принято. За сколько месяцев хочешь достичь цели?",
        reply_markup=kb_months(),
    )
    await state.set_state(Form.months)


MOTIVATION_LABELS = {
    "mot_career":   "Новая профессия",
    "mot_work":     "Текущая работа",
    "mot_personal": "Личное обучение",
}

FORMAT_LABELS = {
    "fmt_video":    "Видео",
    "fmt_articles": "Статьи",
    "fmt_courses":  "Курсы",
    "fmt_any":      "Любой формат",
}


async def _proceed_after_months(callback: CallbackQuery, state: FSMContext, data: dict) -> None:
    if data.get("goal_scope") == "узкий":
        await state.update_data(motivation="Личное обучение", format_pref="Любой формат")
        user_id = callback.from_user.id
        task = _questions_tasks.pop(user_id, None)
        if task:
            task.cancel()
        level = "Полный новичок"
        qa_text = ""
        await state.update_data(level=level, qa_text=qa_text)
        weeks = data["months"] * 4
        prompt = track_prompt(
            goal=data["goal"],
            level=level,
            hours=data["hours"],
            months=data["months"],
            weeks=weeks,
            qa_text=qa_text,
            motivation="Личное обучение",
            format_pref="Любой формат",
            scope="узкий",
        )
        track_task = asyncio.create_task(_fetch_track(prompt))
        _track_tasks[user_id] = track_task
        await callback.message.answer(
            f"*Цель:* {escape_md(data['goal'])}\n"
            f"*Время:* {data['hours']} ч/нед · {data['months']} мес",
            reply_markup=kb_build(),
        )
        await state.set_state(Form.track)
    else:
        await callback.message.answer(
            "*Что движет тобой?* Это поможет подобрать материалы точнее.",
            reply_markup=kb_motivation(),
        )
        await state.set_state(Form.motivation)


@dp.callback_query(Form.months, F.data.startswith("m_"))
async def got_months(callback: CallbackQuery, state: FSMContext):
    months = int(callback.data.split("_")[1])
    await state.update_data(months=months)
    data = await state.get_data()
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    hours = data["hours"]
    weeks = months * 4
    if data.get("goal_scope") != "узкий" and hours * weeks < 20:
        await callback.message.answer(
            f"⚠️ *{hours} ч/нед × {months} мес = {hours * weeks} ч* — это немного для твоей цели.\n\n"
            f"Рекомендуем минимум 20 ч суммарно для ощутимого прогресса. Продолжить или пересмотреть?",
            reply_markup=kb_time_warning(),
        )
        return

    await _proceed_after_months(callback, state, data)


@dp.callback_query(Form.months, F.data == "time_ok")
async def time_warning_ok(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _proceed_after_months(callback, state, data)


@dp.callback_query(Form.months, F.data == "time_change")
async def time_warning_change(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "Сколько часов в неделю готов уделять учёбе?",
        reply_markup=kb_hours(),
    )
    await state.set_state(Form.hours)


@dp.callback_query(Form.motivation, F.data.startswith("mot_"))
async def got_motivation(callback: CallbackQuery, state: FSMContext):
    motivation = MOTIVATION_LABELS[callback.data]
    await state.update_data(motivation=motivation)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "*Какой формат материалов предпочитаешь?*",
        reply_markup=kb_format(),
    )
    await state.set_state(Form.format_pref)


async def _resolve_questions_and_start_quiz(callback: CallbackQuery, state: FSMContext):
    """Получает вопросы квиза (из фонового таска или свежим запросом) и запускает квиз."""
    data = await state.get_data()
    user_id = callback.from_user.id
    task = _questions_tasks.pop(user_id, None)

    if task and task.done() and not task.exception():
        questions = task.result()
    else:
        if task:
            task.cancel()
        stop = asyncio.Event()
        typing_task = asyncio.create_task(_typing_loop(callback.message.chat.id, stop))
        try:
            questions = await _fetch_questions(data["goal"])
        finally:
            stop.set()
            typing_task.cancel()

    await state.update_data(questions=questions, current_q=0, answers=[])
    await _send_question(callback.message, state)
    await state.set_state(Form.quiz)


@dp.callback_query(Form.format_pref, F.data.startswith("fmt_"))
async def got_format(callback: CallbackQuery, state: FSMContext):
    format_pref = FORMAT_LABELS[callback.data]
    await state.update_data(format_pref=format_pref)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _resolve_questions_and_start_quiz(callback, state)


async def _send_question(message: Message, state: FSMContext):
    data = await state.get_data()
    idx = data["current_q"]
    questions = data["questions"]
    q_text = questions[idx].get("question", questions[idx].get("q", f"Вопрос {idx + 1}"))
    try:
        await message.answer(
            f"*Вопрос {idx + 1} из {len(questions)}*\n\n{q_text}",
            reply_markup=kb_quiz(),
        )
    except Exception:
        await message.answer(
            f"Вопрос {idx + 1} из {len(questions)}\n\n{q_text}",
            parse_mode=None,
            reply_markup=kb_quiz(),
        )


@dp.callback_query(Form.quiz, F.data.startswith("q_"))
async def got_answer(callback: CallbackQuery, state: FSMContext):
    letter = callback.data.split("_")[1]
    data = await state.get_data()
    answers = data.get("answers", []) + [letter]
    current_q = data.get("current_q", 0) + 1
    questions = data.get("questions", [])

    await state.update_data(answers=answers, current_q=current_q)
    await callback.answer(f"Ответ {letter} принят ✓")
    try:
        await callback.message.delete()
    except Exception:
        pass

    if current_q < len(questions):
        await _send_question(callback.message, state)
        return

    level = calc_level(answers)
    level_desc = LEVELS.get(level, "")

    qa_lines = []
    for i, (q, a) in enumerate(zip(questions, answers)):
        q_text = q.get("question", q.get("q", ""))
        option_text = dict(QUIZ_OPTIONS).get(a, a)
        qa_lines.append(f"В{i + 1}: {q_text}\nОтвет {a}: {option_text}")
    qa_text = "\n\n".join(qa_lines)

    await state.update_data(level=level, qa_text=qa_text)

    # Запускаем генерацию трека фоново, пока пользователь видит сообщение об уровне
    weeks = data["months"] * 4
    prompt = track_prompt(
        goal=data["goal"],
        level=level,
        hours=data["hours"],
        months=data["months"],
        weeks=weeks,
        qa_text=qa_text,
        motivation=data.get("motivation", ""),
        format_pref=data.get("format_pref", ""),
        scope=data.get("goal_scope", "широкий"),
    )
    track_task = asyncio.create_task(_fetch_track(prompt))
    _track_tasks[callback.from_user.id] = track_task

    motivation = data.get("motivation", "")
    format_pref = data.get("format_pref", "")
    await callback.message.answer(
        f"*Твой профиль*\n\n"
        f"*Цель:* {escape_md(data['goal'])}\n"
        f"*Уровень:* {level}\n"
        f"_{level_desc}_\n\n"
        f"*Мотивация:* {motivation}\n"
        f"*Формат:* {format_pref}\n"
        f"*Время:* {data['hours']} ч/нед · {data['months']} мес",
        reply_markup=kb_build(),
    )


@dp.callback_query(F.data == "build_track")
async def build_track(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    task = _track_tasks.pop(user_id, None)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    loading_msg = await callback.message.answer("Строю персональный трек — это займёт около минуты...")

    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(callback.message.chat.id, stop))
    try:
        if task and task.done():
            if task.exception():
                raise task.exception()
            stages, summary = task.result()
        else:
            if not task:
                data = await state.get_data()
                weeks = data["months"] * 4
                prompt = track_prompt(
                    goal=data["goal"],
                    level=data["level"],
                    hours=data["hours"],
                    months=data["months"],
                    weeks=weeks,
                    qa_text=data["qa_text"],
                    motivation=data.get("motivation", ""),
                    format_pref=data.get("format_pref", ""),
                    scope=data.get("goal_scope", "широкий"),
                )
                task = asyncio.create_task(_fetch_track(prompt))
            stages, summary = await task
    except Exception as e:
        stop.set()
        typing_task.cancel()
        logger.error(f"Track generation failed: {e}")
        try:
            await loading_msg.delete()
        except Exception:
            pass
        await callback.message.answer(
            "❌ Не удалось сгенерировать трек — сервис временно недоступен.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="build_track"),
            ]]),
        )
        return
    finally:
        stop.set()
        typing_task.cancel()

    try:
        await loading_msg.delete()
    except Exception:
        pass

    await state.update_data(stages=stages, summary=summary, current_stage=0, completed=[])
    await state.set_state(Form.track)
    if summary:
        final_data = await state.get_data()
        summary_label = (
            "Что ты умеешь после трека:"
            if final_data.get("goal_scope") == "узкий"
            else "Карьерные перспективы после трека:"
        )
        await callback.message.answer(f"*{summary_label}*\n\n{summary}")
    await _send_stage(callback.message, state, 0)


async def _resolve_youtube_links(text: str, used_ids: set[str], max_videos: int = 2) -> str:
    """Replace YouTube search URLs with direct video URLs via API, skipping already-used video IDs."""
    pattern = re.compile(
        r"\[YouTube: ([^\]]+)\]\((https://www\.youtube\.com/results\?search_query=[^)]+)\)"
    )
    resolved = 0
    for title, search_url in pattern.findall(text):
        if resolved >= max_videos:
            break
        try:
            result = await asyncio.to_thread(search_youtube_video, title, used_ids)
            if result:
                video_url, video_id = result
                used_ids.add(video_id)
                text = text.replace(search_url, video_url)
                resolved += 1
        except Exception:
            pass
    return text


async def _resolve_stepik_links(text: str) -> str:
    """Заменяет поисковые ссылки Stepik на прямые URL через API."""
    stepik_pattern = re.compile(r"\[Stepik: ([^\]]+)\]\((https://stepik\.org/search\?query=[^)]+)\)")
    for title, search_url in stepik_pattern.findall(text):
        try:
            courses = await asyncio.to_thread(search_stepik_courses, title, 0, 1)
            if courses:
                direct_url = courses[0]["url"]
                text = text.replace(search_url, direct_url)
        except Exception:
            pass
    return text


async def _send_stage(message: Message, state: FSMContext, idx: int, edit: bool = False):
    data = await state.get_data()
    stages = data.get("stages", [])

    if idx >= len(stages):
        await message.answer(
            "*Маршрут пройден! Поздравляю!*\n\n"
            f"Ты прошёл весь трек по теме *{escape_md(data.get('goal', ''))}*.\n\n"
            "Это большой шаг — продолжай в том же духе! "
            "Хочешь закрепить результат или освоить что-то новое?",
            reply_markup=kb_restart(),
        )
        await message.answer("Используй кнопки ниже или введи новую цель:", reply_markup=kb_menu())
        return

    stage = stages[idx]
    is_last = idx == len(stages) - 1
    used_ids: set[str] = set(data.get("used_video_ids", []))
    text = format_stage(stage, idx, len(stages))
    text = await _resolve_youtube_links(text, used_ids)
    await state.update_data(used_video_ids=list(used_ids))
    text = await _resolve_stepik_links(text)

    format_pref = data.get("format_pref", "")
    if format_pref in ("Курсы", "Любой формат", ""):
        try:
            courses = await asyncio.to_thread(search_stepik_courses, data.get("goal", stage["title"]), 0, 3)
            logger.info(f"Stepik search for {data.get('goal')!r}: found {len(courses)} courses")
            if courses:
                lines = ["\n*Курсы на Stepik по этому этапу:*\n"]
                for c in courses:
                    lines.append(f"• [{c['title']}]({c['url']})")
                text = text + "\n".join(lines)
        except Exception as e:
            logger.warning(f"Stepik search failed: {e}")

    # Удаляем предыдущее сообщение этапа если он был изменён
    if stage.get("modified"):
        old_msg_id = data.get("stage_message_ids", {}).get(str(idx))
        if old_msg_id:
            try:
                await bot.delete_message(chat_id=message.chat.id, message_id=old_msg_id)
            except Exception:
                pass

    no_preview = LinkPreviewOptions(is_disabled=True)
    if edit:
        try:
            sent = await message.edit_text(text[:4000], reply_markup=kb_stage(idx, is_last), link_preview_options=no_preview)
        except Exception:
            sent = await message.answer(text[:4000], reply_markup=kb_stage(idx, is_last), link_preview_options=no_preview)
    else:
        try:
            sent = await message.answer(text[:4000], reply_markup=kb_stage(idx, is_last), link_preview_options=no_preview)
        except Exception as e:
            logger.warning(f"Markdown send failed for stage {idx}, retrying as plain text: {e}")
            sent = await message.answer(
                re.sub(r"[*_`\[\]]", "", text[:4000]),
                parse_mode=None,
                reply_markup=kb_stage(idx, is_last),
                link_preview_options=no_preview,
            )

    # Сохраняем message_id для возможного удаления при модификации
    msg_ids = data.get("stage_message_ids", {})
    msg_ids[str(idx)] = sent.message_id
    await state.update_data(stage_message_ids=msg_ids)


@dp.callback_query(Form.track, F.data.startswith("done_"))
async def stage_done(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    data = await state.get_data()
    completed = data.get("completed", [])
    if idx not in completed:
        completed.append(idx)
    await state.update_data(completed=completed, current_stage=idx + 1)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("✅ Отмечено как пройденное!")
    await _send_stage(callback.message, state, idx + 1)


@dp.callback_query(Form.track, F.data.startswith("next_"))
async def stage_next(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    await state.update_data(current_stage=idx + 1)
    await callback.answer()
    await _send_stage(callback.message, state, idx + 1, edit=True)


@dp.callback_query(Form.track, F.data.startswith("prev_"))
async def stage_prev(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    prev_idx = idx - 1
    await state.update_data(current_stage=prev_idx)
    await callback.answer()
    await _send_stage(callback.message, state, prev_idx, edit=True)


@dp.callback_query(Form.track, F.data.startswith("hard_"))
async def stage_hard(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    data = await state.get_data()
    stages = data.get("stages", [])
    stage = stages[idx]

    await callback.answer("Адаптирую под твой уровень...")
    msg = await callback.message.answer("Делаю этот этап проще...")

    prompt = simplify_prompt(data["goal"], data["level"], stage["title"], stage["topics"])
    try:
        result = await asyncio.to_thread(call_llm, prompt)
    except Exception:
        await msg.edit_text("❌ Не удалось адаптировать. Попробуй ещё раз.")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    topics, materials = _split_llm_response(result.strip())
    stages[idx]["topics"] = topics
    stages[idx]["materials"] = materials
    stages[idx]["modified"] = "simplified"
    await state.update_data(stages=stages)
    await msg.delete()
    try:
        await _send_stage(callback.message, state, idx)
    except Exception as e:
        logger.error(f"stage_hard _send_stage failed: {e}")
        await callback.message.answer("❌ Не удалось обновить этап. Попробуй ещё раз.")


@dp.callback_query(Form.track, F.data.startswith("easy_"))
async def stage_easy(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    data = await state.get_data()
    stages = data.get("stages", [])
    stage = stages[idx]

    await callback.answer("Усложняю материал...")
    msg = await callback.message.answer("Подбираю более продвинутые материалы...")

    prompt = advance_prompt(data["goal"], data["level"], stage["title"], stage["topics"])
    try:
        result = await asyncio.to_thread(call_llm, prompt)
    except Exception:
        await msg.edit_text("❌ Не удалось усложнить. Попробуй ещё раз.")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    topics, materials = _split_llm_response(result.strip())
    stages[idx]["topics"] = topics
    stages[idx]["materials"] = materials
    stages[idx]["modified"] = "advanced"
    await state.update_data(stages=stages)
    await msg.delete()
    try:
        await _send_stage(callback.message, state, idx)
    except Exception as e:
        logger.error(f"stage_easy _send_stage failed: {e}")
        await callback.message.answer("❌ Не удалось обновить этап. Попробуй ещё раз.")


@dp.callback_query(Form.track, F.data.startswith("bad_"))
async def stage_bad(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    data = await state.get_data()
    stages = data.get("stages", [])
    stage = stages[idx]

    await callback.answer("Подбираю альтернативные материалы...")
    msg = await callback.message.answer("Ищу другие материалы...")

    prompt = alternative_prompt(data["goal"], data["level"], stage["title"], stage["topics"])
    try:
        result = await asyncio.to_thread(call_llm, prompt)
    except Exception:
        await msg.edit_text("❌ Не удалось подобрать альтернативу. Попробуй ещё раз.")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    stages[idx]["materials"] = result.strip()
    stages[idx]["modified"] = "alternative"
    await state.update_data(stages=stages)
    await msg.delete()
    try:
        await _send_stage(callback.message, state, idx)
    except Exception as e:
        logger.error(f"stage_bad _send_stage failed: {e}")
        await callback.message.answer("❌ Не удалось обновить этап. Попробуй ещё раз.")


@dp.callback_query(F.data == "restart")
async def restart(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await cmd_start(callback.message, state)


# ── Entry point (webhook) ─────────────────────────────────────────────────────

WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
if not WEBHOOK_SECRET:
    logger.warning("WEBHOOK_SECRET not set, using insecure default")
    WEBHOOK_SECRET = "progressors-secret-2026"


async def on_startup(bot: Bot) -> None:
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    if not domain:
        logger.error("RAILWAY_PUBLIC_DOMAIN not set — webhook will not work!")
        return
    webhook_url = f"https://{domain}{WEBHOOK_PATH}"
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
    logger.info(f"Webhook registered: {webhook_url}")
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать / перезапустить"),
        BotCommand(command="progress", description="Мой прогресс по треку"),
        BotCommand(command="cancel", description="Отменить текущий процесс"),
        BotCommand(command="help", description="Справка"),
    ])


async def on_shutdown(bot: Bot) -> None:
    logger.info("Bot shutting down")


if __name__ == "__main__":
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    port = int(os.getenv("PORT", 8080))
    logger.info(f"Starting webhook server on port {port}")
    web.run_app(app, host="0.0.0.0", port=port)
