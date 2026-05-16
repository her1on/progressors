import requests


def search_stepik_courses(query: str, budget: int, limit: int = 5) -> list[dict[str, str | int]]:
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
            "learners": learners
        })

    paid = sorted([c for c in filtered if c["price"] > 0], key=lambda c: c["price"])
    free = sorted([c for c in filtered if c["price"] == 0], key=lambda c: c["learners"], reverse=True)
    result = paid + free

    return [
        {"title": c["title"], "url": c["url"], "price": c["price"]}
        for c in result[:limit]
    ]
