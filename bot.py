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
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
    MenuButtonDefault,
    MenuButtonWebApp,
    WebAppInfo,
)
from dotenv import load_dotenv

load_dotenv()

from bot_prompts import (
    advance_prompt,
    alternative_prompt,
    questions_prompt,
    simplify_prompt,
    specialize_prompt,
    stepik_query_prompt,
    track_prompt,
    validate_prompt,
)
from llm_client import call_llm
from level import parse_questions
from stepik import search_stepik_courses
from youtube import search_youtube_video
from supabase_client import (
    save_track as _sb_save_track,
    update_progress as _sb_update_progress,
    update_stages as _sb_update_stages,
    get_track as _sb_get_track,
    save_liked_stage as _sb_save_liked_stage,
    get_liked_stages as _sb_get_liked_stages,
    update_difficulty_bias as _sb_update_difficulty_bias,
    get_difficulty_bias as _sb_get_difficulty_bias,
)

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
_search_term_tasks: dict[int, asyncio.Task] = {}


async def _typing_loop(chat_id: int, stop: asyncio.Event) -> None:
    """Показывает индикатор «печатает...» каждые 4 сек пока не выставлен stop."""
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        await asyncio.sleep(4)


async def _fetch_questions(goal: str, skills_text: str = "") -> list[dict]:
    try:
        raw = await asyncio.to_thread(call_llm, questions_prompt(goal, skills_text))
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
    if "ЗАПРЕЩЕНО" in first:
        return "forbidden", explanation or "Эта тема не поддерживается.", "широкий"
    if "НЕРЕАЛИСТИЧНО" in first:
        return "unrealistic", explanation or "Цель физически невозможна.", "широкий"
    if "АБСТРАКТНО" in first:
        return "abstract", explanation or "Уточни цель — укажи конкретный навык.", "широкий"
    if "ИНСТИТУЦИОНАЛЬНЫЙ" in first:
        return "institutional", explanation, "широкий"
    return "ok", "", "широкий"


async def _assess_timeframe(goal: str, hours: int, months: int) -> tuple[str, str]:
    """Оценивает адекватность временного плана. Возвращает (verdict, explanation).
    verdict: АДЕКВАТНО | МАЛО | МНОГО"""
    total = hours * months * 4
    prompt = (
        f"Оцени адекватность временного плана для учебной цели.\n"
        f"Ответь строго: первая строка — одно слово (АДЕКВАТНО, МАЛО или МНОГО), "
        f"вторая строка — одно предложение объяснения.\n\n"
        f"Цель: {goal}\n"
        f"Часов в неделю: {hours}\n"
        f"Срок: {months} мес\n"
        f"Итого часов: {total}"
    )
    try:
        raw = await asyncio.to_thread(call_llm, prompt)
        lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
        verdict = lines[0].upper() if lines else "АДЕКВАТНО"
        explanation = lines[1] if len(lines) > 1 else ""
        if verdict not in ("АДЕКВАТНО", "МАЛО", "МНОГО"):
            return "АДЕКВАТНО", ""
        return verdict, explanation
    except Exception:
        return "АДЕКВАТНО", ""


async def _recommend_schedule(goal: str, level: str = "") -> tuple[int, int]:
    """Возвращает (hours, months) — рекомендованный план на основе цели и уровня."""
    level_line = f"Уровень пользователя: {level}\n" if level else ""
    prompt = (
        f"Порекомендуй оптимальное количество часов в неделю и срок обучения для цели.\n"
        f"Ответь строго в формате (только два числа):\nЧАСЫ: X\nМЕСЯЦЫ: X\n\n"
        f"Цель: {goal}\n{level_line}"
    )
    try:
        raw = await asyncio.to_thread(call_llm, prompt)
        lines = raw.strip().splitlines()
        hours = int(re.search(r"\d+", lines[0]).group())
        months = int(re.search(r"\d+", lines[1]).group())
        return max(1, min(hours, 40)), max(1, min(months, 24))
    except Exception:
        return 10, 3


async def _extract_search_terms(goal: str) -> str:
    """Извлекает 2-3 ключевых слова из цели для поиска курсов на Stepik."""
    prompt = (
        "Извлеки 2-3 главных ключевых слова из учебной цели для поиска курсов. "
        "Отвечай только словами через пробел, без знаков препинания, без пояснений.\n"
        f"Цель: {goal}"
    )
    try:
        raw = await asyncio.to_thread(call_llm, prompt)
        return raw.strip().splitlines()[0].strip()
    except Exception:
        _STOP = {"стать", "научиться", "хочу", "как", "освоить", "изучить", "понять", "узнать", "начать", "учиться", "получить"}
        words = [w for w in goal.split("—")[0].split() if w.lower() not in _STOP]
        return " ".join(words) if words else goal.split("—")[0].strip()


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
    skills = State()
    quiz = State()
    track = State()


# ── Keyboards ─────────────────────────────────────────────────────────────────

def kb_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚀 Новый маршрут"), KeyboardButton(text="❓ Помощь")],
            [KeyboardButton(text="📊 Прогресс"), KeyboardButton(text="📤 Экспорт")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
        persistent=True,
    )


