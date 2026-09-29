"""네이버 공식 무료 API(검색·검색광고·데이터랩·지역검색) + 플레이스 사실 수집 + 블로그 RSS."""
import base64
import datetime as dt
import hashlib
import hmac
import json
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import httpx

from store import CFG

UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"}
# 맛집·카페·관광지·숙소 등 경로가 제각각이라 다중 패턴, 자릿수 고정 금지
PLACE_ID = re.compile(r"(?:place\.naver\.com/\w+|/(?:place|restaurant|cafe|attraction|accommodation))/(\d{5,})"
                      r"|[?&](?:id|pinId)=(\d{5,})")


def _open_api(path, method="GET", **kw):
    n = CFG["naver"]
    r = httpx.request(method, "https://openapi.naver.com" + path, timeout=10, headers={
        "X-Naver-Client-Id": n["client_id"], "X-Naver-Client-Secret": n["client_secret"]}, **kw)
    r.raise_for_status()
    return r.json()


def blog_search(query, sort="sim", start=1):
    return _open_api("/v1/search/blog.json", params={"query": query, "display": 100, "start": start, "sort": sort})["items"]


def monthly_posts(query):
    """최근 30일 블로그 발행 글 수. 검색 API 한계로 최대 1,000."""
    since = (dt.date.today() - dt.timedelta(days=30)).strftime("%Y%m%d")
    n = 0
    for start in range(1, 1000, 100):
        items = blog_search(query, "date", start)
        fresh = [i for i in items if i["postdate"] >= since]
        n += len(fresh)
        if len(fresh) < len(items) or len(items) < 100:
            break
    return n


def keyword_stats(hints):
    """검색광고 API 키워드도구 → {키워드(공백 없음): (월간 검색량 PC+모바일, 경쟁도)}. 연관 키워드 포함."""
    ad, out = CFG["searchad"], {}
    for i in range(0, len(hints), 5):  # hintKeywords 는 5개까지
        ts, uri = str(int(time.time() * 1000)), "/keywordstool"
        sig = base64.b64encode(hmac.new(ad["secret_key"].encode(), f"{ts}.GET.{uri}".encode(), hashlib.sha256).digest())
        r = httpx.get("https://api.searchad.naver.com" + uri, timeout=10,
                      params={"hintKeywords": ",".join(h.replace(" ", "") for h in hints[i:i + 5]), "showDetail": 1},
                      headers={"X-Timestamp": ts, "X-API-KEY": ad["api_key"], "X-Customer": str(ad["customer_id"]),
                               "X-Signature": sig.decode()})
        r.raise_for_status()
        for k in r.json()["keywordList"]:
            vol = sum(v if isinstance(v, int) else 5 for v in (k["monthlyPcQcCnt"], k["monthlyMobileQcCnt"]))  # "< 10"
            out[k["relKeyword"]] = (vol, k.get("compIdx"))
        time.sleep(0.3)
    return out


def season_boost(keyword):
    """데이터랩 월별 추이: 2~4주 뒤 달의 작년 검색 비율이 연평균의 1.3배 이상이면 성수기 직전."""
    today = dt.date.today()
    data = _open_api("/v1/datalab/search", "POST", json={
        "startDate": today.replace(year=today.year - 1, day=1).isoformat(), "endDate": today.isoformat(),
        "timeUnit": "month", "keywordGroups": [{"groupName": keyword, "keywords": [keyword]}]})["results"][0]["data"]
    if not data:
        return False
    avg = sum(d["ratio"] for d in data) / len(data)
    target = (today + dt.timedelta(days=21)).strftime("-%m-")
    return any(d["ratio"] >= avg * 1.3 for d in data if target in d["period"])


def local(query):
    """지역검색 API — 플레이스 수집 실패 시 상호·주소·분류만 확보."""
    items = _open_api("/v1/search/local.json", params={"query": query, "display": 1})["items"]
    if not items:
        return None
    i = items[0]
    return {"name": re.sub(r"<[^>]+>", "", i["title"]), "category": i["category"].split(">")[-1],
            "address": i["roadAddress"] or i["address"], "jibun": i["address"],
            "lat": int(i["mapy"]) / 1e7, "lng": int(i["mapx"]) / 1e7}


