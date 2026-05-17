import asyncio
import os
import re
from urllib.parse import quote
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

from gigachat_client import call_gigachat
from level import COURSES_BY_LEVEL, parse_questions
from stepik import search_stepik_courses

app = FastAPI(title="Прогрессоры API")

PLATFORM_SEARCH_URLS = {
    "Яндекс Практикум": "https://practicum.yandex.ru/catalog/?text={}",
    "Skillbox":         "https://skillbox.ru/search/?q={}",
    "GeekBrains":       "https://gb.ru/search?query={}",
    "Hexlet":           "https://ru.hexlet.io/search?q={}",
}

PLAT_KEY = {
    "Яндекс Практикум": "practicum",
    "Skillbox":         "skillbox",
    "GeekBrains":       "gb",
    "Hexlet":           "hexlet",
}

PLANETS = ["earth", "mars", "jupiter", "neptune", "star"]

# ── Pydantic models ──────────────────────────────────────────────────────────

class GoalRequest(BaseModel):
    goal: str = Field(max_length=200)

class QuestionsRequest(BaseModel):
    goal: str = Field(max_length=200)

class TrackRequest(BaseModel):
    goal: str = Field(max_length=200)
    level: str = Field(max_length=50)
    hours: int = Field(ge=1, le=100)
    months: int = Field(ge=1, le=24)
    budget: int = Field(ge=0, le=50000)
    qa_text: str = Field(max_length=4000)

class TaskRequest(BaseModel):
    goal: str = Field(max_length=200)
    level: str = Field(max_length=50)
    stage_title: str = Field(max_length=200)
    stage_content: str = Field(max_length=2000)

class FeedbackRequest(BaseModel):
    goal: str = Field(max_length=200)
    level: str = Field(max_length=50)
    task: str = Field(max_length=2000)
    answer: str = Field(max_length=5000)

# ── Track parser ─────────────────────────────────────────────────────────────

def parse_track_to_stages(track_text: str, all_courses: list) -> list:
    stages = []
    parts = re.split(r"\n(?=##\s)", "\n" + track_text.strip())

    for part in parts:
        if not re.match(r"##[^\n]*Этап\s*\d+", part):
            continue

        idx = len(stages)

        title_match = re.match(r"##[^\n]*Этап\s*\d+:\s*(.+)", part)
        title = title_match.group(1).strip() if title_match else f"Этап {idx + 1}"

        weeks_match = re.search(r"\*\*Длительность:\*\*\s*(\d+)", part)
        weeks = int(weeks_match.group(1)) if weeks_match else 2

        topics_match = re.search(r"\*\*Что изучать:\*\*(.+?)(?=\*\*Результат:|\Z)", part, re.DOTALL)
        topics_text = topics_match.group(1).strip() if topics_match else ""

        topics = []
        for line in topics_text.splitlines():
            line = line.strip().lstrip("-•*·").strip()
            if len(line) > 5:
                topics.append({"html": line})
        if not topics and topics_text:
            for item in re.split(r"[;,]", topics_text):
                item = item.strip()
                if len(item) > 5:
                    topics.append({"html": item})

        outcome_match = re.search(r"\*\*Результат:\*\*\s*(.+?)(?=\n##|\Z)", part, re.DOTALL)
        outcome = outcome_match.group(1).strip().replace("\n", " ") if outcome_match else ""

        summary = topics_text[:220].rstrip() + ("…" if len(topics_text) > 220 else "")

        # 2 courses per stage from the global pool
        stage_courses = all_courses[idx * 2:(idx + 1) * 2]

        stages.append({
            "id": idx + 1,
            "planet": PLANETS[idx % len(PLANETS)],
            "title": title,
            "weeks": weeks,
            "state": "locked" if idx > 0 else "current",
            "summary": summary,
            "topics": topics[:6],
            "outcome": outcome,
            "courses": stage_courses,
            "task": "",
        })

    if stages:
        stages[-1]["final"] = True

    return stages

# ── API endpoints ─────────────────────────────────────────────────────────────

