import asyncio
import html as html_lib
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

PLANETS = ["earth", "mars", "jupiter", "neptune"]

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

_REFUSAL_MARKERS = [
    "Пожалуйста, укажи конкретный навык",
    "не могу составить трек",
    "не буду составлять",
]


def parse_track_to_stages(track_text: str) -> list:
    # Strip markdown code fences GigaChat sometimes wraps the response in
    track_text = re.sub(r"```[a-z]*\n?", "", track_text).strip()

    stages = []
    parts = re.split(r"\n(?=##\s)", "\n" + track_text.strip())

    for part in parts:
        if not re.match(r"##[^\n]*(?:Этап|Шаг)\s*\d+", part):
            continue

        idx = len(stages)

        title_match = re.match(r"##[^\n]*(?:Этап|Шаг)\s*\d+[:.]\s*(.+)", part)
        title = title_match.group(1).strip() if title_match else f"Этап {idx + 1}"

        weeks_match = re.search(r"\*\*Длительность:\*\*\s*(\d+)", part)
        weeks = int(weeks_match.group(1)) if weeks_match else 2

        topics_match = re.search(r"\*\*Что изучать:\*\*(.+?)(?=\*\*Результат:|\Z)", part, re.DOTALL)
        topics_text = topics_match.group(1).strip() if topics_match else ""

        topics = []
        for line in topics_text.splitlines():
            line = line.strip().lstrip("-•*·").strip()
            if len(line) > 5:
                topics.append({"html": html_lib.escape(line)})
        if not topics and topics_text:
            for item in re.split(r"[;,]", topics_text):
                item = item.strip()
                if len(item) > 5:
                    topics.append({"html": html_lib.escape(item)})

        outcome_match = re.search(r"\*\*Результат:\*\*\s*(.+?)(?=\n##|\Z)", part, re.DOTALL)
        outcome = html_lib.escape(outcome_match.group(1).strip().replace("\n", " ")) if outcome_match else ""

        summary = topics_text[:220].rstrip() + ("…" if len(topics_text) > 220 else "")

        stages.append({
            "id": idx + 1,
            "planet": PLANETS[idx % len(PLANETS)],
            "title": title,
            "weeks": weeks,
            "state": "locked" if idx > 0 else "current",
            "summary": summary,
            "topics": topics[:6],
            "outcome": outcome,
            "task": "",
        })

    if stages:
        stages[-1]["final"] = True
        stages[-1]["planet"] = "star"

    return stages

# ── API endpoints ─────────────────────────────────────────────────────────────

@app.post("/api/validate")
async def validate_goal(req: GoalRequest):
    goal = req.goal.strip()
    if not goal or not any(c.isalpha() for c in goal):
        return {"status": "invalid", "message": "Цель должна содержать буквы."}

    prompt = f"""Ты — эксперт по оценке целей в образовательном трекере. Интерпретируй запрос буквально, затем классифицируй.

Цель: "{goal}"

## Интерпретация

Читай запрос максимально буквально:
- Технический смысл слова применяй только при явных маркерах рядом с ним: "язык", "код", "программирование", "фреймворк", "разработка", "библиотека".
- Без этих маркеров: "питон" = змея, "гитара" = музыкальный инструмент, "с++" = язык программирования как объект.
- "стать питоном" = буквально стать змеёй, а не изучить Python.
- "стать Python-разработчиком" = профессия, роль человека — это реалистично.

## Категории

1. РЕАЛИСТИЧНО — конкретный навык, технология, профессия или дисциплина, доступная через обучение: языки программирования (Python, SQL, Java), инструменты (Figma, Photoshop), профессии (дизайнер, маркетолог, хирург, юрист), языки (английский, китайский), творческие навыки (гитара, фотография) и т.д.

2. АБСТРАКТНО — только размытые жизненные цели без конкретного навыка: "стать лучше", "быть успешным", "развиваться", "жить счастливо", "стать богатым".

3. ИНСТИТУЦИОНАЛЬНЫЙ — профессия, в которую попадают ТОЛЬКО через закрытый внешний отбор, недоступный для самостоятельного прохождения. Три признака (хотя бы один):
   — требует государственного допуска или лицензии, выдаваемой только государством по итогам специального набора;
   — вход через контракт с закрытой организацией, которая сама выбирает кандидатов (человек не может попасть туда личными усилиями);
   — требует физических данных или нахождения в реестре, которые проверяются только в рамках официального набора.
   Критерий: человек не может попасть в профессию через самообучение или личные усилия — нужен внешний «пропуск» от государства или закрытой структуры.

4. НЕРЕАЛИСТИЧНО — два случая:
   а) Физически невозможно для любого человека: летать без снаряжения, телепортироваться, жить вечно, стать суперменом, стать вымышленным персонажем.
   б) "Стать [не-человеческая сущность]" — ПЕРВОЕ слово после "стать" — то, чем человек буквально не может стать: животное, предмет, вещество, инструмент, технология, язык программирования.
      Примеры: "стать питоном", "стать с++", "стать гитарой", "стать деревом", "стать Excel", "стать маслом".
      НЕ нереалистично: "стать поваром", "стать программистом", "стать Python-разработчиком", "стать музыкантом" — первое слово после "стать" — профессия или роль человека.

⚠️ КЛЮЧЕВОЕ ПРАВИЛО для конструкции "стать X Y":
Анализируй только X — ПЕРВОЕ слово/словосочетание после "стать". Если X — профессия или роль человека, то вся фраза — РЕАЛИСТИЧНО или ИНСТИТУЦИОНАЛЬНЫЙ, никогда не НЕРЕАЛИСТИЧНО.
Слова после X — это уточнения и модификаторы, они не меняют категорию.
Примеры: "стать поваром мишлен" → X=повар (профессия) → РЕАЛИСТИЧНО; "стать топовым программистом" → программист (профессия) → РЕАЛИСТИЧНО; "стать лучшим хирургом России" → хирург (профессия) → РЕАЛИСТИЧНО.

## Формат ответа

Строка 1: ТОЛЬКО одно слово — РЕАЛИСТИЧНО, АБСТРАКТНО, ИНСТИТУЦИОНАЛЬНЫЙ или НЕРЕАЛИСТИЧНО.
Строка 2 (только если не РЕАЛИСТИЧНО):
- АБСТРАКТНО: одно предложение-подсказка, что именно стоит указать. Пример: «Попробуй уточнить цель — например, стать колумнистом, научиться писать тексты или освоить журналистику.»
- ИНСТИТУЦИОНАЛЬНЫЙ: одно предложение — что конкретно требует эта профессия. Пример: «Требует государственного отбора и специального допуска.»
- НЕРЕАЛИСТИЧНО: одно предложение — если похоже на опечатку, предложи как переформулировать; если физически невозможно — объясни почему.
Не добавляй ничего лишнего."""

    try:
        raw = await asyncio.to_thread(call_gigachat, prompt)
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

