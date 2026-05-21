import math
import re
import requests


PRIOR_RATING = 4.0  # ожидаемая оценка курса без отзывов
PRIOR_WEIGHT = 5    # вес prior (5 «фантомных» отзывов)
POP_SCALE    = 5    # чем выше — тем меньше влияние популярности

_SCHOOL_RE = re.compile(r'\d+\s*класс', re.IGNORECASE)


def _course_score(learners: int, average: float, count: int) -> float:
    bayesian = (average * count + PRIOR_RATING * PRIOR_WEIGHT) / (count + PRIOR_WEIGHT)
    return bayesian * (1 + math.log10(learners + 1) / POP_SCALE)


def _fetch_review_summaries(ids: list[int]) -> dict[int, dict]:
    """Батч-запрос: {review_summary_id: {average, count}}."""
    if not ids:
        return {}
    try:
        resp = requests.get(
            "https://stepik.org/api/course-review-summaries",
            params={"ids": ",".join(str(i) for i in ids)},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json().get("course-review-summaries", [])
        return {
            s["id"]: {
                "average": float(s.get("average") or 0),
                "count":   int(s.get("count") or 0),
            }
            for s in data
        }
    except Exception:
        return {}


def search_stepik_courses(
    query: str,
    budget: int,
    limit: int = 5,
    subject: int | None = None,
    difficulty: str | None = None,
    filter_terms: str | None = None,
) -> list[dict[str, str | int]]:
    try:
        params: dict = {
            "search": query,
            "is_public": True,
            "is_archived": False,
            "language": "ru",
            "page_size": 100,
        }
        if subject is not None:
            params["subject"] = subject
        response = requests.get("https://stepik.org/api/courses", params=params, timeout=5)
        response.raise_for_status()
        courses = response.json().get("courses", [])
    except Exception:
        return []

    filtered = []
    for course in courses:
        price   = float(course.get("price", 0) or 0)
        learners = course.get("learners_count") or 0
        title   = course.get("title")

        if not title:
            continue
        if price == 0 and learners == 0:
            continue
        if budget == 0 and price > 0:
            continue
        if budget > 0 and price > budget:
            continue

        filtered.append({
            "title":             title,
            "url":               f"https://stepik.org/course/{course.get('id')}/promo",
            "price":             int(price),
            "learners":          learners,
            "difficulty":        (course.get("difficulty") or "").lower(),
            "review_summary_id": course.get("review_summary"),
        })

    # Убираем школьные курсы вида "2 класс", "10 класс"
    filtered = [c for c in filtered if not _SCHOOL_RE.search(c["title"])]

    # Фильтр релевантности по ключевым словам
    key_words = [w.lower() for w in (filter_terms or query).split() if len(w) > 4]
    if key_words:
        filtered = [c for c in filtered if any(kw in c["title"].lower() for kw in key_words)]

    if not filtered:
        return []

    # Фильтр по сложности (если передан и есть хотя бы один подходящий)
    if difficulty and any(c["difficulty"] == difficulty for c in filtered):
        filtered = [c for c in filtered if c["difficulty"] == difficulty]

    # Батч-запрос отзывов и финальный скоринг
    review_ids = [c["review_summary_id"] for c in filtered if c["review_summary_id"]]
    reviews    = _fetch_review_summaries(review_ids)

    for c in filtered:
        rev        = reviews.get(c["review_summary_id"], {})
        c["average"] = rev.get("average", 0.0)
        c["count"]   = rev.get("count", 0)
        c["final_score"] = _course_score(c["learners"], c["average"], c["count"])

    filtered.sort(key=lambda c: c["final_score"], reverse=True)

    return [
        {"title": c["title"], "url": c["url"], "price": c["price"]}
        for c in filtered[:limit]
    ]
