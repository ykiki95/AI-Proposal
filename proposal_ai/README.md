# 제안서 자동화 AI 팀

한국 웹에이전시를 위한 **제안 발굴 → 적격성 판단 → 제안서 작성 → PT 생성 → 품질 검수**
전 과정을 자동화하는 멀티 에이전트 시스템.

> 5명의 가상 직원(김탐정, 이판단, 박제안, 최피티, 오품질)이 협업하며,
> 사장은 3개의 인간 승인 게이트에서만 의사결정한다.

---

## 1. 빠른 시작

이 프로젝트는 Replit AI Integrations로 LLM을 호출하므로 **Anthropic/OpenAI 키를 직접 발급받지 않아도** 동작합니다.

### 권장: 웹 대시보드 사용 (Streamlit)

워크플로 `Proposal AI Dashboard`가 자동 실행되며, 브라우저에서 한눈에 5명 에이전트의 작업·승인 게이트·모델 변경·로그를 관리할 수 있습니다.

대시보드 7개 탭:
- 📊 **현황판** — 단계별 분포, 비용, 활동 타임라인
- 🔍 **공고 목록** — 전체 공고 표 + 상세 보기
- 🛑 **승인 게이트** — 3개 인간 승인 단계 (참여 결정 / 초안 검토 / 최종 승인)
- 📝 **제안서 검토** — DOCX/PPTX 다운로드, 검수 등급
- ⚙️ **에이전트 설정** — 5명 모델 변경 + 추가 작업 지시
- 🤖 **수동 실행** — 즉시 수집/평가/제안서 작성
- 📜 **활동 로그** — 모든 LLM 호출 비용·토큰 감사

### CLI 사용 (선택)

```bash
python main.py --status                      # 환경 진단
python main.py --mode collect                # 공고 수집 (G2B 키 없으면 샘플 3건)
python main.py --mode evaluate               # 이판단 LLM 스코어링
python main.py --list-awaiting               # 승인 대기 목록 확인
python main.py --approve <bid_id>            # 사장 승인 (게이트 1)
python main.py --mode propose                # 제안서 + PT + 검수 생성
```

산출물은 `storage/outputs/<bid_id>_proposal_v1.docx`, `..._pt.pptx`로 저장됩니다.

---

## 2. 디렉터리 구조

```
proposal_ai/
├── main.py                    # CLI 진입점
├── config/
│   ├── settings.py            # 환경/회사/모델 설정 싱글턴
│   ├── agency_selectors.yaml  # 지자체 크롤러 셀렉터
│   └── scoring_rubric.yaml    # 평가 가중치/가이드
├── agents/                    # 5개 에이전트
│   ├── kim_detective.py       # 김탐정 - 수집
│   ├── lee_judge.py           # 이판단 - 평가
│   ├── park_proposer.py       # 박제안 - 제안서 (병렬 7섹션)
│   ├── choi_pt.py             # 최피티 - PT
│   └── oh_quality.py          # 오품질 - 검수
├── tools/
│   ├── llm_clients.py         # Replit AI Integrations 래퍼
│   ├── db.py                  # SQLite + SQLAlchemy
│   ├── g2b_api.py             # 나라장터 OpenAPI (없으면 샘플)
│   ├── agency_crawler.py      # 지자체 크롤러 (정적+동적)
│   ├── notion_client.py       # Notion 게이트 (없으면 SQLite)
│   ├── docx_generator.py      # python-docx 제안서
│   ├── pptx_generator.py      # python-pptx PT
│   └── korean_checker.py      # hanspell + LLM fallback
├── workflows/
│   ├── crew_definition.py     # 에이전트 팀 오케스트레이션
│   └── human_gates.py         # 3개 인간 승인 게이트
├── schemas/models.py          # Pydantic 데이터 계약
├── storage/                   # 자동 생성
│   ├── db.sqlite
│   └── outputs/               # DOCX, PPTX
└── tests/test_agents.py
```

---

## 3. 인간 승인 게이트 흐름

```
[김탐정] 수집
    │
    ▼  status=수집완료
[이판단] 평가 (가중치: 기술30 / 예산25 / 경쟁20 / 일정15 / 실적10)
    │
    │  점수 ≥ 70 → status=승인대기 (Notion에 업로드)
    │  점수 < 70 → status=비권장 (자동 제외)
    │
    ▼  ────── 🛑 게이트 1: 사장 승인 ──────
        Notion에서 "참여확정" 체크   또는
        CLI:  python main.py --approve <bid_id>
    │
    ▼  status=참여확정
[박제안] 제안서 7개 섹션 병렬 생성 → DOCX
    │
    ▼  status=초안완료
        ────── 🛑 게이트 2: 사장 검토 ──────
        Notion 코멘트로 수정 지시 (재실행 시 v2 생성)
    │
    ▼
[최피티] DOCX → PPTX 슬라이드 변환
    │
    ▼
[오품질] 맞춤법 + 용어 일관성 + RFP 매핑 + 문체 검수
    │
    ├─ A등급 → status=최종승인
    └─ B/C등급 → status=검토중 (박제안 재작업 필요)
        ────── 🛑 게이트 3: 사장 최종 승인 ──────
```

---

## 4. 환경변수 설정

`.env.example`을 참고해 필요한 값을 Replit Secrets 또는 `.env` 파일에 등록하세요.