@app.post("/api/validate")
async def validate_goal(req: GoalRequest):
    goal = req.goal.strip()
    if not goal or not any(c.isalpha() for c in goal):
        return {"status": "invalid", "message": "Цель должна содержать буквы."}

    prompt = f"""Ты — эксперт по оценке карьерных целей. Классифицируй цель пользователя по одной из четырёх категорий.

Цель: "{goal}"

КАТЕГОРИИ:
1. РЕАЛИСТИЧНО — любой конкретный навык, технология, профессия или дисциплина, доступная через обучение: языки программирования (Python, SQL, Java), инструменты (Figma, Photoshop), профессии (дизайнер, маркетолог, хирург, юрист), языки (английский, китайский), творческие навыки (гитара, фотография) и т.д.

2. АБСТРАКТНО — цель не содержит конкретного навыка или предметной области, по которой можно составить учебный план. Два случая:
   а) Размытые жизненные цели без конкретики: "стать лучше", "быть успешным", "развиваться", "жить счастливо", "стать богатым".
   б) "Стать + предмет/инструмент/язык/технология/объект/вещество" — когда после "стать" стоит не профессия, а вещь или инструмент. Классифицируй строго по написанному — не угадывай намерение пользователя.
   Примеры АБСТРАКТНО: "стать Python" / "стать питоном", "стать C++" / "стать с++", "стать JavaScript" / "стать джаваскриптом", "стать гитарой", "стать Excel" / "стать экселем", "стать маслом".
   Признак: слово после "стать" — это предмет, язык, инструмент или технология, а не роль человека.
   НЕ абстрактно: "стать поваром", "стать фотографом", "стать музыкантом", "стать Python-разработчиком", "стать программистом" — здесь после "стать" стоит профессия/роль.

3. ИНСТИТУЦИОНАЛЬНЫЙ — профессия, требующая государственного отбора, секретного допуска или уникальной физической подготовки, которую НЕЛЬЗЯ пройти самостоятельно: космонавт, военный лётчик-истребитель, пилот Формулы-1, действующий президент страны, профессиональный олимпийский спортсмен высшего уровня.

4. НЕРЕАЛИСТИЧНО — цель физически невозможна для любого человека: летать без снаряжения, телепортироваться, жить вечно, стать суперменом, стать вымышленным персонажем.

Верни ТОЛЬКО одно слово из четырёх: РЕАЛИСТИЧНО, АБСТРАКТНО, ИНСТИТУЦИОНАЛЬНЫЙ или НЕРЕАЛИСТИЧНО.
Если АБСТРАКТНО — на следующей строке полное предложение-подсказка: что именно стоит указать вместо этого. Пример формата: «Попробуй уточнить цель — например, стать колумнистом, научиться писать тексты или освоить журналистику.»
Если ИНСТИТУЦИОНАЛЬНЫЙ — на следующей строке одно предложение: что конкретно требует эта профессия.
Если НЕРЕАЛИСТИЧНО — на следующей строке одно предложение: почему физически невозможно.
Не добавляй ничего лишнего."""

    try:
        raw = call_gigachat(prompt)
        lines = raw.strip().splitlines()
        first = lines[0].strip()
        explanation = lines[1].strip() if len(lines) > 1 else ""

        if "НЕРЕАЛИСТИЧНО" in first:
            return {"status": "unrealistic", "message": explanation or "Цель физически невозможна."}
        if "АБСТРАКТНО" in first:
            return {"status": "abstract", "message": explanation or "Уточни цель — укажи конкретный навык."}
        if "ИНСТИТУЦИОНАЛЬНЫЙ" in first:
            return {"status": "institutional", "message": explanation}
        return {"status": "ok"}
    except Exception:
        return {"status": "ok"}  # fail open


