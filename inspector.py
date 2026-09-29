"""SEO & 진정성 검사(§4.5) — LLM 없이 규칙 + Kiwi 형태소로 채점."""
import json
import re
import statistics
from functools import lru_cache

from categories import BANNED, CATEGORIES, DISCLOSURE

TAG = re.compile(r"\s*\[[MVP]\]")
MARK = re.compile(r"\[(?:사진|지도|클립)[^\]]*\]")
PHOTO = re.compile(r"\[사진\s*(\d+)\s*:([^|\]]*)\|?([^\]]*)\]")
SENT = re.compile(r"[^.!?\n]+?(?:[.!?]+|$)(?:\s*\[[MVP]\])*")  # 문장 + 뒤에 붙은 근거 태그
PRICE = re.compile(r"(?<![\d,])(\d{1,3}(?:,\d{3})+|\d{4,})\s*원")
TIME = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")


@lru_cache(None)
def kiwi():
    from kiwipiepy import Kiwi
    return Kiwi()


def nospace(s):
    return re.sub(r"\s+", "", s)


def untag(s):
    return TAG.sub("", s)


def sentences(p):
    return [s.strip() for s in SENT.findall(p) if s.strip()]


def nouns(s):
    return [t.form for t in kiwi().tokenize(s) if t.tag in ("NNG", "NNP")]


def needs_evidence(s):
    """숫자 또는 형용사(VA·XR·XSA)가 있는 문장 = 사실·감각·평가 문장 → 근거 태그 필수."""
    t = MARK.sub("", untag(s))
    return bool(re.search(r"\d", t)) or any(k.tag.startswith(("VA", "XR", "XSA")) for k in kiwi().tokenize(t))  # VA-I 등 불규칙 포함


def texts(d):
    """요약 + 본문 문단 (사진·지도·클립 마커 제외, 태그 유지)."""
    paras = [*d.get("summary", []), *(p for s in d.get("sections", []) for p in s.get("paragraphs", []))]
    return [t for t in (MARK.sub("", p).strip() for p in paras) if t]


def _markers(d):
    return MARK.findall("\n".join(p for s in d.get("sections", []) for p in s.get("paragraphs", [])))


def _numbers(s):
    return [f"{int(x.replace(',', '')):,}원" for x in PRICE.findall(s)] + [f"{int(h)}:{m}" for h, m in TIME.findall(s)]


def shingles(s):
    s = nospace(untag(s))
    return {s[i:i + 5] for i in range(len(s) - 4)}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a | b else 0.0


def _edges(ts):
    """도입·맺음 문장."""
    first, last = sentences(ts[0]) if ts else [], sentences(ts[-1]) if ts else []
    return [nospace(untag(x)) for x in first[:1] + last[-1:]]


def _list(xs, label):
    return f" ({label}: {' / '.join(xs[:3])}{' …' if len(xs) > 3 else ''})" if xs else ""


def autofix(d, post):
    """LLM 없이 고치는 것: 대가성 고지 삽입, 본문과 무관한 해시태그 제거."""
    d["disclosure"] = DISCLOSURE.get(post["sponsorship"], "")
    kw = post["keywords"]
    body = nospace(untag(" ".join(texts(d) + [s.get("heading", "") for s in d.get("sections", [])] + [kw["main"], *kw["subs"]])))
    keep, drop = [], []
    for t in d.get("hashtags", []):
        w = nospace(t.lstrip("#"))
        ok = w in body or all(n in body for n in nouns(w) if len(n) > 1) and any(len(n) > 1 for n in nouns(w))
        (keep if ok else drop).append("#" + w)
    d["hashtags"], d["dropped_tags"] = keep, drop
    return d


