import asyncio
import hashlib
import hmac
import json
import logging
import os
import urllib.parse
from pathlib import Path

import requests as _requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from supabase_client import get_track, update_progress as supabase_update_progress, update_stages as supabase_update_stages

logger = logging.getLogger(__name__)

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


@app.get("/api/webapp/track")
async def webapp_get_track(x_init_data: str = Header(...)):
    user_id = _verify_init_data(x_init_data)
    track = await asyncio.to_thread(get_track, user_id)
    if not track:
        raise HTTPException(404, "Трек не найден. Пройди онбординг в боте.")
    return track


def _notify_bot(user_id: int, completed: list[int], current_stage: int) -> None:
    token = BOT_TOKEN
    if not token:
        return
    done_idx = current_stage - 1
    track = get_track(user_id)
    stages = (track or {}).get("stages") or []
    total = len(stages)
    if done_idx >= 0 and done_idx < total:
        done_title = stages[done_idx].get("title", f"Этап {done_idx + 1}")
        text = f"✅ *{done_title}* отмечен пройденным в мини-апп."
    else:
        text = "✅ Прогресс обновлён в мини-апп."

    if current_stage < total:
        next_title = stages[current_stage].get("title", f"Этап {current_stage + 1}")
        text += f"\n\nСледующий: *{next_title}*"
        keyboard = {"inline_keyboard": [[{"text": "➡️ Открыть в боте", "callback_data": "webapp_continue"}]]}
    else:
        text += "\n\n🎉 Маршрут завершён!"
        keyboard = {"inline_keyboard": [[{"text": "🏁 Завершить в боте", "callback_data": "webapp_continue"}]]}

    try:
        _requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": user_id, "text": text, "parse_mode": "Markdown", "reply_markup": keyboard},
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"Bot notify failed: {e}")


@app.post("/api/webapp/progress")
async def webapp_update_progress(req: ProgressRequest, x_init_data: str = Header(...)):
    user_id = _verify_init_data(x_init_data)
    await asyncio.to_thread(supabase_update_progress, user_id, req.completed, req.current_stage)
    asyncio.create_task(asyncio.to_thread(_notify_bot, user_id, req.completed, req.current_stage))
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