Пользователь будет отвечать на каждый вопрос по фиксированной шкале:
A) Никогда не пробовал / не слышал
B) Знаком в теории, но не практиковал
C) Практиковал, есть реальный опыт
D) Занимаюсь на продвинутом/профессиональном уровне

Формулируй вопросы так, чтобы ответ по этой шкале был естественным.
Используй форму "Насколько ты знаком с...", "Как часто ты...", "В какой мере ты практиковал...".
НЕ используй форму "Что такое...", "Какой...", "Сколько стоит..." — такие вопросы не подходят к шкале.
Не спрашивай о косвенном опыте: просмотре контента, посещении мероприятий, общении с сообществом.

Верни ТОЛЬКО валидный JSON без какого-либо текста до или после. Формат строго такой:
[
  {{"question": "Текст вопроса"}},
  {{"question": "Текст вопроса"}},
  {{"question": "Текст вопроса"}},
  {{"question": "Текст вопроса"}}
]

Не добавляй пояснений, markdown-блоков, комментариев — только JSON-массив."""

    try:
        raw = await asyncio.to_thread(call_gigachat, prompt)
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

    prompt = f"""Ты — персональный ИИ-навигатор по обучению. Составь детальный трек обучения для пользователя.

ОТКАЗ: Откажись ТОЛЬКО если цель не содержит ни одного конкретного навыка, профессии или предметной области (например: "стать лучше", "быть успешным", "жить счастливо"). Любую профессию, ремесло или учебную дисциплину — принимай.
Названия языков программирования, технологий, дисциплин и инструментов (Python, SQL, Figma, фотография, гитара) — всегда принимай как конкретную цель.
При отказе напиши ТОЛЬКО: "Пожалуйста, укажи конкретный навык или профессию. Например: Python-разработка, UX-дизайн, английский язык."

ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:
- Цель: {req.goal}
- Уровень: {req.level}
- Время: {req.hours} ч/нед → срок {req.months} мес ({weeks} нед)
- Бюджет: {req.budget} руб/мес (0 = только бесплатное)

Ответы диагностики:
{req.qa_text}

A/B → пробел в знаниях, включи в трек. C/D → уже владеет, только углубляй, не начинай с азов.

УРОВЕНЬ ПОЛЬЗОВАТЕЛЯ: {req.level}
- Полный новичок — объясняй термины, темп медленный; бесплатные курсы, тренажёры, уроки до 15 мин
- Базовые знания — закрепляй основы, темп умеренный; структурированные курсы, первые pet-проекты
- Средний уровень — реальные проекты, темп активный; хакатоны, code review, open source
- Продвинутый — экспертиза и вклад в сообщество, темп интенсивный; книги, конференции, преподавание

Тон: простой и ободряющий для новичка, лаконичный и технический для продвинутого.

