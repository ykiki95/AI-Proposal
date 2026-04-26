"""
제안서 자동화 AI 팀 - Streamlit 대시보드

사장 한 명이 PC/모바일 브라우저로 전체 팀을 관리한다.
- 5명 에이전트 시각화 (사람 모양 아바타)
- 3개 인간 승인 게이트 UI
- 모델/지시사항 변경
- 수동 실행 버튼
- 활동 로그 + LLM 호출/비용 감사
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# 상위 디렉터리(proposal_ai/)를 import path에 추가
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st
from loguru import logger

from config.settings import (
    COMPANY,
    KEYS,
    LLM,
    MODELS,
    SYS,
    apply_agent_overrides,
    get_effective_company,
    summary,
)
from agents.park_team import SPECIALISTS as PARK_SPECIALISTS
from schemas.models import BidStatus
from tools import db
from workflows.crew_definition import (
    run_all,
    run_collect,
    run_draft,
    run_evaluate,
    run_propose,
    run_pt,
)
from tools.rfp_analyzer import analyze_rfp
from workflows.human_gates import list_awaiting, manual_approve

# ---------------------------------------------------------------------------
# 초기 설정
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="제안서 자동화 AI 팀",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)

db.init_db()
apply_agent_overrides()
COMPANY_NOW = get_effective_company()

AGENTS = [
    {
        "key": "kim",
        "name": "김탐정",
        "role": "공고 수집",
        "avatar": "🕵️",
        "default_model": "gpt-4o-mini",
        "desc": "G2B와 지자체 사이트를 매일 순찰하며 새 공고를 찾아옵니다.",
    },
    {
        "key": "lee",
        "name": "이판단",
        "role": "적격성 평가",
        "avatar": "⚖️",
        "default_model": "claude-haiku-4-5",
        "desc": "5개 축으로 점수를 매기고 70점 이상만 사장님께 보고합니다.",
    },
    {
        "key": "park",
        "name": "박제안 팀장",
        "role": "제안서 팀장 (8명 지휘)",
        "avatar": "✍️",
        "default_model": "claude-opus-4-7",
        "desc": "공고를 분석해 가변 목차를 짜고, 8명 서브 전문가에게 섹션을 배분해 100쪽급 제안서를 만듭니다. 사업이해/기술솔루션/구축방법론은 To-Be 화면 와이어프레임을 포함합니다.",
    },
    {
        "key": "choi",
        "name": "최피티",
        "role": "PT 디자인",
        "avatar": "🎨",
        "default_model": "claude-sonnet-4-6",
        "desc": "본문을 슬라이드로 옮기고 핵심 메시지를 시각화합니다.",
    },
    {
        "key": "oh",
        "name": "오품질",
        "role": "품질 검수",
        "avatar": "🔍",
        "default_model": "gpt-4o-mini",
        "desc": "맞춤법·용어·RFP 매핑을 점검하고 등급(A/B/C)을 매깁니다.",
    },
]

MODEL_OPTIONS = [
    "gpt-4o-mini",
    "gpt-4o",
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
]


# ---------------------------------------------------------------------------
# 사이드바: 시스템 진단
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown(f"### 📋 {COMPANY_NOW.name}")
    st.caption(f"대표 {COMPANY_NOW.ceo}")
    st.divider()
    st.markdown("**🔌 연결 상태**")
    st.write(f"{'🟢' if LLM.has_anthropic else '🔴'} Anthropic (Claude)")
    st.write(f"{'🟢' if LLM.has_openai else '🔴'} OpenAI (GPT)")
    st.write(f"{'🟢' if KEYS.has_g2b else '🟡 샘플'} 나라장터(G2B)")
    st.write(f"{'🟢' if KEYS.has_notion else '🟡 SQLite'} Notion")
    st.divider()
    st.caption(f"DB: {db.DB_PATH if hasattr(db, 'DB_PATH') else 'storage/db.sqlite'}")
    st.caption(f"임계 점수: {SYS.fit_score_threshold}점")
    if st.button("🔄 새로고침", use_container_width=True):
        st.rerun()
    st.divider()
    with st.expander("🧹 시연용 데이터 초기화"):
        st.caption(
            "공고·평가·제안서·품질·활동·감사로그를 모두 삭제합니다. "
            "회사 정보·자산·레퍼런스·에이전트 설정은 보존됩니다."
        )
        confirm = st.checkbox("정말로 모든 시연 데이터를 삭제합니다", key="clear_confirm")
        if st.button("🗑️ 모두 삭제", disabled=not confirm, use_container_width=True):
            counts = db.clear_demo_data()
            st.success(f"삭제 완료: {counts}")
            st.session_state.pop("bid_detail_id", None)
            st.rerun()


# ---------------------------------------------------------------------------
# 헤더 & 에이전트 팀 시각화
# ---------------------------------------------------------------------------

st.title("📋 제안서 자동화 AI 팀")
st.caption("5명의 가상 직원이 사장님을 도와 공공 제안서를 자동으로 만들어 드립니다.")

agent_cols = st.columns(5)
overrides = db.get_agent_overrides()
recent_activity = db.list_activity(limit=20)
last_event_per_agent = {}
for a in recent_activity:
    if a["agent_key"] not in last_event_per_agent:
        last_event_per_agent[a["agent_key"]] = a

for col, agent in zip(agent_cols, AGENTS):
    with col:
        last = last_event_per_agent.get(agent["key"])
        if last and last["event"] == "start":
            status_emoji = "💼 작업중"
            status_color = "blue"
        elif last and last["event"] == "finish":
            status_emoji = "✅ 대기"
            status_color = "green"
        elif last and last["event"] == "error":
            status_emoji = "⚠️ 오류"
            status_color = "red"
        else:
            status_emoji = "💤 대기"
            status_color = "gray"

        active_model = (overrides.get(agent["key"], {}) or {}).get("model") or agent["default_model"]
        st.markdown(
            f"""
            <div style="text-align:center; padding:18px 8px; background:#f1f5f9; border-radius:14px; border:2px solid #e2e8f0;">
                <div style="font-size:64px; line-height:1;">{agent['avatar']}</div>
                <div style="font-weight:700; font-size:18px; margin-top:6px;">{agent['name']}</div>
                <div style="font-size:13px; color:#64748b;">{agent['role']}</div>
                <div style="margin-top:8px; font-size:12px; color:{status_color};">{status_emoji}</div>
                <div style="margin-top:6px; font-size:11px; color:#94a3b8;">🤖 {active_model}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

st.write("")

# ---------------------------------------------------------------------------
# 인간 승인 게이트 - 상단 알림 띠
# ---------------------------------------------------------------------------

awaiting_ids = list_awaiting()
all_bids = db.list_bids()
status_counts: dict[str, int] = {}
for b in all_bids:
    status_counts[b.status if hasattr(b, "status") else "?"] = (
        status_counts.get(getattr(b, "status", "?"), 0) + 1
    )

# bids는 BidNotice 객체 → status 속성이 없을 수 있음. 직접 DB 조회로 정확히 카운트
from sqlalchemy import func, select as sql_select
from tools.db import session_scope, BidRow, ProposalRow, QualityRow

with session_scope() as s:
    raw_counts = dict(
        s.execute(sql_select(BidRow.status, func.count()).group_by(BidRow.status)).all()
    )

gate1_pending = raw_counts.get(BidStatus.AWAITING_APPROVAL.value, 0)
gate2_pending = raw_counts.get(BidStatus.DRAFT_DONE.value, 0)
gate3_pending = raw_counts.get(BidStatus.UNDER_REVIEW.value, 0)

g1, g2, g3 = st.columns(3)
g1.metric(
    "🛑 게이트1 — 참여 승인 대기",
    f"{gate1_pending}건",
    help="이판단이 70점 이상으로 평가한 공고. 사장님이 승인해야 박제안이 작성을 시작합니다.",
)
g2.metric(
    "🛑 게이트2 — 초안 검토 필요",
    f"{gate2_pending}건",
    help="박제안이 작성한 초안. 사장님이 검토 후 다음 단계로 보내거나 v2 재작성을 지시합니다.",
)
g3.metric(
    "🛑 게이트3 — 최종 승인 대기",
    f"{gate3_pending}건",
    help="오품질이 B/C등급으로 평가한 제안서. 사장님이 최종 승인하거나 박제안에게 재작업을 지시합니다.",
)

st.divider()


# ---------------------------------------------------------------------------
# 탭 구성
# ---------------------------------------------------------------------------

(tab_dash, tab_bids, tab_gates, tab_proposals,
 tab_company, tab_agents, tab_run, tab_logs) = st.tabs([
    "📊 현황판",
    "🔍 공고 목록",
    "🛑 승인 게이트",
    "📝 제안서 검토",
    "🏢 회사 정보",
    "⚙️ 에이전트 설정",
    "🤖 수동 실행",
    "📜 활동 로그",
])


# --- 탭1: 현황판 ----------------------------------------------------------

