import os
import threading
import time
from openai import OpenAI

_client: OpenAI | None = None
_sem = threading.Semaphore(3)


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.getenv("CODEX_API_KEY"),
            base_url="https://codex.sale/v1",
            timeout=30,
            max_retries=0,
        )
    return _client


def call_llm(prompt: str, model: str = "gpt-5.5", timeout: float | None = None) -> str:
    client = _get_client()
    for attempt in range(3):
        try:
            with _sem:
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    **({"timeout": timeout} if timeout is not None else {}),
                )
            return response.choices[0].message.content
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
