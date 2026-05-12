import os
import streamlit as st
from gigachat import GigaChat
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="Прогрессоры", page_icon="🚀", layout="centered")
st.title("🚀 Прогрессоры")
st.subheader("Персональный трек онлайн-обучения")

with st.form("user_profile"):
    st.markdown("### Расскажи о себе")
    goal = st.text_input("Чему хочешь научиться?", placeholder="Например: Python-разработка, ML, веб-дизайн")
    level = st.selectbox("Твой текущий уровень", ["Полный новичок", "Базовые знания", "Средний уровень", "Продвинутый"])
    hours = st.slider("Сколько часов в неделю готов учиться?", 1, 40, 10)
    months = st.slider("За сколько месяцев хочешь достичь цели?", 1, 12, 3)
    submitted = st.form_submit_button("Построить мой трек 🗺️")

if submitted:
    if not goal:
        st.warning("Укажи, чему хочешь научиться!")
    else:
        with st.spinner("ИИ строит твой персональный маршрут..."):
            prompt = f"""Ты — персональный ИИ-навигатор по обучению. Составь детальный трек обучения для пользователя.

Профиль пользователя:
- Цель: {goal}
- Текущий уровень: {level}
- Время: {hours} часов в неделю
- Срок: {months} месяцев

Составь трек в следующем формате:

## 📍 Этап 1: [Название]
**Длительность:** X недель
**Что изучать:** конкретные темы
**Ресурсы:** 2-3 конкретных курса/книги/сайта
**Результат:** что умеешь после этапа
**Карьерные возможности:** какие позиции уже доступны

## 📍 Этап 2: [Название]
...и так далее

В конце добавь раздел:
## 🎯 Итог
Краткое описание карьерных перспектив после полного прохождения трека."""

            with GigaChat(
                credentials=os.getenv("GIGACHAT_AUTH_KEY"),
                scope="GIGACHAT_API_PERS",
                verify_ssl_certs=False
            ) as giga:
                response = giga.chat(prompt)
                result = response.choices[0].message.content

            st.markdown("---")
            st.markdown("## Твой персональный трек")
            st.markdown(result)
