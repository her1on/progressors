import requests
import re


def search_habr_articles(query: str, limit: int = 3) -> list[dict]:
    try:
        r = requests.get(
            "https://habr.com/ru/rss/search/posts/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        r.raise_for_status()
    except Exception:
        return []

    PROMO_KEYWORDS = ("вебинар", "открытый урок", "регистрация", "старт потока", "старт курса")

    items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
    results = []
    for item in items:
        title_m = re.search(r"<title><!\[CDATA\[(.+?)\]\]></title>", item)
        link_m = re.search(r"<link>(https://habr\.com[^<]+)</link>", item)
        if not title_m or not link_m:
            continue
        title = title_m.group(1).strip()
        url = link_m.group(1).replace("&amp;", "&").split("?utm_")[0]

        if any(kw in title.lower() for kw in PROMO_KEYWORDS):
            continue

        results.append({"title": title, "url": url})
        if len(results) >= limit:
            break

    return results
