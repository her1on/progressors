import asyncio
import logging
import os
import re

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

from bot_prompts import (
    alternative_prompt,
    questions_prompt,
    simplify_prompt,
    track_prompt,
)
from gigachat_client import call_gigachat
from level import parse_questions

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
storage = MemoryStorage()
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


# ── FSM States ────────────────────────────────────────────────────────────────

class Form(StatesGroup):
    goal = State()
    hours = State()
    months = State()
    budget = State()
    quiz = State()
    track = State()


# ── Keyboards ─────────────────────────────────────────────────────────────────

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


def kb_budget():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="0 руб (бесплатно)", callback_data="b_0"),
            InlineKeyboardButton(text="до 1 000 руб", callback_data="b_1000"),
        ],
        [
            InlineKeyboardButton(text="до 3 000 руб", callback_data="b_3000"),
            InlineKeyboardButton(text="5 000+ руб", callback_data="b_5000"),
        ],
    ])


def kb_quiz():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{k}  {v}", callback_data=f"q_{k}")]
        for k, v in QUIZ_OPTIONS
    ])


def kb_build():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Построить трек", callback_data="build_track"),
    ]])


def kb_stage(idx: int, is_last: bool):
    rows = [
        [InlineKeyboardButton(text="✅ Пройдено", callback_data=f"done_{idx}")],
        [
            InlineKeyboardButton(text="😕 Сложно", callback_data=f"hard_{idx}"),
            InlineKeyboardButton(text="👎 Не подошло", callback_data=f"bad_{idx}"),
        ],
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def escape_md(text: str) -> str:
    for ch in r"*_`[":
        text = text.replace(ch, f"\\{ch}")
    return text


def calc_level(answers: list[str]) -> str:
    score = sum("ABCD".index(a) for a in answers)
    if score <= 3:
        return "Полный новичок"
    elif score <= 6:
        return "Базовые знания"
    elif score <= 9:
        return "Средний уровень"
    return "Продвинутый"


def parse_track(text: str) -> list[dict]:
    text = re.sub(r"```[a-zA-Z]*\n?", "", text).replace("```", "").strip()
    stages = []

    # Разбиваем по любому заголовку ## уровня (включая ###)
    parts = re.split(r"\n(?=#{2,3}\s)", "\n" + text)

    for part in parts:
        first_line = part.split("\n")[0]

        # Пропускаем итоговый раздел и пустые части
        if re.search(r"Итог|Результат трека|Карьер", first_line, re.IGNORECASE):
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
            r"\*{0,2}Что изучать:?\*{0,2}(.+?)(?=\*{0,2}Материал|\*{0,2}Результат|\Z)",
            part, re.DOTALL | re.IGNORECASE,
        )
        topics = topics_m.group(1).strip() if topics_m else ""

        materials_m = re.search(
            r"\*{0,2}Материал[ыь]:?\*{0,2}(.+?)(?=\*{0,2}Результат|\Z)",
            part, re.DOTALL | re.IGNORECASE,
        )
        materials = materials_m.group(1).strip() if materials_m else ""

        outcome_m = re.search(
            r"\*{0,2}Результат:?\*{0,2}\s*(.+?)(?=\n#{2,3}|\Z)",
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

    return stages


def format_stage(stage: dict, idx: int, total: int) -> str:
    text = (
        f"🪐 *Этап {idx + 1} из {total}: {stage['title']}*\n"
        f"⏱ {stage['weeks']} нед\n\n"
    )
    if stage.get("topics"):
        text += f"*Что изучать:*\n{stage['topics']}\n\n"
    if stage.get("materials"):
        text += f"*Материалы:*\n{stage['materials']}\n\n"
    if stage.get("outcome"):
        text += f"✨ *Результат:* _{stage['outcome']}_"
    return text[:4000]


# ── Handlers ──────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🪐 *Прогрессоры* — твой ИИ-навигатор по обучению\n\n"
        "Я помогу построить персональный трек обучения — с бесплатными материалами "
        "на русском языке, подобранными под твой уровень и цели.\n\n"
        "Скажи мне — *чему хочешь научиться?*\n"
        "_Например: Python, UX-дизайн, маркетинг, сварка, английский язык_",
    )
    await state.set_state(Form.goal)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "*Прогрессоры* — ИИ-навигатор по обучению\n\n"
        "*Как это работает:*\n"
        "1. Скажи чему хочешь научиться\n"
        "2. Укажи сколько времени готов тратить\n"
        "3. Пройди короткую диагностику\n"
        "4. Получи персональный трек с бесплатными материалами\n\n"
        "*Команды:*\n"
        "/start — начать или перезапустить\n"
        "/cancel — отменить текущий процесс\n"
        "/help — эта справка\n\n"
        "*На каждом этапе трека можно:*\n"
        "✅ Отметить как пройденное\n"
        "😕 Попросить упростить материал\n"
        "👎 Получить альтернативные ресурсы\n"
        "➡️ Пропустить и перейти дальше",
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Нечего отменять. Введи /start чтобы начать.")
        return
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

    await state.update_data(goal=goal)
    await message.answer(
        f"Отлично! Цель: *{escape_md(goal)}*\n\nСколько часов в неделю готов уделять учёбе?",
        reply_markup=kb_hours(),
    )
    await state.set_state(Form.hours)


