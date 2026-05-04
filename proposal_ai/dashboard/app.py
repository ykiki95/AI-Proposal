"""
v2 Streamlit 대시보드 — 랜딩 페이지.

`pages/` 디렉토리의 모듈이 사이드바에 자동 등록된다.
실행:
  streamlit run proposal_ai/dashboard/app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 상위 디렉토리(proposal_ai/)를 import path에 추가
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from config.settings import (
    KEYS,
    MODELS,
    SYS,
    apply_agent_overrides,
    get_effective_company,
    summary,
)
from schemas.models import BidStatus
from tools import db


st.set_page_config(
    page_title="제안서 자동화 AI 팀 v2",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)

db.init_db()
apply_agent_overrides()
COMPANY = get_effective_company()


st.title("📋 제안서 자동화 AI 팀 v2")
st.caption(
    "Discovery → Analysis → Strategy → Writer → Reviewer → Graphics — "
    "6-에이전트 파이프라인. 사이드바에서 단계별 페이지를 선택하세요."
)

# ---------------------------------------------------------------------------
# 좌: 시스템 상태  /  우: 파이프라인 카운터
# ---------------------------------------------------------------------------

col_sys, col_pipe = st.columns([1, 2])

with col_sys:
    st.subheader("🛠️ 시스템 상태")
    st.text(summary())

with col_pipe:
    st.subheader("📊 파이프라인 진척")
    counts = {s: len(db.list_bids(status=s)) for s in BidStatus}
    pipeline_order = [
        BidStatus.COLLECTED,
        BidStatus.AWAITING_APPROVAL,
        BidStatus.APPROVED,
        BidStatus.STRATEGY_DONE,
        BidStatus.DRAFT_DONE,
        BidStatus.UNDER_REVIEW,
        BidStatus.FINAL_APPROVED,
        BidStatus.DONE,
    ]
    cols = st.columns(len(pipeline_order))
    for c, status in zip(cols, pipeline_order):
        c.metric(status.value, counts.get(status, 0))


# ---------------------------------------------------------------------------
# 6-에이전트 모델 매핑 + 회사 정보
# ---------------------------------------------------------------------------

st.markdown("---")
st.subheader("🤖 6-에이전트 모델 매핑")

agent_rows = [
    ("DiscoveryAgent", "공고 수집 + RFP 파싱 + RAG 인덱싱", MODELS.discovery),
    ("AnalysisAgent",  "1차 외적 평가 (게이트1 자동 승급)",  MODELS.analysis),
    ("StrategyAgent",  "RFP 목차 + 요구사항 + 수용표 전략", MODELS.strategy),
    ("WriterAgent",    "본문 작성 + 수용표 매핑 강제",       MODELS.writer),
    ("ReviewerAgent",  "수용표 100% + 목차 일치율 검증",    MODELS.reviewer),
    ("GraphicsAgent",  "DesignBrief + PPTX + 스토리보드",    MODELS.graphics),
]
for name, role, model in agent_rows:
    col_n, col_r, col_m = st.columns([2, 5, 3])
    col_n.markdown(f"**{name}**")
    col_r.write(role)
    col_m.code(model)


st.markdown("---")
st.subheader("🏢 회사 프로필 (현재 활성)")
c1, c2, c3 = st.columns(3)
c1.metric("회사명", COMPANY.name)
c2.metric("팀 규모", f"{COMPANY.team_size}명")
c3.metric("적정 예산", f"{COMPANY.typical_budget_min:,} ~ {COMPANY.typical_budget_max:,}원")
st.caption(f"기술스택: {', '.join(COMPANY.tech_stack)}")


st.markdown("---")
st.subheader("🔌 통합 상태")
ic1, ic2, ic3, ic4 = st.columns(4)
ic1.metric("Anthropic", "✅" if KEYS.has_anthropic else "❌")
ic2.metric("Voyage AI",  "✅" if KEYS.has_voyage else "❌")
ic3.metric("G2B 키",      "✅" if KEYS.has_g2b else "❌")
ic4.metric("Notion",      "✅" if KEYS.has_notion else "❌")

st.markdown(
    "**다음 단계는 사이드바에서:** 1. 공고 수집·평가 → 2. 전략·작성 → 3. 검수·시각화 → 4. 설정"
)
