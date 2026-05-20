import requests


_DIFFICULTY_RANK = {"easy": 0, "medium": 1, "hard": 2}


def search_stepik_courses(query: str, budget: int, limit: int = 5, difficulty: str | None = None, filter_terms: str | None = None) -> list[dict[str, str | int]]:
    url = "https://stepik.org/api/courses"
    params = {
        "search": query,
        "is_public": True,
        "is_archived": False,
        "language": "ru",
        "page_size": 100
    }
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

        filtered.append({
            "title": title,
            "url": f"https://stepik.org/course/{course.get('id')}/promo",
            "price": int(price),
            "learners": learners,
            "difficulty": (course.get("difficulty") or "").lower(),
        })

    # relevance filter: use filter_terms (full original query) when provided,
    # otherwise fall back to query. This prevents shortened queries like "двойные"
    # from matching "Двойные диаграммы состояния" when searching for "двойные интегралы".
    key_words = [w.lower() for w in (filter_terms or query).split() if len(w) > 4]
    if key_words:
        relevant = [c for c in filtered if any(kw in c["title"].lower() for kw in key_words)]
        if relevant:
            filtered = relevant

    if difficulty and any(c["difficulty"] == difficulty for c in filtered):
        filtered = [c for c in filtered if c["difficulty"] == difficulty]

    paid = sorted([c for c in filtered if c["price"] >= 500], key=lambda c: c["price"], reverse=True)[:2]
    free = sorted([c for c in filtered if c["price"] == 0], key=lambda c: c["learners"], reverse=True)
    result = paid + free

    return [
        {"title": c["title"], "url": c["url"], "price": c["price"]}
        for c in result[:limit]
    ]
