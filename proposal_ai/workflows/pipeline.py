"""
v2 6-에이전트 파이프라인 오케스트레이션.
v1 crew_definition.py를 대체.

흐름:
  Discovery → Analysis → (게이트1) → Strategy → Writer → (게이트2) → Reviewer → (게이트3) → Graphics

각 단계 함수는 단독 실행 가능하며 ProgressCallback(str)을 받는다.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from loguru import logger

from agents.analysis_agent import AnalysisAgent
from agents.discovery_agent import DiscoveryAgent
from agents.graphics_agent import GraphicsAgent
from agents.reviewer_agent import ReviewerAgent
from agents.strategy_agent import StrategyAgent
from agents.writer_agent import WriterAgent
from config.settings import apply_agent_overrides
from schemas.models import (
    BidEvaluation,
    BidNotice,
    BidStatus,
    DesignBrief,
    ProposalDraft,
    QualityReport,
)
from schemas.rfp_schema import RfpStructured
from tools import db
from tools.notion_client import upsert_bid_to_notion
from workflows.human_gates import sync_approvals_from_notion


ProgressCB = Optional[Callable[[str], None]]


# ---------------------------------------------------------------------------
# 단계별 실행
# ---------------------------------------------------------------------------

def run_discovery(progress_cb: ProgressCB = None) -> List[BidNotice]:
    """공고 수집 + RFP 파싱 + RAG 인덱싱."""
    apply_agent_overrides()
    db.log_activity("discovery", "start", "공고 수집 시작")
    bids = DiscoveryAgent(progress_cb=progress_cb).run()
    db.log_activity("discovery", "finish", f"{len(bids)}건 수집 완료")
    return bids


def run_analysis(
    progress_cb: ProgressCB = None, max_auto_promote: int = 20
) -> List[BidEvaluation]:
    """COLLECTED 공고 1차 평가 + 게이트1 자동 승급."""
    apply_agent_overrides()
    db.log_activity("analysis", "start", "공고 적격성 평가 시작")
    results = AnalysisAgent(progress_cb=progress_cb).run(
        max_auto_promote=max_auto_promote
    )
    db.log_activity("analysis", "finish", f"{len(results)}건 평가 완료")

    # Notion 알림 — 임계 통과 공고만
    from config.settings import SYS
    for ev in results:
        bid = db.get_bid(ev.bid_id)
        if bid and ev.fit_score >= SYS.fit_score_threshold:
            try:
                upsert_bid_to_notion(bid, ev, status="승인대기")
            except Exception as e:
                logger.debug(f"[pipeline] Notion 동기화 스킵: {e}")
    return results


def run_strategy(
    bid_id: Optional[str] = None, progress_cb: ProgressCB = None
) -> List[RfpStructured]:
    """게이트1 통과(APPROVED) 공고에 RfpStructured 산출."""
    apply_agent_overrides()
    sync_approvals_from_notion()

    bids = db.list_bids(status=BidStatus.APPROVED)
    if bid_id:
        bids = [b for b in bids if b.bid_id == bid_id]

    db.log_activity("strategy", "start", f"전략 수립 대상 {len(bids)}건")
    agent = StrategyAgent(progress_cb=progress_cb)
    out: List[RfpStructured] = []
    for b in bids:
        try:
            out.append(agent.run(b))
        except Exception as e:
            logger.error(f"[pipeline] strategy 실패 ({b.bid_id}): {e}")
    db.log_activity("strategy", "finish", f"{len(out)}건 전략 완료")
    return out


def run_write(
    bid_id: Optional[str] = None,
    version: int = 1,
    progress_cb: ProgressCB = None,
    max_bids: Optional[int] = None,
) -> List[ProposalDraft]:
    """STRATEGY_DONE 공고에 제안서 초안 작성.

    비용 안전장치: writer는 건당 $1~2의 가장 비싼 단계.
    bid_id 미지정 + max_bids 미지정 + 5건 초과 시 자동으로 5건만 처리.
    전체 처리하려면 max_bids=N을 명시할 것.
    """
    apply_agent_overrides()
    bids = db.list_bids(status=BidStatus.STRATEGY_DONE)
    if bid_id:
        bids = [b for b in bids if b.bid_id == bid_id]
    elif max_bids is not None:
        if len(bids) > max_bids:
            logger.warning(
                f"[writer] {len(bids)}건 대기 중 → max_bids={max_bids} 제한 적용"
            )
        bids = bids[:max_bids]
    elif len(bids) > 5:
        logger.warning(
            f"[writer] {len(bids)}건이 대기 중. 비용 폭주 방지를 위해 5건으로 제한합니다. "
            f"전체 처리하려면 max_bids=N을 명시하세요. (예상 비용: ${len(bids) * 1.5:.1f}~${len(bids) * 2.0:.1f})"
        )
        bids = bids[:5]

    db.log_activity("writer", "start", f"초안 작성 대상 {len(bids)}건")
    agent = WriterAgent(progress_cb=progress_cb)
    out: List[ProposalDraft] = []
    for b in bids:
        try:
            out.append(agent.run(b, version=version))
        except Exception as e:
            logger.error(f"[pipeline] writer 실패 ({b.bid_id}): {e}")
    db.log_activity("writer", "finish", f"{len(out)}건 초안 완료")
    return out


def run_review(
    bid_id: Optional[str] = None, progress_cb: ProgressCB = None
) -> List[QualityReport]:
    """DRAFT_DONE 공고에 검수 + 수용표·TOC 일치율 검증."""
    apply_agent_overrides()
    pairs = _latest_drafts_by_status(BidStatus.DRAFT_DONE, bid_id)

    db.log_activity("reviewer", "start", f"검수 대상 {len(pairs)}건")
    agent = ReviewerAgent(progress_cb=progress_cb)
    out: List[QualityReport] = []
    for bid, draft in pairs:
        try:
            out.append(agent.run(bid, draft))
        except Exception as e:
            logger.error(f"[pipeline] reviewer 실패 ({bid.bid_id}): {e}")
    db.log_activity("reviewer", "finish", f"{len(out)}건 검수 완료")
    return out


def run_graphics(
    bid_id: Optional[str] = None, progress_cb: ProgressCB = None
) -> List[DesignBrief]:
    """FINAL_APPROVED 공고에 PPTX + 스토리보드 생성.
    UNDER_REVIEW도 사장님이 게이트3 우회로 수동 PPTX 빌드를 원할 수 있어 함께 포함."""
    apply_agent_overrides()
    pairs = (
        _latest_drafts_by_status(BidStatus.FINAL_APPROVED, bid_id)
        + _latest_drafts_by_status(BidStatus.UNDER_REVIEW, bid_id)
    )

    db.log_activity("graphics", "start", f"시각화 대상 {len(pairs)}건")
    agent = GraphicsAgent(progress_cb=progress_cb)
    out: List[DesignBrief] = []
    for bid, draft in pairs:
        try:
            out.append(agent.run(bid, draft))
        except Exception as e:
            logger.error(f"[pipeline] graphics 실패 ({bid.bid_id}): {e}")
    db.log_activity("graphics", "finish", f"{len(out)}건 시각화 완료")
    return out


# ---------------------------------------------------------------------------
# 일괄 실행
# ---------------------------------------------------------------------------

def run_all(progress_cb: ProgressCB = None) -> dict:
    """수집 → 평가 → 전략 → 작성 → 검수 → 시각화 일괄 실행.
    게이트1·2·3은 자동 승급 정책에 의존(임계 통과 + 상한). 수동 게이트는 main CLI / dashboard에서."""
    logger.info("=== v2 전체 파이프라인 시작 ===")
    discovered = run_discovery(progress_cb=progress_cb)
    evaluated = run_analysis(progress_cb=progress_cb)
    # 게이트1: AnalysisAgent가 자동 승급 (AWAITING_APPROVAL)
    # 게이트1 통과(APPROVED) → 사용자 승인 필요. 자동 실행 흐름에서는 0건일 수 있음.
    strategized = run_strategy(progress_cb=progress_cb)
    drafted = run_write(progress_cb=progress_cb)
    reviewed = run_review(progress_cb=progress_cb)
    graphics = run_graphics(progress_cb=progress_cb)

    summary = {
        "discovered": len(discovered),
        "evaluated": len(evaluated),
        "strategized": len(strategized),
        "drafted": len(drafted),
        "reviewed": len(reviewed),
        "graphics": len(graphics),
    }
    logger.info(f"=== v2 파이프라인 완료: {summary} ===")
    return summary


def run_propose(
    bid_id: Optional[str] = None, progress_cb: ProgressCB = None
) -> dict:
    """단일 공고에 대한 strategy → write → review → graphics 일괄.
    게이트1을 이미 통과한(APPROVED) 공고를 대상으로 호출."""
    s = run_strategy(bid_id=bid_id, progress_cb=progress_cb)
    w = run_write(bid_id=bid_id, progress_cb=progress_cb)
    r = run_review(bid_id=bid_id, progress_cb=progress_cb)
    g = run_graphics(bid_id=bid_id, progress_cb=progress_cb)
    return {
        "strategized": len(s),
        "drafted": len(w),
        "reviewed": len(r),
        "graphics": len(g),
    }


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------

def _latest_drafts_by_status(
    status: BidStatus, bid_id: Optional[str] = None
) -> List[tuple[BidNotice, ProposalDraft]]:
    """상태별 공고와 가장 최신 ProposalDraft 페어 로드."""
    from sqlalchemy import select
    from schemas.models import ProposalSection
    from tools.db import ProposalRow, session_scope

    bids = db.list_bids(status=status)
    if bid_id:
        bids = [b for b in bids if b.bid_id == bid_id]

    pairs: List[tuple[BidNotice, ProposalDraft]] = []
    for bid in bids:
        with session_scope() as s:
            row = s.execute(
                select(ProposalRow)
                .where(ProposalRow.bid_id == bid.bid_id)
                .order_by(ProposalRow.version.desc())
            ).scalars().first()
            if not row:
                continue
            draft = ProposalDraft(
                bid_id=row.bid_id,
                version=row.version,
                sections=[ProposalSection(**sec) for sec in (row.sections or [])],
                docx_path=row.docx_path,
                pptx_path=row.pptx_path,
            )
        pairs.append((bid, draft))
    return pairs
