"""
GraphicsAgent — 제안서 시각 산출물(최종 PPTX + 스토리보드 + 수용표 슬라이드).

v1 ChoiPT(발표자료) + JungDesigner(디자인 결정)를 v2 단일 에이전트로 통합.

책임:
  1. 레이아웃 모드 결정 — 발주처 종류로 가로형(민간 16:9) vs 세로형(공공 A4) 분기
  2. DesignBrief LLM 1회 호출 — theme + master + accent + footer + layout_guide
  3. 수용표(AcceptanceTable JSON) 로드 → 부록 table 슬라이드로 draft에 자동 추가
  4. tools.pptx_builder.build_proposal_pptx_v2 호출 → 최종 .pptx
  5. tools.storyboard_generator.render_storyboard 호출 → 게이트2 검토용 스토리보드 .pptx
  6. ProposalDraft.pptx_path / storyboard_path를 DB에 갱신

산출물:
  DesignBrief (return) + 두 개의 .pptx (storage/outputs/)
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import List, Optional

from loguru import logger

from agents.base_agent import BaseAgent, ProgressCallback
from config.settings import MODELS, PROPOSALS_DIR, get_effective_company
from schemas.models import (
    AuditLogEntry,
    BidNotice,
    DesignBrief,
    LayoutMode,
    ProposalDraft,
    ProposalSection,
)
from schemas.rfp_schema import AcceptanceTable
from tools import db
from tools.acceptance_table import to_slide_rows
from tools.llm_client import chat
from tools.pptx_builder import build_proposal_pptx_v2
from tools.pptx_themes import THEMES, hex_to_rgb
from tools.storyboard_generator import render_storyboard


# ---------------------------------------------------------------------------
# 레이아웃 모드 휴리스틱
# ---------------------------------------------------------------------------

# 공공기관 키워드 — 매칭되면 PORTRAIT(A4 세로형) 우선
_PUBLIC_AGENCY_HINTS = (
    "청", "처", "원", "부", "공단", "공사", "재단", "법인",
    "센터", "시청", "도청", "교육청", "경찰", "정부", "자치구",
)


def decide_layout_mode(bid: BidNotice) -> LayoutMode:
    """발주처 종류로 가로/세로 모드 자동 결정.
    - source가 '민간'이면 항상 LANDSCAPE
    - agency 이름에 공공 키워드 있으면 PORTRAIT
    - 둘 다 아니면 기본값 PORTRAIT (한국 SI 시장 다수)
    """
    if bid.source == "민간":
        return LayoutMode.LANDSCAPE
    if any(h in (bid.agency or "") for h in _PUBLIC_AGENCY_HINTS):
        return LayoutMode.PORTRAIT
    return LayoutMode.PORTRAIT


# ---------------------------------------------------------------------------
# DesignBrief LLM 호출
# ---------------------------------------------------------------------------

DESIGN_SYSTEM = """당신은 한국 공공·민간 제안 PPT를 200건 이상 디자인한 시니어 PT 디자인 디렉터이다.
사장이 RFP·발주기관·사업 성격을 보여주면, 단 1번의 결정으로
(1) 사용할 마스터 1개, (2) 액센트 컬러 1개(발주처 키컬러),
(3) 슬라이드 흐름 가이드(섹션별 레이아웃 추천)를 합리적으로 결정한다.