ОГРАНИЧЕНИЯ:
Платформы (только без VPN): Яндекс Практикум, GeekBrains, Skillbox, Hexlet, Rutube.
ЗАПРЕЩЕНО упоминать: Coursera, Udemy, edX, Skillshare, LinkedIn Learning, Khan Academy — недоступны из РФ.
Если без иностранного ресурса не обойтись — добавь пометку [⚠️ нужен VPN или альтернативная оплата].
Нагрузка: {req.hours} ч/нед, минимум 40% времени — практика. Ориентируйся на {weeks} недель (±20%).
Бюджет: строго {req.budget} руб/мес. При 0 — только бесплатные материалы.
Без ссылок: не добавляй URL и раздел "Ресурсы" — они будут показаны отдельно.

Составь трек строго в следующем формате:

## 📍 Этап 1: [Название]
**Длительность:** X недель
**Что изучать:** Конкретные понятия, техники, инструменты — на уровне реальной учебной программы. НЕ пиши общие категории («основы», «грамматика», «базовые понятия»). Примеры нужной детализации: Python — «f-strings, list slicing [a:b], dict.get() / .items() / .update()»; английский — «Present Perfect vs Past Simple, reported speech, фразовые глаголы go on / give up»; UX — «CJM, wireframe в Figma, метод 5 seconds test»; маркетинг — «воронка AIDA, UTM-метки, расчёт CPC и ROI». Придерживайся этого уровня для любой предметной области.
**Результат:** что конкретно умеет делать пользователь после этапа

## 📍 Этап 2: [Название]
...

## 🎯 Итог
Карьерные перспективы после прохождения трека (2–3 предложения)."""

    plat_prompt = f"""Назови 2-3 реальных курса по теме «{req.goal}» с российских образовательных платформ.
Платформы: Яндекс Практикум, Skillbox, GeekBrains, Hexlet.
Верни ТОЛЬКО список строк в формате:
Платформа | Название курса
Пример:
Яндекс Практикум | Python-разработчик
Skillbox | Python-разработчик с нуля
Только реальные существующие курсы. Без пояснений и лишнего текста."""

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
    if any(m in track_text for m in _REFUSAL_MARKERS):
        raise HTTPException(422, track_text.split("\n")[0])

    stepik_raw = [] if isinstance(stepik_result, Exception) else stepik_result
    plat_raw = "" if isinstance(plat_result, Exception) else plat_result

    stepik_courses = [
        {"plat": "stepik", "name": "Stepik", "title": c["title"], "price": c["price"], "url": c["url"]}
        for c in stepik_raw
    ]

    platform_courses = []
    for line in plat_raw.strip().splitlines():
        if "|" not in line:
            continue
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        platform, course_name = parts[0].strip(), parts[1].strip()
        for p_name, url_tpl in PLATFORM_SEARCH_URLS.items():
            if p_name.lower() in platform.lower():
                platform_courses.append({
                    "plat": PLAT_KEY.get(p_name, "stepik"),
                    "name": p_name,
                    "title": course_name,
                    "price": None,
                    "url": url_tpl.format(quote(course_name)),
                })
                break

    stages = parse_track_to_stages(track_text)

    if not stages:
        try:
            track_text = await asyncio.to_thread(call_gigachat, prompt)
            stages = parse_track_to_stages(track_text)
        except Exception:
            pass

    if not stages:
        raise HTTPException(500, "Не удалось распарсить трек. Попробуй ещё раз.")

    return {"stages": stages, "courses": {"stepik": stepik_courses, "platforms": platform_courses}}


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
        task = await asyncio.to_thread(call_gigachat, prompt)
        return {"task": task.strip()}
    except Exception as e:
        print(f"ERROR /api/task: {e}")
        raise HTTPException(500, "Не удалось сгенерировать задание. Попробуй ещё раз.")


@app.post("/api/feedback")
async def check_feedback(req: FeedbackRequest):
    if not req.answer.strip():
        raise HTTPException(400, "Пустой ответ")

    prompt = f"""Ты — преподаватель по теме «{req.goal}». Оцени ответ пользователя.

Уровень пользователя: {req.level}
Задание: {req.task}
Ответ пользователя: {req.answer}

Критерии вердикта (строго):
- «Верно» — ответ решает задачу по существу: логика правильная, результат соответствует условию.
- «Частично» — правильный подход, но есть существенные пропуски, ошибки или неполнота.
- «Неверно» — ответ не решает задачу: бессмысленный набор символов, случайный текст, неправильная логика.
Бессмысленный или случайный текст (например «fafafa», «ааа», «не знаю») — всегда «Неверно».

Структура ответа:
1. Первая строка — только одно слово-вердикт: «Верно», «Частично» или «Неверно».
2. Что именно правильно (если есть).
3. Если ответ неверный или частичный — дай подробное правильное решение с пошаговым объяснением, как к нему прийти.
4. Одно ободряющее предложение в конце.

Тон: поддерживающий и конкретный. Не используй общие фразы типа «Молодец!» без объяснения."""

    try:
        fb = (await asyncio.to_thread(call_gigachat, prompt)).strip()
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
