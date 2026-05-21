import json
import logging
import os
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY", "")

_HEADERS_BASE = {
    "Content-Type": "application/json",
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}
_HEADERS_UPSERT = {**_HEADERS_BASE, "Prefer": "resolution=merge-duplicates"}


def _request(method: str, path: str, body: dict | None = None, prefer: str | None = None) -> dict | list | None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error(f"[DB] Supabase not configured: URL={bool(SUPABASE_URL)} KEY={bool(SUPABASE_KEY)}")
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(body).encode() if body else None
    if prefer:
        headers = {**_HEADERS_BASE, "Prefer": prefer}
    elif method == "POST":
        headers = _HEADERS_UPSERT
    else:
        headers = _HEADERS_BASE
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode()
            logger.info(f"[DB] {method} {path} → {resp.status}")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        logger.error(f"[DB] {method} {path} → {e.code}: {body_text}")
        raise RuntimeError(f"Supabase {method} {path}: {e.code} {body_text}")


def save_track(user_id: int, goal: str, level: str, hours: int, months: int,
               goal_scope: str, stages: list, summary: str, skills_text: str = "") -> None:
    _request("POST", "user_tracks", {
        "user_id": user_id,
        "goal": goal,
        "level": level,
        "hours": hours,
        "months": months,
        "goal_scope": goal_scope,
        "stages": stages,
        "summary": summary,
        "skills_text": skills_text,
        "completed": [],
        "current_stage": 0,
    })


def update_progress(user_id: int, completed: list[int], current_stage: int) -> None:
    _request("PATCH", f"user_tracks?user_id=eq.{user_id}", {
        "completed": completed,
        "current_stage": current_stage,
        "updated_at": "now()",
    })


def update_stages(user_id: int, stages: list) -> None:
    _request("PATCH", f"user_tracks?user_id=eq.{user_id}", {
        "stages": stages,
        "updated_at": "now()",
    })


def get_track(user_id: int) -> dict | None:
    result = _request("GET", f"user_tracks?user_id=eq.{user_id}&select=*&order=updated_at.desc&limit=1")
    if result and isinstance(result, list) and len(result) > 0:
        return result[0]
    return None


def save_liked_stage(user_id: int, goal: str, stage_title: str, topics: str, materials: str) -> None:
    """Данные уже сохранены в user_tracks.stages с флагом liked=True — ничего не делаем."""
    pass


def get_liked_stages(user_id: int) -> list[dict]:
    """Извлекает лайкнутые этапы из истории user_tracks."""
    result = _request("GET", f"user_tracks?user_id=eq.{user_id}&select=goal,stages&order=updated_at.desc&limit=10")
    if not isinstance(result, list):
        return []
    liked = []
    for track in result:
        goal = track.get("goal", "")
        for stage in (track.get("stages") or []):
            if stage.get("liked"):
                liked.append({
                    "goal": goal,
                    "stage_title": stage.get("title", ""),
                    "topics": stage.get("topics", ""),
                    "materials": stage.get("materials", ""),
                })
    return liked[:20]


def update_difficulty_bias(user_id: int, direction: str) -> None:
    """Данные уже сохранены в user_tracks.stages с флагом modified — ничего не делаем."""
    pass


def get_difficulty_bias(user_id: int) -> dict:
    """Считает simplified/advanced модификации из истории user_tracks."""
    result = _request("GET", f"user_tracks?user_id=eq.{user_id}&select=stages&order=updated_at.desc&limit=10")
    if not isinstance(result, list):
        return {"hard_count": 0, "easy_count": 0}
    hard_count = sum(
        1 for track in result
        for stage in (track.get("stages") or [])
        if stage.get("modified") == "simplified"
    )
    easy_count = sum(
        1 for track in result
        for stage in (track.get("stages") or [])
        if stage.get("modified") == "advanced"
    )
    return {"hard_count": hard_count, "easy_count": easy_count}
