"""
최피티 - 발표자료 생성 에이전트.
승인된 제안서 + 회사 자산을 활용해 다양한 타입의 슬라이드를 자동 구성한다.

각 슬라이드는 type 필드를 갖는다:
- bullets       : 일반 불릿 본문 (기본)
- table         : 마크다운 표 (예: 일정·인력·산정)
- metric        : 핵심 정량 수치 강조 (예: 90.05%)
- case_table    : 수행실적 표
- solution      : 자사 솔루션 카드
- architecture  : 아키텍처/시스템 구성도 (텍스트 박스 다이어그램)
- section_break : 챕터 구분 슬라이드 (큰 타이틀)
- closing       : 마무리/감사 슬라이드
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional

from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import MODELS, get_effective_company
from schemas.models import AuditLogEntry, BidNotice, BidStatus, ProposalDraft
from tools import db
from tools.llm_clients import chat_anthropic
from tools.pptx_generator import render_pptx

SYSTEM_PROMPT = """당신은 한국 공공/민간 제안 PT를 100건 이상 발표한 시니어 발표 디자이너입니다.
제안서 본문과 회사 자산을 18~24장 PT 슬라이드로 재구성합니다.

각 슬라이드에는 'type' 필드를 반드시 지정합니다:
- "bullets"       : 일반 불릿 본문 (3~6줄)
- "table"         : 표가 더 적합할 때 (마크다운 표)
- "metric"        : 한두 개의 핵심 정량 수치를 크게 강조해야 할 때
- "case_table"    : 수행실적·레퍼런스 표
- "solution"      : 자사 솔루션 한 가지를 카드로 소개할 때
- "architecture"  : 시스템 구성도/아키텍처 다이어그램이 필요할 때 (body에 텍스트 박스 다이어그램 포함)
- "section_break" : 큰 챕터 구분 페이지 (body는 짧은 부제 한 줄)
- "closing"       : 마지막 감사·연락처 슬라이드

표지/목차는 별도 생성기가 만들므로 제외하고 본문 슬라이드만 출력합니다.
출력은 JSON 배열만 (설명 금지).
"""


def _format_assets_for_prompt() -> str:
    """회사 자산을 LLM 프롬프트에 짧게 요약 주입."""
    parts = []
    for kind, label in [
        ("solution", "💡 자사 솔루션"),
        ("case", "🏆 수행실적"),
        ("cert", "🎖️ 인증"),
        ("metric", "📊 정량 수치"),
    ]:
        items = db.list_company_assets(kind=kind)
        if not items:
            continue
        lines = [f"### {label}"]
        for it in items[:6]:
            extra = it.get("extra") or {}
            meta = " / ".join(f"{k}={v}" for k, v in extra.items() if v)
            lines.append(f"- {it['title']}" + (f"  ({meta})" if meta else "") +
                         (f" — {(it.get('body') or '')[:120]}" if it.get("body") else ""))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _build_prompt(bid: BidNotice, draft: ProposalDraft) -> str:
    company = get_effective_company()
    sections_text = "\n\n".join(
        f"## {sec.title}\n{sec.body[:1500]}" for sec in sorted(draft.sections, key=lambda s: s.order)
    )
    asset_block = _format_assets_for_prompt()
    return f"""다음 제안서를 18~24장 PT로 재구성하세요.

# 사업명
{bid.title}

# 제안 회사
{company.name} (대표 {company.ceo})

# 회사 자산 (가능하면 인용해서 슬라이드를 만드세요)
{asset_block or '(자산 라이브러리 비어 있음)'}

# 제안서 본문
{sections_text}

# 권장 흐름
1. section_break: 사업 이해
2~3. bullets: 추진 배경 / 핵심 과업
4. metric: 핵심 정량 효과 한두 개
5. section_break: 추진 전략
6. architecture: 시스템 구성도
7~8. solution: 자사 솔루션 카드 1~2장
9. section_break: 세부 수행 계획
10. table: 일정/마일스톤 표
11~12. bullets: 단계별 활동
13. table: 조직/투입 인력
14. section_break: 회사 소개·실적
15. case_table: 유사 수행실적 표
16. metric: 핵심 인증·수치 강조
17. bullets: 차별화 포인트
18. closing: 감사 인사

위는 권장이며, 본문 분량·자산 보유 정도에 따라 자유롭게 가감하세요.

