"""
ReviewerAgent — 제안서 품질·수용표·목차 일치율 검증.

v1 OhQuality에서 OpenAI 의존을 제거하고 v2 핵심 검증을 추가:
  + 수용표(AcceptanceTable) 100% 수용 검증 — 한국 공공 제안 필수
  + RFP 요청 목차 ↔ 우리 제안서 섹션 일치율 (TocSimilarity)

게이트3 정책:
  overall_grade == "A"        → BidStatus.FINAL_APPROVED
  acceptance_table.is_valid 가 False 또는 grade B/C → BidStatus.UNDER_REVIEW
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Set

from loguru import logger

from agents.base_agent import BaseAgent, ProgressCallback
from agents.strategy_agent import load_strategy
from config.settings import MODELS, PROPOSALS_DIR, SYS
from schemas.models import (
    AuditLogEntry,
    BidNotice,
    BidStatus,
    ProposalDraft,
    QualityReport,
)
from schemas.rfp_schema import (
    AcceptanceStatus,
    AcceptanceTable,
    RfpStructured,
    TocSimilarity,
)
from tools import db
from tools.acceptance_table import build_table, validate_table
from tools.korean_checker import check_spelling
from tools.llm_client import chat
from tools.notion_client import push_quality_report


REVIEW_SYSTEM = """당신은 한국어 제안서 품질 책임자입니다.
입력은 RFP와 제안서 본문입니다. 한국 공공·민간 제안서 평가위원의 시선으로 검수합니다.
JSON 한 객체로만 응답하세요. 설명/마크다운 없이.

# 출력 JSON 스키마
{
  "consistency_issues": ["같은 개념에 다른 용어 혼용 사례 등 5개 이내"],
  "style_issues": ["경어체/평어체 혼용 등 문체 일관성 이슈 3개 이내"],
  "rfp_coverage": {"요구사항 키워드": true|false, ...},
  "overall_grade": "A" | "B" | "C",
  "action_items": ["수정 지시 5개 이내, 구체적·실행 가능한 표현"]
}

평가 기준:
- A: 수정 없이 제출 가능
- B: 경미 수정 (오탈자·용어 통일·표현 다듬기)
- C: 재작성 필요 (요구사항 누락·논리 비약·신뢰성 부족)
- 거짓 수치/실적 의심이 보이면 무조건 C로 강등하고 action_items에 명시.
- 모든 한국어 출력."""


REVIEW_USER_TEMPLATE = """# RFP (발췌)
{rfp_text}

# 제안서 본문 (앞부분)
{proposal_text}

# 수용표 검증 사전 결과
- 총 요구사항: {req_total}건
- 미수용: {rejected_count}건 / 미매핑: {unmapped_count}건
- 검증 이슈: {validation_issues}

