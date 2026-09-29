"""순수 로직 자가 점검 (API 키 불필요): python test_core.py"""
from inspector import autofix, finalize, inspect, needs_evidence, sentences
from media import route_order
from naver import PLACE_ID
from pipeline import regions

# Place ID: 경로가 카테고리마다 다르고 자릿수 고정 금지
for url, want in [("https://m.place.naver.com/restaurant/11592650/home", "11592650"),
                  ("https://pcmap.place.naver.com/cafe/1234567/home", "1234567"),
                  ("https://map.naver.com/p/entry/place/38123456789?c=15", "38123456789"),
                  ("https://m.place.naver.com/attraction/99999/photo", "99999"),
                  ("https://map.naver.com/v5/search/x?pinId=13491212", "13491212")]:
    m = PLACE_ID.search(url)
    assert m and (m.group(1) or m.group(2)) == want, url

assert regions({"jibun": "서울 마포구 망원동 413-5"}) == ["망원동", "마포구"]
assert regions({"jibun": "서울 중구 충무로2가 64-6"}) == ["충무로", "중구"]
assert regions({"address": "강원특별자치도 강릉시 주문진읍 해안로 1"}) == ["주문진읍", "강릉"]

assert needs_evidence("면이 부드러웠어요.") and needs_evidence("12,000원") and not needs_evidence("그럼 출발해볼게요.")
assert sentences("국물이 진했어요. [M] 면은 쫄깃했어요.[M][V] 또 갈래요") == \
    ["국물이 진했어요. [M]", "면은 쫄깃했어요.[M][V]", "또 갈래요"]

post = {"category": "맛집", "sponsorship": "협찬", "photos": [{"no": i} for i in range(1, 10)],
        "facts": [{"name": "망원칼국수", "hours": "화·수 10:30~21:00", "menus": [{"name": "칼국수", "price": "12,000원"}]}],
        "photo_notes": {"photos": [{"no": 3, "menu": [{"name": "만두", "price": "8,000원"}]}]},
        "keywords": {"main": "망원동 칼국수 맛집", "subs": ["망원동 맛집", "망원 바지락칼국수", "망원시장 맛집"]}}
draft = {
    "title": "망원동 칼국수 맛집 바지락 가득한 국물에 반한 평일 점심",
    "summary": ["망원동 칼국수 맛집을 찾는다면 여기 바지락칼국수가 답이에요. [M]",
                "칼국수는 12,000원이고 만두는 8,000원이었어요. [V]", "혼밥도 편한 자리였어요. [M]"],
    "sections": [
        {"heading": "망원동 맛집 웨이팅 얼마나?", "paragraphs": ["평일 11시 반에 가니 바로 앉았어요. [M] 10:30에 문을 열어요. [P]",
                                                      "[사진01: 외관·간판 | 망원동 칼국수 맛집 간판]"]},
        {"heading": "망원 바지락칼국수 뭐 시켜야 해?", "paragraphs": ["바지락이 정말 많았어요. [V] 국물은 시원했어요.",
                                                         "[사진02: 메뉴 | 바지락칼국수]"]},
        {"heading": "양·맛은 어땠어?", "paragraphs": ["면이 두툼해서 배불렀어요. [M] 망원동 칼국수 맛집 중에서도 기억에 남아요. [M]",
                                                "그런데 가격표에 15,000원 메뉴도 보였어요. [V]"]},
        {"heading": "망원시장 맛집 아쉬운 점은?", "paragraphs": ["의자가 조금 낮았어요. [M]", "[지도: 망원칼국수]"]},
    ],
    "table": [["항목", "값"], ["위치", "망원동"], ["영업시간", "10:30~21:00"]],
    "hashtags": ["#망원동맛집", "#칼국수", "#바지락칼국수", "#망원시장", "#먹스타그램", "#강남맛집"],
}
d = autofix(draft, post)
assert "협찬" in d["disclosure"]
assert d["hashtags"] == ["#망원동맛집", "#칼국수", "#바지락칼국수", "#망원시장"], d["hashtags"]
assert d["dropped_tags"] == ["#먹스타그램", "#강남맛집"], d["dropped_tags"]

rep = inspect(d, post, past=["완전히 다른 글입니다. 오늘은 부산 바다를 봤어요."])
ok = {c["msg"].split(" (")[0]: c["ok"] for c in rep["checks"]}
assert ok["메인 키워드 '망원동 칼국수 맛집'로 시작, 제목 안 1회"] and ok["30~40자"], d["title"]
assert ok["첫 문단(두괄식 요약)에 메인 키워드"] and not ok["메인 키워드 본문 3~6회"]  # 2회뿐
assert ok["서브 키워드 소제목·본문에 각 1회 이상"]
assert not ok["감각·평가 문장 100% 근거 태그"]  # "국물은 시원했어요." 태그 없음
assert not ok["가격·시간 숫자 = facts/OCR 값"]  # 15,000원 은 어디에도 없음
assert ok["지도 마커 1개 이상"] and ok["'한눈에 보기' 텍스트 표"]
assert not ok["본문과 직접 관련된 해시태그 5~10개"]  # 무관 태그 제거 후 4개
assert ok["'협찬' 고지 문구 본문 첫 줄"]
assert any("근거 태그" in i for i in rep["issues"]) and 0 < rep["score"] < 100

final, dropped = finalize(d)
assert dropped == ["국물은 시원했어요."], dropped
assert final["sections"][1]["paragraphs"][0] == "바지락이 정말 많았어요."
assert "[M]" not in str(final) and "[V]" not in str(final) and "[사진02:" in str(final)

photos = [{"no": 1, "taken_at": "2026-09-02T15:00:00", "lat": 37.79, "lon": 128.92},
          {"no": 2, "taken_at": "2026-09-01T10:00:00", "lat": 37.75, "lon": 128.87},
          {"no": 3}]
places = [{"name": "경포대", "lat": 37.79, "lng": 128.91}, {"name": "안목해변", "lat": 37.77, "lng": 128.95},
          {"name": "중앙시장", "lat": 37.75, "lng": 128.89}]
route_order(photos, places)
assert [p["taken_at"][:10] if p.get("taken_at") else None for p in photos] == ["2026-09-01", "2026-09-02", None]
assert [p["no"] for p in photos] == [1, 2, 3]
assert [p["name"] for p in places] == ["중앙시장", "경포대", "안목해변"]

print("ok")
