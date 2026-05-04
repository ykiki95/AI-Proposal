# Migration Plan: proposal_ai v1 → v2

작성일: 2026-04-28  
대상 버전: Phase 1 (모듈 A~F)

---

## 1. 기존 코드 분석 요약

### 1-1. 재사용 가능 파일 (KEEP)

| 파일 | 이유 |
|------|------|
| `config/agency_selectors.yaml` | CSS 셀렉터 config-driven 구조, 확장만 하면 됨 |
| `config/scoring_rubric.yaml` | LLM 친화적 구조, 가중치만 업데이트 |
| `tools/db.py` | SQLAlchemy ORM 잘 설계됨, 마이그레이션 포함 |
| `tools/g2b_api.py` | 나라장터 OpenAPI 클라이언트, rate limit/fallback 완비 |
| `tools/agency_crawler.py` | config-driven 크롤러, playwright fallback 포함 |
| `tools/korean_checker.py` | hanspell + LLM fallback 패턴 |
| `tools/notion_client.py` | optional integration 패턴 모범 사례 |
| `tools/pptx_themes.py` | 테마 3종, 그대로 사용 가능 |
| `tools/pptx_master_builder.py` | 마스터 PPTX 자동 생성 유틸 |
| `tools/storyboard_generator.py` | Gate 2 스토리보드 출력 |
| `tools/script_generator.py` | 발표 스크립트 생성 |
| `workflows/human_gates.py` | Gate 1/2/3 로직 완성도 높음 |
| `tests/test_agents.py` | LLM-free 유닛 테스트 |

### 1-2. 수정 후 재사용 (UPDATE)

| 파일 | 변경 내용 |
|------|-----------|
| `config/settings.py` | 에이전트 키 재명명 (kim→discovery 등), Voyage AI / ChromaDB 설정 추가, OpenAI 의존성 제거 |
| `schemas/models.py` | `LayoutMode`, `AcceptanceTable`, `RfpRequirement` 추가; `ProposalDraft`에 수용표/레이아웃 필드 추가 |
| `tools/llm_clients.py` | `llm_client.py`로 통합, Replit proxy 제거 → 직접 Anthropic API 사용, prompt caching 헤더 추가 |
| `tools/rfp_analyzer.py` | 수용표 생성 기능 분리, StrategyAgent 입력 포맷 추가 |
| `tools/rfp_section_guide.py` | specialists.yaml 기반으로 동적 로드 |
| `tools/rfp_toc_extractor.py` | RFP 목차 유사도 검사 로직 추가 |
| `tools/pptx_generator.py` | `pptx_builder.py`로 개명, 가로/세로 레이아웃 모드 분기 추가 |
| `tools/docx_generator.py` | `docx_builder.py`로 개명 |
| `workflows/crew_definition.py` | `workflows/pipeline.py`로 개명, StrategyAgent 단계 삽입 |
| `dashboard/app.py` | 멀티페이지 Streamlit 구조로 리팩터링 |
| `main.py` | StrategyAgent 지원, 새 에이전트 키 반영 |

### 1-3. 재작성 필요 (REWRITE)

| 파일 | 이유 |
|------|------|
| `agents/park_team.py` (953줄) | 8개 Specialist가 하드코딩됨 → `specialists.yaml` 외부화 필요; sub-chapter 재귀 로직 정리 필요; 6-에이전트 체계에서 WriterAgent로 재구조화 |
| `agents/lee_judge.py` | AnalysisAgent + StrategyAgent 두 에이전트로 역할 분리 필요 |
| `agents/kim_detective.py` | DiscoveryAgent로 개명, pdfplumber RFP 파싱 통합 |
| `agents/oh_quality.py` | ReviewerAgent로 개명, 수용표(AcceptanceTable) 검증 로직 추가 |
| `agents/choi_pt.py` | GraphicsAgent로 개명, 가로/세로 레이아웃 모드 분기 추가 |

### 1-4. 삭제/통합 대상 (DELETE/MERGE)

| 파일 | 처리 방법 |
|------|-----------|
| OpenAI 의존 코드 (`chat_openai`, `MODEL_KIM/OH=gpt-4o-mini`) | 전부 Anthropic Claude로 교체. OpenAI 클라이언트 제거 |
| `agents/choi_pt.py` 내 스크립트 생성 코드 | `tools/script_generator.py`로 이미 분리됨 → 중복 제거 |

---

## 2. 새 6-에이전트 매핑

| v1 에이전트 | v2 에이전트 | 모델 | 역할 변화 |
|-------------|-------------|------|-----------|
| KimDetective (김탐정) | DiscoveryAgent | claude-haiku-4-5 | + pdfplumber RFP 파싱, + ChromaDB 저장 |
| LeeJudge (이판단) | AnalysisAgent | claude-sonnet-4-6 | 평가에 집중, 전략 수립은 StrategyAgent로 분리 |
| _(신규)_ | StrategyAgent | claude-opus-4-7 | RFP 목차 분석, 수용표 전략, 차별화 포인트 도출 |
| ParkTeam (박제안) | WriterAgent | claude-opus-4-7 | specialists.yaml 기반, 수용표 매핑 강제 |
| OhQuality (오품질) | ReviewerAgent | claude-sonnet-4-6 | + 수용표 100% 수용 검증, + RFP 목차 일치율 체크 |
| ChoiPT (최피디) | GraphicsAgent | claude-sonnet-4-6 | + 가로/세로 레이아웃 모드, + 수용표 슬라이드 생성 |

---

## 3. 새 폴더 구조