| 카테고리 | 변수 | 필수? | 설명 |
|---|---|---|---|
| LLM | `AI_INTEGRATIONS_*` | ✅ | Replit AI 통합이 자동 주입 (직접 설정 X) |
| 회사 | `COMPANY_NAME`, `COMPANY_CEO`, `COMPANY_TECH_STACK` | 권장 | 제안서 회사소개 섹션에 사용 |
| G2B | `G2B_SERVICE_KEY` | 선택 | 미설정 시 샘플 3건 사용 |
| Notion | `NOTION_API_KEY`, `NOTION_DB_BIDS`, `NOTION_DB_PROPOSALS` | 선택 | 미설정 시 SQLite 로컬 게이트 |
| 알림 | `SLACK_WEBHOOK_URL` | 선택 | |
| 시스템 | `FIT_SCORE_THRESHOLD` | 선택 | 기본 70 |

---

## 5. 외부 API 키 발급 가이드

### 5-1. 나라장터(G2B) OpenAPI 키 발급

1. **공공데이터포털 회원가입** — https://www.data.go.kr/ 에서 회원가입
2. **데이터 신청** — `나라장터 입찰공고정보서비스` 검색 → "활용신청" 클릭
   - 직접 링크: https://www.data.go.kr/data/15129394/openapi.do
3. 활용 목적 입력 (예: "공공 입찰 정보 모니터링")
4. **승인 대기 (보통 1-3시간, 영업일 기준 최대 1일)**
5. 승인되면 마이페이지 → 활용신청 현황에서 **인증키(serviceKey)** 확인
6. Replit Secrets에 `G2B_SERVICE_KEY=<발급받은 키>` 등록

> 💡 키가 없어도 시스템은 샘플 공고 3건으로 정상 동작합니다.

### 5-2. Notion API 통합 발급

1. **Notion Integration 생성** — https://www.notion.so/my-integrations 접속
2. "**+ New integration**" 클릭
   - 이름: `Proposal AI`
   - 워크스페이스 선택
   - Type: Internal
3. 생성 후 **Internal Integration Secret** 복사 → `NOTION_API_KEY`
4. **DB 페이지 생성**:
   - Notion에서 새 페이지 → "Database - Full page" 추가
   - 이름: `공고 관리` (또는 원하는 이름)
   - 다음 속성 추가 (속성명 정확히):
     | 속성명 | 타입 |
     |---|---|
     | 공고번호 | Title |
     | 사업명 | Text |
     | 발주기관 | Text |
     | 예산 | Number |
     | 마감일 | Date |
     | 적합도 | Number |
     | 추천 | Select (참여권장/조건부/비권장) |
     | 상태 | Select (수집완료/평가완료/승인대기/참여확정/초안완료/검토중/최종승인/완료) |
5. **DB 권한 부여**:
   - DB 페이지 우측 상단 `…` → "**+ Add connections**" → 위에서 만든 `Proposal AI` 선택
6. **DB ID 복사**:
   - DB 페이지 URL이 `https://www.notion.so/<workspace>/<DATABASE_ID>?v=...` 형식
   - `<DATABASE_ID>` 부분(32자) 복사 → `NOTION_DB_BIDS`
7. 같은 방식으로 두 번째 DB(`제안서 검수`)를 만들어 `NOTION_DB_PROPOSALS`에 등록
   - 속성: 공고번호(Title), 버전(Number), 등급(Select A/B/C), 조치사항(Text)

> 💡 Notion 미설정 시 `python main.py --approve <bid_id>` 명령으로 수동 승인 가능합니다.

---

## 6. 아키텍처 결정 노트

| 항목 | 명세서 | 본 구현 | 사유 |
|---|---|---|---|
| 멀티 에이전트 프레임워크 | CrewAI | 직접 Python 클래스 (CrewAI 패턴) | Replit AI 통합과의 호환성·디버깅 용이성. CrewAI는 requirements에 포함되어 있어 추후 CrewAI Agent 래핑 전환 가능 |
| LLM 키 관리 | OpenAI/Anthropic 직접 발급 | Replit AI Integrations 프록시 | 별도 키 발급 불필요, 사용량은 Replit 크레딧으로 정산 |
| Claude 모델명 | Opus 4.7 / Sonnet 4.6 / Haiku 4.5 | 명세 그대로 (Replit AI 가용 모델) | Replit AI 통합이 해당 모델을 모두 지원 |
| 동시성 | aiosqlite/SQLAlchemy | SQLAlchemy + `check_same_thread=False` | 동기 안전 |
| Playwright | 필수 | 선택(설치 시 자동 사용) | 첫 빌드 속도. 정적 fetch로 대다수 페이지 처리 가능 |
| hanspell | 필수 | 선택(LLM fallback 내장) | 비공식 라이브러리라 설치 실패 가능성 |

---

## 7. 비용 보호 장치

- 모든 LLM 호출은 `tools/db.py`의 `audit_log` 테이블에 기록
- `MONTHLY_BUDGET_USD` 환경변수로 월 한도 설정 (현재는 기록만, 알림은 추후 연동)
- 박제안의 Opus 호출은 **승인된 공고에만** 사용 (게이트 1 통과 필수)
- 섹션 병렬 처리는 동시 3개로 제한

---

## 8. 자주 묻는 질문

**Q. Replit Agent가 만든 모델명이 `claude-opus-4-7` 같은 미래 버전이라 헷갈립니다.**
A. Replit AI Integrations는 자체 모델 이름을 사용하며, 이 코드의 모델명은 실제 호출 가능합니다. 변경하려면 `.env`의 `MODEL_*` 변수를 수정하세요.

**Q. 박제안이 너무 비쌉니다.**
A. `MODEL_PARK=claude-sonnet-4-6` 으로 변경하면 80% 비용 절감 가능합니다.

**Q. 한 공고에만 시험 적용하고 싶습니다.**
A. `python main.py --approve <bid_id>` 후 `python main.py --mode propose` 하면 해당 공고만 처리됩니다.

---

## 9. 테스트

```bash
python tests/test_agents.py
```

스키마/DB/DOCX/PPTX 등 LLM 비호출 부분의 단위 테스트입니다.