def inspect(d, post, past=()):
    """반환: {"score", "checks": [{item, ok, msg, fix}], "issues": [재작성 지시용 미달 항목]}.
    past = 과거 글 본문(오래된 것 → 최근 순)."""
    cat = CATEGORIES[post["category"]]
    main, subs = post["keywords"]["main"], post["keywords"]["subs"]
    M = nospace(main)
    raw = texts(d)
    plain = [untag(t) for t in raw]
    heads = [s.get("heading", "") for s in d.get("sections", [])]
    body = nospace(" ".join(heads + plain))
    marks = _markers(d)
    checks = []

    def ck(item, ok, msg, fix=True):
        checks.append({"item": item, "ok": bool(ok), "msg": msg, "fix": fix})

    t = d.get("title", "")
    ck("제목", nospace(t).startswith(M) and nospace(t).count(M) == 1, f"메인 키워드 '{main}'로 시작, 제목 안 1회")
    ck("제목", 30 <= len(t) <= 40, f"30~40자 (현재 {len(t)}자)")
    ck("제목", len(re.findall(r"[^\w\s]", t)) <= 2, "특수문자 2개 이하")

    n = len(nospace("".join(plain)))
    ck("분량", n >= cat["length"][0], f"공백 제외 {cat['length'][0]:,}자 이상 (현재 {n:,}자)")

    c = body.count(M)
    ck("키워드 밀도", M in nospace(" ".join(d.get("summary", []))), "첫 문단(두괄식 요약)에 메인 키워드")
    ck("키워드 밀도", 3 <= c <= 6, f"메인 키워드 본문 3~6회 (현재 {c}회, 초과분은 동의어로)")
    miss = [s for s in subs if nospace(s) not in body]
    ck("키워드 밀도", not miss, "서브 키워드 소제목·본문에 각 1회 이상" + _list(miss, "누락"))

    need_maps = len(post["facts"]) if post["category"] == "여행" else 1
    maps = sum(m.startswith("[지도") for m in marks)
    long = [untag(p)[:20] for p in raw if len(sentences(p)) > 4]
    ck("구조", len(d.get("summary", [])) >= 3, "두괄식 3줄 요약")
    ck("구조", 4 <= len(heads) <= 6, f"질문형 소제목 4~6개 (현재 {len(heads)}개)")
    ck("구조", not long, "문단 4문장 이하" + _list(long, "초과"))
    ck("구조", len(d.get("table", [])) >= 3, "'한눈에 보기' 텍스트 표")
    ck("구조", maps >= need_maps, f"지도 마커 {need_maps}개 이상 (현재 {maps}개)")

    caps = [m.group(3) for m in PHOTO.finditer(" ".join(marks))]
    k = sum(M in nospace(x) for x in caps)
    ck("미디어", len(post["photos"]) >= cat["min_photos"],
       f"원본 사진 {cat['min_photos']}장 이상 (현재 {len(post['photos'])}장) — 증식 말고 다음 방문 때 더 찍기", fix=False)
    ck("미디어", caps and 1 <= k <= max(1, len(caps) // 2), f"사진 캡션 중 메인 키워드 포함 1개~과반 이하 (현재 {k}/{len(caps)})")

    untagged = [untag(s)[:25] for p in raw for s in sentences(p) if needs_evidence(s) and not TAG.search(s)]
    ck("근거 검증", not untagged, "감각·평가 문장 100% 근거 태그" + _list(untagged, "태그 없음"))
    known = set(_numbers(json.dumps([post.get("facts"), post.get("photo_notes")], ensure_ascii=False)))
    bad = [x for x in _numbers(" ".join(plain + [" ".join(r) for r in d.get("table", [])])) if x not in known]
    ck("근거 검증", not bad, "가격·시간 숫자 = facts/OCR 값" + _list(bad, "근거 없음"))

    me = shingles(" ".join(plain))
    sim = max((jaccard(me, shingles(x)) for x in past), default=0.0)
    ck("자기 유사도", sim < 0.25, f"과거 글과 문자 5-gram Jaccard < 0.25 (최대 {sim:.2f})")
    seen = {e for x in past[-10:] for e in _edges([ln for ln in x.splitlines() if ln.strip()])}
    dup = [e for e in _edges(raw) if e in seen]
    ck("자기 유사도", not dup, "도입·맺음 문장이 최근 10편과 중복 없음" + _list(dup, "중복"))

    joined = " ".join(plain)
    hits = [w for w in BANNED if w in joined]
    ck("AI 상투어", sum(joined.count(w) for w in hits) <= 1, "상투어·과장어 1회 이하" + _list(hits, "발견"))
    lens = [len(s) for p in plain for s in sentences(p)]
    cv = statistics.pstdev(lens) / statistics.mean(lens) if len(lens) > 2 else 1.0
    # 경험값: 사람 글은 문장 길이 변동계수가 대체로 0.4 이상, 너무 균일하면 기계 생성 의심
    ck("AI 상투어", cv >= 0.3, f"문장 길이 편차 (변동계수 {cv:.2f} ≥ 0.30)")

    tags = d.get("hashtags", [])
    ck("해시태그", 5 <= len(tags) <= 10, f"본문과 직접 관련된 해시태그 5~10개 (현재 {len(tags)}개)")

    if post["sponsorship"] in DISCLOSURE:
        ck("법규 고지", post["sponsorship"] in d.get("disclosure", ""), f"'{post['sponsorship']}' 고지 문구 본문 첫 줄", fix=False)

    return {"score": round(100 * sum(c["ok"] for c in checks) / len(checks)), "checks": checks,
            "issues": [f"[{c['item']}] {c['msg']}" for c in checks if not c["ok"] and c["fix"]]}


def _prune(p, dropped):
    if not MARK.sub("", p).strip():
        return p  # 마커만 있는 문단
    keep = []
    for s in sentences(p):
        (dropped.append(untag(s)) if needs_evidence(s) and not TAG.search(s) else keep.append(s))
    return untag(" ".join(keep))


def finalize(d):
    """재작성 후에도 근거 태그 없는 평가 문장은 삭제, 그다음 근거 태그 제거. 반환: (확정본, 삭제 문장)."""
    f, dropped = json.loads(json.dumps(d)), []
    f["title"] = untag(f.get("title", ""))
    f["summary"] = [x for x in (_prune(s, dropped) for s in f.get("summary", [])) if x]
    for s in f.get("sections", []):
        s["heading"] = untag(s.get("heading", ""))
        s["paragraphs"] = [x for x in (_prune(p, dropped) for p in s.get("paragraphs", [])) if x]
    f["table"] = [[untag(c) for c in row] for row in f.get("table", [])]
    return f, dropped
