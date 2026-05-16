import os
import re
import time
import streamlit as st
from dotenv import load_dotenv

from level import COURSES_BY_LEVEL, FIXED_OPTIONS, parse_questions, calculate_level
from gigachat_client import call_gigachat
from stepik import search_stepik_courses
from habr import search_habr_articles
from urllib.parse import quote

load_dotenv()
if not os.getenv("GIGACHAT_AUTH_KEY"):
    st.error("⛔ GIGACHAT_AUTH_KEY не найден. Создай файл .env и добавь ключ GigaChat.")
    st.stop()

DEFAULT_STATE = [
    ("step", "input"), ("goal", ""), ("hours", 10),
    ("months", 3), ("budget", 0), ("questions", []),
    ("level", ""), ("qa_text", ""), ("realism_warning", ""),
    ("institutional_warning", ""), ("hours_blocked", False),
    ("track_result", ""), ("stepik_cache", []), ("stages", []),
    ("last_submit_time", 0.0), ("current_stage", 0), ("abstract_warning", ""),
    ("platform_courses_cache", []), ("habr_cache", []),
]

COOLDOWN = 15

BUDGET_INFO = {
    0:     "**Бесплатно** — Rutube, бесплатные курсы на Stepik, открытая документация и GitHub. Прогресс возможен, но медленнее: меньше структуры и обратной связи.",
    2500:  "**До 2 500 ₽/мес** — отдельные курсы на Stepik (500–1 500 ₽ за курс), электронные книги, недорогие подписки. Хватит на 1–2 полноценных курса в месяц.",
    5000:  "**До 5 000 ₽/мес** — большинство курсов на российских платформах (Stepik, Hexlet, Яндекс Практикум базовый), 1–2 сессии с ментором, профессиональные инструменты и подписки.",
    10000: "**До 10 000 ₽/мес** — полноценные программы Skillbox, GeekBrains, Яндекс Практикум, регулярный менторинг, онлайн-конференции и интенсивы. Максимальный выбор форматов.",
}

PLATFORM_SEARCH_URLS = {
    "Яндекс Практикум": "https://practicum.yandex.ru/catalog/?text={}",
    "Skillbox":         "https://skillbox.ru/search/?q={}",
    "GeekBrains":       "https://gb.ru/search?query={}",
    "Hexlet":           "https://ru.hexlet.io/search?q={}",
}

BADGES = {
    "Полный новичок": ("🔰", "Новичок",   "Ты в начале пути — самое интересное впереди!"),
    "Базовые знания": ("📚", "Изучающий", "Есть база — теперь время её закрепить."),
    "Средний уровень":("⚡", "Практик",   "Уже есть опыт — пора выходить на новый уровень."),
    "Продвинутый":    ("🏆", "Эксперт",   "Ты в топе — время делиться знаниями с другими."),
}


def _call_ai(prompt: str) -> str | None:
    try:
        return call_gigachat(prompt)
    except Exception as e:
        if "RateLimitError" in type(e).__name__:
            st.error("Слишком много запросов к ИИ. Подожди немного и попробуй снова.")
        else:
            st.error(f"Ошибка при обращении к ИИ: {type(e).__name__}. Попробуй ещё раз.")
        return None


