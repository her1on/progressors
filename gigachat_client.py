import os
import threading
from gigachat import GigaChat

_local = threading.local()


def call_gigachat(prompt: str) -> str:
    if not getattr(_local, "client", None):
        _local.client = GigaChat(
            credentials=os.getenv("GIGACHAT_AUTH_KEY"),
            scope="GIGACHAT_API_PERS",
            verify_ssl_certs=False,
        )
    try:
        return _local.client.chat(prompt).choices[0].message.content
    except Exception:
        _local.client = None
        raise