def kb_hours():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="2 ч/нед", callback_data="h_2"),
            InlineKeyboardButton(text="5 ч/нед", callback_data="h_5"),
            InlineKeyboardButton(text="10 ч/нед", callback_data="h_10"),
            InlineKeyboardButton(text="20+ ч/нед", callback_data="h_20"),
        ],
        [
            InlineKeyboardButton(text="✏️ Ввести своё", callback_data="h_custom"),
            InlineKeyboardButton(text="Рекомендуемый план", callback_data="h_recommend"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_hours")],
    ])


def kb_months():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 мес", callback_data="m_1"),
            InlineKeyboardButton(text="3 мес", callback_data="m_3"),
            InlineKeyboardButton(text="6 мес", callback_data="m_6"),
            InlineKeyboardButton(text="12 мес", callback_data="m_12"),
        ],
        [
            InlineKeyboardButton(text="✏️ Ввести своё", callback_data="m_custom"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_months")],
    ])



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
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_motivation")],
    ])


def kb_format():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Видео", callback_data="fmt_video"),
            InlineKeyboardButton(text="Курсы", callback_data="fmt_courses"),
        ],
        [InlineKeyboardButton(text="Любой формат", callback_data="fmt_any")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_format")],
    ])


def kb_build():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Построить трек", callback_data="build_track"),
    ]])


WEBAPP_URL = os.getenv("WEBAPP_URL", "https://progressors-production.up.railway.app")


def kb_stage(idx: int, is_last: bool, feedback: str | None = None, is_done: bool = False):
    rows = [
        [InlineKeyboardButton(
            text="✓ Этап пройден" if is_done else "✅ Пройдено",
            callback_data="noop" if is_done else f"done_{idx}",
        )],
    ]
    if feedback is None:
        rows.append([
            InlineKeyboardButton(text="❤️", callback_data=f"like_{idx}"),
            InlineKeyboardButton(text="👎", callback_data=f"dislike_{idx}"),
        ])
    elif feedback == "liked":
        rows.append([InlineKeyboardButton(text="❤️ Понравилось", callback_data="noop")])
    else:
        rows.append([InlineKeyboardButton(text="👎 Оценено", callback_data="noop")])
    rows.append([InlineKeyboardButton(text="📱 Открыть трек", web_app=WebAppInfo(url=WEBAPP_URL))])
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"prev_{idx}"))
    if not is_last:
        nav.append(InlineKeyboardButton(text="➡️ Следующий этап", callback_data=f"next_{idx}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_dislike(idx: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😕 Слишком сложно", callback_data=f"hard_{idx}")],
        [InlineKeyboardButton(text="😊 Слишком просто", callback_data=f"easy_{idx}")],
        [InlineKeyboardButton(text="↩️ Отмена", callback_data=f"cancel_feedback_{idx}")],
    ])


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
    "youtube": "https://www.youtube.com/results?search_query={}",
    "stepik":  "https://stepik.org/search?query={}",
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
        title = re.sub(r"[\[\]\*\"\'\\\_]+", "", title).strip()
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
        outcome = re.sub(r"[-–—]{2,}", "", outcome_m.group(1).strip().replace("\n", " ")).strip() if outcome_m else ""

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


def _stage_feedback(stage: dict) -> str | None:
    if stage.get("liked"):
        return "liked"
    if stage.get("modified") or stage.get("disliked"):
        return "disliked"
    return None


def format_stage(stage: dict, idx: int, total: int) -> str:
    badge = {
        "simplified": "_Этап упрощён под твой уровень_\n\n",
        "advanced":   "_Этап усложнён под твой уровень_\n\n",
        "alternative": "_Альтернативные материалы_\n\n",
    }.get(stage.get("modified", ""), "")
    text = (
        f"{badge}"
        f"*Этап {idx + 1} из {total}: {stage['title']}*\n"
        f"{stage['weeks']} нед\n\n"
    )
    if stage.get("topics"):
        safe_topics = stage['topics'].replace('[', '(').replace(']', ')')
        text += f"*Что изучать:*\n{safe_topics}\n\n"
    if stage.get("materials"):
        materials = _remove_stepik_lines(stage["materials"])
        materials = _limit_source_lines(materials, "YouTube", 2)
        materials = _limit_source_lines(materials, "GitHub", 0)
        if materials:
            text += f"*Материалы:*\n{linkify_materials(materials, stage['title'])}\n\n"
    if stage.get("outcome"):
        text += f"*Результат:* _{stage['outcome']}_\n"
    text += "\n_Изучи материалы и оцени этап 👇_"
    return text[:4000]


# ── Handlers ──────────────────────────────────────────────────────────────────

def _cancel_user_tasks(user_id: int) -> None:
    for store in (_questions_tasks, _track_tasks, _search_term_tasks):
        task = store.pop(user_id, None)
        if task and not task.done():
            task.cancel()


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    _cancel_user_tasks(message.from_user.id)
    await state.clear()

    track = await asyncio.to_thread(_sb_get_track, message.from_user.id)
    if track and track.get("stages"):
        goal = track.get("goal", "")
        current = track.get("current_stage") or 0
        total = len(track["stages"])
        completed = len(track.get("completed") or [])
        await message.answer(
            f"👋 С возвращением!\n\n"
            f"У тебя есть незавершённый трек: *{escape_md(goal)}*\n"
            f"Прогресс: {completed} из {total} этапов пройдено\n\n"
            f"Продолжить с этапа {current + 1}?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Продолжить трек", callback_data="resume_track")],
                [InlineKeyboardButton(text="🚀 Начать новый маршрут", callback_data="start_new")],
            ]),
        )
        return

    await _send_welcome(message, state)