```
proposal_ai/
├── agents/
│   ├── base_agent.py          # 추상 베이스 클래스 (NEW)
│   ├── discovery_agent.py     # v1 kim_detective.py (REWRITE)
│   ├── analysis_agent.py      # v1 lee_judge.py 일부 (REWRITE)
│   ├── strategy_agent.py      # 신규 (NEW)
│   ├── writer_agent.py        # v1 park_team.py (REWRITE)
│   ├── reviewer_agent.py      # v1 oh_quality.py (REWRITE)
│   └── graphics_agent.py      # v1 choi_pt.py (REWRITE)
├── config/
│   ├── settings.py            # 업데이트
│   ├── agency_selectors.yaml  # 유지
│   ├── scoring_rubric.yaml    # 유지
│   └── specialists.yaml       # park_team.py에서 분리 (NEW)
├── schemas/
│   ├── models.py              # 업데이트 (LayoutMode, AcceptanceTable 추가)
│   └── rfp_schema.py          # RFP 전용 스키마 (NEW)
├── tools/
│   ├── llm_client.py          # 통합 LLM 클라이언트 (UPDATE)
│   ├── vector_store.py        # ChromaDB + Voyage AI 얇은 래퍼 (NEW)
│   ├── rfp_parser.py          # pdfplumber 기반 (NEW)
│   ├── acceptance_table.py    # 수용표 생성기 (NEW)
│   ├── db.py                  # 유지
│   ├── g2b_api.py             # 유지
│   ├── agency_crawler.py      # 유지
│   ├── korean_checker.py      # 유지
│   ├── rfp_analyzer.py        # 유지 (수용표 분리)
│   ├── rfp_section_guide.py   # 유지
│   ├── rfp_toc_extractor.py   # 유지
│   ├── notion_client.py       # 유지
│   ├── pptx_builder.py        # 개명 + 레이아웃 모드 추가
│   ├── docx_builder.py        # 개명
│   ├── storyboard_generator.py # 유지
│   ├── script_generator.py    # 유지
│   ├── pptx_themes.py         # 유지
│   └── pptx_master_builder.py # 유지
├── rag/                       # RAG 풀 파이프라인 (NEW, 모듈 B 산출)
│   ├── pdf_processor.py       # PDF → 구조화 텍스트
│   ├── pptx_processor.py      # PPTX → 구조화 텍스트
│   ├── chunker.py             # 청킹 전략
│   ├── embeddings.py          # Voyage AI 임베딩
│   ├── vector_store.py        # Chroma 컬렉션 관리
│   ├── ingest.py              # 수주 제안서/RFP 인덱싱 진입점
│   └── retrieve.py            # 의미 검색 API
├── data/                      # RAG 코퍼스 (NEW, *.pdf/*.pptx/processed/chroma_db는 .gitignore)
│   ├── winning_proposals/     # 수주 제안서 PDF
│   ├── winning_proposals_pptx/# 수주 제안서 PPTX
│   ├── test_rfps/             # 테스트 RFP
│   ├── processed/             # 파싱된 JSON 캐시
│   └── chroma_db/             # Chroma 영구 인덱스
├── scripts/                   # 유틸 스크립트 (NEW)
│   └── test_rag.py            # RAG 파이프라인 동작 검증
├── workflows/
│   ├── pipeline.py            # crew_definition.py 개명 + Strategy 단계 추가
│   └── human_gates.py         # 유지
├── dashboard/
│   ├── app.py                 # 멀티페이지 메인
│   └── pages/                 # Streamlit 멀티페이지 (NEW)
├── tests/
│   ├── test_agents.py         # 유지
│   ├── test_schemas.py        # 신규
│   └── test_tools.py          # 신규
└── storage/
    ├── masters/               # PPTX 마스터 파일
    ├── proposals/             # 생성된 제안서
    └── rfp_cache/             # 파싱된 RFP 캐시
```

---

## 4. 새 핵심 개념: 수용표 (AcceptanceTable)

한국 공공 제안서 규정상, **모든 RFP 요구사항이 100% "완전수용"** 으로 매핑되어야 한다.

- **StrategyAgent**: RFP 전체 텍스트 → `RfpRequirement` 리스트 추출
- **WriterAgent**: 각 섹션 작성 시 연결된 요구사항 ID 기록
- **ReviewerAgent**: `AcceptanceTable` 완성 + 미수용 항목 0건 검증
- **GraphicsAgent**: 수용표를 제안서 부록 슬라이드로 렌더링

---

## 5. 마이그레이션 단계별 일정

| 모듈 | 내용 | 상태 |
|------|------|------|
| A | 폴더 구조 + 스키마 + 설정 | ✅ 완료 |
| B | LLM 클라이언트 통합 + RAG 파이프라인 | ✅ 완료 (`tools/llm_client.py` 직접 Anthropic + prompt caching) |
| C | DiscoveryAgent + RFP 파서 | ✅ 완료 (`agents/discovery_agent.py` + `tools/rfp_parser.py` 본 구현) |
| D | AnalysisAgent + StrategyAgent | ✅ 완료 (`analysis_agent.py` + `strategy_agent.py`, 산출물 `storage/rfp_cache/`) |
| E | WriterAgent + ReviewerAgent | ✅ 완료 (`writer_agent.py` + `reviewer_agent.py`, 수용표·TOC 일치율 검증 포함) |
| F | GraphicsAgent + Dashboard + Pipeline | ✅ 완료 (`graphics_agent.py` + `workflows/pipeline.py` + `main.py` v2 모드 + `dashboard/app.py` + `pages/` 4개) |

> **계획 변경 메모 (2026-04-29):** 모듈 B는 원안 `tools/vector_store.py` 한 파일에서
> `rag/` 풀 파이프라인 + `data/` 코퍼스로 확장됨. PDF/PPTX → 청킹 → Voyage 임베딩 →
> Chroma 인덱스 → 의미 검색까지 전 단계 구현. 7건의 수주 제안서를 학습 데이터로 확보.
