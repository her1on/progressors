import os
import time
import threading
from gigachat import GigaChat

_local = threading.local()


def call_gigachat(prompt: str) -> str:
    for attempt in range(3):
        if not getattr(_local, "client", None):
            _local.client = GigaChat(
                credentials=os.getenv("GIGACHAT_AUTH_KEY"),
                scope="GIGACHAT_API_PERS",
                verify_ssl_certs=False,
                timeout=90,
            )
        try:
            return _local.client.chat(prompt).choices[0].message.content
        except Exception:
            _local.client = None
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
