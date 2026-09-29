# blog_auto_gen

네이버 블로그(맛집·카페·여행) 포스팅 보조 에이전트 — 계획서 v2 구현.
경험은 사람이 입력하고, AI 는 키워드 기획·구조화·검증만 한다. 발행은 사람이 직접.

- 설정: `needs.txt`
- 실행: `.venv/bin/python main.py serve` → 아이폰에서 `/` 입력, 메일로 온 `/post/{id}` 에서 복사·발행
- 기타 명령: `main.py style | prepare ID | generate [ID] | track`
- 자가 점검: `.venv/bin/python test_core.py`

| 파일 | 역할 |
|---|---|
| `main.py` | FastAPI(PWA 입력·발행 도우미 API) + 스케줄러 + CLI |
| `pipeline.py` | prepare(사실·키워드) → generate(Vision·Writer·검사·재작성·클립·알림) → track(RSS·순위) |
| `naver.py` | 검색·검색광고·데이터랩·지역검색 API, 플레이스 수집, RSS |
| `llm.py` | Gemini 호출·프롬프트 |
| `inspector.py` | §4.5 SEO·진정성 체크리스트 |
| `media.py` | 768px 사본, 얼굴 흐림, 여행 동선, FFmpeg 클립 |
| `categories.py` | 카테고리 템플릿·인터뷰·금칙어·고지 문구 |