def generate_questions():
    questions_prompt = f"""Ты — эксперт по диагностике уровня знаний. Сгенерируй РОВНО 4 вопроса для оценки уровня пользователя по теме: "{st.session_state.goal}".

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

    with st.spinner("Составляем вопросы для диагностики..."):
        raw = _call_ai(questions_prompt)
        if raw is not None:
            questions = parse_questions(raw)
            if questions:
                st.session_state.questions = questions
                st.session_state.step = "quiz"
                st.rerun()
            else:
                st.error("Не удалось сгенерировать вопросы. Попробуй ещё раз.")


for key, default in DEFAULT_STATE:
    if key not in st.session_state:
        st.session_state[key] = default


st.set_page_config(page_title="Прогрессоры", page_icon="🚀", layout="centered")
st.title("🚀 Прогрессоры")
st.subheader("Персональный трек онлайн-обучения")

# ── Шаг 1: ввод данных ──────────────────────────────────────────────────────
if st.session_state.step == "input":
    with st.form("user_profile"):
        st.markdown("### Расскажи о себе")
        goal = st.text_input("Чему хочешь научиться?", placeholder="Например: Python-разработка, ML, веб-дизайн", max_chars=100)
        hours = st.slider("Сколько часов в неделю готов учиться?", 1, 40, 10)
        months = st.slider("За сколько месяцев хочешь достичь цели?", 1, 12, 3)
        budget = st.select_slider(
            "Бюджет на обучение в месяц",
            options=[0, 2500, 5000, 10000],
            value=0,
            format_func=lambda x: "Бесплатно" if x == 0 else f"{x} ₽"
        )
        submitted = st.form_submit_button("Пройти диагностику 🎯")

    if submitted:
        now = time.time()
        elapsed = now - st.session_state.last_submit_time
        if elapsed < COOLDOWN:
            st.warning(f"⏳ Подожди ещё {int(COOLDOWN - elapsed)} сек. перед следующим запросом.")
        elif not goal.strip():
            st.warning("Укажи, чему хочешь научиться!")
        elif not any(c.isalpha() for c in goal):
            st.warning("Цель должна содержать буквы, а не только цифры или символы!")
        else:
            st.session_state.last_submit_time = time.time()
            st.session_state.goal = " ".join(goal.strip().split())
            st.session_state.hours = hours
            st.session_state.months = months
            st.session_state.budget = budget
            st.session_state.realism_warning = ""
            st.session_state.institutional_warning = ""
            st.session_state.abstract_warning = ""
            st.session_state.hours_blocked = False

            st.info(f"💰 {BUDGET_INFO[budget]}")

            total_hours = hours * months * 4

            # Сначала проверяем саму цель, часы — только если цель реалистична
            with st.spinner("Проверяем реалистичность цели..."):
                realism_prompt = f"""Ты — эксперт по оценке карьерных целей. Классифицируй цель пользователя по одной из четырёх категорий.

Цель: "{goal}"

КАТЕГОРИИ:
1. РЕАЛИСТИЧНО — любой конкретный навык, технология, профессия или дисциплина, доступная через обучение: языки программирования (Python, SQL, Java), инструменты (Figma, Photoshop), профессии (дизайнер, маркетолог, хирург, юрист), языки (английский, китайский), творческие навыки (гитара, фотография) и т.д.

2. АБСТРАКТНО — цель не содержит конкретного навыка или предметной области, по которой можно составить учебный план. Два случая:
   а) Размытые жизненные цели без конкретики: "стать лучше", "быть успешным", "развиваться", "жить счастливо", "стать богатым".
   б) "Стать + предмет/инструмент/объект/вещество" — когда после "стать" стоит не профессия, а вещь: "стать Python", "стать гитарой", "стать маслом", "стать фотографией". Признак: предмет не является ролью, которую занимает человек.
   НЕ абстрактно: "стать поваром", "стать фотографом", "стать музыкантом", "стать Python-разработчиком" — здесь после "стать" стоит профессия/роль человека.

3. ИНСТИТУЦИОНАЛЬНЫЙ — профессия, требующая государственного отбора, секретного допуска или уникальной физической подготовки, которую НЕЛЬЗЯ пройти самостоятельно: космонавт, военный лётчик-истребитель, пилот Формулы-1, действующий президент страны, профессиональный олимпийский спортсмен высшего уровня.

4. НЕРЕАЛИСТИЧНО — цель физически невозможна для любого человека: летать без снаряжения, телепортироваться, жить вечно, стать суперменом, стать вымышленным персонажем.