with tab_dash:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("총 수집 공고", f"{len(all_bids)}건")

    with session_scope() as s:
        approved_count = s.execute(
            sql_select(func.count()).select_from(BidRow).where(
                BidRow.status == BidStatus.APPROVED.value
            )
        ).scalar() or 0
        proposal_count = s.execute(sql_select(func.count()).select_from(ProposalRow)).scalar() or 0
        # 등급 A
        grade_a_count = s.execute(
            sql_select(func.count()).select_from(QualityRow).where(QualityRow.overall_grade == "A")
        ).scalar() or 0

    c2.metric("참여 확정", f"{approved_count}건")
    c3.metric("생성된 제안서", f"{proposal_count}건")
    c4.metric("A등급 통과", f"{grade_a_count}건")

    st.subheader("단계별 공고 분포")
    if raw_counts:
        df_status = pd.DataFrame(
            [{"단계": k, "건수": v} for k, v in raw_counts.items()]
        ).sort_values("건수", ascending=False)
        st.bar_chart(df_status.set_index("단계"))
    else:
        st.info("아직 수집된 공고가 없습니다. '🤖 수동 실행' 탭에서 수집을 시작하세요.")

    st.subheader("최근 활동 타임라인")
    if recent_activity:
        for a in recent_activity[:10]:
            agent_def = next((x for x in AGENTS if x["key"] == a["agent_key"]), None)
            avatar = agent_def["avatar"] if agent_def else "🤖"
            name = agent_def["name"] if agent_def else a["agent_key"]
            ts = a["created_at"].strftime("%m-%d %H:%M:%S") if a["created_at"] else ""
            event_icon = {"start": "▶️", "finish": "✅", "error": "⚠️", "info": "ℹ️"}.get(
                a["event"], "•"
            )
            st.write(f"{ts} {avatar} **{name}** {event_icon} {a['message']}")
    else:
        st.caption("활동 기록이 없습니다.")


# --- 탭2: 공고 목록 -------------------------------------------------------

with tab_bids:
    st.subheader("전체 공고")

    sort_col1, sort_col2 = st.columns([1, 1])
    sort_key = sort_col1.selectbox(
        "정렬 기준",
        ["수집일 (최신순)", "예산 (큰 금액순)", "마감일 (가까운순)", "점수 (높은순)", "추천도"],
    )
    sort_desc = sort_col2.checkbox("내림차순", value=True)

    with session_scope() as s:
        q = s.execute(sql_select(BidRow)).scalars().all()
        rows = []
        for b in q:
            ev = db.get_evaluation(b.bid_id)
            budget_won = b.budget_krw or 0
            rows.append({
                "공고번호": b.bid_id,
                "사업명": b.title,
                "기관": b.agency,
                "예산(원)": f"{budget_won:,}",
                "_budget_raw": budget_won,
                "마감일": b.deadline.strftime("%Y-%m-%d") if b.deadline else "",
                "_deadline_raw": b.deadline,
                "_collected_raw": b.collected_at,
                "단계": b.status,
                "점수": ev.fit_score if ev else None,
                "추천": ev.recommendation if ev else "-",
            })

    if rows:
        df = pd.DataFrame(rows)

        # 정렬 적용
        sort_map = {
            "수집일 (최신순)": "_collected_raw",
            "예산 (큰 금액순)": "_budget_raw",
            "마감일 (가까운순)": "_deadline_raw",
            "점수 (높은순)": "점수",
            "추천도": "추천",
        }
        sort_field = sort_map[sort_key]
        # 마감일 가까운순은 오름차순이 직관적
        ascending = (sort_key == "마감일 (가까운순)") != sort_desc
        df_sorted = df.sort_values(sort_field, ascending=ascending, na_position="last")

        display = df_sorted[["공고번호", "사업명", "기관", "예산(원)", "마감일", "단계", "점수", "추천"]].reset_index(drop=True)

        # 페이지네이션 (한 페이지 25행)
        PAGE_SIZE = 25
        total = len(display)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        pcol1, pcol2 = st.columns([1, 5])
        page = pcol1.number_input(
            f"페이지 (총 {total_pages})",
            min_value=1, max_value=total_pages, value=1, step=1,
            key="bid_page",
        )
        pcol2.caption(f"전체 {total}건 중 {(page-1)*PAGE_SIZE+1}~{min(page*PAGE_SIZE, total)}건 표시 · 공고번호를 클릭하면 아래 공고 상세에 표시됩니다.")
        page_df = display.iloc[(page-1)*PAGE_SIZE : page*PAGE_SIZE]

        # 헤더 (⭐ 찜 컬럼 추가)
        col_widths = [0.4, 1.4, 3.0, 1.4, 1.2, 1.0, 0.9, 0.5, 0.6]
        header = st.columns(col_widths)
        for h, label in zip(header, ["⭐", "공고번호", "사업명", "기관", "예산(원)", "마감일", "단계", "점수", "추천"]):
            h.markdown(f"**{label}**")
        st.divider()

        # 행 — ⭐ 찜 토글 + 공고번호 클릭으로 상세 표시
        for _, row in page_df.iterrows():
            bid_id = row["공고번호"]
            cells = st.columns(col_widths)
            pinned = db.is_bid_pinned(bid_id)
            star_label = "★" if pinned else "☆"
            if cells[0].button(star_label, key=f"star_{bid_id}",
                               help="찜하면 점수와 무관하게 게이트1로 자동 이관됩니다"):
                db.set_bid_pinned(bid_id, not pinned)
                st.rerun()
            if cells[1].button(bid_id, key=f"pick_{bid_id}", use_container_width=True):
                st.session_state["bid_detail_id"] = bid_id
                st.rerun()
            cells[2].write(row["사업명"])
            cells[3].write(row["기관"])
            cells[4].write(row["예산(원)"])
            cells[5].write(row["마감일"])
            cells[6].write(row["단계"])
            cells[7].write(str(row["점수"]) if row["점수"] is not None else "-")
            cells[8].write(row["추천"])

        # 선택된 공고 결정
        sel = st.session_state.get("bid_detail_id")
        all_ids = display["공고번호"].tolist()
        if not sel or sel not in all_ids:
            sel = all_ids[0] if all_ids else None
            if sel:
                st.session_state["bid_detail_id"] = sel

        st.divider()
        st.subheader("공고 상세")
        if sel:
            bid = db.get_bid(sel)
            ev = db.get_evaluation(sel)
            col_a, col_b = st.columns([2, 1])
            with col_a:
                st.markdown(f"### {bid.title}")
                st.write(f"**공고번호:** `{bid.bid_id}`")
                st.write(f"**발주기관:** {bid.agency}")
                st.write(f"**예산:** {(bid.budget_krw or 0):,}원")
                st.write(f"**기간:** {bid.duration_months}개월")
                st.write(f"**마감:** {bid.deadline.strftime('%Y-%m-%d %H:%M')}")
                with session_scope() as _s:
                    _br = _s.get(BidRow, bid.bid_id)
                    _stage = _br.status if _br else "-"
                st.write(f"**현재 단계:** `{_stage}`")
                if bid.rfp_url:
                    st.markdown(f"🔗 [원문 공고 페이지 열기]({bid.rfp_url})")

                # --- 액션 (찜 / 게이트1 보내기) ---
                act_cols = st.columns([1, 1, 2])
                _is_pinned = db.is_bid_pinned(bid.bid_id)
                if act_cols[0].button(
                    "★ 찜 해제" if _is_pinned else "☆ 찜하기",
                    key=f"detail_star_{bid.bid_id}",
                ):
                    db.set_bid_pinned(bid.bid_id, not _is_pinned)
                    st.rerun()
                if act_cols[1].button("📨 게이트1로 보내기", key=f"to_gate1_{bid.bid_id}", type="primary"):
                    db.set_bid_status(bid.bid_id, BidStatus.AWAITING_APPROVAL)
                    st.success("승인 대기 목록(게이트1)에 추가되었습니다.")
                    st.rerun()

                # --- RFP 본문 입력: PDF 업로드 또는 텍스트 직접 입력 ---
                with st.expander("📜 RFP 원문 입력 (PDF 업로드 또는 텍스트 붙여넣기)",
                                 expanded=not bool(bid.rfp_full_text)):
                    st.caption(
                        "원문 공고 페이지에서 받은 RFP 파일을 업로드하면 자동으로 텍스트와 표를 추출합니다. "
                        "PDF / TXT 만 지원합니다 (HWP는 한글에서 PDF로 저장 후 올려 주세요). "
                        "추출된 텍스트는 자동으로 사업개요·요구사항·목차 핵심부만 추려 저장됩니다."
                    )
                    up = st.file_uploader(
                        "RFP 파일 (PDF/TXT)", type=["pdf", "txt"],
                        key=f"rfp_upload_{bid.bid_id}",
                        accept_multiple_files=False,
                    )
                    if up is not None and st.button("📥 추출하여 저장", key=f"do_extract_{bid.bid_id}", type="primary"):
                        from tools.rfp_extractor import extract_text_from_upload
                        try:
                            with st.spinner(f"{up.name} 분석 중..."):
                                ext = extract_text_from_upload(up.name, up.getvalue())
                            db.update_bid_full_text(bid.bid_id, ext.core_text or ext.full_text)
                            st.success(
                                f"✅ 추출 완료: 페이지 {ext.page_count}장 / 표 {ext.table_count}개 / "
                                f"전체 {len(ext.full_text):,}자 → 핵심부 {len(ext.core_text):,}자 저장"
                                + (f" (시작: '{ext.started_from}')" if ext.started_from else "")
                                + (f" / (잘림: '{ext.truncated_from}' 이전까지)" if ext.truncated_from else "")
                            )
                            st.rerun()
                        except Exception as e:
                            st.error(f"추출 실패: {e}")

                    rfp_value = st.text_area(
                        "RFP 본문 (직접 편집 가능)",
                        value=bid.rfp_full_text or "",
                        height=280,
                        key=f"rfp_edit_{bid.bid_id}",
                        help="PDF 추출이 깨졌거나 직접 붙여넣고 싶을 때 사용하세요. 저장 시 정밀 분석 캐시는 초기화됩니다.",
                    )
                    if st.button("💾 RFP 본문 저장", key=f"save_rfp_{bid.bid_id}"):
                        db.update_bid_full_text(bid.bid_id, rfp_value)
                        st.success(f"RFP 본문 {len(rfp_value)}자가 저장되었습니다.")
                        st.rerun()

                # --- 심층 분석 ---
                st.markdown("#### 🔬 심층 분석 (파견·소재지·독소조항 등)")
                # 항상 DB에서 최신 본문/분석 조회
                with session_scope() as s:
                    row = s.get(BidRow, bid.bid_id)
                    deep = row.rfp_deep_analysis if row else None
                    cur_full = row.rfp_full_text if row else None

                # 심층분석은 'RFP 본문(rfp_full_text)'이 실제로 있을 때만 활성화.
                # rfp_summary는 G2B에서 URL만 들어오는 경우가 많아 분석 의미가 없음.
                has_body = bool((cur_full or "").strip())
                btn_col1, btn_col_g, btn_col2 = st.columns([1, 1, 3])
                if btn_col1.button(
                    "🔄 다시 분석" if deep else "🔬 심층 분석 실행",
                    key=f"analyze_{bid.bid_id}",
                    disabled=not has_body,
                ):
                    with st.spinner("박제안 모델로 RFP 본문을 정밀 분석 중... (30~60초)"):
                        # DB의 최신 본문을 반영해서 분석
                        bid_for_analysis = db.get_bid(bid.bid_id) or bid
                        result = analyze_rfp(bid_for_analysis)
                        db.update_bid_deep_analysis(bid.bid_id, result)
                    st.rerun()
                # 박제안 8섹션 작성 가이드를 RFP에서 자동 추출 (재실행)
                if btn_col_g.button(
                    "📋 작성 가이드 재생성",
                    key=f"toc_guide_{bid.bid_id}",
                    disabled=not has_body,
                    help="박제안 팀장이 RFP를 다시 읽고 8명 전문가별 작성 지시문을 새로 만듭니다.",
                ):
                    from agents.park_team import ParkTeam
                    with st.spinner("박제안 팀장이 RFP에서 8섹션 작성 가이드를 추출 중... (15~30초)"):
                        bid_for_guide = db.get_bid(bid.bid_id) or bid
                        toc = ParkTeam().plan_toc(bid_for_guide, force_refresh_guides=True)
                        filled = sum(1 for t in toc if (t.get("brief") or "").strip())
                    st.success(f"가이드 {filled}/8 섹션 채움 완료. 다음 제안서 생성 시 자동 반영됩니다.")
                if not has_body:
                    btn_col2.warning("RFP 본문이 없습니다. 위 '📜 RFP 원문' 탭에 본문을 붙여넣어 저장한 뒤 분석을 실행하세요.")
                elif deep:
                    st.markdown(deep)
                else:
                    btn_col2.caption(
                        f"RFP 원문 {len((cur_full or bid.rfp_summary or ''))}자가 준비되어 있습니다. "
                        "LLM이 파견·근무지·소재지 제약·독소조항 등을 정리합니다."
                    )

            with col_b:
                if ev:
                    st.metric("적합도 점수", f"{ev.fit_score}점", delta=ev.recommendation)
                    st.write("**점수 분해**")
                    st.json(ev.score_breakdown)
                    if ev.opportunity_factors:
                        st.success("기회: " + ", ".join(ev.opportunity_factors[:3]))
                    if ev.risk_factors:
                        st.warning("리스크: " + ", ".join(ev.risk_factors[:3]))
                else:
                    st.info("아직 평가되지 않았습니다.")
    else:
        st.info("수집된 공고가 없습니다.")


