import asyncio
import hashlib
import hmac
import json
import os
import re
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from supabase_client import get_track, update_progress as supabase_update_progress, update_stages as supabase_update_stages
from youtube import search_youtube_video
from stepik import search_stepik_courses

_yt_cache: dict[str, tuple[str, str]] = {}
_stepik_cache: dict[str, list[dict]] = {}

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

app = FastAPI(title="Прогрессоры Mini App")

# ── Telegram Mini App ───────────────────────────────────────────────────────

def _verify_init_data(init_data: str) -> int:
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN not configured")
    parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Missing hash in initData")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(401, "Invalid initData signature")
    user_data = json.loads(parsed.get("user", "{}"))
    user_id = user_data.get("id")
    if not user_id:
        raise HTTPException(401, "No user_id in initData")
    return int(user_id)


class ProgressRequest(BaseModel):
    completed: list[int]
    current_stage: int


class StageFeedbackRequest(BaseModel):
    stage_idx: int
    feedback: str  # "liked" или "disliked"


_SEARCH_URLS = {
    "youtube": "https://www.youtube.com/results?search_query={}",
    "stepik":  "https://stepik.org/catalog?q={}",
}

async def _parse_materials_with_links(materials: str) -> list[dict]:
    result = []
    yt_count = 0
    for line in materials.split("\n"):
        m = re.match(r"\[([^\]\n]+)\]\s+([^\n]+)", line.strip())
        if not m:
            continue
        source = m.group(1).strip()
        if source.lower() == "stepik":
            continue  # добавляется отдельно через Stepik API
        rest = m.group(2).strip()
        title = rest.split(" — ")[0].strip()
        base = _SEARCH_URLS.get(source.lower())
        if not base or not title:
            continue
        if source.lower() == "youtube":
            if yt_count >= 2:
                continue
            url = base.format(urllib.parse.quote_plus(title))
            thumb = None
            if title in _yt_cache:
                url, vid_id = _yt_cache[title]
                thumb = f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg"
            else:
                try:
                    res = await asyncio.to_thread(search_youtube_video, title)
                    if res:
                        url, vid_id = res
                        _yt_cache[title] = (url, vid_id)
                        thumb = f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg"
                except Exception:
                    pass
            yt_count += 1
            result.append({"source": source, "title": title, "url": url, "thumb": thumb})
    return result


async def _stepik_links_for_stage(stage: dict, goal: str = "") -> list[dict]:
    query = goal or stage.get("title", "")
    if not query:
        return []
    if query in _stepik_cache:
        return _stepik_cache[query]
    try:
        courses = await asyncio.to_thread(search_stepik_courses, query, 0, 2)
        links = [
            {"source": "Stepik", "title": c["title"], "url": c["url"], "thumb": None}
            for c in courses
        ]
    except Exception:
        links = []
    _stepik_cache[query] = links
    return links


@app.get("/api/webapp/track")
async def webapp_get_track(x_init_data: str = Header(...)):
    user_id = _verify_init_data(x_init_data)
    track = await asyncio.to_thread(get_track, user_id)
    if not track:
        raise HTTPException(404, "Трек не найден. Пройди онбординг в боте.")
    stages = track.get("stages") or []
    goal = track.get("goal", "")
    yt_tasks = [_parse_materials_with_links(s.get("materials", "")) for s in stages]
    st_tasks = [_stepik_links_for_stage(s, goal) for s in stages]
    yt_results, st_results = await asyncio.gather(
        asyncio.gather(*yt_tasks),
        asyncio.gather(*st_tasks),
    )
    for s, yt_links, st_links in zip(stages, yt_results, st_results):
        s["materials_links"] = yt_links + st_links
    return track


@app.post("/api/webapp/progress")
async def webapp_update_progress(req: ProgressRequest, x_init_data: str = Header(...)):
    user_id = _verify_init_data(x_init_data)
    await asyncio.to_thread(supabase_update_progress, user_id, req.completed, req.current_stage)
    return {"ok": True}


@app.post("/api/webapp/stage-feedback")
async def webapp_stage_feedback(req: StageFeedbackRequest, x_init_data: str = Header(...)):
    user_id = _verify_init_data(x_init_data)
    if req.feedback not in ("liked", "disliked"):
        raise HTTPException(400, "Invalid feedback value")
    track = await asyncio.to_thread(get_track, user_id)
    if not track:
        raise HTTPException(404, "Track not found")
    stages = track.get("stages") or []
    if req.stage_idx < 0 or req.stage_idx >= len(stages):
        raise HTTPException(400, "Invalid stage index")
    stage = stages[req.stage_idx]
    if stage.get("liked") or stage.get("disliked"):
        raise HTTPException(409, "Already rated")
    stage["liked"] = req.feedback == "liked"
    stage["disliked"] = req.feedback == "disliked"
    await asyncio.to_thread(supabase_update_stages, user_id, stages)
    return {"ok": True}


# ── Статические файлы ────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def root():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(404, "index.html not found in static/")
    return FileResponse(str(index))
