"""정디자(鄭디자) - PPTX 디자인 디렉터 에이전트.

매 제안서마다 '디자인 결정'을 1회 산출한다. 결정 = DesignBrief.
- 사용 마스터 (시스템 3종 + 사용자 슬롯 4개 후보 중 1개)
- 액센트 컬러 (발주처 키컬러 1개 — 가능하면 발주처 CI 색)
- 푸터 텍스트 / 디자인 톤 사유

입력: BidNotice + ProposalDraft (slides_json 합산해서 어떤 layout이 많은지 참조)
출력: DesignBrief
LLM 호출 1회. 사용자가 마스터 슬롯에 .pptx를 등록해 두었으면 그 파일을 우선 후보에 올린다.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional

from loguru import logger

from config.settings import MODELS, get_effective_company
from schemas.models import AuditLogEntry, BidNotice, DesignBrief, ProposalDraft
from tools import db
from tools.llm_clients import chat_anthropic
from tools.pptx_themes import THEMES, hex_to_rgb

SYSTEM_PROMPT = """당신은 한국 공공·민간 제안 PPT를 200건 이상 디자인한 시니어 PT 디자인 디렉터(정디자)이다.
사장이 RFP·발주기관·사업 성격을 보여주면, 단 1번의 결정으로
(1) 사용할 마스터 1개, (2) 액센트 컬러 1개(발주처 키컬러),
(3) 슬라이드 흐름 가이드(섹션별 레이아웃 추천)를 합리적으로 결정한다.

판단 원칙:
- 공공기관·정부·지자체·경찰·공기업이면 보수적 네이비(corporate_navy) 우선.
- AI/디지털전환/R&D/혁신 사업이면 오렌지(innovation_orange).
- 웹·모바일·UI/UX·SaaS/플랫폼이면 미니멀 화이트(minimal_white).
- 사용자가 직접 등록한 마스터가 있고, 발주처와 그 회사 톤이 잘 맞으면 그 사용자 마스터를 우선 추천.
- 액센트 컬러는 가능하면 발주처(예: 경찰청 폴리스블루 #003D7A) 코퍼릿 컬러를 #RRGGBB 로.
- 출력은 JSON 한 객체만. 설명·마크다운 코드블록 금지."""


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
    profile = db.get_company_profile() or {}
    for i in range(1, 5):
        path = profile.get(f"pptx_master_{i}_path")
        if not path:
            continue
        label = profile.get(f"pptx_master_{i}_label") or f"사용자 슬롯 #{i}"
        cands.append({
            "id": f"user:{i}",
            "theme_key": "corporate_navy",  # 사용자 마스터는 색상 보정 baseline
            "label": f"사용자 등록 · {label}",
            "tone": "사용자가 직접 올린 마스터 — 회사·고객 톤이 이미 들어 있을 가능성",
            "master_path": path,
        })
    return cands


def _build_prompt(bid: BidNotice, draft: ProposalDraft, candidates: List[dict]) -> str:
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
    return f"""# 제안 회사
{company.name} (대표 {company.ceo})

# 공고
- 사업명: {bid.title}
- 발주기관: {bid.agency}
- 예산: {(bid.budget_krw or 0):,}원
- 사업기간: {bid.duration_months or '미상'}개월
- 핵심 요약: {(bid.rfp_summary or '')[:1500]}

# 제안서 슬라이드 layout 분포 (박제안 팀 산출)
{json.dumps(layout_summary, ensure_ascii=False)}

# 마스터 후보 (반드시 이 중 1개의 id를 선택)
{cand_block}

# 출력 (JSON 객체만, 설명 금지)
{{
  "chosen_id": "system:corporate_navy",
  "theme_key": "corporate_navy",
  "accent_hex": "#003D7A",
  "footer_text": "{company.name}  |  {bid.title[:30]}",
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


def _parse_response(raw: str, candidates: List[dict]) -> DesignBrief:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("DesignBrief JSON 추출 실패")
    data = json.loads(m.group(0))
    chosen_id = data.get("chosen_id", "system:corporate_navy")
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
        layout_guide=data.get("layout_guide") or {},
        rationale=data.get("rationale", "")[:1000],
    )


class JungDesigner:
    name = "정디자"
    role = "PPT 디자인 디렉터 (마스터·액센트·레이아웃 결정)"

    def design(self, bid: BidNotice, draft: ProposalDraft) -> DesignBrief:
        logger.info(f"[{self.name}] 디자인 결정 시작 ({bid.bid_id})")
        candidates = _candidate_masters()
        try:
            raw = chat_anthropic(
                MODELS.park,
                SYSTEM_PROMPT,
                _build_prompt(bid, draft, candidates),
                max_tokens=1500,
            )
            brief = _parse_response(raw, candidates)
        except Exception as e:
            logger.error(f"[{self.name}] LLM 실패 → 기본값(navy) 사용: {e}")
            brief = DesignBrief(
                theme_key="corporate_navy",
                master_path=None,
                master_label="시스템 기본 · 공공기관용 네이비",
                rationale=f"(LLM 실패 fallback: {e})",
            )
        db.log_audit(AuditLogEntry(
            agent_name=self.name,
            model=MODELS.park,
            bid_id=bid.bid_id,
            note=f"theme={brief.theme_key} master={brief.master_label} accent={brief.accent_hex}",
        ))
        logger.info(
            f"[{self.name}] 결정: theme={brief.theme_key} master={brief.master_label} accent={brief.accent_hex}"
        )
        return brief