Верни ТОЛЬКО одно слово из четырёх: РЕАЛИСТИЧНО, АБСТРАКТНО, ИНСТИТУЦИОНАЛЬНЫЙ или НЕРЕАЛИСТИЧНО.
Если АБСТРАКТНО — на следующей строке полное предложение-подсказка: что именно стоит указать вместо этого. Пример формата: «Попробуй уточнить цель — например, стать колумнистом, научиться писать тексты или освоить журналистику.»
Если ИНСТИТУЦИОНАЛЬНЫЙ — на следующей строке одно предложение: что конкретно требует эта профессия.
Если НЕРЕАЛИСТИЧНО — на следующей строке одно предложение: почему физически невозможно.
Не добавляй ничего лишнего."""

                raw = _call_ai(realism_prompt)
                if raw:
                    lines = raw.strip().splitlines()
                    first_line = lines[0].strip()
                    explanation = lines[1].strip() if len(lines) > 1 else ""

                    if "НЕРЕАЛИСТИЧНО" in first_line:
                        st.session_state.realism_warning = explanation or "Цель физически невозможна для человека."
                    elif "АБСТРАКТНО" in first_line:
                        st.session_state.abstract_warning = explanation or "Уточни цель — укажи конкретный навык или профессию."
                    elif "ИНСТИТУЦИОНАЛЬНЫЙ" in first_line:
                        st.session_state.institutional_warning = explanation or "Эта профессия требует официального государственного отбора."
                    else:
                        # Цель реалистична — теперь проверяем параметры
                        # Слой 1: жёсткий блок по времени
                        if total_hours < 8:
                            st.session_state.hours_blocked = True
                        else:
                            # Слой 2: мягкое предупреждение, флоу продолжается
                            if total_hours < 20:
                                st.warning(f"⚠️ Суммарно {total_hours} ч за весь срок — это мало. Прогресс будет медленным, но реальным.")
                            generate_questions()
                else:
                    # GigaChat недоступен — проверяем только часы
                    if total_hours < 8:
                        st.session_state.hours_blocked = True
                    else:
                        if total_hours < 20:
                            st.warning(f"⚠️ Суммарно {total_hours} ч за весь срок — это мало. Прогресс будет медленным, но реальным.")
                        generate_questions()

    # Слой 1: жёсткий блок по времени
    if st.session_state.hours_blocked:
        total_hours = st.session_state.hours * st.session_state.months * 4
        st.error(
            f"⛔ Слишком мало времени: {total_hours} ч за весь срок "
            f"({st.session_state.hours} ч/нед × {st.session_state.months} мес). "
            f"Для реального результата нужно минимум 8 часов суммарно."
        )
        if st.button("Скорректировать параметры"):
            st.session_state.hours_blocked = False
            st.rerun()

    # Слой 3: институциональная профессия
    if st.session_state.institutional_warning:
        st.info(
            f"ℹ️ {st.session_state.institutional_warning} "
            f"Онлайн-трек покажет теоретическую базу и смежные навыки, "
            f"но путь в профессию лежит через официальные институты."
        )
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Посмотреть трек всё равно"):
                st.session_state.institutional_warning = ""
                generate_questions()
        with col2:
            if st.button("Скорректировать цель"):
                st.session_state.institutional_warning = ""
                st.rerun()

    # Слой 3: абстрактная цель
    if st.session_state.abstract_warning:
        st.warning(f"⚠️ Цель слишком абстрактна. {st.session_state.abstract_warning}")
        if st.button("Скорректировать цель"):
            st.session_state.abstract_warning = ""
            st.rerun()

    # Слой 4: физически невозможная цель
    if st.session_state.realism_warning:
        st.warning(f"⚠️ {st.session_state.realism_warning}")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Продолжить всё равно"):
                st.session_state.realism_warning = ""
                generate_questions()
        with col2:
            if st.button("Скорректировать параметры"):
                st.session_state.realism_warning = ""
                st.rerun()

# ── Шаг 2: диагностика ──────────────────────────────────────────────────────
elif st.session_state.step == "quiz":
    st.markdown("### 🎯 Диагностика уровня")
    st.markdown(f"**Тема:** {st.session_state.goal}")
    st.markdown("Ответь на вопросы — ИИ определит твой уровень автоматически.")
    st.markdown("---")
    if st.button("← Назад"):
        st.session_state.step = "input"
        st.session_state.questions = []
        st.rerun()

    for i, q in enumerate(st.session_state.questions):
        st.radio(f"**{i + 1}. {q['question']}**", FIXED_OPTIONS, key=f"q_{i}")

    st.markdown("---")
    if st.button("Определить мой уровень ➡️"):
        answers = [st.session_state.get(f"q_{i}", FIXED_OPTIONS[0]) for i in range(len(st.session_state.questions))]
        qa_lines = []
        for i, q in enumerate(st.session_state.questions):
            qa_lines.append(f"Вопрос {i + 1}: {q['question']}\nОтвет: {answers[i]}")

        st.session_state.level = calculate_level(answers)
        st.session_state.qa_text = "\n\n".join(qa_lines)
        st.session_state.step = "result"
        st.rerun()

# ── Шаг 3: результат ────────────────────────────────────────────────────────
elif st.session_state.step == "result":
    goal = st.session_state.goal
    level = st.session_state.level
    hours = st.session_state.hours
    months = st.session_state.months
    budget = st.session_state.budget
    weeks = months * 4
    limit = COURSES_BY_LEVEL[level]

    emoji, badge_title, badge_desc = BADGES[level]

    st.markdown(f"### 🎯 Цель: {goal}")
    st.markdown(
        f"""<div style="background:#1e1e2e;border-radius:12px;padding:16px 20px;display:flex;align-items:center;gap:16px;margin-bottom:8px">
        <span style="font-size:2.5rem">{emoji}</span>
        <div>
            <div style="font-size:1.1rem;font-weight:700;color:#fff">{badge_title}</div>
            <div style="color:#aaa;font-size:0.9rem">{badge_desc}</div>
        </div>
        </div>""",
        unsafe_allow_html=True
    )
    st.success(f"Твой уровень: **{level}**")

    col1, col2, col3 = st.columns(3)
    col1.metric("Часов в неделю", f"{hours} ч")
    col2.metric("Срок", f"{months} мес")
    col3.metric("Бюджет", "Бесплатно" if budget == 0 else f"{budget} ₽/мес")

    rutube_url = f"https://rutube.ru/search/?query={quote(goal)}"

    if not st.session_state.track_result:
        prompt = f"""Ты — персональный ИИ-навигатор по обучению. Составь детальный трек обучения для пользователя.

