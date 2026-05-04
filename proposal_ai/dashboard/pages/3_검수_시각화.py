"""페이지 3 — 검수 + 시각화 (ReviewerAgent + GraphicsAgent)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from config.settings import PROPOSALS_DIR
from schemas.models import BidStatus
from schemas.rfp_schema import AcceptanceTable
from tools import db
from workflows.pipeline import run_graphics, run_review


st.set_page_config(page_title="3. 검수·시각화", page_icon="🛡️", layout="wide")
st.title("🛡️ 검수 + 시각화")
st.caption(
    "ReviewerAgent: DRAFT_DONE → FINAL_APPROVED(A) / UNDER_REVIEW(B/C). "
    "수용표 100% + TOC 일치율 게이트3.\n"
    "GraphicsAgent: FINAL_APPROVED·UNDER_REVIEW → 최종 PPTX + 스토리보드."
)

# ---------------------------------------------------------------------------
# 액션
# ---------------------------------------------------------------------------

c1, c2 = st.columns(2)

with c1:
    st.markdown("### 🛡️ 검수 (Reviewer)")
    draft_done = db.list_bids(status=BidStatus.DRAFT_DONE)
    if not draft_done:
        st.info("DRAFT_DONE 공고가 없습니다. 페이지 2에서 작성을 완료하세요.")
    else:
        options = ["(전체)"] + [f"{b.bid_id} — {b.title[:50]}" for b in draft_done]
        pick = st.selectbox("대상 공고", options=options, key="rev_pick")
        bid_id = None if pick == "(전체)" else pick.split(" — ")[0]
        if st.button("🛡️ ReviewerAgent 실행", type="primary", use_container_width=True):
            with st.spinner("수용표 + TOC 일치율 + 맞춤법 + LLM 검수 중..."):
                log_box = st.empty()
                msgs: list[str] = []

                def cb(m: str) -> None:
                    msgs.append(m)
                    log_box.code("\n".join(msgs[-12:]))

                reports = run_review(bid_id=bid_id, progress_cb=cb)
            st.success(f"검수 완료: {len(reports)}건")
            for r in reports:
                st.write(
                    f"- **{r.bid_id}**: grade=`{r.overall_grade}` / "
                    f"수용표={'✅' if r.acceptance_table_valid else '⚠️'} "
                    f"(미수용 {r.acceptance_rejected_count}) / "
                    f"TOC {r.toc_similarity_score:.2f}"
                )
            st.rerun()

with c2:
    st.markdown("### 🎨 시각화 (Graphics)")
    ready = (
        db.list_bids(status=BidStatus.FINAL_APPROVED)
        + db.list_bids(status=BidStatus.UNDER_REVIEW)
    )
    if not ready:
        st.info("검수 완료된 공고가 없습니다.")
    else:
        options = ["(전체)"] + [f"{b.bid_id} — {b.title[:50]}" for b in ready]
        pick = st.selectbox("대상 공고", options=options, key="gfx_pick")
        bid_id = None if pick == "(전체)" else pick.split(" — ")[0]
        if st.button("🎨 GraphicsAgent 실행", type="primary", use_container_width=True):
            with st.spinner(
                "DesignBrief + 수용표 부록 + PPTX + 스토리보드 생성 중..."
            ):
                log_box = st.empty()
                msgs: list[str] = []

                def cb2(m: str) -> None:
                    msgs.append(m)
                    log_box.code("\n".join(msgs[-12:]))

                briefs = run_graphics(bid_id=bid_id, progress_cb=cb2)
            st.success(f"시각화 완료: {len(briefs)}건 → storage/outputs/*.pptx")
            for b in briefs:
                st.write(
                    f"- theme=`{b.theme_key}` / master={b.master_label} / "
                    f"layout={b.layout_mode.value} / accent={b.accent_hex or '-'}"
                )


# ---------------------------------------------------------------------------
# 검수 리포트 + 수용표 미리보기
# ---------------------------------------------------------------------------

st.markdown("---")
st.subheader("📑 검수 결과 미리보기")

reviewed_bids = (
    db.list_bids(status=BidStatus.UNDER_REVIEW)
    + db.list_bids(status=BidStatus.FINAL_APPROVED)
)
if not reviewed_bids:
    st.info("검수된 공고가 없습니다.")
else:
    options = [f"{b.bid_id} — {b.title[:60]}" for b in reviewed_bids]
    pick = st.selectbox("대상 공고", options=options, key="report_pick")
    bid_id = pick.split(" — ")[0]

    if hasattr(db, "get_quality_report"):
        report = db.get_quality_report(bid_id)
        if report:
            cols = st.columns(4)
            cols[0].metric("Grade", report.overall_grade)
            cols[1].metric("수용표 유효", "✅" if report.acceptance_table_valid else "❌")
            cols[2].metric("미수용", report.acceptance_rejected_count)
            cols[3].metric("TOC 일치율", f"{report.toc_similarity_score:.2f}")
            with st.expander(f"Action items ({len(report.action_items)})"):
                for a in report.action_items:
                    st.markdown(f"- {a}")
            with st.expander(f"맞춤법 이슈 ({len(report.spelling_issues)})"):
                for s in report.spelling_issues[:10]:
                    st.write(s)

    # 수용표 JSON 미리보기 (최신 버전)
    matches = sorted(
        PROPOSALS_DIR.glob(f"{bid_id}_v*_acceptance.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if matches:
        try:
            table = AcceptanceTable.model_validate_json(
                matches[0].read_text(encoding="utf-8")
            )
            st.markdown(f"#### 📋 수용표 — {matches[0].name}")
            st.write(
                f"총 {table.total}건 · 완전수용 {table.full_count}건 · "
                f"미수용 {table.rejected_count}건"
            )
            import pandas as pd
            df = pd.DataFrame([
                {
                    "REQ ID": i.req_id,
                    "요구": i.requirement_summary,
                    "수용": i.acceptance_status.value,
                    "대응 섹션": i.proposal_section,
                }
                for i in table.items
            ])
            st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception as e:
            st.warning(f"수용표 로드 실패: {e}")
    else:
        st.caption("수용표 JSON이 아직 없습니다.")
