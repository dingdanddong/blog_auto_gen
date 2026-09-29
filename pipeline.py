"""파이프라인
prepare (제출 직후, 0토큰): 사실 수집 → 여행 동선 정렬 → 키워드 기획 → 큐
generate (월·수 21:00 또는 즉시): Vision → Writer → 검사 → 조건부 재작성(최대 1회) → 확정 → 클립 → Gmail 알림
track (매일): RSS 로 발행 URL 연결 → D+1/3/7 순위 기록 → 여행 글 발행 시 역링크 알림
"""
import datetime as dt
import difflib
import json
import re
import smtplib
import threading
import traceback
from collections import Counter
from email.message import EmailMessage

import inspector
import llm
import media
import naver
import store
from categories import CATEGORIES
from inspector import nospace
from store import CFG, DATA

_lock = threading.Lock()  # ponytail: 전역 락으로 글 1건씩 직렬 생성 — 주 2건이라 충분


def _now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def prepare(pid):
    p = store.load(pid)
    try:
        p["facts"] = [naver.place(u) for u in p["urls"]]
        if p["category"] == "여행":
            media.route_order(p["photos"], p["facts"])
        p["region"] = (regions(p["facts"][0]) or [""])[-1]
        p["keywords"] = plan_keywords(p)
        p["status"] = "queued"
    except Exception as e:
        return _fail(p, e)
    store.save(p)
    if p["mode"] == "즉시":
        generate(pid)


def regions(f):
    """주소 → [동, 시·구]. '서울 중구 충무로2가 64-6' → ['충무로', '중구'], '강원특별자치도 강릉시 …' → ['강릉']."""
    toks = (f.get("jibun") or f.get("address") or "").split()
    dong = [re.sub(r"\d+동$", "동", re.sub(r"\d+가$", "", t)) for t in toks[2:] if re.search(r"(동|가|읍|면|리)$", t)][:1]
    si = [re.sub(r"(특별자치시|특별자치도|특별시|광역시|시|군)$", "", t) for t in toks[1:2]]
    return dong + si