async def _send_welcome(message: Message, state: FSMContext):
    await message.answer(
        "🪐 *Прогрессоры* — твой ИИ-навигатор по обучению\n\n"
        "Я помогу построить персональный трек обучения — с бесплатными материалами "
        "на русском языке, подобранными под твой уровень и цели.\n\n"
        "Скажи мне — *чему хочешь научиться?*\n"
        "_Например: Python, UX-дизайн, маркетинг, сварка, английский язык_",
        reply_markup=kb_menu(),
    )
    await state.set_state(Form.goal)


@dp.callback_query(F.data == "resume_track")
async def resume_track(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    track = await asyncio.to_thread(_sb_get_track, callback.from_user.id)
    if not track or not track.get("stages"):
        await _send_welcome(callback.message, state)
        return
    current_stage = track.get("current_stage") or 0
    await state.update_data(
        stages=track["stages"],
        completed=track.get("completed") or [],
        current_stage=current_stage,
        goal=track.get("goal") or "",
        level=track.get("level") or "",
        hours=track.get("hours") or 0,
        months=track.get("months") or 0,
        goal_scope=track.get("goal_scope") or "широкий",
        skills_text=track.get("skills_text") or "",
    )
    await state.set_state(Form.track)
    await _send_stage(callback.message, state, current_stage)


@dp.callback_query(F.data == "start_new")
async def start_new(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _send_welcome(callback.message, state)


@dp.message(Command("help"))
@dp.message(F.text == "❓ Помощь")
async def cmd_help(message: Message):
    await message.answer(
        "*Прогрессоры* — ИИ-навигатор по обучению\n\n"
        "*Как это работает:*\n"
        "1. Скажи чему хочешь научиться\n"
        "2. Укажи время, срок, мотивацию и формат материалов\n"
        "3. Опиши навыки, которыми уже обладаешь (или пропусти)\n"
        "4. Пройди короткую диагностику уровня\n"
        "5. Получи персональный трек с бесплатными материалами\n\n"
        "*Команды:*\n"
        "/start — начать или перезапустить\n"
        "/progress — прогресс по текущему треку\n"
        "/export — экспорт трека текстом\n"
        "/cancel — отменить текущий процесс\n"
        "/help — эта справка\n\n"
        "*На каждом этапе трека можно:*\n"
        "✅ Отметить как пройденное\n"
        "❤️ Сохранить этап как понравившийся\n"
        "👎 Дать обратную связь — упростить или усложнить материал\n"
        "➡️ Пропустить и перейти дальше",
        reply_markup=kb_menu(),
    )


@dp.message(F.text.in_({"🚀 Новый маршрут", "Новый маршрут"}))
async def menu_new_route(message: Message, state: FSMContext):
    _cancel_user_tasks(message.from_user.id)
    await state.clear()
    await _send_welcome(message, state)


@dp.message(F.text == "📊 Прогресс")
async def menu_progress(message: Message, state: FSMContext):
    await cmd_progress(message, state)


@dp.message(F.text == "📤 Экспорт")
async def menu_export(message: Message, state: FSMContext):
    await cmd_export(message, state)


@dp.message(F.text == "❌ Отмена")
async def menu_cancel(message: Message, state: FSMContext):
    await cmd_cancel(message, state)


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


@dp.message(Command("export"))
async def cmd_export(message: Message, state: FSMContext):
    data = await state.get_data()
    stages = data.get("stages")
    if not stages:
        await message.answer("У тебя пока нет активного трека. Начни с /start")
        return

    goal = data.get("goal", "")
    level = data.get("level", "")
    hours = data.get("hours", 0)
    months = data.get("months", 0)
    completed = data.get("completed", [])
    total = len(stages)
    done = len(completed)
    pct = round(done / total * 100) if total else 0
    current = min(data.get("current_stage", 0), total - 1)

    lines = [
        f"📚 *Твой трек: {escape_md(goal)}*\n",
        f"⭐ Уровень: {escape_md(level)}",
        f"⏱ {hours} ч/нед · {months} мес\n",
        "━━━━━━━━━━━━━━━",
    ]

    for i, s in enumerate(stages):
        status = "✅ Пройдено" if i in completed else ("▶️ Текущий" if i == current else "⬜ Впереди")
        lines.append(f"\n*Этап {i + 1} · {escape_md(s['title'])}* ({s.get('weeks', '?')} нед) — {status}")
        for topic in (s.get("topics") or "").split("\n"):
            t = topic.strip().lstrip("-•*· ")
            if len(t) > 4:
                lines.append(f"  • {escape_md(t)}")
        if s.get("outcome"):
            lines.append(f"  _→ {escape_md(s['outcome'])}_")

    lines += [
        "\n━━━━━━━━━━━━━━━",
        f"Прогресс: {done} из {total} этапов ({pct}%)",
    ]

    text = "\n".join(lines)
    try:
        await message.answer(text[:4096], parse_mode="Markdown")
    except Exception:
        plain = text.replace("*", "").replace("_", "").replace("\\", "")
        await message.answer(plain[:4096])


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

    if status == "forbidden":
        await message.answer(f"🚫 {explanation}\n\nПопробуй другую цель.")
        return

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

    if scope == "узкий":
        await message.answer(
            f"Отлично! Цель: *{escape_md(goal)}*\n\nСколько часов в неделю готов уделять учёбе?",
            reply_markup=kb_hours(),
        )
        await state.set_state(Form.hours)
    else:
        await message.answer(
            f"Отлично! Цель: *{escape_md(goal)}*\n\n*Что движет тобой?* Это поможет подобрать материалы точнее.",
            reply_markup=kb_motivation(),
        )
        await state.set_state(Form.motivation)


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

    await callback.message.answer(
        f"Цель: *{escape_md(goal)}*\n\n*Что движет тобой?* Это поможет подобрать материалы точнее.",
        reply_markup=kb_motivation(),
    )
    await state.set_state(Form.motivation)


# ── Кнопки «Назад» ────────────────────────────────────────────────────────────

@dp.callback_query(Form.months, F.data == "back_months")
async def back_from_months(callback: CallbackQuery, state: FSMContext):
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


@dp.callback_query(Form.hours, F.data == "back_hours")
async def back_from_hours(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    data = await state.get_data()
    if data.get("goal_scope") == "узкий":
        await callback.message.answer("Что хочешь освоить? Можешь написать любую цель 👇")
        await state.set_state(Form.goal)
    else:
        await callback.message.answer(
            "*Какой формат материалов предпочитаешь?*",
            reply_markup=kb_format(),
        )
        await state.set_state(Form.format_pref)


@dp.callback_query(Form.format_pref, F.data == "back_format")
async def back_from_format(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "*Что движет тобой?* Это поможет подобрать материалы точнее.",
        reply_markup=kb_motivation(),
    )
    await state.set_state(Form.motivation)


@dp.callback_query(Form.motivation, F.data == "back_motivation")
async def back_from_motivation(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    data = await state.get_data()
    spec_options = data.get("spec_options", [])
    if spec_options:
        base_goal = data["goal"].split(" — ")[0]
        await state.update_data(goal=base_goal)
        await callback.message.answer(
            f"Цель: *{escape_md(base_goal)}*\n\nУточни направление:",
            reply_markup=kb_specialization(spec_options),
        )
        await state.set_state(Form.specialization)
    else:
        await callback.message.answer("Что хочешь освоить? Можешь написать любую цель 👇")
        await state.set_state(Form.goal)


@dp.callback_query(Form.hours, F.data.in_({"h_2", "h_5", "h_10", "h_20"}))
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


@dp.callback_query(Form.hours, F.data == "h_custom")
async def hours_custom(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("Введи количество часов в неделю (например: 7):")


@dp.message(Form.hours)
async def got_hours_text(message: Message, state: FSMContext):
    try:
        hours = int(message.text.strip())
        if hours <= 0 or hours > 168:
            raise ValueError
    except ValueError:
        await message.answer("Введи число от 1 до 168:")
        return
    await state.update_data(hours=hours)
    await message.answer(
        f"*{hours} ч/нед* — принято. За сколько месяцев хочешь достичь цели?",
        reply_markup=kb_months(),
    )
    await state.set_state(Form.months)


@dp.callback_query(Form.hours, F.data == "h_recommend")
async def hours_recommend(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    data = await state.get_data()
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(callback.message.chat.id, stop))
    try:
        hours, months = await _recommend_schedule(data["goal"], level=data.get("level", ""))
    finally:
        stop.set()
        typing_task.cancel()
    await state.update_data(hours=hours, months=months)
    data = await state.get_data()
    await callback.message.answer(
        f"*Рекомендуемый план:* {hours} ч/нед · {months} мес\n"
        f"_На основе твоей цели и типичного темпа обучения._",
    )
    await _proceed_after_months(callback.message, callback.from_user.id, state, data)


@dp.callback_query(Form.months, F.data == "m_custom")
async def months_custom(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("Введи количество месяцев (например: 4):")


@dp.message(Form.months)
async def got_months_text(message: Message, state: FSMContext):
    try:
        months = int(message.text.strip())
        if months <= 0 or months > 24:
            raise ValueError
    except ValueError:
        await message.answer("Введи число от 1 до 24:")
        return
    await state.update_data(months=months)
    data = await state.get_data()
    hours = data["hours"]
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(message.chat.id, stop))
    try:
        verdict, explanation = await _assess_timeframe(data["goal"], hours, months)
    finally:
        stop.set()
        typing_task.cancel()
    if verdict != "АДЕКВАТНО":
        await message.answer(
            f"⚠️ {explanation}\n\nПродолжить с *{hours} ч/нед × {months} мес*?",
            reply_markup=kb_time_warning(),
        )
        return
    await _proceed_after_months(message, message.from_user.id, state, data)


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


async def _proceed_after_months(message: Message, user_id: int, state: FSMContext, data: dict) -> None:
    build_track_now = data.get("goal_scope") == "узкий" or data.get("quiz_done")

    if build_track_now:
        if data.get("goal_scope") == "узкий":
            await state.update_data(motivation="Личное обучение", format_pref="Любой формат")
            task = _questions_tasks.pop(user_id, None)
            if task:
                task.cancel()
            level = "Полный новичок"
            qa_text = ""
            await state.update_data(level=level, qa_text=qa_text)
        else:
            level = data.get("level", "")
            qa_text = data.get("qa_text", "")

        data = await state.get_data()
        weeks = data["months"] * 4
        try:
            liked_stages, difficulty_bias = await asyncio.gather(
                asyncio.to_thread(_sb_get_liked_stages, user_id),
                asyncio.to_thread(_sb_get_difficulty_bias, user_id),
            )
        except Exception:
            liked_stages, difficulty_bias = [], {"hard_count": 0, "easy_count": 0}
        logger.info(f"[MEMORY] user={user_id} liked={len(liked_stages)} stages, bias={difficulty_bias}")
        for s in liked_stages:
            logger.info(f"[MEMORY] liked stage: goal={s.get('goal')!r} title={s.get('stage_title')!r}")
        prompt = track_prompt(
            goal=data["goal"],
            level=level,
            hours=data["hours"],
            months=data["months"],
            weeks=weeks,
            qa_text=qa_text,
            motivation=data.get("motivation", "Личное обучение"),
            format_pref=data.get("format_pref", "Любой формат"),
            scope=data.get("goal_scope", "широкий"),
            skills_text=data.get("skills_text", ""),
            liked_stages=liked_stages,
            difficulty_bias=difficulty_bias,
        )
        track_task = asyncio.create_task(_fetch_track(prompt))
        _track_tasks[user_id] = track_task
        _search_term_tasks[user_id] = asyncio.create_task(_extract_search_terms(data["goal"]))

        if data.get("goal_scope") == "узкий":
            profile_text = (
                f"*Цель:* {escape_md(data['goal'])}\n"
                f"*Время:* {data['hours']} ч/нед · {data['months']} мес"
            )
        else:
            skills_line = f"\n*Навыки:* {escape_md(data['skills_text'])}" if data.get("skills_text") else ""
            profile_text = (
                f"*Твой профиль*\n\n"
                f"*Цель:* {escape_md(data['goal'])}\n"
                f"*Уровень:* {level}{skills_line}\n\n"
                f"*Мотивация:* {data.get('motivation', '')}\n"
                f"*Формат:* {data.get('format_pref', '')}\n"
                f"*Время:* {data['hours']} ч/нед · {data['months']} мес"
            )
        await message.answer(profile_text, reply_markup=kb_build())
        await state.set_state(Form.track)
    else:
        await message.answer(
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
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(callback.message.chat.id, stop))
    try:
        verdict, explanation = await _assess_timeframe(data["goal"], hours, months)
    finally:
        stop.set()
        typing_task.cancel()

    if verdict != "АДЕКВАТНО":
        await callback.message.answer(
            f"⚠️ {explanation}\n\nПродолжить с *{hours} ч/нед × {months} мес*?",
            reply_markup=kb_time_warning(),
        )
        return

    await _proceed_after_months(callback.message, callback.from_user.id, state, data)


@dp.callback_query(F.data == "time_ok")
async def time_warning_ok(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await state.set_state(Form.months)
    await _proceed_after_months(callback.message, callback.from_user.id, state, data)


@dp.callback_query(F.data == "time_change")
async def time_warning_change(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "За сколько месяцев хочешь достичь цели?",
        reply_markup=kb_months(),
    )
    await state.set_state(Form.months)


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



@dp.callback_query(Form.format_pref, F.data.startswith("fmt_"))
async def got_format(callback: CallbackQuery, state: FSMContext):
    format_pref = FORMAT_LABELS[callback.data]
    await state.update_data(format_pref=format_pref)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "*Какими навыками по этой теме ты уже обладаешь?*\n"
        "_Напиши свободно — это поможет не повторять то, что ты уже знаешь, и точнее настроить квиз._",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="skip_skills")],
        ]),
    )
    await state.set_state(Form.skills)


@dp.message(Form.skills)
async def got_skills(message: Message, state: FSMContext):
    skills_text = message.text.strip()
    await state.update_data(skills_text=skills_text)
    await _start_quiz_with_skills(message, state, skills_text)


@dp.callback_query(Form.skills, F.data == "skip_skills")
async def skip_skills(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await state.update_data(skills_text="")
    await _start_quiz_with_skills(callback.message, state, "")


async def _start_quiz_with_skills(message: Message, state: FSMContext, skills_text: str):
    data = await state.get_data()
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(message.chat.id, stop))
    try:
        questions = await _fetch_questions(data["goal"], skills_text)
    finally:
        stop.set()
        typing_task.cancel()
    await state.update_data(questions=questions, current_q=0, answers=[])
    await _send_question(message, state)
    await state.set_state(Form.quiz)


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

    await state.update_data(level=level, qa_text=qa_text, quiz_done=True)

    await callback.message.answer(
        f"*Уровень определён:* {level}\n_{level_desc}_\n\n"
        f"Последний шаг — укажи, сколько времени готов уделять учёбе.",
        reply_markup=kb_hours(),
    )
    await state.set_state(Form.hours)


@dp.callback_query(F.data == "build_track")
async def build_track(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    task = _track_tasks.pop(user_id, None)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    loading_msg = await callback.message.answer("Составляю твой трек, немного подожди ⏳")

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
                try:
                    _liked, _bias = await asyncio.gather(
                        asyncio.to_thread(_sb_get_liked_stages, user_id),
                        asyncio.to_thread(_sb_get_difficulty_bias, user_id),
                    )
                except Exception:
                    _liked, _bias = [], {"hard_count": 0, "easy_count": 0}
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
                    skills_text=data.get("skills_text", ""),
                    liked_stages=_liked,
                    difficulty_bias=_bias,
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

    search_task = _search_term_tasks.pop(user_id, None)
    final_data = await state.get_data()
    search_terms = final_data.get("goal", "").split("—")[0].strip()
    try:
        liked_stages, difficulty_bias = await asyncio.gather(
            asyncio.to_thread(_sb_get_liked_stages, user_id),
            asyncio.to_thread(_sb_get_difficulty_bias, user_id),
        )
    except Exception:
        liked_stages, difficulty_bias = [], {"hard_count": 0, "easy_count": 0}
    await state.update_data(liked_stages=liked_stages, difficulty_bias=difficulty_bias)
    if search_task:
        if search_task.done() and not search_task.exception():
            search_terms = search_task.result()
        else:
            try:
                search_terms = await asyncio.wait_for(asyncio.shield(search_task), timeout=10)
            except Exception as e:
                logger.warning(f"search_terms extraction timed out: {e}")

    await state.update_data(stages=stages, summary=summary, current_stage=0, completed=[], search_terms=search_terms)
    await state.set_state(Form.track)

    try:
        d = await state.get_data()
        await asyncio.to_thread(
            _sb_save_track,
            callback.from_user.id,
            d.get("goal", ""),
            d.get("level", ""),
            d.get("hours", 0),
            d.get("months", 0),
            d.get("goal_scope", "широкий"),
            stages,
            summary,
            d.get("skills_text", ""),
        )
    except Exception as e:
        logger.warning(f"Supabase save_track failed: {e}")
    if summary:
        final_data = await state.get_data()
        summary_label = (
            "Что ты умеешь после трека:"
            if final_data.get("goal_scope") == "узкий"
            else "Карьерные перспективы после трека:"
        )
        await callback.message.answer(f"*{summary_label}*\n\n{summary}")
    await _send_stage(callback.message, state, 0)


async def _resolve_youtube_links(text: str, used_ids: set[str], max_videos: int = 2, context: str = "") -> str:
    """Replace YouTube search URLs with direct video URLs via API, skipping already-used video IDs."""
    pattern = re.compile(
        r"\[YouTube: ([^\]]+)\]\((https://www\.youtube\.com/results\?search_query=[^)]+)\)"
    )
    # Берём первое слово контекста (например "Python") чтобы уточнить поиск
    context_word = context.split()[0] if context else ""
    resolved = 0
    for title, search_url in pattern.findall(text):
        if resolved >= max_videos:
            break
        # Добавляем тему если она не упомянута в названии видео
        query = title if (not context_word or context_word.lower() in title.lower()) else f"{title} {context_word}"
        try:
            result = await asyncio.to_thread(search_youtube_video, query, used_ids)
            if result:
                video_url, video_id = result
                used_ids.add(video_id)
                text = text.replace(search_url, video_url)
                resolved += 1
            else:
                logger.warning(f"YouTube API returned None for {query!r}")
        except Exception as e:
            logger.warning(f"YouTube resolve failed for {query!r}: {e}")
    return text


async def _get_stepik_params(stage_title: str, topics: str) -> dict:
    """Возвращает {"subject_id": int|None, "query": str} для поиска в Stepik через LLM."""
    try:
        raw = await asyncio.to_thread(call_llm, stepik_query_prompt(stage_title, topics))
        raw = re.sub(r"```[a-z]*\n?", "", raw).replace("```", "").strip()
        parsed = json.loads(raw)
        subject_id = int(parsed.get("subject_id") or 0) or None
        query = str(parsed.get("query") or stage_title).strip() or stage_title
        return {"subject_id": subject_id, "query": query}
    except Exception as e:
        logger.warning(f"_get_stepik_params failed: {e!r}")
        return {"subject_id": None, "query": stage_title}


async def _resolve_stepik_links(text: str, difficulty: str | None = None) -> str:
    """Заменяет поисковые ссылки Stepik на прямые URL через API."""
    stepik_pattern = re.compile(r"\[Stepik: ([^\]]+)\]\((https://stepik\.org/search\?query=[^)]+)\)")
    for title, search_url in stepik_pattern.findall(text):
        try:
            courses = await asyncio.to_thread(search_stepik_courses, title, 0, 1, difficulty)
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
    stage_difficulty = {"simplified": "easy", "advanced": "hard"}.get(stage.get("modified", ""))

    text = format_stage(stage, idx, len(stages))
    text = await _resolve_youtube_links(text, used_ids, context=data.get("goal", ""))
    await state.update_data(used_video_ids=list(used_ids))
    text = await _resolve_stepik_links(text, stage_difficulty)

    format_pref = data.get("format_pref", "")
    if format_pref in ("Курсы", "Любой формат", ""):
        # Получаем LLM-параметры (кэшируем в stage)
        sp = stage.get("stepik_params")
        if not sp:
            sp = await _get_stepik_params(stage["title"], stage.get("topics", ""))
            stage["stepik_params"] = sp
            stages[idx] = stage
            await state.update_data(stages=stages)

        # Fallback: пробуем 3 → 2 → 1 слово из LLM-запроса
        words = sp["query"].split()
        seen: set[str] = set()
        queries = []
        for n in (3, 2, 1):
            q = " ".join(words[:n])
            if q and q not in seen:
                seen.add(q)
                queries.append(q)

        courses = []
        for query in queries:
            try:
                found = await asyncio.to_thread(
                    search_stepik_courses, query, 0, 3,
                    subject=sp.get("subject_id"),
                    difficulty=stage_difficulty,
                    filter_terms=sp["query"],
                )
                logger.info(f"Stepik {query!r} subject={sp.get('subject_id')} diff={stage_difficulty}: {len(found)}")
                if found:
                    courses = found
                    break
            except Exception as e:
                logger.warning(f"Stepik search failed for {query!r}: {e}")

        if not courses and sp.get("subject_id"):
            for query in queries:
                try:
                    found = await asyncio.to_thread(
                        search_stepik_courses, query, 0, 3,
                        subject=None,
                        difficulty=stage_difficulty,
                        filter_terms=sp["query"],
                    )
                    logger.info(f"Stepik {query!r} subject=None (no-subj fallback) diff={stage_difficulty}: {len(found)}")
                    if found:
                        courses = found
                        break
                except Exception as e:
                    logger.warning(f"Stepik no-subj search failed for {query!r}: {e}")

        if courses:
            lines = ["\n*Курсы на Stepik:*\n"]
            for c in courses:
                lines.append(f"• [{c['title']}]({c['url']})")
            text = text + "\n".join(lines)
        else:
            text += "\n\n🔍 *Степик:* курсы по этой теме не найдены"

    # Удаляем предыдущее сообщение этапа если он был изменён
    if stage.get("modified"):
        old_msg_id = data.get("stage_message_ids", {}).get(str(idx))
        if old_msg_id:
            try:
                await bot.delete_message(chat_id=message.chat.id, message_id=old_msg_id)
            except Exception:
                pass

    feedback = _stage_feedback(stage)
    is_done = idx in data.get("completed", [])
    no_preview = LinkPreviewOptions(is_disabled=True)
    if edit:
        try:
            sent = await message.edit_text(text[:4000], reply_markup=kb_stage(idx, is_last, feedback, is_done), link_preview_options=no_preview)
        except Exception:
            sent = await message.answer(text[:4000], reply_markup=kb_stage(idx, is_last, feedback, is_done), link_preview_options=no_preview)
    else:
        try:
            sent = await message.answer(text[:4000], reply_markup=kb_stage(idx, is_last, feedback, is_done), link_preview_options=no_preview)
        except Exception as e:
            logger.warning(f"Markdown send failed for stage {idx}, retrying as plain text: {e}")
            sent = await message.answer(
                re.sub(r"[*_`\[\]]", "", text[:4000]),
                parse_mode=None,
                reply_markup=kb_stage(idx, is_last, feedback, is_done),
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
    stages = data.get("stages", [])
    completed = data.get("completed", [])

    is_last = idx == len(stages) - 1
    if is_last:
        skipped = [i for i in range(idx) if i not in completed]
        if skipped:
            if idx not in completed:
                completed.append(idx)
            await state.update_data(completed=completed)
            await callback.answer()
            await callback.message.answer(
                f"⚠️ Ты пропустил {len(skipped)} этап(а). Хочешь завершить маршрут?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Всё равно завершить", callback_data=f"finish_anyway_{idx}")],
                    [InlineKeyboardButton(text="↩️ Вернуться к пропущенным", callback_data=f"goto_{skipped[0]}")],
                ]),
            )
            return

    if idx not in completed:
        completed.append(idx)
    await state.update_data(completed=completed, current_stage=idx + 1)
    asyncio.create_task(asyncio.to_thread(
        _sb_update_progress, callback.from_user.id, completed, idx + 1
    ))
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("✅ Отмечено как пройденное!")
    next_idx = idx + 1
    if len(completed) >= len(stages):
        next_idx = len(stages)
    await _send_stage(callback.message, state, next_idx)


