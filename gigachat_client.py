import os
from gigachat import GigaChat


def call_gigachat(prompt: str) -> str:
    with GigaChat(
        credentials=os.getenv("GIGACHAT_AUTH_KEY"),
        scope="GIGACHAT_API_PERS",
        verify_ssl_certs=False
    ) as giga:
        response = giga.chat(prompt)
        return response.choices[0].message.content