@app.post("/api/questions")
async def generate_questions(req: QuestionsRequest):
    prompt = f"""Ты — эксперт по диагностике уровня знаний. Сгенерируй РОВНО 4 вопроса для оценки уровня пользователя по теме: "{req.goal}".

Пользователь отвечает по шкале A/B/C/D. Формулируй: "Насколько ты знаком с...", "Как часто ты...", "В какой мере ты практиковал...".
НЕ используй форму "Что такое...", "Какой...".

Верни ТОЛЬКО валидный JSON без текста до или после:
[
  {{"question": "Текст вопроса"}},
  {{"question": "Текст вопроса"}},
  {{"question": "Текст вопроса"}},
  {{"question": "Текст вопроса"}}
]"""

    try:
        raw = call_gigachat(prompt)
        questions = parse_questions(raw)
        if not questions:
            raise HTTPException(500, "Не удалось распарсить вопросы")
        # Return in {q: ...} format matching the static QUESTIONS in data.js
        return {"questions": [{"q": q["question"]} for q in questions]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/track")
async def generate_track(req: TrackRequest):
    weeks = req.months * 4
    limit = COURSES_BY_LEVEL.get(req.level, 3)

    prompt = f"""Ты — персональный ИИ-навигатор по обучению. Составь детальный трек обучения.

ПРОФИЛЬ:
- Цель: {req.goal}
- Уровень: {req.level}
- Время: {req.hours} ч/нед, срок {req.months} мес ({weeks} нед)
- Бюджет: {req.budget} руб/мес (0 = только бесплатное)

Ответы диагностики:
{req.qa_text}

A/B → пробел, включи в трек. C/D → уже владеет, только углубляй.

ОГРАНИЧЕНИЯ:
Платформы без VPN: Яндекс Практикум, GeekBrains, Skillbox, Hexlet, Rutube.
ЗАПРЕЩЕНО: Coursera, Udemy, edX, Skillshare, LinkedIn Learning.
Нагрузка: {req.hours} ч/нед, минимум 40% — практика. Ориентируйся на {weeks} нед (±20%).
Бюджет: строго {req.budget} руб/мес. При 0 — только бесплатные.
Без ссылок и раздела Ресурсы.

Составь РОВНО 5 этапов:

## 📍 Этап 1: [Название]
**Длительность:** X недель
**Что изучать:** Конкретные понятия и инструменты. Каждый пункт с новой строки, начиная с дефиса.
**Результат:** что умеет пользователь после этапа

## 📍 Этап 2: [Название]
...до Этапа 5. Последний — финальный проект / портфолио."""

    plat_prompt = f"""Назови 2-3 реальных курса по теме «{req.goal}» с российских платформ.
Платформы: Яндекс Практикум, Skillbox, GeekBrains, Hexlet.
Формат строго: Платформа | Название курса
Только реальные существующие курсы. Без пояснений."""

    # Запускаем все три вызова параллельно
    results = await asyncio.gather(
        asyncio.to_thread(call_gigachat, prompt),
        asyncio.to_thread(search_stepik_courses, req.goal, req.budget, limit),
        asyncio.to_thread(call_gigachat, plat_prompt),
        return_exceptions=True,
    )

    track_result, stepik_result, plat_result = results

    if isinstance(track_result, Exception):
        print(f"ERROR /api/track — GigaChat track: {track_result}")
        raise HTTPException(500, "Не удалось сгенерировать трек. Попробуй ещё раз.")

    track_text = track_result
    stepik_raw = [] if isinstance(stepik_result, Exception) else stepik_result
    plat_raw = "" if isinstance(plat_result, Exception) else plat_result

    all_courses = []

    for c in stepik_raw:
        all_courses.append({
            "plat": "stepik",
            "name": "Stepik",
            "title": c["title"],
            "price": c["price"],
            "url": c["url"],
        })

    for line in plat_raw.strip().splitlines():
        if "|" not in line:
            continue
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        platform, course_name = parts[0].strip(), parts[1].strip()
        for p_name, url_tpl in PLATFORM_SEARCH_URLS.items():
            if p_name.lower() in platform.lower():
                all_courses.append({
                    "plat": PLAT_KEY.get(p_name, "stepik"),
                    "name": p_name,
                    "title": course_name,
                    "price": None,
                    "url": url_tpl.format(quote(course_name)),
                })
                break

    stages = parse_track_to_stages(track_text, all_courses)

    if not stages:
        raise HTTPException(500, "Не удалось распарсить трек. Попробуй ещё раз.")

    return {"stages": stages}


@app.post("/api/task")
async def generate_task(req: TaskRequest):
    prompt = f"""Ты — преподаватель по теме «{req.goal}». Составь одно практическое задание для этапа «{req.stage_title}».

Уровень пользователя: {req.level}
Содержание этапа: {req.stage_content[:400]}

Требования:
- Выполнимо текстом, без запуска кода и специального ПО
- Проверяет понимание, а не знание определений
- Чёткий вопрос с понятным ожидаемым ответом

Напиши ТОЛЬКО текст задания, без вводных слов и пояснений."""

    try:
        task = call_gigachat(prompt)
        return {"task": task.strip()}
    except Exception as e:
        print(f"ERROR /api/task: {e}")
        raise HTTPException(500, "Не удалось сгенерировать задание. Попробуй ещё раз.")


@app.post("/api/feedback")
async def check_feedback(req: FeedbackRequest):
    if not req.answer.strip():
        raise HTTPException(400, "Пустой ответ")

    prompt = f"""Ты — преподаватель по теме «{req.goal}». Оцени ответ пользователя.

Уровень: {req.level}
Задание: {req.task}
Ответ пользователя: {req.answer}

Критерии вердикта (строго):
- «Верно» — ответ решает задачу по существу: логика правильная, результат соответствует условию.
- «Частично» — правильный подход, но есть существенные пропуски или неполнота.
- «Неверно» — бессмысленный набор символов, случайный текст, неправильная логика.
Бессмысленный или случайный текст (например «fafafa», «ааа», «не знаю») — всегда «Неверно».

Структура ответа:
1. Первая строка — только одно слово-вердикт: «Верно», «Частично» или «Неверно».
2. Что именно правильно (если есть).
3. Если неверно/частично — подробное правильное решение.
4. Одно ободряющее предложение в конце."""

    try:
        fb = call_gigachat(prompt).strip()
    except Exception as e:
        print(f"ERROR /api/feedback: {e}")
        raise HTTPException(500, "Не удалось проверить ответ. Попробуй ещё раз.")

    first_line = fb.split("\n")[0].strip().strip("*").rstrip(".!").lower()
    if first_line == "верно":
        verdict = "good"
    elif first_line == "частично":
        verdict = "warn"
    else:
        verdict = "bad"

    return {"verdict": verdict, "text": fb}


# ── Static files ──────────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def root():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(404, "index.html not found in static/")
    return FileResponse(str(index))