@dp.callback_query(Form.track, F.data.startswith("finish_anyway_"))
async def finish_anyway(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[2])
    data = await state.get_data()
    completed = data.get("completed", [])
    if idx not in completed:
        completed.append(idx)
    await state.update_data(completed=completed, current_stage=idx + 1)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("✅ Маршрут завершён!")
    await _send_stage(callback.message, state, idx + 1)


@dp.callback_query(Form.track, F.data.startswith("goto_"))
async def goto_stage(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    data = await state.get_data()
    completed = data.get("completed", [])
    await state.update_data(current_stage=idx)
    asyncio.create_task(asyncio.to_thread(_sb_update_progress, callback.from_user.id, completed, idx))
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()
    await _send_stage(callback.message, state, idx)


@dp.callback_query(Form.track, F.data.startswith("next_"))
async def stage_next(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    data = await state.get_data()
    completed = data.get("completed", [])
    await state.update_data(current_stage=idx + 1)
    asyncio.create_task(asyncio.to_thread(_sb_update_progress, callback.from_user.id, completed, idx + 1))
    await callback.answer()
    await _send_stage(callback.message, state, idx + 1, edit=True)


@dp.callback_query(Form.track, F.data.startswith("prev_"))
async def stage_prev(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    prev_idx = idx - 1
    data = await state.get_data()
    completed = data.get("completed", [])
    await state.update_data(current_stage=prev_idx)
    asyncio.create_task(asyncio.to_thread(_sb_update_progress, callback.from_user.id, completed, prev_idx))
    await callback.answer()
    await _send_stage(callback.message, state, prev_idx, edit=True)


@dp.callback_query(F.data == "noop")
async def noop_handler(callback: CallbackQuery):
    await callback.answer("Ты уже оценил этот этап")


@dp.callback_query(F.data == "webapp_continue")
async def webapp_continue(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    track = await asyncio.to_thread(_sb_get_track, callback.from_user.id)
    if not track:
        await callback.message.answer("Трек не найден. Начни с /start")
        return
    completed = track.get("completed") or []
    current_stage = track.get("current_stage") or 0
    await state.update_data(
        stages=track.get("stages") or [],
        completed=completed,
        current_stage=current_stage,
        goal=track.get("goal") or "",
        level=track.get("level") or "",
        hours=track.get("hours") or 0,
        months=track.get("months") or 0,
        goal_scope=track.get("goal_scope") or "",
    )
    await state.set_state(Form.track)
    await _send_stage(callback.message, state, current_stage)


@dp.callback_query(Form.track, F.data.startswith("like_"))
async def stage_like(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    data = await state.get_data()
    stages = data.get("stages", [])
    if stages[idx].get("liked") or stages[idx].get("disliked") or stages[idx].get("modified"):
        await callback.answer("Ты уже оценил этот этап")
        return
    stages[idx]["liked"] = True
    await state.update_data(stages=stages)
    asyncio.create_task(asyncio.to_thread(_sb_update_stages, callback.from_user.id, stages))
    asyncio.create_task(asyncio.to_thread(
        _sb_save_liked_stage,
        callback.from_user.id,
        data.get("goal", ""),
        stages[idx]["title"],
        stages[idx].get("topics", ""),
        stages[idx].get("materials", ""),
    ))
    is_last = idx == len(stages) - 1
    await callback.message.edit_reply_markup(reply_markup=kb_stage(idx, is_last, "liked"))
    await callback.answer("❤️ Этап сохранён как понравившийся!")


@dp.callback_query(Form.track, F.data.startswith("dislike_"))
async def stage_dislike(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    data = await state.get_data()
    stages = data.get("stages", [])
    if stages[idx].get("liked") or stages[idx].get("disliked") or stages[idx].get("modified"):
        await callback.answer("Ты уже оценил этот этап")
        return
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=kb_dislike(idx))


@dp.callback_query(Form.track, F.data.startswith("cancel_feedback_"))
async def cancel_feedback(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[2])
    data = await state.get_data()
    stages = data.get("stages", [])
    is_last = idx == len(stages) - 1
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=kb_stage(idx, is_last))


@dp.callback_query(Form.track, F.data.startswith("hard_"))
async def stage_hard(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    data = await state.get_data()
    stages = data.get("stages", [])
    stage = stages[idx]

    await callback.answer("Адаптирую под твой уровень...")
    msg = await callback.message.answer("Упрощаю, секунду...")

    prompt = simplify_prompt(data["goal"], data["level"], stage["title"], stage["topics"], data.get("format_pref", ""))
    try:
        result = await asyncio.to_thread(call_llm, prompt)
    except Exception:
        await msg.edit_text("Что-то пошло не так, попробуй снова 🔄")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    topics, materials = _split_llm_response(result.strip())
    stages[idx]["topics"] = topics
    stages[idx]["materials"] = materials
    stages[idx]["modified"] = "simplified"
    stages[idx]["disliked"] = True
    await state.update_data(stages=stages)
    asyncio.create_task(asyncio.to_thread(_sb_update_stages, callback.from_user.id, stages))
    asyncio.create_task(asyncio.to_thread(_sb_update_difficulty_bias, callback.from_user.id, "hard"))
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
    msg = await callback.message.answer("Ищу что-то посложнее...")

    prompt = advance_prompt(data["goal"], data["level"], stage["title"], stage["topics"], data.get("format_pref", ""))
    try:
        result = await asyncio.to_thread(call_llm, prompt)
    except Exception:
        await msg.edit_text("Что-то пошло не так, попробуй снова 🔄")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    topics, materials = _split_llm_response(result.strip())
    stages[idx]["topics"] = topics
    stages[idx]["materials"] = materials
    stages[idx]["modified"] = "advanced"
    stages[idx]["disliked"] = True
    await state.update_data(stages=stages)
    asyncio.create_task(asyncio.to_thread(_sb_update_stages, callback.from_user.id, stages))
    asyncio.create_task(asyncio.to_thread(_sb_update_difficulty_bias, callback.from_user.id, "easy"))
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
    msg = await callback.message.answer("Подбираю альтернативу...")

    prompt = alternative_prompt(data["goal"], data["level"], stage["title"], stage["topics"], data.get("format_pref", ""))
    try:
        result = await asyncio.to_thread(call_llm, prompt)
    except Exception:
        await msg.edit_text("Что-то пошло не так, попробуй снова 🔄")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    stages[idx]["materials"] = result.strip()
    stages[idx]["modified"] = "alternative"
    await state.update_data(stages=stages)
    asyncio.create_task(asyncio.to_thread(_sb_update_stages, callback.from_user.id, stages))
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
        logger.warning("RAILWAY_PUBLIC_DOMAIN not set — running in polling mode")
        return
    webhook_url = f"https://{domain}{WEBHOOK_PATH}"
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
    logger.info(f"Webhook registered: {webhook_url}")
    await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    await bot.delete_my_commands()


async def on_shutdown(bot: Bot) -> None:
    logger.info("Bot shutting down")


if __name__ == "__main__":
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")

    if domain:
        # Webhook mode (production on Render)
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
    else:
        # Polling mode (local development / Docker without public domain)
        import asyncio as _asyncio

        async def _run_polling():
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
            await bot.delete_my_commands()
            logger.info("Starting in polling mode")
            await dp.start_polling(bot)

        _asyncio.run(_run_polling())
