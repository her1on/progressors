import requests


def search_stepik_courses(query: str, budget: int, limit: int = 5) -> list[dict[str, str | int]]:
    url = "https://stepik.org/api/courses"
    params = {
        "search": query,
        "is_public": True,
        "is_archived": False,
        "language": "ru",
        "page_size": 50
    }
    try:
        response = requests.get(url, params=params, timeout=5)
        courses = response.json().get("courses", [])
    except Exception:
        return []

    result = []
    for course in courses:
        price = float(course.get("price", 0) or 0)
        if budget == 0 and price > 0:
            continue
        if budget > 0 and price > budget:
            continue
        result.append({
            "title": course.get("title"),
            "url": f"https://stepik.org/course/{course.get('id')}/promo",
            "price": int(price)
        })
        if len(result) >= limit:
            break
    return result
