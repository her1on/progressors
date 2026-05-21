import re
import requests


_SCHOOL_RE = re.compile(r'\d+\s*класс', re.IGNORECASE)


def search_stepik_courses(
    query: str,
    budget: int,
    limit: int = 5,
    subject: int | None = None,
    difficulty: str | None = None,
    filter_terms: str | None = None,
) -> list[dict[str, str | int]]:
    url = "https://stepik.org/api/courses"
    params = {
        "search": query,
        "is_public": True,
        "is_archived": False,
        "language": "ru",
        "page_size": 100,
    }
    if subject is not None:
        params["subject"] = subject
    try:
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        courses = response.json().get("courses", [])
    except Exception:
        return []

    filtered = []
    for course in courses:
        price = float(course.get("price", 0) or 0)
        learners = course.get("learners_count") or 0
        title = course.get("title")

        if not title:
            continue
        if price == 0 and learners == 0:
            continue
        if budget == 0 and price > 0:
            continue
        if budget > 0 and price > budget:
            continue

        score = float(course.get("score") or 0)
        filtered.append({
            "title": title,
            "url": f"https://stepik.org/course/{course.get('id')}/promo",
            "price": int(price),
            "learners": learners,
            "score": score,
            "difficulty": (course.get("difficulty") or "").lower(),
        })

    # Исключаем школьные курсы с указанием класса ("2 класс", "10 класс" и т.п.)
    filtered = [c for c in filtered if not _SCHOOL_RE.search(c["title"])]

    # Фильтр релевантности: исключает ложные совпадения по слишком коротким словам
    key_words = [w.lower() for w in (filter_terms or query).split() if len(w) > 4]
    if key_words:
        filtered = [c for c in filtered if any(kw in c["title"].lower() for kw in key_words)]

    if difficulty and any(c["difficulty"] == difficulty for c in filtered):
        filtered = [c for c in filtered if c["difficulty"] == difficulty]

    paid = sorted([c for c in filtered if c["price"] >= 500], key=lambda c: c["price"], reverse=True)[:2]
    free = sorted([c for c in filtered if c["price"] == 0], key=lambda c: c["learners"] * (c["score"] or 1), reverse=True)
    result = paid + free

    return [
        {"title": c["title"], "url": c["url"], "price": c["price"]}
        for c in result[:limit]
    ]