def place_id(url):
    m = PLACE_ID.search(url)
    if not m:  # naver.me 단축 URL 등 → 리다이렉트 추적
        r = httpx.get(url, headers=UA, follow_redirects=True, timeout=10)
        m = PLACE_ID.search(" ".join([str(h.url) for h in r.history] + [str(r.url)]))
    if not m:
        raise ValueError(f"Place ID 를 찾지 못함: {url}")
    return m.group(1) or m.group(2)


def place(text):
    """지도 URL(공유 텍스트 통째로 OK) → 장소 facts. 없는 값은 None(= 본문에 '확인 필요'), 추측 금지."""
    url = re.search(r"https?://\S+", text).group(0)
    hint = next((s.strip() for s in text.splitlines() if s.strip() and "http" not in s and not s.strip().startswith("[")), None)
    base = {"url": url, "fetched_at": dt.datetime.now().isoformat(timespec="minutes")}
    html = ""
    try:
        pid = place_id(url)
        time.sleep(1.5)  # 건당 요청 2~3회, 간격 두기
        html = httpx.get(f"https://m.place.naver.com/place/{pid}/home", headers=UA, follow_redirects=True, timeout=15).text
        st = json.JSONDecoder().raw_decode(html, html.index("{", html.index("__APOLLO_STATE__")))[0]
        b = st[f"PlaceDetailBase:{pid}"]
        conv = b.get("conveniences") or []
        sub = next((v for k, v in st.items() if k.startswith("SubwayStationInfo:")), {})
        return {**base, "source": "place", "id": pid, "name": b.get("name"), "category": b.get("category"),
                "subway": sub and {"name": sub.get("name"), "exit": sub.get("nearestExit"), "walk_min": sub.get("walkTime")},
                "address": b.get("roadAddress"), "jibun": b.get("address"), "phone": b.get("virtualPhone") or b.get("phone"),
                "hours": _hours(st), "parking": "가능(플레이스 편의시설)" if any("주차" in c or "발렛" in c for c in conv) else None,
                "conveniences": conv,
                "menus": [{"name": v.get("name"), "price": (v.get("price") or {}).get("displayText")}
                          for k, v in st.items() if k.startswith(("PlaceMenuItem:", "Menu:"))][:10],
                "lat": float(b["coordinate"]["y"]), "lng": float(b["coordinate"]["x"])}
    except Exception as e:
        m = re.search(r'og:title" content="([^"]+?)(?: : 네이버)?"', html)
        name = (m and m.group(1)) or hint
        found = local(name) if name else None
        if not found:
            raise ValueError(f"장소 정보를 가져오지 못함: {url} ({e})")
        return {**base, "source": "local_api", **found}


def _walk(o):
    yield o
    for v in o.values() if isinstance(o, dict) else o if isinstance(o, list) else []:
        yield from _walk(v)


def _hours(st):
    """newBusinessHours → '화·수·목 10:30~21:00 (브레이크 15:00~17:00)'."""
    groups = {}
    for v in _walk(st):
        if isinstance(v, dict) and "day" in v and isinstance(v.get("businessHours"), dict):
            bh = v["businessHours"]
            t = f'{bh.get("start")}~{bh.get("end")}' + "".join(
                f' (브레이크 {x.get("start")}~{x.get("end")})' for x in v.get("breakHours") or [])
            groups.setdefault(t + (f' {v["description"]}' if v.get("description") else ""), []).append(v["day"])
    return ", ".join(f"{'·'.join(dict.fromkeys(days))} {t}" for t, days in groups.items()) or None


def rss(blog_id):
    root = ET.fromstring(httpx.get(f"https://rss.blog.naver.com/{blog_id}.xml", timeout=10).content)
    return [{"title": i.findtext("title"), "link": i.findtext("link"),
             "log_no": re.search(r"/(\d{9,})", i.findtext("link")).group(1),
             "date": parsedate_to_datetime(i.findtext("pubDate")).isoformat()} for i in root.iter("item")]


def rank(query, log_no):
    """블로그 검색(sort=sim) 100건 안의 내 글 순위, 없으면 None. 통합검색 스마트블록 순위와는 다를 수 있음."""
    return next((n for n, it in enumerate(blog_search(query), 1) if log_no in it["link"]), None)
