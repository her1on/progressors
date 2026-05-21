import json
import re


def parse_questions(raw: str) -> list[dict[str, str]]:
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip().replace("```", "")
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group())
        if not isinstance(data, list) or not data:
            return []
        for item in data:
            if "question" not in item:
                return []
        return data
    except json.JSONDecodeError:
        return []
