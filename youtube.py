import os
import requests

_API_KEY = None
SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


def _get_key() -> str | None:
    global _API_KEY
    if _API_KEY is None:
        _API_KEY = os.getenv("YOUTUBE_API_KEY")
    return _API_KEY


def search_youtube_video(query: str, exclude_ids: set[str] | None = None) -> tuple[str, str] | None:
    """Returns (video_url, video_id) for the top non-excluded result, or None on failure."""
    key = _get_key()
    if not key:
        return None
    try:
        resp = requests.get(SEARCH_URL, params={
            "key": key,
            "q": query,
            "part": "snippet",
            "type": "video",
            "maxResults": 5,
            "relevanceLanguage": "ru",
            "order": "viewCount",
        }, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        for item in items:
            video_id = item["id"]["videoId"]
            if exclude_ids and video_id in exclude_ids:
                continue
            return f"https://www.youtube.com/watch?v={video_id}", video_id
    except Exception:
        pass
    return None
