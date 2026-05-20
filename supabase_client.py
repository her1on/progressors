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


def _request(method: str, path: str, body: dict | None = None) -> dict | None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=_HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase {method} {path}: {e.code} {e.read().decode()}")


def save_track(user_id: int, goal: str, level: str, hours: int, months: int,
               goal_scope: str, stages: list, summary: str) -> None:
    _request("POST", "user_tracks", {
        "user_id": user_id,
        "goal": goal,
        "level": level,
        "hours": hours,
        "months": months,
        "goal_scope": goal_scope,
        "stages": stages,
        "summary": summary,
        "completed": [],
        "current_stage": 0,
    })


def update_progress(user_id: int, completed: list[int], current_stage: int) -> None:
    _request("PATCH", f"user_tracks?user_id=eq.{user_id}", {
        "completed": completed,
        "current_stage": current_stage,
        "updated_at": "now()",
    })


def get_track(user_id: int) -> dict | None:
    result = _request("GET", f"user_tracks?user_id=eq.{user_id}&select=*")
    if result and isinstance(result, list) and len(result) > 0:
        return result[0]
    return None
