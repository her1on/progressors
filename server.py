import asyncio
import hashlib
import hmac
import json
import os
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from supabase_client import get_track, update_progress as supabase_update_progress

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

app = FastAPI(title="Прогрессоры Mini App")

# ── Telegram Mini App ─────────────────────────────────────────────────────────

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


@app.get("/api/webapp/track")
async def webapp_get_track(x_init_data: str = Header(...)):
    user_id = _verify_init_data(x_init_data)
    track = await asyncio.to_thread(get_track, user_id)
    if not track:
        raise HTTPException(404, "Трек не найден. Пройди онбординг в боте.")
    return track


@app.post("/api/webapp/progress")
async def webapp_update_progress(req: ProgressRequest, x_init_data: str = Header(...)):
    user_id = _verify_init_data(x_init_data)
    await asyncio.to_thread(supabase_update_progress, user_id, req.completed, req.current_stage)
    return {"ok": True}


# ── Static files ──────────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def root():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(404, "index.html not found in static/")
    return FileResponse(str(index))
