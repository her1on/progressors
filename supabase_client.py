import json
import os
import urllib.request
import urllib.error

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY", "")

_HEADERS = {
    "Content-Type": "application/json",
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Prefer": "resolution=merge-duplicates",
}


def _request(method: str, path: str, body: dict | None = None, prefer: str | None = None) -> dict | list | None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(body).encode() if body else None
    headers = {**_HEADERS, "Prefer": prefer} if prefer else _HEADERS
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase {method} {path}: {e.code} {e.read().decode()}")


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
    _request("POST", "liked_stages", {
        "user_id": user_id,
        "goal": goal,
        "stage_title": stage_title,
        "topics": topics or "",
        "materials": materials or "",
    }, prefer="return=minimal")


def get_liked_stages(user_id: int) -> list[dict]:
    result = _request("GET", f"liked_stages?user_id=eq.{user_id}&order=created_at.desc&limit=20")
    return result if isinstance(result, list) else []


def update_difficulty_bias(user_id: int, direction: str) -> None:
    """direction: 'hard' или 'easy'."""
    field = "hard_count" if direction == "hard" else "easy_count"
    existing = _request("GET", f"user_preferences?user_id=eq.{user_id}&select={field}")
    if existing and isinstance(existing, list) and existing:
        current = existing[0].get(field, 0) or 0
        _request("PATCH", f"user_preferences?user_id=eq.{user_id}", {
            field: current + 1,
            "updated_at": "now()",
        })
    else:
        _request("POST", "user_preferences", {
            "user_id": user_id,
            "hard_count": 1 if direction == "hard" else 0,
            "easy_count": 1 if direction == "easy" else 0,
        }, prefer="return=minimal")


def get_difficulty_bias(user_id: int) -> dict:
    """Возвращает {'hard_count': int, 'easy_count': int}."""
    result = _request("GET", f"user_preferences?user_id=eq.{user_id}&select=hard_count,easy_count")
    if result and isinstance(result, list) and result:
        return result[0]
    return {"hard_count": 0, "easy_count": 0}
