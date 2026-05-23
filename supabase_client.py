import json
import logging
import os
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

def _request(method: str, path: str, body: dict | None = None, prefer: str | None = None) -> dict | list | None:
    supabase_url = os.getenv("SUPABASE_URL", "")
    supabase_key = os.getenv("SUPABASE_SECRET_KEY", "")
    if not supabase_url or not supabase_key:
        logger.error(f"[DB] Supabase not configured: URL={bool(supabase_url)} KEY={bool(supabase_key)}")
        return None
    base_headers = {
        "Content-Type": "application/json",
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
    }
    url = f"{supabase_url}/rest/v1/{path}"
    data = json.dumps(body).encode() if body else None
    if prefer:
        headers = {**base_headers, "Prefer": prefer}
    elif method == "POST":
        headers = {**base_headers, "Prefer": "resolution=merge-duplicates"}
    else:
        headers = base_headers
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
    data = {
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
        "updated_at": "now()",
    }
    updated = _request("PATCH", f"user_tracks?user_id=eq.{user_id}", data, prefer="return=representation")
    if updated == [] or updated is None:
        _request("POST", "user_tracks", {"user_id": user_id, **data})


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


def save_liked_stages(user_id: int, goal: str, stages: list) -> None:
    for stage in stages:
        if stage.get("liked"):
            _request("POST", "liked_stages", {
                "user_id": user_id,
                "goal": goal,
                "stage_title": stage.get("title", ""),
                "topics": stage.get("topics", ""),
                "materials": stage.get("materials", ""),
            }, prefer="return=minimal")


def get_liked_stages(user_id: int) -> list[dict]:
    result = _request("GET", f"liked_stages?user_id=eq.{user_id}&select=goal,stage_title,topics,materials&order=id.desc&limit=20")
    if not isinstance(result, list):
        return []
    return [
        {
            "goal": r.get("goal", ""),
            "stage_title": r.get("stage_title", ""),
            "topics": r.get("topics", ""),
            "materials": r.get("materials", ""),
        }
        for r in result
    ]


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
