import os
from gigachat import GigaChat

_client: GigaChat | None = None


def call_gigachat(prompt: str) -> str:
    global _client
    if _client is None:
        _client = GigaChat(
            credentials=os.getenv("GIGACHAT_AUTH_KEY"),
            scope="GIGACHAT_API_PERS",
            verify_ssl_certs=False,
        )
    try:
        return _client.chat(prompt).choices[0].message.content
    except Exception:
        _client = None
        raise
