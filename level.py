import json
import re

COURSES_BY_LEVEL = {
    "Полный новичок": 5,
    "Базовые знания": 4,
    "Средний уровень": 3,
    "Продвинутый": 2
}

FIXED_OPTIONS = [
    "A) Никогда не пробовал / не слышал",
    "B) Знаком в теории, но не практиковал",
    "C) Практиковал, есть реальный опыт",
    "D) Занимаюсь на продвинутом/профессиональном уровне"
]


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


def calculate_level(answers: list[str]) -> str:
    score = sum(FIXED_OPTIONS.index(a) for a in answers)
    if score <= 3:
        return "Полный новичок"
    elif score <= 6:
        return "Базовые знания"
    elif score <= 9:
        return "Средний уровень"
    return "Продвинутый"
