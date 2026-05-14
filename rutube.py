import requests

EDUCATION_CATEGORY_ID = 17


def format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}ч {m}мин"
    return f"{m}мин"


def search_rutube_videos(query: str, limit: int = 3) -> list[dict]:
    try:
        response = requests.get(
            "https://rutube.ru/api/search/video/",
            params={"query": query, "page": 1},
            timeout=8,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Accept-Language": "ru-RU,ru;q=0.9",
            }
        )
        results = response.json().get("results", [])
    except Exception:
        return []

    filtered = []
    for v in results:
        if v.get("is_hidden") or v.get("is_deleted") or v.get("is_locked"):
            continue
        if v.get("is_paid"):
            continue
        if v.get("is_adult") or v.get("is_livestream"):
            continue
        category = v.get("category") or {}
        if category.get("id") != EDUCATION_CATEGORY_ID:
            continue

        filtered.append({
            "title": v.get("title", ""),
            "url": v.get("video_url", ""),
            "author": (v.get("author") or {}).get("name", ""),
            "duration": format_duration(v.get("duration") or 0),
            "hits": v.get("hits") or 0,
        })

    filtered.sort(key=lambda v: v["hits"], reverse=True)

    return [
        {"title": v["title"], "url": v["url"], "author": v["author"], "duration": v["duration"]}
        for v in filtered[:limit]
    ]