ОТКАЗ: Откажись ТОЛЬКО если цель не содержит ни одного конкретного навыка, профессии или предметной области (например: "стать лучше", "быть успешным", "жить счастливо"). Любую профессию, ремесло или учебную дисциплину — принимай.
Названия языков программирования, технологий, дисциплин и инструментов (Python, SQL, Figma, фотография, гитара) — всегда принимай как конкретную цель.
При отказе напиши ТОЛЬКО: "Пожалуйста, укажи конкретный навык или профессию. Например: Python-разработка, UX-дизайн, английский язык."

ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:
- Цель: {goal}
- Уровень: {level}
- Время: {hours} ч/нед → срок {months} мес ({weeks} нед)
- Бюджет: {budget} руб/мес (0 = только бесплатное)

Ответы диагностики:
{st.session_state.qa_text}

A/B → пробел в знаниях, включи в трек. C/D → уже владеет, только углубляй, не начинай с азов.

УРОВЕНЬ ПОЛЬЗОВАТЕЛЯ: {level}
- Полный новичок — объясняй термины, темп медленный; бесплатные курсы, тренажёры, уроки до 15 мин
- Базовые знания — закрепляй основы, темп умеренный; структурированные курсы, первые pet-проекты
- Средний уровень — реальные проекты, темп активный; хакатоны, code review, open source
- Продвинутый — экспертиза и вклад в сообщество, темп интенсивный; книги, конференции, преподавание

Тон: простой и ободряющий для новичка, лаконичный и технический для продвинутого.