def volume_range():
    """검색량 구간. D+7 순위가 쌓이면 상한을 내 블로그 지수에 맞게 보정."""
    k = CFG.get("keyword", {})
    lo, hi = k.get("min_volume", 500), k.get("max_volume", 5000)
    done = [p for p in store.posts() if "7" in (p.get("ranks") or {}) and p["keywords"].get("volume")]
    wins = [p["keywords"]["volume"] for p in done if (p["ranks"]["7"] or 999) <= 10]
    # ponytail: 성공 키워드 최대 검색량×1.5 로 상한을 옮기는 단순 휴리스틱 — 표본이 쌓이면 구간별 성공률로 교체
    if len(done) >= 4:
        hi = max(lo * 2, int(max(wins) * 1.5)) if wins else max(lo * 2, hi // 2)
    return lo, hi


def _days(p):
    ds = sorted({x["taken_at"][:10] for x in p["photos"] if x.get("taken_at")})
    if not ds:
        return ""
    n = (dt.date.fromisoformat(ds[-1]) - dt.date.fromisoformat(ds[0])).days
    return f"{n}박{n + 1}일" if n else "당일치기"


def plan_keywords(p):
    """후보 조합 → 검색량(검색광고 API) → 30일 발행량(검색 API) → 포화지수 = 발행량/검색량×100 → 메인 1 + 서브 3~5."""
    f, cat = p["facts"][0], p["category"]
    name = f.get("name") or ""
    regs = list(dict.fromkeys(filter(None, [(f.get("subway") or {}).get("name"), *regions(f)]))) or [name]  # 역 이름이 흔한 검색어
    kind = (f.get("category") or "").split(",")[0]
    menus = [re.sub(r"\(.*", "", m["name"]).strip() for m in (f.get("menus") or [])[:2] if m.get("name")]
    said = " ".join(x["a"] for x in p["interview"]) + " " + p.get("memo", "")
    talk = [w for w, _ in Counter(n for n in inspector.nouns(said) if len(n) > 1).most_common(3)]
    if cat == "맛집":
        topics = ["맛집", kind, *menus, name]
        cands = [f"{r} {m} 맛집" for r in regs for m in dict.fromkeys([kind, *menus])] + [f"{r} 맛집" for r in regs] + [f"{regs[0]} {name}"]
    elif cat == "카페":
        topics = ["카페", "디저트", *talk, *menus, name]
        cands = [f"{r} {t} 카페" for r in regs for t in talk] + [f"{r} 카페" for r in regs] + [f"{r} {m}" for r in regs for m in menus]
    else:
        city, spots = regs[-1], [x.get("name") or "" for x in p["facts"]]
        topics = ["여행", "코스", "가볼만한곳", "당일치기", "박", *spots]
        cands = [f"{city} {_days(p)} 여행 코스", f"{city} 여행 코스", f"{city} 가볼만한곳", f"{city} 여행"] + [f"{city} {s}" for s in spots]
    cands = list(dict.fromkeys(" ".join(c.split()) for c in cands if c.strip()))[:20]

    notes = []
    try:
        stats = naver.keyword_stats(cands)
    except Exception as e:
        stats = {}
        notes.append(f"검색광고 API 미설정/실패 → 기본 패턴 키워드 사용 ({type(e).__name__})")
    rn, tn = [nospace(r) for r in regs], [nospace(t) for t in topics if t]
    rel = {k: v for k, (v, _) in stats.items() if any(r in k for r in rn) and any(t in k for t in tn)}
    lo, hi = volume_range()
    pool = sorted((k for k, v in rel.items() if lo <= v <= hi), key=lambda k: -rel[k])[:10]
    if rel and not pool:
        notes.append(f"검색량 {lo:,}~{hi:,} 구간 키워드 없음 → 구간에 가장 가까운 키워드 사용")
        pool = sorted(rel, key=lambda k: max(lo - rel[k], rel[k] - hi))[:5]
    spaced = lambda k: next((c for c in cands if nospace(c) == k), None) or inspector.kiwi().space(k)
    rows = []
    for k in pool:
        n = naver.monthly_posts(spaced(k))
        rows.append({"keyword": spaced(k), "volume": rel[k], "posts": n, "saturation": round(n / max(rel[k], 1) * 100, 1),
                     "comp": stats[k][1]})
    rows.sort(key=lambda r: r["saturation"])
    main = rows[0] if rows else {"keyword": cands[0]}
    M = nospace(main["keyword"])
    subs = [spaced(k) for k in sorted(rel, key=lambda k: -rel[k]) if M not in k][:5]
    subs += [c for c in cands if M not in nospace(c) and c not in subs][:max(0, 3 - len(subs))]
    season = False
    if cat == "여행":
        try:
            season = naver.season_boost(main["keyword"])
        except Exception:
            notes.append("데이터랩 API 실패 → 시즌성 미반영")
    return {"main": main["keyword"], "volume": main.get("volume"), "saturation": main.get("saturation"),
            "subs": subs, "rows": rows, "season_boost": season, "notes": notes}


def _next():
    """큐 우선순위: 성수기 직전 여행 > 직전 생성과 다른 카테고리(로테이션) > 최근 방문일."""
    ps = store.posts()
    q = [p for p in ps if p["status"] == "queued"]
    if not q:
        return None
    last = max((p for p in ps if p.get("generated_at")), key=lambda p: p["generated_at"], default={}).get("category")
    q.sort(key=lambda p: (not p["keywords"]["season_boost"], p["category"] == last,
                          -dt.date.fromisoformat(p["visit_date"]).toordinal()))
    return q[0]["id"]


def _past(pid):
    mine = [f.read_text(encoding="utf-8") for f in sorted((DATA / "my_posts").glob("*.txt"))]
    return mine + [p["body_text"] for p in store.posts() if p.get("body_text") and p["id"] != pid]


def generate(pid=None):
    with _lock:
        pid = pid or _next()
        if not pid:
            return None
        p = store.load(pid)
        p["status"] = "processing"
        store.save(p)
        try:
            d = store.post_dir(pid)
            photos = sorted(p["photos"], key=lambda x: x["no"])
            p["photo_notes"] = llm.vision(p, [d / "photos" / x["file"] for x in photos])
            past = _past(pid)
            draft = inspector.autofix(llm.write(p), p)
            rep = inspector.inspect(draft, p, past)
            if rep["issues"]:
                draft = inspector.autofix(llm.rewrite(p, draft, rep["issues"]), p)
                rep = inspector.inspect(draft, p, past)
            final, dropped = inspector.finalize(draft)
        except Exception as e:
            return _fail(p, e)
        clip_err = None
        try:
            clip = media.make_clip(d, p["videos"], draft.get("clip_captions") or [], CFG.get("media", {}).get("clip_font"))
        except Exception as e:
            clip, clip_err = None, f"{type(e).__name__}: {(getattr(e, 'stderr', None) or b'')[-300:].decode(errors='ignore')}"
        p.update(draft=draft, final=final, report=rep, clip=clip, body_text="\n".join(inspector.texts(final)),
                 warnings=_warnings(p, draft, dropped, clip, clip_err), generated_at=_now(), status="ready")
        store.save(p)
        _notify(p)
        return pid


def _warnings(p, draft, dropped, clip, clip_err):
    cat = CATEGORIES[p["category"]]
    scenes = {x.get("scene") for x in p["photo_notes"].get("photos", [])}
    w = list(p["keywords"]["notes"])
    miss = [s for s in cat["shots"] if s not in scenes]
    if miss:
        w.append("촬영 체크리스트 누락: " + ", ".join(miss))
    if len(nospace(" ".join(x["a"] for x in p["interview"]) + p.get("memo", ""))) < 150:
        w.append("인터뷰 답변이 짧음 — 좋았던 점·아쉬운 점·꿀팁을 더 적으면 분량·진정성이 올라갑니다")
    if draft.get("dropped_tags"):
        w.append("무관 해시태그 제거: " + " ".join(draft["dropped_tags"]))
    if dropped:
        w.append("근거 태그 없는 문장 삭제 (사실이면 직접 다시 넣기): " + " / ".join(dropped))
    if p["videos"] and not clip:
        w.append("클립 생성 실패: " + (clip_err or "ffmpeg 미설치"))
    elif clip and clip < 30:
        w.append(f"클립 {clip:.0f}초 — 네이버 클립 권장 30초 이상")
    return w


def _url(p):
    return f"{CFG.get('server', {}).get('base_url', 'http://localhost:8000')}/post/{p['id']}"


def send_mail(subject, body, attach=None):
    g = CFG.get("gmail") or {}
    if not g.get("user"):
        print(f"[메일 생략: gmail 미설정] {subject}")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, g["user"], g.get("to") or g["user"]
    msg.set_content(body)
    if attach:
        msg.add_attachment(attach.read_bytes(), maintype="video", subtype="mp4", filename="clip.mp4")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(g["user"], g["app_password"])
            s.send_message(msg)
    except Exception:
        traceback.print_exc()


def _notify(p):
    rep = p["report"]
    lines = [f"{'✅' if c['ok'] else '❌'} [{c['item']}] {c['msg']}" for c in rep["checks"]]
    body = (f"{p['final']['title']}\nSEO {rep['score']}점\n\n" + "\n".join(lines)
            + "".join(f"\n⚠️ {x}" for x in p["warnings"])
            + f"\n\n발행 도우미: {_url(p)}\n\n최종 검수 3분: 사실 오류 · 내 말투인지 · 한 문장이라도 직접 덧붙이기 · 사진 순서 · 지도 · 고지 문구")
    clip = store.post_dir(p["id"]) / "clip.mp4"
    attach = clip if p.get("clip") and CFG.get("gmail", {}).get("attach_clip") and clip.stat().st_size < 20e6 else None
    send_mail(f"[발행 준비] {p['keywords']['main']} · SEO {rep['score']}점", body, attach)


def _fail(p, e):
    traceback.print_exc()
    p["status"], p["error"] = "failed", f"{type(e).__name__}: {e}"
    store.save(p)
    send_mail(f"[생성 실패] #{p['id']}", f"{p['error']}\n\n재시도: python main.py prepare {p['id']}  또는  generate {p['id']}")


def related(p, n=3):
    """같은 지역 발행 글 — 여행(허브) ↔ 맛집·카페(스포크) 우선."""
    if not p.get("region"):
        return []
    same = [x for x in store.posts() if x["status"] == "published" and x["id"] != p.get("id") and x.get("region") == p["region"]]
    same.sort(key=lambda x: (x["category"] == p["category"], -x["id"]))
    return [{"title": x["final"]["title"], "url": x["url"], "category": x["category"]} for x in same[:n]]


def track():
    blog = CFG.get("naver", {}).get("blog_id")
    if not blog:
        return
    ps = store.posts()
    used = {p.get("log_no") for p in ps}
    items = [i for i in naver.rss(blog) if i["log_no"] not in used]
    for p in (x for x in ps if x["status"] == "ready"):
        t, M = p["final"]["title"], nospace(p["keywords"]["main"])
        ratio = lambda i: difflib.SequenceMatcher(None, t, i["title"]).ratio()
        cand = [i for i in items if i["date"] >= p["generated_at"][:10] and (ratio(i) >= 0.6 or M in nospace(i["title"]))]
        if not cand:
            continue
        i = max(cand, key=ratio)
        items.remove(i)
        p.update(status="published", url=i["link"].split("?")[0], log_no=i["log_no"], published_at=i["date"])
        store.save(p)
        spokes = [x for x in related(p, 20) if x["category"] != "여행"] if p["category"] == "여행" else []
        if spokes:
            send_mail(f"[역링크 추가] {p['region']} 여행 코스 글 발행됨",
                      f"새 여행 글: {p['url']}\n아래 글 하단 '함께 보면 좋은 글'에 링크를 추가하세요.\n"
                      + "\n".join(f"- {s['title']} {s['url']}" for s in spokes))
    now = dt.datetime.now().astimezone()
    for p in (x for x in store.posts() if x["status"] == "published"):
        ranks = p.setdefault("ranks", {})
        due = [d for d in (1, 3, 7) if str(d) not in ranks
               and now >= dt.datetime.fromisoformat(p["published_at"]) + dt.timedelta(days=d)]
        if due:
            r = naver.rank(p["keywords"]["main"], p["log_no"])
            ranks.update({str(d): r for d in due})
            store.save(p)


def build_style():
    """data/my_posts/*.txt (본인 글 10편) → 문체 규칙 + 발췌 2개 → data/style_profile.json."""
    texts = [f.read_text(encoding="utf-8") for f in sorted((DATA / "my_posts").glob("*.txt"))]
    if not texts:
        raise SystemExit("data/my_posts/ 에 본인 블로그 글을 .txt 로 넣으세요 (10편 권장)")
    prof = llm.style_profile(texts)
    (DATA / "style_profile.json").write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
    print(prof["rules"])
