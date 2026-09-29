"""FastAPI 서버(PWA 입력 · 발행 도우미) + 스케줄러 + CLI.

  python main.py serve              서버 + 스케줄러(월·수 21:00 생성, 매일 순위 추적)
  python main.py style              data/my_posts/*.txt → 문체 프로필
  python main.py prepare ID         사실·키워드 다시 수집
  python main.py generate [ID]      지금 생성 (ID 없으면 큐 1순위)
  python main.py track              RSS 발행 확인 + 순위 기록
"""
import datetime as dt
import hmac
import json
import re
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import media
import pipeline
import store
from categories import CATEGORIES, DISCLOSURE, INTERVIEW
from store import CFG, DATA, ROOT

SPONSOR = ["내돈내산", *DISCLOSURE]


@asynccontextmanager
async def lifespan(app):
    s = CFG.get("schedule", {})
    tz = s.get("timezone", "Asia/Seoul")
    sched = BackgroundScheduler(timezone=tz)
    sched.add_job(pipeline.generate, CronTrigger.from_crontab(s.get("generate", "0 21 * * mon,wed"), timezone=tz))
    sched.add_job(pipeline.track, CronTrigger.from_crontab(s.get("track", "0 10 * * *"), timezone=tz))
    sched.start()
    yield
    sched.shutdown()


app = FastAPI(lifespan=lifespan)
app.mount("/files", StaticFiles(directory=DATA / "posts"), name="files")


@app.middleware("http")
async def auth(request: Request, call_next):
    """auth_token 이 있으면 ?token= 또는 쿠키 필수 (Cloudflare Tunnel 등 공개 URL용). Tailscale 전용이면 비워둠."""
    tok = CFG.get("auth_token") or ""
    q = request.query_params.get("token", "")
    if tok and not any(hmac.compare_digest(tok.encode(), x.encode()) for x in (q, request.cookies.get("token", ""))):
        return JSONResponse({"error": "unauthorized"}, 401)
    resp = await call_next(request)
    if tok and q:
        resp.set_cookie("token", tok, max_age=31536000, httponly=True, samesite="lax")
    return resp


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/post/{pid}")
def helper_page(pid: int):
    return FileResponse(ROOT / "static" / "post.html")


@app.get("/manifest.webmanifest")
def manifest():
    return {"name": "블로그 경험 입력", "short_name": "블로그", "start_url": "/", "display": "standalone",
            "background_color": "#fafafa", "theme_color": "#03c75a"}


@app.get("/api/templates")
def templates():
    return {"categories": CATEGORIES, "interview": INTERVIEW, "sponsorship": SPONSOR}


@app.get("/api/posts")
def list_posts():
    return [{"id": p["id"], "status": p["status"], "category": p["category"], "visit_date": p["visit_date"],
             "title": (p.get("final") or {}).get("title") or (p.get("keywords") or {}).get("main") or "",
             "score": (p.get("report") or {}).get("score"), "error": p.get("error")} for p in reversed(store.posts())][:30]


@app.get("/api/posts/{pid}")
def get_post(pid: int):
    p = store.load(pid)
    if not p:
        raise HTTPException(404)
    return {**p, "related": pipeline.related(p)}


@app.post("/api/posts")
def create(bg: BackgroundTasks, category: str = Form(...), visit_date: str = Form(...), urls: str = Form(...),
           sponsorship: str = Form("내돈내산"), mode: str = Form("큐"), memo: str = Form(""),
           q1: str = Form(""), q2: str = Form(""), q3: str = Form(""), q4: str = Form(""), q5: str = Form(""),
           photo_meta: str = Form("[]"), photos: List[UploadFile] = File(...),
           videos: Optional[List[UploadFile]] = File(None)):
    """입력: 카테고리 · 지도 URL(공유 텍스트, 여행은 여러 개) · 방문일 · 사진 · 영상 · 5문항 인터뷰/메모 · 대가성 · 발송 모드.
    photo_meta: iOS 단축어가 보내는 [{"taken_at","lat","lon"}, …] (사진 순서대로, 선택)."""
    places = [x.strip() for x in re.findall(r"[\s\S]*?https?://\S+", urls)]
    try:
        dt.date.fromisoformat(visit_date)
        meta = json.loads(photo_meta or "[]")
    except ValueError:
        raise HTTPException(400, "visit_date(YYYY-MM-DD) 또는 photo_meta(JSON) 형식 오류")
    if category not in CATEGORIES or sponsorship not in SPONSOR or mode not in ("큐", "즉시") or not places:
        raise HTTPException(400, "category · sponsorship · mode · 지도 URL 을 확인하세요")
    p = {"status": "new", "created_at": pipeline._now(), "category": category, "visit_date": visit_date,
         "sponsorship": sponsorship, "mode": mode, "urls": places, "memo": memo.strip(),
         "interview": [{"q": q, "a": a.strip()} for q, a in zip(INTERVIEW, (q1, q2, q3, q4, q5))], "photos": [], "videos": []}
    pid = store.save(p)
    d = store.post_dir(pid)
    for i, f in enumerate(photos, 1):
        try:
            info = media.save_photo(f.file.read(), d / "photos" / f"{i:02d}.jpg", meta[i - 1] if i <= len(meta) else None)
        except Exception:
            raise HTTPException(400, f"사진 {i} ({f.filename}) 을 읽을 수 없습니다")
        p["photos"].append({"no": i, "file": f"{i:02d}.jpg", **info})
    for i, f in enumerate(videos or [], 1):
        ext = Path(f.filename or "").suffix.lower()
        name = f"v{i}{ext if ext in ('.mov', '.mp4', '.m4v') else '.mp4'}"
        with open(d / name, "wb") as out:
            shutil.copyfileobj(f.file, out)
        p["videos"].append(name)
    store.save(p)
    bg.add_task(pipeline.prepare, pid)
    return {"id": pid}


if __name__ == "__main__":
    cmd = sys.argv[1:] or ["serve"]
    if cmd[0] == "serve":
        import uvicorn
        srv = CFG.get("server", {})
        uvicorn.run(app, host=srv.get("host", "0.0.0.0"), port=srv.get("port", 8000))
    elif cmd[0] == "style":
        pipeline.build_style()
    elif cmd[0] == "prepare":
        pipeline.prepare(int(cmd[1]))
    elif cmd[0] == "generate":
        print(pipeline.generate(int(cmd[1]) if len(cmd) > 1 else None))
    elif cmd[0] == "track":
        pipeline.track()
    else:
        print(__doc__)
