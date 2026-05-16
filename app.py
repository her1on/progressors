import os
import re
import time
import streamlit as st
from dotenv import load_dotenv

from level import COURSES_BY_LEVEL, FIXED_OPTIONS, parse_questions, calculate_level
from gigachat_client import call_gigachat
from stepik import search_stepik_courses
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
    ("last_submit_time", 0.0), ("current_stage", 0),
]

COOLDOWN = 15

BUDGET_INFO = {
    0:     "**Бесплатно** — Rutube, бесплатные курсы на Stepik, открытая документация и GitHub. Прогресс возможен, но медленнее: меньше структуры и обратной связи.",
    2500:  "**До 2 500 ₽/мес** — отдельные курсы на Stepik (500–1 500 ₽ за курс), электронные книги, недорогие подписки. Хватит на 1–2 полноценных курса в месяц.",
    5000:  "**До 5 000 ₽/мес** — большинство курсов на российских платформах (Stepik, Hexlet, Яндекс Практикум базовый), 1–2 сессии с ментором, профессиональные инструменты и подписки.",
    10000: "**До 10 000 ₽/мес** — полноценные программы Skillbox, GeekBrains, Яндекс Практикум, регулярный менторинг, онлайн-конференции и интенсивы. Максимальный выбор форматов.",
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
            st.session_state.hours_blocked = False

            st.info(f"💰 {BUDGET_INFO[budget]}")

            total_hours = hours * months * 4

            # Сначала проверяем саму цель, часы — только если цель реалистична
            with st.spinner("Проверяем реалистичность цели..."):
                realism_prompt = f"""Ты — эксперт по оценке карьерных целей. Классифицируй цель пользователя по одной из трёх категорий.

Цель: "{goal}"

КАТЕГОРИИ:
1. РЕАЛИСТИЧНО — любой навык, профессия или карьерный рост, доступный через обучение: программирование, дизайн, маркетинг, иностранные языки, бизнес, медицина, юриспруденция, рабочие специальности и т.д. Сюда входят сложные и долгие пути (стать хирургом, юристом, архитектором, пилотом гражданской авиации).

2. ИНСТИТУЦИОНАЛЬНЫЙ — профессия, требующая государственного отбора, секретного допуска или уникальной физической подготовки, которую НЕЛЬЗЯ пройти самостоятельно ни через какое обучение: космонавт, военный лётчик-истребитель, пилот Формулы-1, действующий президент страны, профессиональный олимпийский спортсмен высшего уровня. Онлайн-трек не откроет путь в саму профессию.

3. НЕРЕАЛИСТИЧНО — цель физически невозможна для любого человека: летать без снаряжения, телепортироваться, жить вечно, стать суперменом, стать вымышленным персонажем.

Верни ТОЛЬКО одно слово из трёх: РЕАЛИСТИЧНО, ИНСТИТУЦИОНАЛЬНЫЙ или НЕРЕАЛИСТИЧНО.
Если ИНСТИТУЦИОНАЛЬНЫЙ — на следующей строке одно предложение: что конкретно требует эта профессия (государственный отбор / секретный допуск / и т.д.).
Если НЕРЕАЛИСТИЧНО — на следующей строке одно предложение: почему физически невозможно.
Не добавляй ничего лишнего."""

                raw = _call_ai(realism_prompt)
                if raw:
                    lines = raw.strip().splitlines()
                    first_line = lines[0].strip()
                    explanation = lines[1].strip() if len(lines) > 1 else ""

                    if "НЕРЕАЛИСТИЧНО" in first_line:
                        st.session_state.realism_warning = explanation or "Цель физически невозможна для человека."
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

    # Слой 3: физически невозможная цель
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

Откажись составлять трек только если цель абстрактна и не подразумевает конкретных навыков ("стать богатым", "стать лучше", "стать счастливым"). Реальные профессии и навыки — всегда принимай.
При отказе напиши ТОЛЬКО: "Пожалуйста, укажи конкретный навык или профессию. Например: Python-разработка, UX-дизайн, английский язык."

УРОВЕНЬ ПОЛЬЗОВАТЕЛЯ: {level}
- Полный новичок — никогда не сталкивался с темой, начинай с азов, объясняй термины, темп медленный. Рекомендуй: бесплатные вводные курсы, тренажёры, короткие уроки до 15 мин.
- Базовые знания — слышал понятия, возможно пробовал. Закрепляй основы, вводи структуру, темп умеренный. Рекомендуй: структурированные курсы, первые pet-проекты, менторские программы.
- Средний уровень — есть практический опыт. Фокус на углублении и реальных проектах, темп активный. Рекомендуй: платные углублённые курсы, open source, хакатоны, code review.
- Продвинутый — уверенно работает в теме. Фокус на экспертизе и вкладе в сообщество, темп интенсивный. Рекомендуй: книги, конференции, написание статей, преподавание.

ВАЖНЫЕ ОГРАНИЧЕНИЯ:

1. ДОСТУПНОСТЬ В РФ
   - Только платформы без VPN: Яндекс Практикум, GeekBrains, Skillbox, Hexlet, Habr, Rutube
   - Для иностранных ресурсов — указывай способ оплаты (СБП / крипта / нет российских карт)
   - Если для темы нет подходящего ресурса в РФ — укажи иностранный, но обязательно пометь: [⚠️ нужен VPN или альтернативная оплата]

2. РЕАЛЬНОСТЬ ВЫПОЛНЕНИЯ
   - Время пользователя: {hours} часов в неделю — учитывай при распределении нагрузки по этапам
   - Бюджет пользователя: {budget} руб/мес — подбирай ресурсы строго в рамках бюджета (0 = только бесплатные)
   - Минимум 40% времени — практика
   - Метрики прогресса для каждого этапа: «к концу этапа ты умеешь делать X»

3. СТРУКТУРА ТРЕКА
   - Фазы: основы → практика → проект → результат
   - НЕ добавляй никаких ссылок — они будут показаны отдельно
   - Обязательный финальный проект или портфолио

Профиль пользователя:
- Цель: {goal}
- Текущий уровень: {level}
- Время: {hours} часов в неделю
- Срок: {months} месяцев ({weeks} недель)
- Бюджет: {budget} руб/мес (0 = только бесплатные ресурсы)

Результаты диагностики (ответы пользователя на вопросы по теме):
{st.session_state.qa_text}

Ответы C ("Практиковал, есть реальный опыт") и D ("Занимаюсь на продвинутом уровне") означают, что пользователь уже владеет этой темой — не включай её в трек с нуля, только углубляй.
Ответы A ("Никогда не пробовал") и B ("Знаком в теории") означают пробел — эти темы включи в трек.

Ориентируйся на {weeks} недель (допустимо ±20%). Тон ответа: простой и ободряющий для новичка, лаконичный и технический для продвинутого.

Составь трек в следующем формате:

## 📍 Этап 1: [Название]
**Длительность:** X недель
**Что изучать:** Перечисли конкретные понятия, техники, инструменты и конструкции — на уровне детализации реальной учебной программы. Не пиши общие категории («основы», «базовые понятия», «грамматика», «инструменты»). Вместо этого называй точные элементы: как они называются, как выглядят, чем отличаются друг от друга. Примеры нужного уровня детализации: Python — «f-strings, list slicing [a:b], методы dict: get(), items(), update()»; английский язык — «Present Perfect vs Past Simple, reported speech, фразовые глаголы go on / give up»; UX-дизайн — «CJM (customer journey map), wireframe в Figma, метод 5 seconds test»; маркетинг — «воронка AIDA, UTM-метки, расчёт CPC и ROI». Придерживайся этого уровня конкретности для любой предметной области.
**Результат:** что умеешь после этапа

НЕ добавляй раздел "Ресурсы" и никаких ссылок — они будут показаны отдельно.

## 📍 Этап 2: [Название]
...и так далее

В конце добавь раздел:
## 🎯 Итог
Краткое описание карьерных перспектив после полного прохождения трека."""

        with st.spinner("ИИ строит твой персональный маршрут..."):
            result = _call_ai(prompt)
            if result is None:
                st.stop()
            st.session_state.track_result = result
            st.session_state.stages = re.findall(r"## 📍 Этап \d+: (.+)", result)

    result = st.session_state.track_result
    stages = st.session_state.stages

    if not st.session_state.stepik_cache:
        stepik_query = stages[0] if stages else goal
        with st.spinner("Ищем курсы на Stepik..."):
            st.session_state.stepik_cache = search_stepik_courses(stepik_query, budget, limit)
    stepik_courses = st.session_state.stepik_cache

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
        if part.startswith("## 📍 Этап"):
            title_match = re.match(r"## 📍 Этап \d+: (.+)", part)
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

    st.markdown("---")
    st.markdown("## 🎬 Видео на Rutube")
    st.markdown(f"[🔍 Найти видео по теме «{goal}» на Rutube]({rutube_url})")

    st.markdown("---")
    if st.button("🔄 Начать заново"):
        for key, default in DEFAULT_STATE:
            st.session_state[key] = default
        for key in list(st.session_state.keys()):
            if key.startswith("stage_"):
                del st.session_state[key]
        st.rerun()
