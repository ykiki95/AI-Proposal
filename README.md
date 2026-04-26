# AI-Proposal

  크림하우스 제안서 자동화 AI 팀 시스템 (Python + Streamlit).

  5명 메인 에이전트(김탐정 / 이판단 / 박제안팀 / 최피티 / 오품질) + 나라장터 크롤러
  + DOCX/PPTX/발표문 자동 생성 + Notion 승인 게이트.

  자세한 내용은 [`proposal_ai/README.md`](proposal_ai/README.md) 참조.

  ## 빠른 시작

  ```bash
  pip install -r proposal_ai/requirements.txt
  cp proposal_ai/.env.example proposal_ai/.env  # OPENAI_API_KEY, G2B_SERVICE_KEY 등 설정
  streamlit run proposal_ai/dashboard/app.py --server.port 5000
  ```

  ## 디렉토리

  - `proposal_ai/agents/` — 5명 메인 에이전트
  - `proposal_ai/tools/` — RFP 추출, 목차 추출, PPTX/DOCX 빌더, G2B API, DB 등
  - `proposal_ai/dashboard/` — Streamlit 대시보드 (5단계 휴먼 게이트)
  - `proposal_ai/schemas/` — Pydantic 모델
  - `proposal_ai/storage/` — SQLite DB, PPTX 마스터, 출력물 (uploads는 .gitignore 처리)

  ## 주요 기능

  - 나라장터 G2B 자동 크롤링 → RFP 본문 추출 (PDF/HWP)
  - LLM으로 RFP 목차 100% 자동 추출 + 챕터별 분할 작성
  - 글로벌 스토리 컨텍스트(비전·키워드·차별점) 모든 챕터에 주입
  - PPTX 신규 도형 5종: AS-IS/TO-BE, 시스템 구성도, 프로세스 플로우, 화면 mockup, mockup grid
  - 5단계 휴먼 게이트(참여확정 / 본문승인 / PT승인 / 발표문승인 / 최종)

  ## 라이선스

  내부용. 외부 저작권 PDF(국가유산진흥원 RFP, 경찰청 제안발표 샘플, 회사소개서)는 저장소에 포함되지 않음.
  