# --- 탭3: 승인 게이트 ----------------------------------------------------

with tab_gates:
    st.subheader("🛑 게이트1: 참여 여부 승인")
    st.caption(
        "이판단이 추천한 공고를 승인하면 박제안이 제안서 작성을 시작합니다. "
        "✍️ 작성 중에는 8명 전문가가 동시 작업하며 5~10분이 소요됩니다. "
        "진행률 바를 참고하세요 (상단 에이전트 카드는 작업 완료 후 갱신됩니다)."
    )

    with session_scope() as s:
        awaiting = s.execute(
            sql_select(BidRow).where(BidRow.status == BidStatus.AWAITING_APPROVAL.value)
        ).scalars().all()
        awaiting_data = [
            {
                "bid_id": b.bid_id,
                "title": b.title,
                "agency": b.agency,
                "budget": b.budget_krw or 0,
                "deadline": b.deadline,
            }
            for b in awaiting
        ]

    # ─────────────────────────────────────────────────────────────
    # 작업 중 표시 (한 건 작업 중일 때 다른 행 클릭이 어색해지는 문제 해결)
    # ─────────────────────────────────────────────────────────────
    drafting_bid = st.session_state.get("g1_drafting_bid")
    if drafting_bid:
        st.warning(f"⚙️ 박제안 팀이 [{drafting_bid}] 초안을 작성 중입니다. 완료될 때까지 다른 공고는 잠시 대기됩니다.")
        bar_park = st.progress(0.0, text="박제안 초안 준비 중...")
        msg_park = st.empty()

        def _mk(bar, msg, label):
            def cb(d, t, m):
                r = (d / t) if t > 0 else 1.0
                r = min(max(r, 0.0), 1.0)
                bar.progress(r, text=f"{label} {d}/{t} ({int(r*100)}%)")
                msg.caption(f"📍 {m}")
            return cb

        try:
            out = run_draft(
                bid_id=drafting_bid,
                park_cb=_mk(bar_park, msg_park, "박제안 초안"),
            )
            st.success(
                f"✨ 초안 완료! {len(out['drafts'])}건 작성됨. "
                "다음 단계는 게이트2(초안 검토)에서 기획 PPT(스토리보드)를 확인 후 PT 작성을 진행하세요."
            )
        except Exception as e:
            st.error(f"초안 작성 실패: {e}")
        finally:
            st.session_state.pop("g1_drafting_bid", None)
        st.rerun()

    if not awaiting_data:
        st.success("승인 대기 중인 공고가 없습니다.")
    else:
        for item in awaiting_data:
            ev = db.get_evaluation(item["bid_id"])
            bid_obj = db.get_bid(item["bid_id"])
            has_body = bool((bid_obj.rfp_full_text or "").strip()) if bid_obj else False
            has_deep = False
            if bid_obj:
                with session_scope() as _s:
                    _br = _s.get(BidRow, item["bid_id"])
                    has_deep = bool(_br and (_br.rfp_deep_analysis or "").strip())
            with st.container(border=True):
                cc1, cc2, cc3, cc4 = st.columns([3, 1, 1.2, 1.3])
                with cc1:
                    pin_mark = "⭐ " if db.is_bid_pinned(item["bid_id"]) else ""
                    st.markdown(f"**{pin_mark}{item['title']}**")
                    st.caption(
                        f"{item['agency']} · 예산 {item['budget']:,}원 · "
                        f"마감 {item['deadline'].strftime('%Y-%m-%d')}"
                        + (f" · 🔬 정밀 분석 캐시 있음" if has_deep else "")
                    )
                    # 🔗 공고 원문 링크 — 사장님이 RFP 첨부파일을 받으러 가는 진입점
                    rfp_link = (bid_obj.rfp_url if bid_obj else "") or ""
                    if rfp_link.startswith("http"):
                        st.markdown(
                            f"🔗 [나라장터 공고 원문 / RFP 첨부파일 보러가기]({rfp_link})"
                            "  ·  새 탭에서 공고를 열어 RFP를 내려받은 뒤 아래 업로드"
                        )
                    else:
                        st.caption(
                            "🔗 공고 원문 링크 미수집 — 나라장터에서 공고번호로 직접 검색해 RFP를 내려받으세요."
                        )
                with cc2:
                    if ev:
                        st.metric("점수", f"{ev.fit_score}점")
                with cc3:
                    if st.button(
                        "🔬 RFP 정밀 분석" if not has_deep else "🔁 정밀 재분석",
                        key=f"deep_{item['bid_id']}",
                        disabled=not has_body,
                        help=(
                            "이 공고만 RFP 본문을 정밀 분석합니다. "
                            "본문이 없으면 비활성화됩니다 — 공고 목록에서 PDF 업로드 후 다시 시도하세요."
                            if has_body else
                            "RFP 본문이 없습니다. 공고 목록 → 공고 상세 → PDF 업로드/붙여넣기 후 다시 시도하세요."
                        ),
                    ):
                        with st.spinner("이판단이 RFP 본문을 정밀 분석 중..."):
                            from agents.lee_judge import LeeJudge
                            LeeJudge().deep_review(bid_obj)
                        st.rerun()
                with cc4:
                    if st.button("✅ 참여확정", key=f"approve_{item['bid_id']}", type="primary"):
                        manual_approve(item["bid_id"])
                        st.session_state["g1_drafting_bid"] = item["bid_id"]
                        st.rerun()
                    if st.button("❌ 비참여", key=f"reject_{item['bid_id']}"):
                        db.set_bid_status(item["bid_id"], BidStatus.REJECTED)
                        st.rerun()
                # 📜 RFP 업로드 (본문이 없으면 펼쳐서 보여줌, 있어도 직전에 추출 성공했다면 펼쳐둠)
                _last_msg_key = f"g1_rfp_msg_{item['bid_id']}"
                _last_msg = st.session_state.get(_last_msg_key)
                # 직전 카드 작업 결과 메시지(rerun에 살아남도록 session_state에 저장한 것)
                if _last_msg:
                    st.success(_last_msg)
                    st.session_state.pop(_last_msg_key, None)

                with st.expander(
                    "📜 RFP 원문 업로드 / 붙여넣기"
                    + ("" if has_body else " — ⚠️ 정밀분석을 위해 필요")
                    + (f" — ✅ 본문 {len(bid_obj.rfp_full_text):,}자 저장됨" if has_body and bid_obj else ""),
                    expanded=not has_body,
                ):
                    st.caption(
                        "여기서 바로 RFP를 올리면 공고 목록으로 이동하지 않아도 정밀 분석이 가능합니다. "
                        "PDF/TXT 지원 (HWP는 한글에서 PDF로 저장 후 업로드)."
                    )
                    if has_body and bid_obj:
                        st.info(
                            f"이미 RFP 본문 **{len(bid_obj.rfp_full_text):,}자**가 저장되어 있습니다. "
                            "다른 파일로 교체하려면 아래에 새로 업로드하세요."
                        )
                    g1_up = st.file_uploader(
                        "RFP 파일", type=["pdf", "txt"],
                        key=f"g1_rfp_up_{item['bid_id']}",
                        accept_multiple_files=False,
                        label_visibility="collapsed",
                    )
                    cu1, cu2 = st.columns([1, 1])
                    if g1_up is not None and cu1.button(
                        "📥 추출하여 저장", key=f"g1_extract_{item['bid_id']}", type="primary"
                    ):
                        from tools.rfp_extractor import extract_text_from_upload
                        try:
                            with st.spinner(
                                f"{g1_up.name} 분석 중... (큰 PDF는 30~90초 걸릴 수 있습니다)"
                            ):
                                ext = extract_text_from_upload(g1_up.name, g1_up.getvalue())
                            saved_text = ext.core_text or ext.full_text
                            db.update_bid_full_text(item["bid_id"], saved_text)
                            msg = (
                                f"✅ 추출 완료: 페이지 {ext.page_count}장 / 표 {ext.table_count}개 / "
                                f"본문 {len(saved_text):,}자 저장됨. 위 '🔬 RFP 정밀 분석' 버튼이 활성화됐습니다."
                            )
                            st.session_state[_last_msg_key] = msg
                            st.toast(msg, icon="✅")
                            st.rerun()
                        except Exception as e:
                            st.error(f"추출 실패: {e}")
                            logger.error(f"[게이트1] RFP 추출 실패 {item['bid_id']}: {e}")
                    g1_paste = cu2.text_area(
                        "또는 텍스트 붙여넣기",
                        value="",
                        height=120,
                        key=f"g1_rfp_paste_{item['bid_id']}",
                        placeholder="RFP 본문을 직접 붙여넣고 아래 저장",
                    )
                    if g1_paste and cu2.button("💾 텍스트 저장", key=f"g1_save_paste_{item['bid_id']}"):
                        db.update_bid_full_text(item["bid_id"], g1_paste)
                        msg = f"✅ RFP 본문 {len(g1_paste):,}자 저장됨. 위 '🔬 RFP 정밀 분석' 버튼이 활성화됐습니다."
                        st.session_state[_last_msg_key] = msg
                        st.toast(msg, icon="✅")
                        st.rerun()

                # 📑 RFP 목차 자동 추출 (RFP 본문이 있을 때만)
                if has_body:
                    _saved_toc = db.get_proposal_toc(item["bid_id"]) or []
                    _saved_gctx = db.get_proposal_toc_global_context(item["bid_id"]) or {}
                    _has_tree = any(s.get("sub_chapters") for s in _saved_toc)
                    _ext_msg_key = f"g1_toc_msg_{item['bid_id']}"
                    if st.session_state.get(_ext_msg_key):
                        st.success(st.session_state.pop(_ext_msg_key))

                    with st.expander(
                        "📑 RFP 목차 자동 추출"
                        + (f" — ✅ {len(_saved_toc)}챕터 트리 적용 중" if _has_tree
                           else " — RFP에 명시된 목차를 그대로 따라 작성합니다"),
                        expanded=False,
                    ):
                        st.caption(
                            "RFP 본문에서 발주처가 요구한 제안서 목차(Ⅰ~Ⅴ + 붙임 등)를 LLM이 1회 분석으로 추출합니다. "
                            "추출된 목차는 박제안 팀장이 100% 그대로 따라 작성합니다."
                        )
                        ec1, ec2 = st.columns([1, 1])
                        if ec1.button(
                            "🚀 목차 추출 실행 (LLM 1회)",
                            key=f"g1_extract_toc_{item['bid_id']}",
                            type="primary",
                        ):
                            from agents.park_team import ParkTeam
                            try:
                                with st.spinner("RFP 목차 추출 중... (15~40초)"):
                                    bid_obj_local = db.get_bid(item["bid_id"])
                                    chapters, gctx = ParkTeam().plan_toc_from_rfp(bid_obj_local)
                                if chapters:
                                    st.session_state[_ext_msg_key] = (
                                        f"✅ {len(chapters)}챕터 / sub "
                                        f"{sum(len(c.get('sub_chapters', [])) for c in chapters)}개 추출됨. "
                                        "박제안 팀장이 이 목차로 작성합니다."
                                    )
                                else:
                                    st.error("추출 실패 — RFP 본문이 너무 짧거나 LLM 응답이 비었습니다.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"추출 실패: {e}")
                                logger.error(f"[게이트1] RFP 목차 추출 실패 {item['bid_id']}: {e}")
                        if _has_tree and ec2.button(
                            "🗑️ 추출 결과 삭제 (8명 기본 구조로 복귀)",
                            key=f"g1_clear_toc_{item['bid_id']}",
                        ):
                            db.save_proposal_toc(item["bid_id"], [], global_context={})
                            st.session_state[_ext_msg_key] = "추출 결과를 삭제했습니다. 다음 작성부터 8명 기본 구조를 사용합니다."
                            st.rerun()

                        # 미리보기 — 트리 표시
                        if _saved_toc:
                            if _saved_gctx.get("vision"):
                                st.markdown(
                                    f"**🎯 한 줄 비전:** {_saved_gctx['vision']}"
                                )
                            if _saved_gctx.get("key_keywords"):
                                st.markdown(
                                    "**🔑 핵심 키워드:** "
                                    + ", ".join(f"`{k}`" for k in _saved_gctx["key_keywords"][:5])
                                )
                            if _saved_gctx.get("differentiators"):
                                st.markdown("**🏆 자사 차별점:**")
                                for _d in _saved_gctx["differentiators"][:3]:
                                    st.markdown(f"- {_d}")
                            st.markdown("---")
                            st.markdown(f"**📑 챕터 트리 ({len(_saved_toc)}개)**")
                            _total_p = sum(c.get("target_pages") or 0 for c in _saved_toc)
                            st.caption(f"총 목표 분량: 약 {_total_p}페이지")
                            for _ci, _c in enumerate(_saved_toc, 1):
                                _subs = _c.get("sub_chapters") or []
                                with st.container(border=True):
                                    st.markdown(
                                        f"**{_c.get('title', '(제목 없음)')}** "
                                        f"· `{_c.get('specialty', '?')}` "
                                        f"· 🎭 {_c.get('display_role', '-')} "
                                        f"· 📄 {_c.get('target_pages', 0)}p"
                                        + (f" · sub {len(_subs)}개" if _subs else "")
                                    )
                                    if _c.get("brief"):
                                        st.caption(f"📝 {_c['brief'][:200]}")
                                    if _subs:
                                        for _s in _subs:
                                            st.markdown(
                                                f"  └ {_s.get('title', '?')} "
                                                f"· {_s.get('target_pages', 0)}p"
                                            )

                # 정밀 분석 결과를 줄여서 노출
                if has_deep:
                    with st.expander("🔬 정밀 분석 결과 보기", expanded=False):
                        with session_scope() as _s2:
                            _b = _s2.get(BidRow, item["bid_id"])
                            st.markdown(_b.rfp_deep_analysis or "_없음_")

    st.divider()

    st.subheader("🛑 게이트2: 초안 검토")
    st.caption("박제안이 작성한 초안을 검토하고 다음 단계로 보내거나 재작성을 지시합니다.")
    with session_scope() as s:
        drafts_done = s.execute(
            sql_select(BidRow).where(BidRow.status == BidStatus.DRAFT_DONE.value)
        ).scalars().all()
        drafts_data = [{"bid_id": b.bid_id, "title": b.title} for b in drafts_done]

    if not drafts_data:
        st.info("검토 대기 중인 초안이 없습니다.")
    else:
        for item in drafts_data:
            with st.container(border=True):
                cc1, cc2 = st.columns([3, 1])
                with cc1:
                    st.markdown(f"**{item['title']}** ({item['bid_id']})")
                    proposals = db.list_proposals(item["bid_id"])
                    if proposals:
                        latest = proposals[0]
                        st.caption(f"v{latest['version']} · {latest['section_count']}개 섹션")
                        sb_path = latest.get("storyboard_path")
                        if sb_path and Path(sb_path).exists():
                            with open(sb_path, "rb") as f:
                                st.download_button(
                                    "📋 기획 PPT(스토리보드) 다운로드",
                                    f.read(),
                                    file_name=Path(sb_path).name,
                                    key=f"sb_{item['bid_id']}",
                                    type="primary",
                                    help="페이지별 핵심 메시지·레이아웃·시각요소·작성 프롬프트를 슬라이드로 정리한 검토용 PPT",
                                )
                        else:
                            st.caption("⚠️ 스토리보드가 아직 생성되지 않았습니다. (구버전 초안)")
                        if latest.get("docx_path") and Path(latest["docx_path"]).exists():
                            with open(latest["docx_path"], "rb") as f:
                                st.download_button(
                                    "📄 본문 DOCX (참고용 백업)",
                                    f.read(),
                                    file_name=Path(latest["docx_path"]).name,
                                    key=f"dl_{item['bid_id']}",
                                )
                with cc2:
                    if st.button("📊 PT 작성 시작", key=f"to_pt_{item['bid_id']}", type="primary"):
                        db.set_bid_status(item["bid_id"], BidStatus.APPROVED)
                        st.success("최피티가 PPT 슬라이드를, 오품질이 검수를 시작합니다.")

                        bar_choi = st.progress(0.0, text="최피티 PT 변환 중...")
                        msg_choi = st.empty()
                        bar_oh = st.progress(0.0, text="오품질 검수 대기 중...")
                        msg_oh = st.empty()

                        def _mk2(bar, msg, label):
                            def cb(d, t, m):
                                r = (d / t) if t > 0 else 1.0
                                r = min(max(r, 0.0), 1.0)
                                bar.progress(r, text=f"{label} {d}/{t} ({int(r*100)}%)")
                                msg.caption(f"📍 {m}")
                            return cb

                        out = run_pt(
                            bid_id=item["bid_id"],
                            choi_cb=_mk2(bar_choi, msg_choi, "최피티 PPT"),
                            oh_cb=_mk2(bar_oh, msg_oh, "오품질 검수"),
                        )
                        st.success(
                            f"✨ PPT {len(out['pts'])}건 / 검수 {len(out['reports'])}건 완료! "
                            "제안서 검토 탭에서 DOCX와 PPTX를 모두 다운로드할 수 있습니다."
                        )
                        st.rerun()

    st.divider()

    st.subheader("🛑 게이트3: 최종 승인")
    st.caption("오품질이 B/C등급으로 평가한 제안서. 그대로 승인하거나 박제안에게 재작업을 보냅니다.")
    with session_scope() as s:
        review_data = s.execute(
            sql_select(BidRow).where(BidRow.status == BidStatus.UNDER_REVIEW.value)
        ).scalars().all()
        review_list = [{"bid_id": b.bid_id, "title": b.title} for b in review_data]

    if not review_list:
        st.info("최종 승인 대기 중인 제안서가 없습니다.")
    else:
        for item in review_list:
            reports = db.list_quality_reports(item["bid_id"])
            proposals_for_bid = [p for p in db.list_proposals() if p["bid_id"] == item["bid_id"]]
            latest_p = proposals_for_bid[0] if proposals_for_bid else None
            with st.container(border=True):
                cc1, cc2 = st.columns([3, 1.4])
                with cc1:
                    st.markdown(f"**{item['title']}**")
                    if reports:
                        r = reports[0]
                        st.write(f"등급: **{r['overall_grade']}** · 맞춤법 {r['spelling_count']}건 / 일관성 {r['consistency_count']}건")
                        if r["action_items"]:
                            st.warning("조치사항: " + " / ".join(r["action_items"][:3]))
                with cc2:
                    # 발표문 분량 입력
                    minutes = st.number_input(
                        "발표 분량(분)",
                        min_value=10, max_value=30, value=18, step=1,
                        key=f"speech_min_{item['bid_id']}",
                        help="본 발표 시간. Q&A 10~15분은 별도로 시나리오에 포함됩니다.",
                    )
                    has_script = bool(latest_p and latest_p.get("script_path") and Path(latest_p["script_path"]).exists())
                    if st.button(
                        "🔁 발표문 재생성" if has_script else "🎤 발표문 생성",
                        key=f"gen_speech_{item['bid_id']}",
                        disabled=not latest_p,
                    ):
                        try:
                            with st.spinner(f"발표 시나리오({minutes}분) 생성 중..."):
                                from tools.script_generator import render_script
                                from schemas.models import ProposalDraft, ProposalSection
                                bid_obj_full = db.get_bid(item["bid_id"])
                                draft_obj = ProposalDraft(
                                    bid_id=item["bid_id"],
                                    version=latest_p["version"],
                                    sections=[ProposalSection(**sec) for sec in db.list_proposals_full_sections(item["bid_id"], latest_p["version"])] if hasattr(db, "list_proposals_full_sections") else [],
                                )
                                # ProposalRow에서 sections 직접 조회
                                if not draft_obj.sections:
                                    with session_scope() as _s3:
                                        from tools.db import ProposalRow
                                        _pr = _s3.execute(
                                            sql_select(ProposalRow).where(
                                                ProposalRow.bid_id == item["bid_id"],
                                                ProposalRow.version == latest_p["version"],
                                            )
                                        ).scalar_one_or_none()
                                        if _pr and _pr.sections:
                                            draft_obj.sections = [ProposalSection(**sec) for sec in _pr.sections]
                                path = render_script(bid_obj_full, draft_obj, minutes=minutes)
                                db.update_proposal_script(item["bid_id"], latest_p["version"], str(path))
                            st.success(f"✅ 발표문 생성 완료: {path.name}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"발표문 생성 실패: {e}")
                    if has_script:
                        with open(latest_p["script_path"], "rb") as f:
                            st.download_button(
                                "🎤 발표문 DOCX 다운로드",
                                f.read(),
                                file_name=Path(latest_p["script_path"]).name,
                                key=f"dl_speech_{item['bid_id']}",
                            )
                    if st.button("✅ 최종 승인", key=f"final_{item['bid_id']}", type="primary"):
                        db.set_bid_status(item["bid_id"], BidStatus.FINAL_APPROVED)
                        st.rerun()


# --- 탭4: 제안서 검토 ----------------------------------------------------

with tab_proposals:
    st.subheader("생성된 제안서")
    proposals = db.list_proposals()
    if not proposals:
        st.info("아직 생성된 제안서가 없습니다.")
    else:
        for p in proposals:
            with st.container(border=True):
                bid = db.get_bid(p["bid_id"])
                title = bid.title if bid else p["bid_id"]
                st.markdown(f"### {title}")
                st.caption(
                    f"{p['bid_id']} · v{p['version']} · {p['section_count']}개 섹션 · "
                    f"{p['generated_at'].strftime('%Y-%m-%d %H:%M')}"
                )
                cols = st.columns(4)
                if p.get("docx_path") and Path(p["docx_path"]).exists():
                    with cols[0], open(p["docx_path"], "rb") as f:
                        st.download_button(
                            "📄 제안서 DOCX",
                            f.read(),
                            file_name=Path(p["docx_path"]).name,
                            key=f"docx_{p['id']}",
                        )
                if p.get("pptx_path") and Path(p["pptx_path"]).exists():
                    with cols[1], open(p["pptx_path"], "rb") as f:
                        st.download_button(
                            "📊 발표 PPTX",
                            f.read(),
                            file_name=Path(p["pptx_path"]).name,
                            key=f"pptx_{p['id']}",
                        )

                # 🎨 정디자 + 새 PPTX 빌더 (slides_json + 마스터 기반)
                if cols[3].button("🎨 정디자+새 PPT", key=f"v2_{p['id']}",
                                   help="박제안 팀의 슬라이드 스펙(JSON) + 정디자가 고른 마스터로 새 PPT 생성"):
                    try:
                        from agents.jung_designer import JungDesigner
                        from tools.pptx_builder import build_proposal_pptx_v2
                        from schemas.models import ProposalDraft, ProposalSection
                        from sqlalchemy import select
                        from tools.db import ProposalRow, session_scope
                        with st.spinner("정디자 디자인 결정 + PPTX 빌드 중…"):
                            with session_scope() as s:
                                # 같은 bid_id에 v1/v2가 모두 있을 수 있어 단일 버전을 명시 조회
                                row = s.execute(
                                    select(ProposalRow)
                                    .where(
                                        ProposalRow.bid_id == p["bid_id"],
                                        ProposalRow.version == p["version"],
                                    )
                                ).scalars().first()
                            if not row or not bid:
                                st.error("초안을 찾을 수 없습니다.")
                            else:
                                draft = ProposalDraft(
                                    bid_id=row.bid_id, version=row.version,
                                    sections=[ProposalSection(**sec) for sec in (row.sections or [])],
                                )
                                brief = JungDesigner().design(bid, draft)
                                out_path = build_proposal_pptx_v2(bid, draft, brief)
                                db.update_proposal_pptx(bid.bid_id, draft.version, str(out_path))
                                st.success(
                                    f"🎨 새 PPTX 생성 완료 — 마스터: {brief.master_label or brief.theme_key}"
                                )
                                if brief.rationale:
                                    st.caption(f"💬 정디자: {brief.rationale[:200]}")
                                st.rerun()
                    except Exception as e:
                        st.error(f"새 PPTX 빌드 실패: {e}")
                if p.get("script_path") and Path(p["script_path"]).exists():
                    with cols[2], open(p["script_path"], "rb") as f:
                        st.download_button(
                            "🎤 발표문 DOCX",
                            f.read(),
                            file_name=Path(p["script_path"]).name,
                            key=f"script_{p['id']}",
                        )

                reports = db.list_quality_reports(p["bid_id"])
                if reports:
                    r = reports[0]
                    cols[3].metric("검수 등급", r["overall_grade"])


# --- 탭5: 회사 정보 ------------------------------------------------------

with tab_company:
    st.subheader("🏢 우리 회사 정보")
    st.caption(
        "여기에 입력한 정보는 모든 제안서 작성에 자동 반영됩니다. "
        "회사 소개서를 첨부하시면 박제안 팀이 본문을 그대로 인용해 회사 소개·실적 섹션을 작성합니다."
    )

    # ── 디자인 템플릿 선택 ──────────────────────────────────────────
    st.markdown("### 🎨 PT 디자인 템플릿")
    st.caption("최종 PT(최피티) 및 기획 PPT(스토리보드)에 동일하게 적용되는 색상 팔레트입니다.")
    _tpl_options = {
        "navy": "🌊 Navy — 공공·SI 정통 (네이비/블루/오렌지 액센트)",
        "mono": "⚫ Mono — 컨설팅풍 모노톤 (블랙/그레이/오렌지)",
        "warm": "🔥 Warm — 민간·스타트업 (브라운/오렌지/그린)",
    }
    _cur_tpl = db.get_design_template()
    _tpl_keys = list(_tpl_options.keys())
    _idx = _tpl_keys.index(_cur_tpl) if _cur_tpl in _tpl_keys else 0
    chosen_tpl = st.radio(
        "디자인 템플릿",
        options=_tpl_keys,
        format_func=lambda k: _tpl_options[k],
        index=_idx,
        horizontal=False,
        key="design_template_radio",
    )
    if chosen_tpl != _cur_tpl:
        db.set_design_template(chosen_tpl)
        st.success(f"디자인 템플릿이 '{chosen_tpl}' 로 변경되었습니다. 다음 PT부터 적용됩니다.")

    st.divider()

    cp = db.get_company_profile() or {}
    with st.form("company_form"):
        c1, c2 = st.columns(2)
        name = c1.text_input("회사명", value=cp.get("name") or COMPANY.name)
        ceo = c2.text_input("대표자", value=cp.get("ceo") or COMPANY.ceo)
        biz_num = c1.text_input("사업자번호", value=cp.get("biz_num") or COMPANY.biz_num)
        team_size = c2.number_input(
            "팀 규모(명)",
            min_value=1, max_value=2000,
            value=int(cp.get("team_size") or COMPANY.team_size),
        )
        tech_stack_str = st.text_input(
            "보유 기술 (쉼표로 구분)",
            value=", ".join(cp.get("tech_stack") or COMPANY.tech_stack),
            placeholder="예: Python, Django, React, AWS",
        )
        c3, c4 = st.columns(2)
        budget_min = c3.number_input(
            "선호 사업 예산 하한 (원)",
            min_value=0, max_value=100_000_000_000, step=10_000_000,
            value=int(cp.get("typical_budget_min") or COMPANY.typical_budget_min),
            format="%d",
        )
        budget_max = c4.number_input(
            "선호 사업 예산 상한 (원)",
            min_value=0, max_value=100_000_000_000, step=10_000_000,
            value=int(cp.get("typical_budget_max") or COMPANY.typical_budget_max),
            format="%d",
        )
        differentiators = st.text_area(
            "우리만의 차별점 (경쟁사가 흉내 못 내는 강점)",
            value=cp.get("differentiators") or "",
            height=120,
            placeholder="예: 자체 개발한 행정시스템 모듈 보유 / 특정 지자체 5년 운영 경험 / ISMS 인증 / 24시간 운영팀",
        )
        intro_text = st.text_area(
            "회사 소개서 본문 (자유 양식, 박제안 팀이 인용)",
            value=cp.get("intro_text") or "",
            height=200,
            placeholder="회사 연혁, 비전, 핵심 사업 영역, 주요 고객사 등을 자유롭게 적어주세요.",
        )
        brochure_file = st.file_uploader(
            "회사 소개서 첨부 (PDF/DOCX/PPTX, 선택)",
            type=["pdf", "docx", "pptx"],
        )

        st.divider()
        st.markdown("### 📚 참고할 과거 제안서 (선택)")
        st.caption(
            "박제안 팀이 제안서를 작성할 때 톤·구성·표현을 참고합니다. "
            "예전에 작성한 우수 제안서를 붙여넣으면 비슷한 문체로 작성합니다."
        )
        reference_proposal_text = st.text_area(
            "참고 제안서 본문 (텍스트 붙여넣기)",
            value=cp.get("reference_proposal_text") or "",
            height=200,
            placeholder="과거 작성한 제안서 전체 또는 인상적인 섹션을 그대로 붙여넣으세요. (HWP/PDF의 경우 텍스트만 복사)",
        )
        reference_file = st.file_uploader(
            "참고 제안서 파일 첨부 (PDF/DOCX, 선택)",
            type=["pdf", "docx", "pptx", "txt"],
            key="reference_uploader",
        )
        reference_instructions = st.text_area(
            "참고 지시 텍스트 (박제안에게 어떻게 참고하라고 지시할지)",
            value=cp.get("reference_instructions") or "",
            height=120,
            placeholder=(
                "예: 위 참고 제안서의 톤과 매너를 그대로 따라주세요. 특히 '~하겠습니다'체와 "
                "표/도식 활용 방식을 그대로 모방해 주세요. 단, 회사명·고객사명은 이번 공고에 맞게 바꿔주세요."
            ),
        )

        submitted = st.form_submit_button("💾 저장", type="primary")
        if submitted:
            brochure_path = cp.get("brochure_path") or ""
            if brochure_file is not None:
                upload_dir = ROOT / "storage" / "uploads"
                upload_dir.mkdir(parents=True, exist_ok=True)
                target = upload_dir / brochure_file.name
                target.write_bytes(brochure_file.getbuffer())
                brochure_path = str(target)
            reference_proposal_path = cp.get("reference_proposal_path") or ""
            if reference_file is not None:
                upload_dir = ROOT / "storage" / "uploads"
                upload_dir.mkdir(parents=True, exist_ok=True)
                target = upload_dir / f"reference_{reference_file.name}"
                target.write_bytes(reference_file.getbuffer())
                reference_proposal_path = str(target)
            db.save_company_profile({
                "name": name,
                "ceo": ceo,
                "biz_num": biz_num,
                "team_size": int(team_size),
                "tech_stack": [t.strip() for t in tech_stack_str.split(",") if t.strip()],
                "typical_budget_min": int(budget_min),
                "typical_budget_max": int(budget_max),
                "differentiators": differentiators,
                "intro_text": intro_text,
                "brochure_path": brochure_path,
                "reference_proposal_text": reference_proposal_text,
                "reference_proposal_path": reference_proposal_path,
                "reference_instructions": reference_instructions,
            })
            st.success("회사 정보가 저장되었습니다. 다음 제안서부터 자동 반영됩니다.")
            st.rerun()

    if cp.get("brochure_path"):
        st.info(f"📎 첨부된 회사 소개서: {Path(cp['brochure_path']).name}")
    if cp.get("reference_proposal_path"):
        st.info(f"📚 첨부된 참고 제안서: {Path(cp['reference_proposal_path']).name}")
    if cp.get("reference_proposal_text") or cp.get("reference_instructions"):
        st.success(
            f"✅ 박제안 팀이 참고 제안서({len(cp.get('reference_proposal_text') or '')}자)와 "
            f"참고 지시({len(cp.get('reference_instructions') or '')}자)를 다음 작성에 반영합니다."
        )

    # ------------------------------------------------------------------
    # 🎨 PPTX 마스터 템플릿 (4슬롯) — 정디자가 매 제안서마다 골라 사용
    # ------------------------------------------------------------------
    st.divider()
    st.subheader("🎨 PPTX 마스터 템플릿 (디자인 베이스)")
    st.caption(
        "사장님이 직접 디자인한 PPTX 마스터를 4개까지 등록해 두면, 매 제안서마다 **정디자(디자인 총괄)**가 "
        "발주처 성격에 맞는 마스터 1개를 골라 그 위에 콘텐츠를 채워 넣습니다. "
        "다음 라운드에 시스템이 기본 마스터 3종을 자동 생성해 드릴 예정이며, 마음에 들지 않으시면 "
        "여기에 직접 만든 .pptx 파일을 올려 교체할 수 있습니다."
    )
    upload_dir = ROOT / "storage" / "uploads" / "pptx_masters"
    upload_dir.mkdir(parents=True, exist_ok=True)

    bm1, bm2 = st.columns([1, 3])
    if bm1.button("🎨 기본 마스터 3종 자동 생성", key="btn_build_master_3",
                  help="네이비/오렌지/화이트 3종을 자동으로 생성하고 빈 슬롯에 등록합니다."):
        try:
            from tools.pptx_master_builder import build_all_master_previews_and_register
            with st.spinner("3종 마스터 PPTX 생성 중…"):
                results = build_all_master_previews_and_register()
            st.success(f"기본 마스터 {len(results)}종 생성 완료. 빈 슬롯에 자동 등록됨.")
            st.rerun()
        except Exception as e:
            st.error(f"마스터 생성 실패: {e}")
    bm2.caption("⚠️ 빈 슬롯이 있을 때만 자동 등록됩니다. 슬롯이 다 차 있으면 파일만 생성됩니다.")

    for i in range(1, 5):
        path_key = f"pptx_master_{i}_path"
        label_key = f"pptx_master_{i}_label"
        cur_path = cp.get(path_key)
        cur_label = cp.get(label_key) or ""
        with st.container(border=True):
            st.markdown(f"**슬롯 #{i}**" + (f" — `{Path(cur_path).name}`" if cur_path else " — _비어 있음_"))
            mc1, mc2, mc3 = st.columns([2, 2, 1])
            new_label = mc1.text_input(
                "라벨 (정디자가 식별)",
                value=cur_label,
                key=f"pm_label_{i}",
                placeholder="예: 공공기관용 네이비 / 혁신사업용 오렌지 / 미니멀 화이트",
            )
            up_pptx = mc2.file_uploader(
                "PPTX 업로드",
                type=["pptx", "potx"],
                key=f"pm_up_{i}",
                label_visibility="collapsed",
            )
            if mc3.button("저장", key=f"pm_save_{i}"):
                save_data = {label_key: new_label}
                if up_pptx is not None:
                    safe_name = Path(up_pptx.name).name  # 경로 traversal 방지
                    safe_name = "".join(c for c in safe_name if c.isalnum() or c in ("._- "))
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    target = upload_dir / f"master_{i}_{ts}_{safe_name}"
                    target.write_bytes(up_pptx.getbuffer())
                    save_data[path_key] = str(target)
                db.save_company_profile(save_data)
                st.success(f"슬롯 #{i} 저장됨")
                st.rerun()
            if cur_path and mc3.button("삭제", key=f"pm_del_{i}"):
                db.save_company_profile({path_key: None, label_key: ""})
                st.rerun()

    # ------------------------------------------------------------------
    # 📚 레퍼런스 제안서 라이브러리 (다건)
    # ------------------------------------------------------------------
    st.divider()
    st.subheader("📑 레퍼런스 제안서 라이브러리")
    st.caption(
        "다건 등록 가능. 박제안 팀이 새 공고를 작성할 때 도메인이 비슷한 레퍼런스를 자동으로 골라 톤·구조·표현을 모방합니다. "
        "수주 성공 사례를 우선 등록하시면 도움이 됩니다."
    )
    refs = db.list_reference_proposals()
    target_count = 5
    progress_bar_len = min(len(refs), target_count)
    st.caption(
        f"현재 등록된 레퍼런스 제안서: **{len(refs)}건** / 권장 **{target_count}건** "
        f"{'🟦' * progress_bar_len}{'⬜' * (target_count - progress_bar_len)}  "
        f"— 도메인 태그(공공/AI/SI 등)를 다양하게 등록할수록 박제안 팀이 새 공고에 가까운 1~2건을 자동 매칭합니다."
    )

    with st.expander("➕ 레퍼런스 제안서 추가", expanded=(len(refs) == 0)):
        with st.form("add_reference_form", clear_on_submit=True):
            r1, r2 = st.columns(2)
            ref_title = r1.text_input("제안서 제목", placeholder="예: 경찰청 통합신고대응센터 AI 구축")
            ref_client = r2.text_input("발주처", placeholder="예: 경찰청")
            r3, r4 = st.columns([2, 1])
            ref_domain = r3.text_input(
                "도메인 태그 (쉼표 구분)",
                placeholder="예: 공공, AI, SI, 음성인식, 데이터분석",
            )
            ref_won = r4.selectbox("결과", ["수주 성공", "참고용"], index=0)
            ref_body = st.text_area(
                "본문 텍스트 (붙여넣기)",
                height=200,
                placeholder="제안서 본문 또는 인상적인 섹션 일부를 붙여넣으세요.",
            )
            ref_file = st.file_uploader(
                "파일 첨부 (선택)",
                type=["pdf", "docx", "pptx", "txt", "hwp"],
                key="lib_ref_file",
            )
            ref_inst = st.text_area(
                "모방 지시 (선택)",
                height=80,
                placeholder="예: 표지·목차 구조와 인포그래픽 활용 방식을 따라하세요. 회사명/숫자는 새 공고에 맞게 갱신.",
            )
            if st.form_submit_button("추가", type="primary"):
                file_path = ""
                if ref_file is not None:
                    upload_dir = ROOT / "storage" / "uploads"
                    upload_dir.mkdir(parents=True, exist_ok=True)
                    target = upload_dir / f"refdoc_{datetime.now().strftime('%Y%m%d%H%M%S')}_{ref_file.name}"
                    target.write_bytes(ref_file.getbuffer())
                    file_path = str(target)
                db.add_reference_proposal({
                    "title": ref_title,
                    "client": ref_client,
                    "domain": ref_domain,
                    "won": 1 if ref_won == "수주 성공" else 0,
                    "body_text": ref_body,
                    "file_path": file_path,
                    "instructions": ref_inst,
                })
                st.success("레퍼런스 제안서가 추가되었습니다.")
                st.rerun()

    if refs:
        for r in refs:
            won_badge = "🏆 수주성공" if r.get("won") else "📖 참고용"
            with st.expander(f"{won_badge}  ·  {r['title']}  ·  {r.get('client','')}", expanded=False):
                st.caption(f"도메인: {r.get('domain') or '-'}  |  본문 {len(r.get('body_text') or '')}자  |  등록 {r.get('created_at')}")
                if r.get("instructions"):
                    st.info(f"📌 모방 지시: {r['instructions']}")
                if r.get("file_path"):
                    st.caption(f"📎 첨부: {Path(r['file_path']).name}")
                preview = (r.get("body_text") or "")[:600]
                if preview:
                    st.text(preview + ("..." if len(r.get("body_text") or "") > 600 else ""))
                if st.button("🗑️ 삭제", key=f"del_ref_{r['id']}"):
                    db.delete_reference_proposal(r["id"])
                    st.rerun()

    # ------------------------------------------------------------------
    # 💼 회사 자산 라이브러리 (솔루션 / 실적 / 인증 / 수치)
    # ------------------------------------------------------------------
    st.divider()
    st.subheader("💼 회사 자산 라이브러리")
    st.caption(
        "박제안 팀의 8명 전문가가 섹션을 작성할 때 자기 영역에 맞는 자산만 골라서 자동 인용합니다. "
        "예: 솔루션 아키텍트는 솔루션·인증·수치를, 회사·실적 전문가는 실적·인증·수치를 인용합니다."
    )

    ASSET_META = {
        "solution": ("💡 자사 솔루션 / 엔진", "예: VoiceEz E2E 음성인식 엔진"),
        "case":     ("🏆 수행 실적",            "예: 경찰청 조서시스템(2021)"),
        "cert":     ("🎖️ 인증 / 자격",          "예: ISMS, GS인증"),
        "metric":   ("📊 정량 자산 수치",        "예: 음성인식 정확도 90.05%"),
    }

    asset_tabs = st.tabs([ASSET_META[k][0] for k in ASSET_META])
    for tab, kind in zip(asset_tabs, ASSET_META.keys()):
        label, hint = ASSET_META[kind]
        with tab:
            items = db.list_company_assets(kind=kind)
            st.caption(f"등록된 {label}: **{len(items)}건**")
            with st.form(f"add_asset_{kind}", clear_on_submit=True):
                a1, a2 = st.columns([2, 3])
                a_title = a1.text_input("제목", placeholder=hint, key=f"at_{kind}")
                a_meta = a2.text_input(
                    "메타 (key=value, 쉼표 구분)",
                    placeholder="예: year=2024, client=경찰청, value=90.05, unit=%",
                    key=f"am_{kind}",
                )
                a_body = st.text_area(
                    "설명 / 본문",
                    height=120,
                    placeholder="간단한 설명을 적어주세요. 박제안이 이 텍스트를 그대로 인용합니다.",
                    key=f"ab_{kind}",
                )
                if st.form_submit_button("추가", type="primary"):
                    extra = {}
                    for chunk in a_meta.split(","):
                        if "=" in chunk:
                            k, v = chunk.split("=", 1)
                            extra[k.strip()] = v.strip()
                    if a_title.strip():
                        db.add_company_asset(kind, a_title.strip(), a_body, extra)
                        st.success("자산이 추가되었습니다.")
                        st.rerun()
                    else:
                        st.warning("제목은 필수입니다.")
            if items:
                for it in items:
                    extra = it.get("extra") or {}
                    meta = " · ".join(f"{k}:{v}" for k, v in extra.items() if v)
                    cols = st.columns([5, 1])
                    cols[0].markdown(f"**{it['title']}**" + (f"  _({meta})_" if meta else ""))
                    if cols[1].button("삭제", key=f"del_asset_{kind}_{it['id']}"):
                        db.delete_company_asset(it["id"])
                        st.rerun()
                    if it.get("body"):
                        st.caption(it["body"][:300])


# --- 탭6: 에이전트 설정 -------------------------------------------------

with tab_agents:
    st.subheader("에이전트별 모델 변경 및 작업 지시")
    st.caption(
        "저장 즉시 다음 실행부터 적용됩니다. 비용을 줄이려면 박제안을 sonnet으로 낮추는 것이 가장 효과적입니다."
    )

    overrides_now = db.get_agent_overrides()
    for agent in AGENTS:
        with st.expander(f"{agent['avatar']} {agent['name']} — {agent['role']}", expanded=False):
            st.caption(agent["desc"])

            current_model = (
                (overrides_now.get(agent["key"], {}) or {}).get("model")
                or agent["default_model"]
            )
            current_instructions = (overrides_now.get(agent["key"], {}) or {}).get("extra_instructions", "")
            current_keywords = (overrides_now.get(agent["key"], {}) or {}).get("keywords", []) or []

            sel_model = st.selectbox(
                "모델",
                MODEL_OPTIONS,
                index=MODEL_OPTIONS.index(current_model) if current_model in MODEL_OPTIONS else 0,
                key=f"model_{agent['key']}",
            )

            keywords_str = ""
            if agent["key"] == "kim":
                st.markdown("**🔍 G2B 수집 키워드** (쉼표로 구분, 비우면 기본값 사용)")
                keywords_str = st.text_input(
                    "수집 키워드",
                    value=", ".join(current_keywords) if current_keywords
                          else "홈페이지, 웹사이트, 시스템 구축, 플랫폼, 포털",
                    key=f"kw_{agent['key']}",
                    placeholder="예: 홈페이지, 챗봇, AI, 스마트시티, 통합관제",
                    label_visibility="collapsed",
                )
                st.caption("💡 김탐정은 매 실행마다 위 키워드 각각으로 G2B를 검색합니다.")

            if agent["key"] == "park":
                st.markdown("**👥 박제안 팀 구성 (8명 서브 전문가)**")
                for sp in PARK_SPECIALISTS:
                    badge = " 🌟 핵심" if sp.is_critical else ""
                    st.markdown(
                        f"- {sp.avatar} **{sp.name}**{badge} — {sp.role} (약 {sp.default_pages}p)"
                    )
                st.caption(
                    "🌟 핵심 3인은 To-Be 화면 와이어프레임을 텍스트로 그려 차별화된 구현 모습을 시각화합니다. "
                    "공고별 가변 목차는 향후 '제안서 검토' 탭에서 수정 가능합니다."
                )

            instructions = st.text_area(
                "추가 작업 지시 (시스템 프롬프트에 덧붙임)",
                value=current_instructions,
                height=100,
                key=f"instr_{agent['key']}",
                placeholder="예: 답변에서 마케팅 용어보다 기술 용어를 우선 사용해 주세요.",
            )
            if st.button("💾 저장", key=f"save_{agent['key']}"):
                kws = None
                if agent["key"] == "kim":
                    kws = [k.strip() for k in keywords_str.split(",") if k.strip()]
                db.save_agent_override(agent["key"], sel_model, instructions, kws)
                st.success("저장되었습니다.")
                st.rerun()


# --- 탭6: 수동 실행 ------------------------------------------------------

with tab_run:
    st.subheader("파이프라인 수동 실행")
    st.caption("자동 스케줄(매일 09:00) 외에 즉시 실행하고 싶을 때 사용합니다.")

    rc1, rc2, rc3, rc4 = st.columns(4)

    def _make_progress(label: str):
        """프로그래스바 + 상태 텍스트 placeholder 콜백을 만들어 반환."""
        bar = st.progress(0.0, text=f"{label} 준비 중...")
        text_ph = st.empty()

        def cb(done: int, total: int, msg: str) -> None:
            ratio = (done / total) if total > 0 else 1.0
            ratio = min(max(ratio, 0.0), 1.0)
            bar.progress(ratio, text=f"{label} {done}/{total} ({int(ratio*100)}%)")
            text_ph.caption(f"📍 {msg}")

        return cb

    if rc1.button("🕵️ 수집 실행", use_container_width=True):
        cb = _make_progress("김탐정 수집")
        bids = run_collect(progress_cb=cb)
        st.success(f"{len(bids)}건 수집 완료 — 화면을 갱신합니다…")
        st.rerun()

    if rc2.button("⚖️ 1차 평가(외적)", use_container_width=True,
                  help="모든 신규 공고에 메타데이터만 보고 빠르게 점수 매김"):
        cb = _make_progress("이판단 1차 평가")
        evs = run_evaluate(progress_cb=cb)
        st.success(f"{len(evs)}건 평가 완료 — 화면을 갱신합니다…")
        for ev in evs:
            st.write(f"- {ev.bid_id}: **{ev.fit_score}점** ({ev.recommendation})")
        st.rerun()

    if rc3.button("✍️ 제안서 작성", use_container_width=True):
        st.info("박제안 → 최피티 → 오품질 순으로 진행됩니다.")
        park_cb = _make_progress("박제안 초안")
        choi_cb = _make_progress("최피티 PT")
        oh_cb = _make_progress("오품질 검수")
        out = run_propose(park_cb=park_cb, choi_cb=choi_cb, oh_cb=oh_cb)
        st.success(
            f"초안 {len(out['drafts'])} / PT {len(out['pts'])} / 검수 {len(out['reports'])}"
        )
        st.rerun()

    if rc4.button("🚀 전체 실행", use_container_width=True, type="primary"):
        kim_cb = _make_progress("김탐정 수집")
        lee_cb = _make_progress("이판단 평가")
        park_cb = _make_progress("박제안 초안")
        choi_cb = _make_progress("최피티 PT")
        oh_cb = _make_progress("오품질 검수")
        res = run_all(kim_cb=kim_cb, lee_cb=lee_cb, park_cb=park_cb, choi_cb=choi_cb, oh_cb=oh_cb)
        st.success(f"완료: {res}")
        st.rerun()

    st.divider()
    st.subheader("🔍 2차 정밀 분석 (RFP 본문)")
    st.caption(
        "1차 통과(승인 대기) 공고 중 점수 상위 N건만 RFP 본문을 정밀 분석합니다. "
        "공고 상세 화면에서 RFP 본문을 미리 붙여넣어 두셔야 정밀 분석이 됩니다."
    )
    pc1, pc2 = st.columns([1, 3])
    deep_n = pc1.number_input("상위 N건", min_value=1, max_value=20, value=5, step=1)
    if pc2.button("🔬 상위 N건 RFP 정밀 분석 실행", use_container_width=True):
        from agents.lee_judge import LeeJudge
        cb = _make_progress(f"이판단 정밀 분석 (상위 {int(deep_n)})")
        results = LeeJudge().run_deep_top_n(n=int(deep_n), progress_cb=cb)
        ok = sum(1 for r in results if r.get("ok"))
        st.success(f"정밀 분석 완료 — 성공 {ok} / 전체 {len(results)}건")
        for r in results:
            mark = "✅" if r.get("ok") else "⚠️"
            st.write(f"{mark} {r['title']} ({r['bid_id']}) · {r['score']}점 · "
                     + (f"{r.get('len',0)}자" if r.get("ok") else r.get("error", "오류")))

    st.divider()
    st.subheader("개별 공고 강제 처리")
    awaiting_now = list_awaiting()
    if awaiting_now:
        target = st.selectbox("공고 선택", awaiting_now)
        if st.button("이 공고만 승인 후 제안서 작성"):
            manual_approve(target)
            with st.spinner("작성 중..."):
                out = run_propose()
            st.success(f"완료: {out}")
    else:
        st.caption("승인 대기 중인 공고가 없습니다.")


# --- 탭7: 활동 로그 ------------------------------------------------------

with tab_logs:
    st.subheader("LLM 호출 감사 로그")
    audits = db.list_audit(limit=200)
    if audits:
        df_audit = pd.DataFrame(audits)
        st.dataframe(
            df_audit[["created_at", "agent_name", "model", "tokens_in", "tokens_out", "cost_usd", "bid_id", "note"]],
            use_container_width=True,
            hide_index=True,
        )
        total_cost = df_audit["cost_usd"].sum()
        total_in = df_audit["tokens_in"].sum()
        total_out = df_audit["tokens_out"].sum()
        m1, m2, m3 = st.columns(3)
        m1.metric("총 비용 (USD)", f"${total_cost:.4f}")
        m2.metric("입력 토큰", f"{int(total_in):,}")
        m3.metric("출력 토큰", f"{int(total_out):,}")
    else:
        st.info("아직 LLM 호출 기록이 없습니다.")

    st.divider()
    st.subheader("에이전트 활동 타임라인")
    activities = db.list_activity(limit=100)
    if activities:
        df_act = pd.DataFrame(activities)
        st.dataframe(df_act, use_container_width=True, hide_index=True)
    else:
        st.caption("기록 없음")
