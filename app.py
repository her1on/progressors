import json
import os
import re
import requests
import streamlit as st
from gigachat import GigaChat
from dotenv import load_dotenv

load_dotenv()

for key, default in [
    ("step", "input"), ("goal", ""), ("hours", 10),
    ("months", 3), ("budget", 0), ("questions", []),
    ("level", ""), ("qa_text", "")
]:
    if key not in st.session_state:
        st.session_state[key] = default

COURSES_BY_LEVEL = {
    "Полный новичок": 5,
    "Базовые знания": 4,
    "Средний уровень": 3,
    "Продвинутый": 2
}

VALID_LEVELS = ["Полный новичок", "Базовые знания", "Средний уровень", "Продвинутый"]


def parse_questions(raw: str) -> list:
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip().replace("```", "")
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group())
        if not isinstance(data, list) or not data:
            return []
        for item in data:
            if "question" not in item or "options" not in item:
                return []
        return data
    except json.JSONDecodeError:
        return []


def parse_level(raw: str) -> str | None:
    raw = raw.strip().strip("\"'.,")
    for level in VALID_LEVELS:
        if level in raw:
            return level
    return None


def call_gigachat(prompt: str) -> str | None:
    try:
        with GigaChat(
            credentials=os.getenv("GIGACHAT_AUTH_KEY"),
            scope="GIGACHAT_API_PERS",
            verify_ssl_certs=False
        ) as giga:
            response = giga.chat(prompt)
            return response.choices[0].message.content
    except Exception as e:
        if "RateLimitError" in type(e).__name__:
            st.error("Слишком много запросов к ИИ. Подожди немного и попробуй снова.")
        else:
            st.error(f"Ошибка при обращении к ИИ: {type(e).__name__}. Попробуй ещё раз.")
        return None


def search_stepik_courses(query, budget, limit=5):
    url = "https://stepik.org/api/courses"
    params = {
        "search": query,
        "is_public": True,
        "is_archived": False,
        "page_size": 50
    }
    try:
        response = requests.get(url, params=params, timeout=5)
        courses = response.json().get("courses", [])
    except Exception:
        return []

    result = []
    for course in courses:
        price = float(course.get("price", 0) or 0)
        if budget == 0 and price > 0:
            continue
        if budget > 0 and price > budget:
            continue
        result.append({
            "title": course.get("title"),
            "url": f"https://stepik.org/course/{course.get('id')}/promo",
            "price": int(price)
        })
        if len(result) >= limit:
            break
    return result


st.set_page_config(page_title="Прогрессоры", page_icon="🚀", layout="centered")
st.title("🚀 Прогрессоры")
st.subheader("Персональный трек онлайн-обучения")

# ── Шаг 1: ввод данных ──────────────────────────────────────────────────────
if st.session_state.step == "input":
    with st.form("user_profile"):
        st.markdown("### Расскажи о себе")
        goal = st.text_input("Чему хочешь научиться?", placeholder="Например: Python-разработка, ML, веб-дизайн")
        hours = st.slider("Сколько часов в неделю готов учиться?", 1, 40, 10)
        months = st.slider("За сколько месяцев хочешь достичь цели?", 1, 12, 3)
        budget = st.slider("Бюджет на обучение в месяц (руб)", 0, 10000, 0, step=500)
        submitted = st.form_submit_button("Пройти диагностику 🎯")

    if submitted:
        if not goal.strip():
            st.warning("Укажи, чему хочешь научиться!")
        elif not any(c.isalpha() for c in goal):
            st.warning("Цель должна содержать буквы, а не только цифры или символы!")
        else:
            st.session_state.goal = goal
            st.session_state.hours = hours
            st.session_state.months = months
            st.session_state.budget = budget

            with st.spinner("Составляем вопросы для диагностики..."):
                prompt = f"""Ты — эксперт по диагностике уровня знаний. Сгенерируй РОВНО 4 вопроса для оценки уровня пользователя по теме: "{goal}".

Вопросы должны быть двух типов (по 2 каждого):

Тип 1 — личный опыт: спроси, что пользователь уже делал или пробовал в этой теме.
Варианты ответов — шкала от "никогда не пробовал" до "делаю регулярно/профессионально".
Пример для CS:GO: "Участвовал ли ты в соревнованиях или рейтинговых матчах?"
A) Никогда не играл в CS:GO
B) Играю иногда для развлечения, без рейтинга
C) Регулярно играю рейтинговые матчи
D) Участвую в турнирах или играю на высоком уровне

Тип 2 — понимание концепций: спроси о знании ключевых понятий темы.
Варианты ответов — от "никогда не слышал" до "уверенно применяю".
Пример для CS:GO: "Насколько ты знаком с понятием spray control?"
A) Впервые слышу этот термин
B) Слышал, но не понимаю как применять
C) Понимаю принцип, иногда использую осознанно
D) Уверенно контролирую отдачу всех основных оружий

Верни ТОЛЬКО валидный JSON без какого-либо текста до или после. Формат строго такой:
[
  {{"question": "Текст вопроса", "options": {{"A": "Вариант A", "B": "Вариант B", "C": "Вариант C", "D": "Вариант D"}}}}
]

Не добавляй пояснений, markdown-блоков, комментариев — только JSON-массив."""

                raw = call_gigachat(prompt)
                if raw is not None:
                    questions = parse_questions(raw)
                    if questions:
                        st.session_state.questions = questions
                        st.session_state.step = "quiz"
                        st.rerun()
                    else:
                        st.error("Не удалось сгенерировать вопросы. Попробуй ещё раз.")