@dp.callback_query(Form.hours, F.data.startswith("h_"))
async def got_hours(callback: CallbackQuery, state: FSMContext):
    hours = int(callback.data.split("_")[1])
    await state.update_data(hours=hours)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"⏱ *{hours} ч/нед* — принято!\n\nЗа сколько месяцев хочешь достичь цели?",
        reply_markup=kb_months(),
    )
    await state.set_state(Form.months)
    await callback.answer()


@dp.callback_query(Form.months, F.data.startswith("m_"))
async def got_months(callback: CallbackQuery, state: FSMContext):
    months = int(callback.data.split("_")[1])
    await state.update_data(months=months)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"📅 *{months} мес* — отлично!\n\nКакой месячный бюджет на обучение?",
        reply_markup=kb_budget(),
    )
    await state.set_state(Form.budget)
    await callback.answer()


@dp.callback_query(Form.budget, F.data.startswith("b_"))
async def got_budget(callback: CallbackQuery, state: FSMContext):
    budget = int(callback.data.split("_")[1])
    await state.update_data(budget=budget)
    data = await state.get_data()
    await callback.message.edit_reply_markup(reply_markup=None)

    budget_label = "бесплатно" if budget == 0 else f"до {budget:,} руб/мес".replace(",", " ")
    await callback.message.answer(
        f"💰 *{budget_label}* — принято!\n\n⏳ Генерирую вопросы для диагностики...",
    )

    msg = await callback.message.answer("...")
    try:
        raw = await asyncio.to_thread(call_gigachat, questions_prompt(data["goal"]))
        questions = parse_questions(raw)
        if not questions:
            raise ValueError("empty")
    except Exception:
        questions = [
            {"question": f"Насколько ты знаком с основами темы «{data['goal']}»?"},
            {"question": f"Как часто ты практикуешь навыки по теме «{data['goal']}»?"},
            {"question": f"В какой мере ты применял «{data['goal']}» на практике?"},
            {"question": f"Насколько ты знаком с продвинутыми аспектами «{data['goal']}»?"},
        ]

    await state.update_data(questions=questions, current_q=0, answers=[])
    await msg.delete()
    await _send_question(callback.message, state)
    await state.set_state(Form.quiz)
    await callback.answer()


