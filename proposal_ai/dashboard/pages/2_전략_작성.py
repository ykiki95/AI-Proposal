"""페이지 2 — 전략 수립 + 본문 작성 (StrategyAgent + WriterAgent)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from agents.strategy_agent import load_strategy
from schemas.models import BidStatus
from tools import db
from workflows.pipeline import run_strategy, run_write


st.set_page_config(page_title="2. 전략·작성", page_icon="📝", layout="wide")
st.title("📝 전략 수립 + 본문 작성")
st.caption(
    "StrategyAgent: APPROVED → STRATEGY_DONE  (RfpStructured 산출, storage/rfp_cache/)\n"
    "WriterAgent:   STRATEGY_DONE → DRAFT_DONE (specialists.yaml 기반 sub-chapter 병렬)"
)

# ---------------------------------------------------------------------------
# 단일 공고 선택
# ---------------------------------------------------------------------------

approved = db.list_bids(status=BidStatus.APPROVED)
strategy_done = db.list_bids(status=BidStatus.STRATEGY_DONE)
draft_done = db.list_bids(status=BidStatus.DRAFT_DONE)

c1, c2 = st.columns(2)
with c1:
    st.markdown("### 🧭 전략 수립 (Strategy)")
    if not approved:
        st.info("APPROVED 공고가 없습니다. 페이지 1에서 게이트1을 통과시키세요.")
    else:
        options = ["(전체)"] + [f"{b.bid_id} — {b.title[:50]}" for b in approved]
        pick = st.selectbox("대상 공고", options=options, key="strat_pick")
        bid_id = None if pick == "(전체)" else pick.split(" — ")[0]
        if st.button("🧭 StrategyAgent 실행", type="primary", use_container_width=True):
            with st.spinner("RFP TOC + 요구사항 + 전략메모 생성 중 (Opus)..."):
                log_box = st.empty()
                msgs: list[str] = []

                def cb(m: str) -> None:
                    msgs.append(m)
                    log_box.code("\n".join(msgs[-12:]))

                from agents.strategy_agent import StrategyAgent
                agent = StrategyAgent(progress_cb=cb)
                target = approved if bid_id is None else [
                    b for b in approved if b.bid_id == bid_id
                ]
                results = []
                for b in target:
                    try:
                        results.append(agent.run(b))
                    except Exception as e:
                        st.error(f"{b.bid_id} 실패: {e}")
            st.success(f"전략 완료: {len(results)}건 (storage/rfp_cache/<bid_id>.json)")
            st.rerun()

with c2:
    st.markdown("### ✍️ 본문 작성 (Writer)")
    if not strategy_done:
        st.info("STRATEGY_DONE 공고가 없습니다. 위에서 전략을 먼저 실행하세요.")
    else:
        options = ["(전체)"] + [f"{b.bid_id} — {b.title[:50]}" for b in strategy_done]
        pick = st.selectbox("대상 공고", options=options, key="write_pick")
        version = st.number_input("버전", min_value=1, max_value=99, value=1, step=1)
        bid_id = None if pick == "(전체)" else pick.split(" — ")[0]
        if st.button("✍️ WriterAgent 실행", type="primary", use_container_width=True):
            with st.spinner(
                "8명 specialist 병렬 작성 중 (Opus + sub-chapter 재귀 + 수용표 매핑 강제)..."
            ):
                log_box = st.empty()
                msgs: list[str] = []

                def cb2(m: str) -> None:
                    msgs.append(m)
                    log_box.code("\n".join(msgs[-12:]))

                drafts = run_write(bid_id=bid_id, version=int(version), progress_cb=cb2)
            st.success(f"초안 완료: {len(drafts)}건 → BidStatus.DRAFT_DONE")
            st.rerun()


# ---------------------------------------------------------------------------
# RfpStructured / Draft 미리보기
# ---------------------------------------------------------------------------

st.markdown("---")
st.subheader("🔍 산출물 미리보기")

candidates = strategy_done + draft_done
if not candidates:
    st.info("전략/초안이 생성된 공고가 없습니다.")
else:
    options = [f"{b.bid_id} — {b.title[:60]}" for b in candidates]
    pick = st.selectbox("미리볼 공고", options=options, key="preview_pick")
    bid_id = pick.split(" — ")[0]

    rfp = load_strategy(bid_id)
    if rfp is None:
        st.warning("storage/rfp_cache/ 에 산출물 없음.")
    else:
        st.markdown(f"#### 📐 RfpStructured — {rfp.bid_id}")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("TOC 챕터", len(rfp.requested_toc))
        m2.metric("요구사항", len(rfp.requirements))
        m3.metric("평가기준", len(rfp.evaluation_criteria))
        m4.metric("목표 페이지", rfp.target_pages)

        with st.expander("Win themes / Differentiators / Risks"):
            st.markdown("**Win themes**: " + " / ".join(rfp.win_themes) if rfp.win_themes else "_없음_")
            st.markdown("**Differentiators**:")
            for d in rfp.differentiators:
                st.markdown(f"- {d}")
            st.markdown("**Risks**:")
            for r in rfp.risk_notes:
                st.markdown(f"- {r}")

        with st.expander(f"📑 요청 목차 ({len(rfp.requested_toc)} 챕터)"):
            for ch in rfp.requested_toc:
                st.markdown(
                    f"- **{ch.chapter_no}. {ch.title}** "
                    f"(specialty: `{ch.specialty or '-'}`, "
                    f"required: {ch.required_pages or '-'}p, "
                    f"sub: {len(ch.sub_chapters)})"
                )
