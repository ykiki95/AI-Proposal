"""페이지 1 — 공고 수집 + 1차 평가 (DiscoveryAgent + AnalysisAgent)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st

from schemas.models import BidStatus
from tools import db
from workflows.pipeline import run_analysis, run_discovery


st.set_page_config(page_title="1. 공고 수집·평가", page_icon="🕵️", layout="wide")
st.title("🕵️ 공고 수집 + 1차 평가")
st.caption("DiscoveryAgent → AnalysisAgent. 게이트1 자동 승급(임계 + 상위 N).")

# ---------------------------------------------------------------------------
# 액션
# ---------------------------------------------------------------------------

a1, a2 = st.columns(2)
with a1:
    if st.button("🕵️ 공고 수집 (Discovery)", use_container_width=True, type="primary"):
        with st.spinner("G2B + 지자체 크롤링 + RFP 파싱 + RAG 인덱싱 중..."):
            log_box = st.empty()
            messages: list[str] = []

            def cb(msg: str) -> None:
                messages.append(msg)
                log_box.code("\n".join(messages[-12:]))

            bids = run_discovery(progress_cb=cb)
        st.success(f"수집 완료: {len(bids)}건")

with a2:
    promote = st.number_input(
        "게이트1 자동 승급 상한", min_value=1, max_value=100, value=20, step=5
    )
    if st.button("⚖️ 1차 평가 (Analysis)", use_container_width=True, type="primary"):
        with st.spinner("외적 메타데이터 기반 채점 중 (prompt caching 활성)..."):
            log_box = st.empty()
            messages: list[str] = []

            def cb2(msg: str) -> None:
                messages.append(msg)
                log_box.code("\n".join(messages[-12:]))

            from agents.analysis_agent import AnalysisAgent
            agent = AnalysisAgent(progress_cb=cb2)
            results = agent.run(max_auto_promote=promote)
        st.success(f"평가 완료: {len(results)}건 채점, 게이트1 자동 승급은 임계 + 상위 {promote}건")

# ---------------------------------------------------------------------------
# 공고 목록
# ---------------------------------------------------------------------------

st.markdown("---")
st.subheader("📋 공고 목록 (상태별)")

status_filter = st.selectbox(
    "상태 필터",
    options=["(전체)"] + [s.value for s in BidStatus],
    index=0,
)

selected = None if status_filter == "(전체)" else next(
    (s for s in BidStatus if s.value == status_filter), None
)
bids = db.list_bids(status=selected)

if not bids:
    st.info("표시할 공고가 없습니다. 위에서 수집을 실행하세요.")
else:
    rows = []
    for b in bids:
        ev = db.get_evaluation(b.bid_id) if hasattr(db, "get_evaluation") else None
        rows.append({
            "공고번호": b.bid_id,
            "발주처": b.agency,
            "사업명": b.title[:50],
            "예산(원)": f"{b.budget_krw:,}" if b.budget_krw else "-",
            "마감": b.deadline.strftime("%Y-%m-%d"),
            "RFP 본문": "✓" if b.rfp_full_text else "",
            "점수": ev.fit_score if ev else "",
            "추천": ev.recommendation if ev else "",
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# 게이트1 수동 승인
# ---------------------------------------------------------------------------

st.markdown("---")
st.subheader("✅ 게이트1 수동 승인 (Notion 미사용 시)")

awaiting = db.list_bids(status=BidStatus.AWAITING_APPROVAL)
if not awaiting:
    st.info("승인 대기 공고가 없습니다.")
else:
    options = [f"{b.bid_id} — {b.title[:60]}" for b in awaiting]
    pick = st.selectbox("승급할 공고 선택", options=options)
    if st.button("APPROVED로 승급"):
        from workflows.human_gates import manual_approve
        bid_id = pick.split(" — ")[0]
        if manual_approve(bid_id):
            st.success(f"{bid_id} 승급 완료. 이제 페이지 2에서 전략을 수립하세요.")
            st.rerun()
        else:
            st.error("승급 실패")