async def _send_question(message: Message, state: FSMContext):
    data = await state.get_data()
    idx = data["current_q"]
    questions = data["questions"]
    q_text = questions[idx].get("question", questions[idx].get("q", f"Вопрос {idx + 1}"))
    try:
        await message.answer(
            f"📋 *Вопрос {idx + 1} из {len(questions)}*\n\n{q_text}",
            reply_markup=kb_quiz(),
        )
    except Exception:
        await message.answer(
            f"📋 Вопрос {idx + 1} из {len(questions)}\n\n{q_text}",
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
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer(f"Ответ {letter} принят ✓")

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

    budget = data.get("budget", 0)
    budget_label = "бесплатно" if budget == 0 else f"до {budget:,} руб/мес".replace(",", " ")
    await callback.message.answer(
        f"🎯 *Уровень определён*\n\n"
        f"*{level}*\n_{level_desc}_\n\n"
        f"📌 Цель: *{escape_md(data['goal'])}*\n"
        f"⏱ {data['hours']} ч/нед · {data['months']} мес · {budget_label}\n\n"
        f"Готов к персональному треку?",
        reply_markup=kb_build(),
    )


@dp.callback_query(F.data == "build_track")
async def build_track(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.message.edit_reply_markup(reply_markup=None)
    msg = await callback.message.answer("🪐 Строю твой персональный трек...")

    weeks = data["months"] * 4
    prompt = track_prompt(
        goal=data["goal"],
        level=data["level"],
        hours=data["hours"],
        months=data["months"],
        weeks=weeks,
        budget=data.get("budget", 0),
        qa_text=data["qa_text"],
    )

    try:
        track_text = await asyncio.to_thread(call_gigachat, prompt)
        logger.info(f"Track raw response (first 300): {track_text[:300]}")
        stages = parse_track(track_text)
        if not stages:
            logger.error(f"parse_track returned empty. Full response:\n{track_text}")
            raise ValueError("no stages")
    except Exception as e:
        logger.error(f"Track generation failed: {e}")
        await msg.edit_text("❌ Не удалось сгенерировать трек. Попробуй ещё раз — /start")
        await callback.answer()
        return

    await state.update_data(stages=stages, current_stage=0, completed=[])
    await state.set_state(Form.track)
    await msg.delete()
    await callback.answer()
    await _send_stage(callback.message, state, 0)


async def _send_stage(message: Message, state: FSMContext, idx: int):
    data = await state.get_data()
    stages = data.get("stages", [])

    if idx >= len(stages):
        await message.answer(
            "🌟 *Маршрут пройден! Поздравляю!*\n\n"
            f"Ты прошёл весь трек по теме *{escape_md(data.get('goal', ''))}*.\n\n"
            "Хочешь построить новый маршрут?",
            reply_markup=kb_restart(),
        )
        return

    stage = stages[idx]
    is_last = idx == len(stages) - 1
    text = format_stage(stage, idx, len(stages))
    try:
        await message.answer(text, reply_markup=kb_stage(idx, is_last))
    except Exception as e:
        logger.warning(f"Markdown send failed for stage {idx}, retrying as plain text: {e}")
        await message.answer(
            re.sub(r"[*_`\[\]]", "", text),
            parse_mode=None,
            reply_markup=kb_stage(idx, is_last),
        )


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
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await _send_stage(callback.message, state, idx + 1)


@dp.callback_query(Form.track, F.data.startswith("prev_"))
async def stage_prev(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    prev_idx = idx - 1
    await state.update_data(current_stage=prev_idx)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await _send_stage(callback.message, state, prev_idx)


@dp.callback_query(Form.track, F.data.startswith("hard_"))
async def stage_hard(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    data = await state.get_data()
    stages = data.get("stages", [])
    stage = stages[idx]

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Адаптирую под твой уровень...")
    msg = await callback.message.answer("⏳ Делаю этот этап проще...")

    prompt = simplify_prompt(data["goal"], data["level"], stage["title"], stage["topics"])
    try:
        result = await asyncio.to_thread(call_gigachat, prompt)
        stages[idx]["topics"] = result.strip()
        stages[idx]["materials"] = ""
        await state.update_data(stages=stages)
        await msg.delete()
        await _send_stage(callback.message, state, idx)
    except Exception:
        await msg.edit_text("❌ Не удалось адаптировать. Попробуй перейти к следующему этапу.")


@dp.callback_query(Form.track, F.data.startswith("bad_"))
async def stage_bad(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    data = await state.get_data()
    stages = data.get("stages", [])
    stage = stages[idx]

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Подбираю альтернативные материалы...")
    msg = await callback.message.answer("⏳ Ищу другие материалы...")

    prompt = alternative_prompt(data["goal"], data["level"], stage["title"], stage["topics"])
    try:
        result = await asyncio.to_thread(call_gigachat, prompt)
        stages[idx]["materials"] = result.strip()
        await state.update_data(stages=stages)
        await msg.delete()
        await _send_stage(callback.message, state, idx)
    except Exception:
        await msg.edit_text("❌ Не удалось подобрать альтернативу.")


@dp.callback_query(F.data == "restart")
async def restart(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await cmd_start(callback.message, state)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    logger.info("Bot starting...")
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать / перезапустить"),
        BotCommand(command="cancel", description="Отменить текущий процесс"),
        BotCommand(command="help", description="Справка"),
    ])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