# ── Шаг 2: диагностика ──────────────────────────────────────────────────────
elif st.session_state.step == "quiz":
    st.markdown("### 🎯 Диагностика уровня")
    st.markdown(f"**Тема:** {st.session_state.goal}")
    st.markdown("Ответь на вопросы — ИИ определит твой уровень автоматически.")
    st.markdown("---")

    for i, q in enumerate(st.session_state.questions):
        options = q["options"]
        choices = [f"{k}) {v}" for k, v in options.items()]
        st.radio(f"**{i + 1}. {q['question']}**", choices, key=f"q_{i}")

    st.markdown("---")
    if st.button("Определить мой уровень ➡️"):
        qa_lines = []
        for i, q in enumerate(st.session_state.questions):
            answer = st.session_state.get(f"q_{i}", "")
            qa_lines.append(f"Вопрос {i + 1}: {q['question']}\nОтвет: {answer}")
        qa_text = "\n\n".join(qa_lines)

        level_prompt = f"""Ты — эксперт по диагностике уровня знаний. Проанализируй ответы пользователя по теме "{st.session_state.goal}".

{qa_text}

Верни СТРОГО ОДНО из четырёх значений (без кавычек, без пояснений, одна строка):
Полный новичок
Базовые знания
Средний уровень
Продвинутый"""

        with st.spinner("Определяем твой уровень..."):
            raw = call_gigachat(level_prompt)

        if raw is not None:
            level = parse_level(raw)
            if level:
                st.session_state.level = level
                st.session_state.qa_text = qa_text
                st.session_state.step = "result"
                st.rerun()
            else:
                st.warning("Не удалось определить уровень автоматически. Выбери вручную:")
                manual_level = st.selectbox("Твой уровень", VALID_LEVELS)
                if st.button("Продолжить"):
                    st.session_state.level = manual_level
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

    st.success(f"Твой уровень: **{level}**")

    with st.spinner("Ищем курсы на Stepik..."):
        stepik_courses = search_stepik_courses(goal, budget, limit)

    with st.spinner("ИИ строит твой персональный маршрут..."):
        prompt = f"""Ты — персональный ИИ-навигатор по обучению. Составь детальный трек обучения для пользователя.

ВАЖНО: Откажись составлять трек ТОЛЬКО в двух случаях:
1. Цель физически невозможна ("стать суперменом", "научиться летать без снаряжения")
2. Цель абстрактна и не подразумевает конкретных навыков ("стать богатым", "стать лучше", "стать счастливым")

Реальные профессии и навыки — ВСЕГДА принимай, даже если они амбициозные или высококонкурентные ("пилот Формулы 1", "олимпийский чемпион", "профессиональный музыкант"). Для таких целей составь трек развития конкретных навыков, необходимых для этой профессии.

При отказе напиши ТОЛЬКО: "Пожалуйста, укажи конкретный навык или профессию. Например: Python-разработка, UX-дизайн, английский язык." И больше ничего не добавляй.

УРОВЕНЬ ПОЛЬЗОВАТЕЛЯ — адаптируй трек под один из четырёх уровней:

┌─────────────────┬──────────────────────────────────────────────────────────────┐
│ Полный новичок  │ Никогда не сталкивался с темой. Начинай с самых азов,        │
│                 │ избегай жаргона, объясняй каждый термин. Темп: медленный.     │
├─────────────────┼──────────────────────────────────────────────────────────────┤
│ Базовые знания  │ Слышал основные понятия, возможно пробовал самостоятельно.   │
│                 │ Закрепляй основы + вводи структуру. Темп: умеренный.         │
├─────────────────┼──────────────────────────────────────────────────────────────┤
│ Средний уровень │ Есть практический опыт, понимает основы. Фокус на            │
│                 │ углублении, best practices, реальных проектах. Темп: активный.│
├─────────────────┼──────────────────────────────────────────────────────────────┤
│ Продвинутый     │ Уверенно работает в теме. Фокус на экспертизе, нишевых       │
│                 │ знаниях, менторстве, вкладе в сообщество. Темп: интенсивный. │
└─────────────────┴──────────────────────────────────────────────────────────────┘

ДЛЯ КАЖДОГО УРОВНЯ — разные рекомендации:
- Полный новичок → бесплатные вводные курсы, много практики на тренажёрах, короткие уроки (до 15 мин)
- Базовые знания → структурированные курсы, первые pet-проекты, менторские программы
- Средний уровень → платные углублённые курсы, open source, хакатоны, code review
- Продвинутый → книги, конференции, написание статей, преподавание, сложные проекты

ВАЖНЫЕ ОГРАНИЧЕНИЯ:

1. ДОСТУПНОСТЬ В РФ
   - Только платформы без VPN: Яндекс Практикум, GeekBrains, Skillbox, Hexlet, Habr, YouTube
   - Для иностранных ресурсов — указывай способ оплаты (СБП / крипта / нет российских карт)
   - Если для темы нет подходящего ресурса в РФ — укажи иностранный, но обязательно пометь: [⚠️ нужен VPN или альтернативная оплата]

2. РЕАЛЬНОСТЬ ВЫПОЛНЕНИЯ
   - Время пользователя: {hours} часов в неделю — учитывай при распределении нагрузки по этапам
   - Бюджет пользователя: {budget} руб/мес — подбирай ресурсы строго в рамках бюджета (0 = только бесплатные)
   - Закладывай буфер +20% к срокам
   - Минимум 40% времени — практика
   - Метрики прогресса для каждого этапа: «к концу этапа ты умеешь делать X»

3. СТРУКТУРА ТРЕКА
   - Фазы: основы → практика → проект → результат
   - НЕ добавляй никаких ссылок — они будут добавлены отдельно
   - Обязательный финальный проект или портфолио

Профиль пользователя:
- Цель: {goal}
- Текущий уровень: {level}
- Время: {hours} часов в неделю
- Срок: {months} месяцев ({weeks} недель)
- Бюджет: {budget} руб/мес (0 = только бесплатные ресурсы)

Результаты диагностики (ответы пользователя на вопросы по теме):
{st.session_state.qa_text}

ВАЖНО: используй эти ответы при составлении трека — не включай темы и навыки, которые пользователь явно уже освоил согласно его ответам.

ВАЖНО: суммарная длительность ВСЕХ этапов должна быть ровно {weeks} недель. Не больше и не меньше.

Тон ответа: адаптируй под уровень — простой и ободряющий для новичка, лаконичный и технический для продвинутого.

Составь трек в следующем формате:

## 📍 Этап 1: [Название]
**Длительность:** X недель
**Что изучать:** конкретные темы
**Результат:** что умеешь после этапа

НЕ добавляй раздел "Ресурсы" и никаких ссылок — они будут показаны отдельно.

## 📍 Этап 2: [Название]
...и так далее

В конце добавь раздел:
## 🎯 Итог
Краткое описание карьерных перспектив после полного прохождения трека."""

        result = call_gigachat(prompt)
        if result is None:
            st.stop()

    st.markdown("---")
    st.markdown("## Твой персональный трек")
    st.markdown(result)

    if stepik_courses:
        st.markdown("---")
        st.markdown("## 📚 Курсы на Stepik по твоей теме")
        for course in stepik_courses:
            price_text = "бесплатно" if course["price"] == 0 else f"{course['price']} руб"
            st.markdown(f"- [{course['title']}]({course['url']}) — {price_text}")
    else:
        st.info("Курсы на Stepik по данной теме не найдены.")

    st.markdown("---")
    if st.button("🔄 Начать заново"):
        st.session_state.step = "input"
        st.session_state.questions = []
        st.session_state.level = ""
        st.rerun()