# 목차 일치율
- 점수: {toc_score:.2f} (임계 {toc_threshold:.2f})
- 미커버 RFP 챕터: {unmatched_rfp}
"""


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _tokens(s: str) -> Set[str]:
    """한글/영문 토큰화 — 길이 2자 이상."""
    return {
        t.strip().lower()
        for t in re.split(r"[\s\.\,\-\/\[\]\(\)·]+", s or "")
        if len(t.strip()) >= 2
    }


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def compute_toc_similarity(
    rfp: RfpStructured, draft: ProposalDraft
) -> TocSimilarity:
    """RFP 요청 목차 ↔ 제안서 섹션 매칭. jaccard >= 0.5면 매치."""
    rfp_titles = [c.title for c in rfp.requested_toc]
    draft_titles = [s.title for s in draft.sections]

    rfp_token_sets = [(t, _tokens(t)) for t in rfp_titles]
    draft_token_sets = [(t, _tokens(t)) for t in draft_titles]

    matched: List[dict] = []
    matched_rfp: Set[str] = set()
    matched_draft: Set[str] = set()

    for rt, rtokens in rfp_token_sets:
        best_score = 0.0
        best_dt: Optional[str] = None
        for dt, dtokens in draft_token_sets:
            if dt in matched_draft:
                continue
            score = _jaccard(rtokens, dtokens)
            if score > best_score:
                best_score, best_dt = score, dt
        if best_dt and best_score >= 0.5:
            matched.append({
                "rfp_chapter": rt,
                "proposal_section": best_dt,
                "score": round(best_score, 3),
            })
            matched_rfp.add(rt)
            matched_draft.add(best_dt)

    denom = max(len(rfp_titles), len(draft_titles), 1)
    similarity = len(matched) / denom
    threshold = TocSimilarity.passing_threshold()

    return TocSimilarity(
        bid_id=draft.bid_id,
        similarity_score=round(similarity, 3),
        matched_chapters=matched,
        unmatched_rfp_chapters=[t for t in rfp_titles if t not in matched_rfp],
        unmatched_proposal_chapters=[t for t in draft_titles if t not in matched_draft],
        is_passing=similarity >= threshold,
    )


def _coverage_from_acceptance(table: AcceptanceTable) -> Dict[str, bool]:
    """AcceptanceTable → rfp_coverage(req_id → 매핑 여부) dict."""
    return {
        item.req_id: (
            item.acceptance_status == AcceptanceStatus.FULL
            and item.proposal_section
            and item.proposal_section != "(매핑 필요)"
        )
        for item in table.items
    }


def _save_acceptance_table(table: AcceptanceTable, version: int) -> str:
    """AcceptanceTable JSON 영속화."""
    path = PROPOSALS_DIR / f"{table.bid_id}_v{version}_acceptance.json"
    path.write_text(table.model_dump_json(indent=2), encoding="utf-8")
    return str(path)


class ReviewerAgent(BaseAgent):
    agent_name = "reviewer"

    def __init__(self, progress_cb: Optional[ProgressCallback] = None) -> None:
        super().__init__(progress_cb)
        self.model = MODELS.reviewer

    def run(self, bid: BidNotice, draft: ProposalDraft) -> QualityReport:
        rfp = load_strategy(bid.bid_id)
        if rfp is None:
            raise ValueError(
                f"[{self.agent_name}] {bid.bid_id}: StrategyAgent 산출물(RfpStructured) 없음"
            )

        self.progress(
            f"검수 시작: {bid.bid_id} v{draft.version} (model={self.model})"
        )

        # 1) 수용표
        table = build_table(rfp, draft)
        validation_issues = validate_table(table)
        unmapped = [
            i for i in table.items if i.proposal_section == "(매핑 필요)"
        ]
        coverage = _coverage_from_acceptance(table)
        table_path = _save_acceptance_table(table, draft.version)
        self.progress(
            f"수용표: 총 {table.total}건 / 완전수용 {table.full_count} / "
            f"미수용 {table.rejected_count} / 미매핑 {len(unmapped)}"
        )

        # 2) TOC 일치율
        toc_sim = compute_toc_similarity(rfp, draft)
        self.progress(
            f"목차 일치율 {toc_sim.similarity_score:.2f} "
            f"({'통과' if toc_sim.is_passing else '미달'} / 임계 "
            f"{TocSimilarity.passing_threshold():.2f})"
        )

        # 3) 맞춤법
        full_text = "\n\n".join(
            f"# {sec.title}\n{sec.body}"
            for sec in sorted(draft.sections, key=lambda s: s.order)
        )
        spelling_issues = check_spelling(full_text)

        # 4) LLM 종합 검수
        llm_data = self._llm_review(
            bid=bid,
            full_text=full_text,
            table=table,
            validation_issues=validation_issues,
            unmapped_count=len(unmapped),
            toc_sim=toc_sim,
        )

        # action_items: validation 이슈 + LLM 결과 병합 (중복 제거, 순서 유지)
        action_items: List[str] = []
        action_items.extend(validation_issues)
        if not toc_sim.is_passing:
            action_items.append(
                f"목차 일치율 {toc_sim.similarity_score:.2f} "
                f"< 임계 {TocSimilarity.passing_threshold():.2f} — "
                f"미커버 RFP 챕터 보강 필요: {toc_sim.unmatched_rfp_chapters[:3]}"
            )
        action_items.extend(llm_data.get("action_items", []))
        action_items = list(dict.fromkeys(a.strip() for a in action_items if a))[:10]

        # 게이트3 grade 보정: 수용표 부적합 시 최소 B 강등
        grade = llm_data.get("overall_grade", "B")
        if grade not in ("A", "B", "C"):
            grade = "B"
        if not table.is_valid and grade == "A":
            logger.info(
                f"[{self.agent_name}] {bid.bid_id}: 수용표 미통과 → A→B 강등"
            )
            grade = "B"
        if table.rejected_count > 0:
            grade = "C"

        report = QualityReport(
            bid_id=bid.bid_id,
            version=draft.version,
            spelling_issues=spelling_issues[:20],
            consistency_issues=llm_data.get("consistency_issues", []),
            rfp_coverage=coverage,
            acceptance_table_valid=table.is_valid,
            acceptance_rejected_count=table.rejected_count,
            toc_similarity_score=toc_sim.similarity_score,
            overall_grade=grade,
            action_items=action_items,
        )

        db.save_quality_report(report)
        db.log_audit(AuditLogEntry(
            agent_name=self.agent_name,
            model=self.model,
            bid_id=bid.bid_id,
            note=(
                f"grade={grade} accept_valid={table.is_valid} "
                f"toc={toc_sim.similarity_score:.2f} "
                f"reject={table.rejected_count} unmap={len(unmapped)} "
                f"→ {table_path.split('/')[-1].split(chr(92))[-1]}"
            ),
        ))
        try:
            push_quality_report(bid, report)
        except Exception as e:
            logger.debug(f"[{self.agent_name}] notion push 스킵: {e}")

        # 게이트3 상태 전이
        if grade == "A" and table.is_valid:
            db.set_bid_status(bid.bid_id, BidStatus.FINAL_APPROVED)
        else:
            db.set_bid_status(bid.bid_id, BidStatus.UNDER_REVIEW)

        self.progress(
            f"검수 완료: grade={grade}, accept_valid={table.is_valid}, "
            f"action {len(action_items)}건"
        )
        return report

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------

    def _llm_review(
        self,
        *,
        bid: BidNotice,
        full_text: str,
        table: AcceptanceTable,
        validation_issues: List[str],
        unmapped_count: int,
        toc_sim: TocSimilarity,
    ) -> dict:
        rfp_text = (bid.rfp_full_text or bid.rfp_summary or "")[:6000]
        user = REVIEW_USER_TEMPLATE.format(
            rfp_text=rfp_text,
            proposal_text=full_text[:8000],
            req_total=table.total,
            rejected_count=table.rejected_count,
            unmapped_count=unmapped_count,
            validation_issues=" / ".join(validation_issues) or "(없음)",
            toc_score=toc_sim.similarity_score,
            toc_threshold=TocSimilarity.passing_threshold(),
            unmatched_rfp=", ".join(toc_sim.unmatched_rfp_chapters[:5]) or "(없음)",
        )
        try:
            raw = chat(
                self.model,
                REVIEW_SYSTEM,
                user,
                max_tokens=2500,
                cache_system=True,
            )
        except Exception as e:
            logger.warning(f"[{self.agent_name}] LLM 검수 실패 ({bid.bid_id}): {e}")
            return {}
        return _parse_json(raw)
