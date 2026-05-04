"""페이지 4 — 설정 (모델 매핑·키워드·회사 정보)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from config.settings import (
    KEYS,
    MODELS,
    SYS,
    apply_agent_overrides,
    get_collection_keywords,
    get_effective_company,
)
from tools import db


st.set_page_config(page_title="4. 설정", page_icon="⚙️", layout="wide")
st.title("⚙️ 설정")
st.caption(
    "DB에 저장되는 사장님 오버라이드. .env 기본값보다 우선 적용됨. "
    "변경 후 다른 페이지에서 재실행 시 자동 반영."
)

apply_agent_overrides()

# ---------------------------------------------------------------------------
# 6-에이전트 모델 + 추가 지시
# ---------------------------------------------------------------------------

st.subheader("🤖 에이전트별 모델 / 추가 지시")

agent_keys = ["discovery", "analysis", "rfp_struct", "strategy",
              "writer", "reviewer", "graphics"]
default_models = {k: getattr(MODELS, k) for k in agent_keys}

with st.form("agent_form"):
    overrides_input: dict[str, dict] = {}
    for k in agent_keys:
        c1, c2, c3 = st.columns([2, 4, 6])
        c1.markdown(f"**{k}**")
        model = c2.text_input(
            "모델",
            value=default_models[k],
            key=f"model_{k}",
            label_visibility="collapsed",
        )
        instr = c3.text_input(
            "추가 지시 (system 프롬프트 보충, 짧게)",
            value="",
            key=f"instr_{k}",
            label_visibility="collapsed",
        )
        overrides_input[k] = {"model": model, "extra_instructions": instr}

    # discovery 키워드는 별도
    keywords_str = st.text_input(
        "Discovery 수집 키워드 (콤마 구분)",
        value=",".join(get_collection_keywords()),
    )

    submitted = st.form_submit_button("💾 저장", type="primary")

if submitted:
    overrides_input["discovery"]["keywords"] = [
        k.strip() for k in keywords_str.split(",") if k.strip()
    ]
    if hasattr(db, "save_agent_overrides"):
        db.save_agent_overrides(overrides_input)
        st.success("저장됨. 변경된 모델/지시는 다음 실행부터 반영됩니다.")
    else:
        st.error("db.save_agent_overrides 함수가 없습니다 — DB 스키마 확인 필요.")


# ---------------------------------------------------------------------------
# 회사 정보
# ---------------------------------------------------------------------------

st.markdown("---")
st.subheader("🏢 회사 정보")
company = get_effective_company()

with st.form("company_form"):
    name = st.text_input("회사명", value=company.name)
    ceo = st.text_input("대표", value=company.ceo)
    biz_num = st.text_input("사업자번호", value=company.biz_num)
    tech_stack = st.text_input(
        "기술 스택 (콤마 구분)", value=",".join(company.tech_stack)
    )
    team_size = st.number_input("팀 규모", min_value=1, max_value=500, value=company.team_size)
    c1, c2 = st.columns(2)
    typical_min = c1.number_input(
        "적정 예산 최소(원)", min_value=0, value=company.typical_budget_min, step=10_000_000
    )
    typical_max = c2.number_input(
        "적정 예산 최대(원)", min_value=0, value=company.typical_budget_max, step=10_000_000
    )
    intro_text = st.text_area("회사 소개 원문", value=getattr(company, "intro_text", "") or "")
    diff_text = st.text_area("차별점", value=getattr(company, "differentiators", "") or "")

    save_co = st.form_submit_button("💾 회사 정보 저장", type="primary")

if save_co:
    if hasattr(db, "save_company_profile"):
        db.save_company_profile({
            "name": name, "ceo": ceo, "biz_num": biz_num,
            "tech_stack": [t.strip() for t in tech_stack.split(",") if t.strip()],
            "team_size": int(team_size),
            "typical_budget_min": int(typical_min),
            "typical_budget_max": int(typical_max),
            "intro_text": intro_text,
            "differentiators": diff_text,
        })
        st.success("회사 정보 저장됨.")
    else:
        st.error("db.save_company_profile 함수가 없습니다 — DB 스키마 확인 필요.")


# ---------------------------------------------------------------------------
# 시스템 임계값 (read-only)
# ---------------------------------------------------------------------------

st.markdown("---")
st.subheader("📐 시스템 임계값 (.env)")
st.caption("이 값들은 .env에서만 변경 가능합니다.")
c1, c2, c3, c4 = st.columns(4)
c1.metric("FIT 임계점수", SYS.fit_score_threshold)
c2.metric("TOC 일치율 임계", f"{SYS.toc_similarity_threshold:.2f}")
c3.metric("목표 페이지", f"{SYS.target_pages_min}~{SYS.target_pages_max}")
c4.metric("월 예산 ($)", SYS.monthly_budget_usd)


# ---------------------------------------------------------------------------
# 통합 키 상태
# ---------------------------------------------------------------------------

st.markdown("---")
st.subheader("🔌 외부 통합 키 상태 (.env에서 설정)")
ic1, ic2, ic3, ic4 = st.columns(4)
ic1.metric("Anthropic", "✅" if KEYS.has_anthropic else "❌ 미설정")
ic2.metric("Voyage AI", "✅" if KEYS.has_voyage else "❌ 미설정 (RAG 비활성)")
ic3.metric("G2B 키",     "✅" if KEYS.has_g2b else "❌ 미설정 (샘플 데이터)")
ic4.metric("Notion",     "✅" if KEYS.has_notion else "❌ 미설정 (수동 게이트)")