판단 원칙:
- 공공기관·정부·지자체·경찰·공기업이면 보수적 네이비(corporate_navy) 우선.
- AI/디지털전환/R&D/혁신 사업이면 오렌지(innovation_orange).
- 웹·모바일·UI/UX·SaaS/플랫폼이면 미니멀 화이트(minimal_white).
- 사용자가 직접 등록한 마스터가 있고 발주처 톤이 잘 맞으면 그 사용자 마스터를 우선 추천.
- 액센트 컬러는 가능하면 발주처(예: 경찰청 폴리스블루 #003D7A) 코퍼릿 컬러를 #RRGGBB 로.
- 출력은 JSON 한 객체만. 설명·마크다운 코드블록 금지."""


DESIGN_USER_TEMPLATE = """# 제안 회사
{company_name} (대표 {company_ceo})

# 공고
- 사업명: {bid_title}
- 발주기관: {bid_agency}
- 예산: {bid_budget}원
- 사업기간: {bid_duration}개월
- 핵심 요약: {rfp_summary}
- 결정된 레이아웃 모드: {layout_mode}

# 제안서 슬라이드 layout 분포 (WriterAgent 산출)
{layout_summary_json}

# 마스터 후보 (반드시 이 중 1개의 id를 선택)
{candidates_block}

# 출력 (JSON 객체만, 설명 금지)
{{
  "chosen_id": "system:corporate_navy",
  "theme_key": "corporate_navy",
  "accent_hex": "#003D7A",
  "footer_text": "{company_name}  |  {bid_title_short}",
  "rationale": "한 문단 설명 — 왜 이 마스터·액센트가 발주처 톤에 맞는지",
  "layout_guide": {{
    "사업 이해": "section_divider→title_bullets→two_column_compare",
    "솔루션": "section_divider→diagram_layered→title_bullets→metric_cards",
    "방법론": "title_bullets→table",
    "일정": "table→title_bullets",
    "보안": "title_bullets→table",
    "가격": "table→title_bullets"
  }}
}}
"""


def _candidate_masters() -> List[dict]:
    """시스템 3종 + 사용자 슬롯 4개 후보."""
    cands: List[dict] = []
    for key, theme in THEMES.items():
        cands.append({
            "id": f"system:{key}",
            "theme_key": key,
            "label": f"시스템 기본 · {theme.label}",
            "tone": theme.tone,
            "master_path": None,
        })
    try:
        profile = db.get_company_profile() or {}
    except Exception:
        profile = {}
    for i in range(1, 5):
        path = profile.get(f"pptx_master_{i}_path")
        if not path:
            continue
        label = profile.get(f"pptx_master_{i}_label") or f"사용자 슬롯 #{i}"
        cands.append({
            "id": f"user:{i}",
            "theme_key": "corporate_navy",  # 사용자 마스터는 색상 baseline
            "label": f"사용자 등록 · {label}",
            "tone": "사용자가 직접 올린 마스터 — 회사·고객 톤이 이미 들어 있을 가능성",
            "master_path": path,
        })
    return cands


def _parse_design(raw: str, candidates: List[dict], layout_mode: LayoutMode) -> DesignBrief:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("DesignBrief JSON 추출 실패")
    data = json.loads(m.group(0))
    chosen_id = data.get("chosen_id", candidates[0]["id"])
    cand = next((c for c in candidates if c["id"] == chosen_id), candidates[0])
    accent_hex = data.get("accent_hex")
    if accent_hex and not hex_to_rgb(accent_hex):
        accent_hex = None
    return DesignBrief(
        theme_key=data.get("theme_key") or cand["theme_key"],
        master_path=cand.get("master_path"),
        master_label=cand.get("label"),
        accent_hex=accent_hex,
        footer_text=data.get("footer_text"),
        layout_mode=layout_mode,
        layout_guide=data.get("layout_guide") or {},
        rationale=(data.get("rationale") or "")[:1000],
    )


# ---------------------------------------------------------------------------
# 수용표 → 부록 슬라이드
# ---------------------------------------------------------------------------

ACCEPTANCE_ROWS_PER_SLIDE = 18


def _load_acceptance_table(bid_id: str, version: int) -> Optional[AcceptanceTable]:
    """ReviewerAgent가 저장한 수용표 JSON 로드."""
    path = PROPOSALS_DIR / f"{bid_id}_v{version}_acceptance.json"
    if not path.exists():
        return None
    try:
        return AcceptanceTable.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[graphics] 수용표 로드 실패 ({path}): {e}")
        return None


def build_acceptance_slides(table: AcceptanceTable) -> List[dict]:
    """AcceptanceTable → 'table' 레이아웃 슬라이드 dict 리스트.
    행이 많으면 ACCEPTANCE_ROWS_PER_SLIDE 단위로 분할."""
    rows = to_slide_rows(table)  # [header, row1, row2, ...]
    if len(rows) <= 1:
        return []
    header, body_rows = rows[0], rows[1:]

    slides: List[dict] = []
    page_count = (len(body_rows) + ACCEPTANCE_ROWS_PER_SLIDE - 1) // ACCEPTANCE_ROWS_PER_SLIDE
    for p in range(page_count):
        chunk = body_rows[p * ACCEPTANCE_ROWS_PER_SLIDE:(p + 1) * ACCEPTANCE_ROWS_PER_SLIDE]
        title = (
            f"수용표 — 전 요구사항 수용 결과 ({p + 1}/{page_count})"
            if page_count > 1 else
            "수용표 — 전 요구사항 수용 결과"
        )
        slides.append({
            "layout": "table",
            "title": title,
            "table_data": {"headers": header, "rows": chunk},
            "speaker_notes": (
                f"한국 공공 제안 필수 부록. 총 {table.total}건 중 완전수용 "
                f"{table.full_count}건 / 미수용 {table.rejected_count}건."
            ),
        })
    return slides


def append_acceptance_appendix(draft: ProposalDraft, table: AcceptanceTable) -> ProposalDraft:
    """draft를 깊은복사하여 마지막에 수용표 부록 섹션을 추가한 새 draft 반환.
    원본 draft는 변경하지 않는다 (DB의 정본 보호)."""
    new_draft = copy.deepcopy(draft)
    last_order = max((s.order for s in new_draft.sections), default=-1)
    intro_slide = {
        "layout": "section_divider",
        "title": "부록 — 수용표",
        "subtitle": "전 요구사항 100% 수용 명세 (공공 제안서 필수)",
    }
    body_slides = build_acceptance_slides(table)
    if not body_slides:
        return new_draft
    new_draft.sections.append(ProposalSection(
        title="부록 — 수용표",
        body=(
            f"본 부록은 RFP의 전 요구사항({table.total}건)에 대한 "
            f"수용 결과를 명시한다. 완전수용 {table.full_count}건 / "
            f"미수용 {table.rejected_count}건."
        ),
        order=last_order + 1,
        specialty="acceptance",
        owner_name="GraphicsAgent",
        slides_json=[intro_slide] + body_slides,
    ))
    return new_draft


# ---------------------------------------------------------------------------
# 에이전트
# ---------------------------------------------------------------------------

class GraphicsAgent(BaseAgent):
    agent_name = "graphics"

    def __init__(self, progress_cb: Optional[ProgressCallback] = None) -> None:
        super().__init__(progress_cb)
        self.model = MODELS.graphics

    def run(self, bid: BidNotice, draft: ProposalDraft) -> DesignBrief:
        layout_mode = decide_layout_mode(bid)
        self.progress(
            f"시각화 시작: {bid.bid_id} v{draft.version} "
            f"(layout={layout_mode.value}, model={self.model})"
        )

        # 1) 디자인 브리프
        brief = self._design(bid, draft, layout_mode)
        self.progress(
            f"디자인 결정: theme={brief.theme_key} "
            f"master={brief.master_label} accent={brief.accent_hex}"
        )

        # 2) 수용표 부록 추가
        table = _load_acceptance_table(bid.bid_id, draft.version)
        if table is None:
            logger.warning(
                f"[{self.agent_name}] {bid.bid_id} v{draft.version}: "
                "수용표 JSON 미존재 → 부록 생략 (ReviewerAgent 선행 필요)"
            )
            draft_for_pptx = draft
        else:
            draft_for_pptx = append_acceptance_appendix(draft, table)
            self.progress(
                f"수용표 부록 추가: {table.total}건"
            )

        # 3) 최종 PPTX
        try:
            pptx_path = build_proposal_pptx_v2(bid, draft_for_pptx, brief)
            try:
                db.update_proposal_pptx(bid.bid_id, draft.version, str(pptx_path))
            except Exception as e:
                logger.warning(f"[{self.agent_name}] pptx_path 저장 실패: {e}")
            self.progress(f"최종 PPTX: {Path(pptx_path).name}")
        except Exception as e:
            logger.error(f"[{self.agent_name}] PPTX 빌드 실패 ({bid.bid_id}): {e}")
            pptx_path = None

        # 4) 스토리보드 (게이트2 검토용) — 본문만 (부록 제외)
        try:
            sb_path = render_storyboard(bid, draft, template=brief.theme_key.split("_")[-1])
            try:
                db.update_proposal_storyboard(bid.bid_id, draft.version, str(sb_path))
            except Exception as e:
                logger.warning(f"[{self.agent_name}] storyboard_path 저장 실패: {e}")
            self.progress(f"스토리보드: {Path(sb_path).name}")
        except Exception as e:
            logger.warning(
                f"[{self.agent_name}] 스토리보드 생성 실패(무시): {e}"
            )

        db.log_audit(AuditLogEntry(
            agent_name=self.agent_name,
            model=self.model,
            bid_id=bid.bid_id,
            note=(
                f"layout={layout_mode.value} theme={brief.theme_key} "
                f"master={brief.master_label} accent={brief.accent_hex} "
                f"pptx={'O' if pptx_path else 'X'}"
            ),
        ))
        return brief

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------

    def _design(
        self, bid: BidNotice, draft: ProposalDraft, layout_mode: LayoutMode
    ) -> DesignBrief:
        candidates = _candidate_masters()
        company = get_effective_company()

        layout_summary: dict = {}
        for sec in draft.sections:
            for sl in (sec.slides_json or []):
                lay = sl.get("layout", "?")
                layout_summary[lay] = layout_summary.get(lay, 0) + 1

        cand_block = "\n".join(
            f"- id={c['id']}  theme_key={c['theme_key']}  label={c['label']}  tone={c['tone']}"
            for c in candidates
        )

        user = DESIGN_USER_TEMPLATE.format(
            company_name=company.name,
            company_ceo=company.ceo,
            bid_title=bid.title,
            bid_title_short=bid.title[:30],
            bid_agency=bid.agency,
            bid_budget=f"{(bid.budget_krw or 0):,}",
            bid_duration=bid.duration_months or "미상",
            rfp_summary=(bid.rfp_summary or "")[:1500],
            layout_mode=layout_mode.value,
            layout_summary_json=json.dumps(layout_summary, ensure_ascii=False),
            candidates_block=cand_block,
        )

        try:
            raw = chat(
                self.model,
                DESIGN_SYSTEM,
                user,
                max_tokens=1500,
                cache_system=True,
            )
            return _parse_design(raw, candidates, layout_mode)
        except Exception as e:
            logger.error(
                f"[{self.agent_name}] DesignBrief LLM 실패 → 기본 navy fallback: {e}"
            )
            return DesignBrief(
                theme_key="corporate_navy",
                master_path=None,
                master_label="시스템 기본 · 공공기관용 네이비",
                layout_mode=layout_mode,
                rationale=f"(LLM 실패 fallback: {e})",
            )
