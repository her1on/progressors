import logging
import os
import threading
import time
from openai import OpenAI

logger = logging.getLogger(__name__)

LLM_MODEL = "gpt-5.5"
LLM_MODEL_FALLBACK = "gpt-5.4"

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


def call_llm(prompt: str, model: str = LLM_MODEL, timeout: float | None = None) -> str:
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
        except Exception as e:
            if attempt == 2:
                raise
            logger.warning(f"[LLM] attempt {attempt + 1} failed ({model}): {e!r}, retrying in {2 ** attempt}s")
            time.sleep(2 ** attempt)