ОГРАНИЧЕНИЯ:
Платформы (только без VPN): Яндекс Практикум, GeekBrains, Skillbox, Hexlet, Habr, Rutube.
ЗАПРЕЩЕНО упоминать: Coursera, Udemy, edX, Skillshare, LinkedIn Learning, Khan Academy — недоступны из РФ.
Если без иностранного ресурса не обойтись — добавь пометку [⚠️ нужен VPN или альтернативная оплата].
Нагрузка: {hours} ч/нед, минимум 40% времени — практика. Ориентируйся на {weeks} недель (±20%).
Бюджет: строго {budget} руб/мес. При 0 — только бесплатные материалы.
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

        with st.spinner("ИИ строит твой персональный маршрут..."):
            result = _call_ai(prompt)
            if result is None:
                st.stop()
            st.session_state.track_result = result
            st.session_state.stages = re.findall(r"##[^\n]*Этап \d+: (.+)", result)

    result = st.session_state.track_result
    stages = st.session_state.stages

    if not st.session_state.stepik_cache:
        stepik_query = goal
        with st.spinner("Ищем курсы на Stepik..."):
            st.session_state.stepik_cache = search_stepik_courses(stepik_query, budget, limit)
    stepik_courses = st.session_state.stepik_cache

    if not st.session_state.platform_courses_cache:
        platform_prompt = f"""Назови 2-3 реальных курса по теме «{goal}» с российских образовательных платформ.
Платформы: Яндекс Практикум, Skillbox, GeekBrains, Hexlet.
Верни ТОЛЬКО список строк в формате:
Платформа | Название курса
Пример:
Яндекс Практикум | Python-разработчик
Skillbox | Python-разработчик с нуля
Только реальные существующие курсы. Без пояснений и лишнего текста."""
        with st.spinner("Ищем курсы на крупных платформах..."):
            raw = _call_ai(platform_prompt)
            courses = []
            if raw:
                for line in raw.strip().splitlines():
                    if "|" in line:
                        parts = line.split("|", 1)
                        if len(parts) == 2:
                            platform = parts[0].strip()
                            course_name = parts[1].strip()
                            for p_name, url_template in PLATFORM_SEARCH_URLS.items():
                                if p_name.lower() in platform.lower():
                                    courses.append({
                                        "platform": p_name,
                                        "course": course_name,
                                        "url": url_template.format(quote(course_name)),
                                    })
                                    break
            st.session_state.platform_courses_cache = courses
    platform_courses = st.session_state.platform_courses_cache

    if not st.session_state.habr_cache:
        with st.spinner("Ищем статьи на Habr..."):
            st.session_state.habr_cache = search_habr_articles(goal)
    habr_articles = st.session_state.habr_cache

    st.markdown("---")
    st.markdown("## Твой персональный трек")

    done = sum(1 for i in range(len(stages)) if st.session_state.get(f"stage_{i}", False))
    first_undone = next((i for i in range(len(stages)) if not st.session_state.get(f"stage_{i}", False)), len(stages))

    if st.session_state.get(f"stage_{st.session_state.current_stage}", False):
        st.session_state.current_stage = first_undone

    if stages:
        st.progress(done / len(stages))
        st.caption(f"{done} из {len(stages)} этапов пройдено")

    parts = re.split(r"\n(?=## )", "\n" + result.strip())
    parts = [p.strip() for p in parts if p.strip()]

    stage_index = 0
    for part in parts:
        if re.match(r"##[^\n]*Этап \d+", part):
            title_match = re.match(r"##[^\n]*Этап \d+: (.+)", part)
            title = title_match.group(1).strip() if title_match else f"Этап {stage_index + 1}"
            content = part[part.index("\n"):].strip() if "\n" in part else ""
            is_done = st.session_state.get(f"stage_{stage_index}", False)
            is_current = stage_index == st.session_state.current_stage

            if is_done:
                bg, border, dot, label = "#0d2b0d", "#4CAF50", "🟢", "Пройдено"
            elif is_current:
                bg, border, dot, label = "#0d1b2b", "#2196F3", "🔵", "Текущий"
            else:
                bg, border, dot, label = "#1a1a2a", "#555555", "⚫", "Впереди"

            st.markdown(
                f"""<div style="background:{bg};border-left:4px solid {border};border-radius:8px;
                padding:14px 18px;margin-bottom:2px;display:flex;justify-content:space-between;align-items:center">
                <span style="font-weight:700;color:#fff">{dot} Этап {stage_index + 1}: {title}</span>
                <span style="color:{border};font-size:0.8rem;font-weight:600">{label}</span>
                </div>""",
                unsafe_allow_html=True
            )

            with st.expander("Подробнее →", expanded=is_current):
                st.markdown(content)
                st.checkbox("Отметить как пройденный", key=f"stage_{stage_index}")
                if not is_done and not is_current:
                    if st.button("Начать этот этап", key=f"start_{stage_index}"):
                        st.session_state.current_stage = stage_index
                        st.rerun()

                if not is_done:
                    st.markdown("---")
                    task_key = f"task_{stage_index}"
                    feedback_key = f"task_feedback_{stage_index}"
                    answer_key = f"task_answer_{stage_index}"

                    if not st.session_state.get(task_key):
                        if st.button("🎯 Получить задание", key=f"get_task_{stage_index}"):
                            task_prompt = f"""Ты — преподаватель по теме «{goal}». Составь одно практическое задание для этапа «{title}».

Уровень пользователя: {level}
Краткое содержание этапа: {content[:400]}

Требования:
- Задание выполнимо текстом — не требует запуска кода или специального ПО
- Проверяет понимание, а не просто знание определений
- Чёткий вопрос или задача с понятным ожиданием ответа

Напиши ТОЛЬКО текст задания, без вводных слов и пояснений."""
                            with st.spinner("Составляем задание..."):
                                task = _call_ai(task_prompt)
                                if task:
                                    st.session_state[task_key] = task.strip()
                                    st.rerun()

                    if st.session_state.get(task_key):
                        st.markdown("**📝 Задание:**")
                        st.info(st.session_state[task_key])
                        st.text_area("Твой ответ:", key=answer_key, height=120)
                        if st.button("✅ Проверить ответ", key=f"check_{stage_index}"):
                            answer = st.session_state.get(answer_key, "").strip()
                            if answer:
                                feedback_prompt = f"""Ты — преподаватель по теме «{goal}». Оцени ответ пользователя.

Уровень пользователя: {level}
Задание: {st.session_state[task_key]}
Ответ пользователя: {answer}

Дай фидбек в 3-4 предложениях: что верно, что можно улучшить, ободряющий итог.
Тон: поддерживающий и конкретный. Не начинай с общих слов типа «Отлично!»."""
                                with st.spinner("Проверяем ответ..."):
                                    feedback = _call_ai(feedback_prompt)
                                    if feedback:
                                        st.session_state[feedback_key] = feedback.strip()
                                        st.rerun()
                            else:
                                st.warning("Введи ответ перед проверкой.")

                        if st.session_state.get(feedback_key):
                            st.markdown("**💬 Фидбек:**")
                            st.success(st.session_state[feedback_key])

            if stage_index < len(stages) - 1:
                st.markdown(
                    '<div style="display:flex;justify-content:center;margin:2px 0">'
                    '<div style="width:3px;height:20px;background:#333;border-radius:2px"></div>'
                    '</div>',
                    unsafe_allow_html=True
                )

            stage_index += 1
        else:
            st.markdown(part)

    if stages and done == len(stages):
        st.success("🎉 Поздравляем! Ты прошёл весь трек. Время двигаться дальше!")

    if stepik_courses:
        st.markdown("---")
        st.markdown("## 📚 Курсы на Stepik по твоей теме")
        for course in stepik_courses:
            price_text = "бесплатно" if course["price"] == 0 else f"{course['price']} руб"
            st.markdown(f"- [{course['title']}]({course['url']}) — {price_text}")
    else:
        st.info("Курсы на Stepik по данной теме не найдены.")

    if platform_courses:
        st.markdown("---")
        st.markdown("## 🎓 Курсы на крупных платформах")
        for c in platform_courses:
            st.markdown(f"- [{c['platform']} — {c['course']}]({c['url']})")

    if habr_articles:
        st.markdown("---")
        st.markdown("## 📰 Статьи на Habr")
        for article in habr_articles:
            st.markdown(f"- [{article['title']}]({article['url']})")

    st.markdown("---")
    st.markdown("## 🎬 Видео на Rutube")
    st.markdown(f"[🔍 Найти видео по теме «{goal}» на Rutube]({rutube_url})")

    st.markdown("---")
    if st.button("🔄 Начать заново"):
        for key, default in DEFAULT_STATE:
            st.session_state[key] = default
        for key in list(st.session_state.keys()):
            if key.startswith("stage_") or key.startswith("task_"):
                del st.session_state[key]
        st.session_state.platform_courses_cache = []
        st.session_state.habr_cache = []
        st.rerun()
