"""설정(config.yaml) · 경로 · SQLite 저장소 (글 1건 = JSON 1행)."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
(DATA / "posts").mkdir(parents=True, exist_ok=True)
(DATA / "my_posts").mkdir(exist_ok=True)

_cfg = ROOT / "config.yaml"
CFG = yaml.safe_load(_cfg.read_text(encoding="utf-8")) or {} if _cfg.exists() else {}


def _db():
    c = sqlite3.connect(DATA / "posts.db")
    c.execute("CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)")
    return c


def save(p):
    with closing(_db()) as c, c:
        if "id" not in p:
            p["id"] = c.execute("INSERT INTO posts (data) VALUES ('{}')").lastrowid
        c.execute("UPDATE posts SET data=? WHERE id=?", (json.dumps(p, ensure_ascii=False), p["id"]))
    return p["id"]


def load(pid):
    with closing(_db()) as c:
        row = c.execute("SELECT data FROM posts WHERE id=?", (pid,)).fetchone()
    return json.loads(row[0]) if row else None


def posts():
    with closing(_db()) as c:
        return [json.loads(r[0]) for r in c.execute("SELECT data FROM posts ORDER BY id")]


def post_dir(pid):
    d = DATA / "posts" / str(pid)
    (d / "photos").mkdir(parents=True, exist_ok=True)
    return d
