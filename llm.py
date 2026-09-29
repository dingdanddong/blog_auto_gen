"""Gemini 호출: Vision(사진 해석) · Writer(초안) · 부분 재작성 · 문체 프로필. 건당 2~3회."""
import base64
import json
import time

import httpx

from categories import BANNED, CATEGORIES
from store import CFG, DATA

API = "https://generativelanguage.googleapis.com/v1beta/models"


def gemini(parts, temperature=0.7):
    """JSON 응답. 무료 한도는 수시로 바뀌므로 하드코딩하지 않고 429·5xx 에 지수 백오프."""
    g = CFG["gemini"]
    retries = g.get("max_retries", 5)
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"}}
    for i in range(retries + 1):
        r = httpx.post(f"{API}/{g['model']}:generateContent", json=body, headers={"x-goog-api-key": g["api_key"]}, timeout=300)
        if r.status_code not in (429, 500, 503) or i == retries:
            break
        time.sleep(2 ** (i + 1))
    r.raise_for_status()
    return json.loads("".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]))


VISION = """너는 블로그 사진 분석가다. 아래 사진 {n}장은 글쓴이가 '{cat}' 방문 때 직접 찍은 원본이다.
보이는 것만 사실대로 적고 맛·향처럼 사진으로 알 수 없는 것은 추측하지 마라.
scene 은 다음 중 하나: {shots} / 기타
menu 의 price 는 메뉴판·영수증에 보이는 값만 "12,000원" 형식으로.
JSON 만 출력:
{{"photos":[{{"no":1,"scene":"","desc":"보이는 특징 1~2문장","ocr":"메뉴판·영수증·간판 글자(없으면 빈 문자열)",
"menu":[{{"name":"","price":""}}],"caption":"캡션 초안 20자 이내"}}],
"order":[글 흐름에 맞는 사진 번호 순서]}}"""


def vision(p, paths):
    """사진을 긴 변 768px 로 1장씩(장당 1타일 ≈ 258토큰) 넣는다 — 바둑판 합치기 없음."""
    cat = CATEGORIES[p["category"]]
    parts = [{"text": VISION.format(n=len(paths), cat=p["category"], shots=" / ".join(cat["shots"]))}]
    for i, path in enumerate(paths, 1):
        parts += [{"text": f"사진{i:02d}"},
                  {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(path.read_bytes()).decode()}}]
    return gemini(parts, 0.2)


def _style():
    f = DATA / "style_profile.json"
    if not f.exists():
        return "(문체 프로필 없음 — 담백한 '~요' 구어체)"
    s = json.loads(f.read_text(encoding="utf-8"))
    return s["rules"] + "\n[본인 글 발췌]\n" + "\n---\n".join(s["excerpts"])


def _rules(p):
    cat, kw = CATEGORIES[p["category"]], p["keywords"]
    return f"""너는 이 블로거 본인의 말투로 방문 후기를 정리하는 편집자다. 경험은 아래 [인터뷰/메모]·[사진 분석]·[플레이스 사실]에 있는 것만 쓴다.
규칙
1. title: 메인 키워드 "{kw['main']}"로 시작(제목 안 1회만), 공백 포함 30~40자, 특수문자 2개 이하. 대가성 고지 문구는 쓰지 마라(시스템이 넣는다).
2. summary: 인사말 없이 핵심 답 3줄({cat['summary']}). 첫 줄에 메인 키워드 1회.
3. sections: 질문형 소제목 4~6개 (예: {' / '.join(cat['headings'])}). 서브 키워드({', '.join(kw['subs'])})를 소제목·본문에 각 1회 이상 자연스럽게. 문단은 3~4문장 이하.
4. 메인 키워드는 summary+본문 합계 3~6회. 더 필요하면 동의어로.
5. 사진: photo_notes 의 order 순서로 모든 사진을 한 번씩, 문단 사이 단독 문단 "[사진03: 장면 | 캡션]"으로 넣는다. 캡션 중 메인 키워드 포함은 1개 이상 절반 이하.
6. 지도: 단독 문단 "[지도: 장소명]" ({cat['map']}). 영상이 있으면 단독 문단 "[클립]" 1개.
7. 근거 태그: 사실·감각·평가·숫자가 있는 모든 문장 끝(마침표 뒤)에 [M](인터뷰/메모) [V](사진 분석) [P](플레이스 사실) 중 해당하는 것을 붙인다. summary 도 마찬가지. 근거가 없으면 그 문장을 쓰지 않는다.
8. 가격·시간은 데이터에 있는 값 그대로(가격은 사진 OCR 우선). 모르는 값은 "확인 필요".
9. table: "한눈에 보기" 텍스트 표 [["항목","값"], ...] — 항목: {', '.join(cat['table'])}. 가격 값 뒤에 "(방문일 기준)".
10. hashtags: 본문 주제와 직접 관련된 5~10개.
11. clip_captions: 영상 수만큼, 각 15자 이내 자막.
12. 분량: 공백 제외 {cat['length'][0]}~{cat['length'][1]}자. 문장 길이는 짧은 것과 긴 것을 섞는다.
13. 금지: 인사·맺음 상투어, 과장 최상급, 다음 표현 — {', '.join(BANNED)}.
출력(JSON 만): {{"title":"","summary":["","",""],"sections":[{{"heading":"","paragraphs":[""]}}],"table":[["",""]],"hashtags":["#"],"clip_captions":[""]}}
"""


def _context(p):
    qa = "\n".join(f"- {x['q']} {x['a'] or '(무응답)'}" for x in p["interview"])
    return f"""[카테고리] {p['category']}  [방문일] {p['visit_date']}  [영상] {len(p['videos'])}개
[플레이스 사실] {json.dumps(p['facts'], ensure_ascii=False)}
[사진 분석] {json.dumps(p['photo_notes'], ensure_ascii=False)}
[인터뷰/메모]
{qa}
- 메모: {p.get('memo') or '(없음)'}
[문체 프로필]
{_style()}"""


def write(p):
    return gemini([{"text": _rules(p) + "\n" + _context(p)}])


def rewrite(p, draft, issues):
    """미달 항목만 지정해 부분 재작성 (조건부, 최대 1회)."""
    return gemini([{"text": _rules(p) + "\n" + _context(p) + "\n[초안]\n" + json.dumps(draft, ensure_ascii=False)
                    + "\n[미달 항목]\n" + "\n".join(issues)
                    + "\n위 초안에서 미달 항목과 관련된 부분만 고치고 나머지 문장은 그대로 둔 채 같은 형식의 JSON 전체를 출력하라. 새 사실을 지어내지 마라."}], 0.4)


def style_profile(texts):
    return gemini([{"text": f"""아래는 한 블로거의 과거 글 {len(texts)}편이다.
1) 이 사람의 문체 규칙을 800~1,500자로 정리하라 (어미·말투, 문장 길이, 이모티콘·기호, 자주 쓰는 표현, 문단 습관, 독자 호칭 등).
2) 문체가 가장 잘 드러나는 원문 발췌 2개를 고르라 (각 300자 내외, 원문 그대로).
JSON 만 출력: {{"rules":"","excerpts":["",""]}}

""" + "\n\n=====\n\n".join(texts)}], 0.3)
