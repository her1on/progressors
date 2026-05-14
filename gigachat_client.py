import os
import streamlit as st
from gigachat import GigaChat
from dotenv import load_dotenv

load_dotenv()


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
