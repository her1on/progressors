import os
import time
from openai import OpenAI

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.getenv("CODEX_API_KEY"),
            base_url="https://codex.sale/v1",
            timeout=90,
        )
    return _client


def call_gigachat(prompt: str) -> str:
    client = _get_client()
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-5.5",
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