# 출력 (JSON 배열만, 설명 없이)
[
  {{"type":"section_break", "title":"사업 이해", "subtitle":"발주처가 정말 원하는 것"}},
  {{"type":"bullets", "title":"추진 배경", "body":"불릿1\\n불릿2\\n불릿3"}},
  {{"type":"metric", "title":"핵심 정량 효과", "metrics":[{{"value":"90.05%","label":"음성인식 정확도"}}, {{"value":"-45%","label":"행정 처리시간 단축"}}]}},
  {{"type":"table", "title":"추진 일정", "table":[["단계","기간","주요 활동"],["분석","1개월","요구사항 정의"],["개발","3개월","핵심 기능 구현"]]}},
  {{"type":"case_table", "title":"유사 수행실적", "cases":[{{"year":"2024","client":"OO부","title":"OOO 구축","value":"5억"}}]}},
  {{"type":"solution", "title":"자사 핵심 솔루션", "name":"VoiceEz","tagline":"E2E 한국어 음성인식","points":["GS인증 보유","자체 신경망"]}},
  {{"type":"architecture", "title":"시스템 구성도", "body":"┌─ 사용자 ─┐\\n│  웹·모바일  │\\n└─────┬─────┘\\n      ↓\\n  API Gateway"}},
  {{"type":"closing", "title":"감사합니다", "subtitle":"함께 만들어 가겠습니다"}}
]
"""


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=8))
def _ask_llm(model: str, bid: BidNotice, draft: ProposalDraft) -> List[dict]:
    raw = chat_anthropic(model, SYSTEM_PROMPT, _build_prompt(bid, draft), max_tokens=8000)
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        raise ValueError("PT JSON 추출 실패")
    arr = json.loads(m.group(0))
    cleaned = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        if not item.get("title"):
            continue
        item.setdefault("type", "bullets")
        cleaned.append(item)
    return cleaned


class ChoiPT:
    name = "최피티"
    role = "발표자료 자동 생성"
    model = MODELS.choi

    def generate(self, bid: BidNotice, draft: ProposalDraft) -> str:
        logger.info(f"[{self.name}] PT 생성 시작 ({bid.bid_id})")
        slides = _ask_llm(self.model, bid, draft)
        template = None
        try:
            template = db.get_design_template()
        except Exception:
            template = None
        path = render_pptx(bid.bid_id, bid.title, slides, template=template)
        try:
            db.update_proposal_pptx(bid.bid_id, draft.version, str(path))
        except Exception as e:
            logger.warning(f"[{self.name}] pptx_path 저장 실패: {e}")
        db.log_audit(AuditLogEntry(
            agent_name=self.name,
            model=self.model,
            bid_id=bid.bid_id,
            note=f"slides={len(slides)}",
        ))
        logger.info(f"[{self.name}] PT 완료: {path}")
        return str(path)

    def run(self, bid_id: Optional[str] = None, progress_cb=None) -> List[str]:
        from sqlalchemy import select
        from tools.db import ProposalRow, session_scope

        results: List[str] = []
        target_bids = db.list_bids(status=BidStatus.DRAFT_DONE) + db.list_bids(status=BidStatus.APPROVED)
        if bid_id:
            target_bids = [b for b in target_bids if b.bid_id == bid_id]

        total = len(target_bids)
        if progress_cb:
            progress_cb(0, total, f"PT 변환 대상 {total}건")
        for idx, bid in enumerate(target_bids, start=1):
            with session_scope() as s:
                row = s.execute(
                    select(ProposalRow)
                    .where(ProposalRow.bid_id == bid.bid_id)
                    .order_by(ProposalRow.version.desc())
                ).scalar_one_or_none()
                if not row:
                    logger.warning(f"[{self.name}] 초안 미존재 ({bid.bid_id})")
                    continue
                from schemas.models import ProposalDraft, ProposalSection
                draft = ProposalDraft(
                    bid_id=row.bid_id,
                    version=row.version,
                    sections=[ProposalSection(**sec) for sec in (row.sections or [])],
                    docx_path=row.docx_path,
                )

            try:
                path = self.generate(bid, draft)
                results.append(path)
            except Exception as e:
                logger.error(f"[{self.name}] PT 실패 ({bid.bid_id}): {e}")
            if progress_cb:
                progress_cb(idx, total, f"{bid.title[:30]} PT 완료")
        return results
