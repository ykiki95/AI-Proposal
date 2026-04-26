"""
에이전트 팀 오케스트레이션.
명세서의 'CrewAI Crew' 역할을 이 모듈이 담당한다.
각 에이전트를 직접 호출 가능한 함수로 노출하고,
'all' 모드는 게이트를 거치며 순차 실행한다.
"""

from __future__ import annotations

from loguru import logger

from agents import ChoiPT, KimDetective, LeeJudge, OhQuality, ParkProposer
from config.settings import apply_agent_overrides
from schemas.models import BidStatus
from tools import db
from workflows.human_gates import sync_approvals_from_notion


def run_collect(progress_cb=None):
    """김탐정 - 수집."""
    apply_agent_overrides()
    db.log_activity("kim", "start", "공고 수집 시작")
    bids = KimDetective().run(progress_cb=progress_cb)
    db.log_activity("kim", "finish", f"{len(bids)}건 수집 완료")
    return bids


def run_evaluate(progress_cb=None):
    """이판단 - 평가 + 게이트1 알림."""
    apply_agent_overrides()
    db.log_activity("lee", "start", "공고 평가 시작")
    results = LeeJudge().run(progress_cb=progress_cb)
    db.log_activity("lee", "finish", f"{len(results)}건 평가 완료")
    # Notion 등 외부 보고
    from tools.notion_client import upsert_bid_to_notion
    for ev in results:
        bid = db.get_bid(ev.bid_id)
        if bid and ev.fit_score >= 70:
            upsert_bid_to_notion(bid, ev, status="승인대기")
    return results


def run_draft(bid_id=None, park_cb=None):
    """게이트1 참여확정 직후 — 박제안만 실행하여 DOCX 초안 생성 (DRAFT_DONE)."""
    apply_agent_overrides()
    sync_approvals_from_notion()
    db.log_activity("park", "start", f"제안서 초안 작성 시작{f' ({bid_id})' if bid_id else ''}")
    drafts = ParkProposer().run(bid_id=bid_id, progress_cb=park_cb)
    db.log_activity("park", "finish", f"{len(drafts)}건 초안 완료")
    return {"drafts": drafts}


def run_pt(bid_id=None, choi_cb=None, oh_cb=None):
    """게이트2 통과 후 — 최피티 PPT 변환 + 오품질 검수 (UNDER_REVIEW)."""
    apply_agent_overrides()
    db.log_activity("choi", "start", "PT 슬라이드 변환 시작")
    pts = ChoiPT().run(bid_id=bid_id, progress_cb=choi_cb)
    db.log_activity("choi", "finish", f"{len(pts)}건 PT 완료")
    db.log_activity("oh", "start", "품질 검수 시작")
    reports = OhQuality().run(bid_id=bid_id, progress_cb=oh_cb)
    db.log_activity("oh", "finish", f"{len(reports)}건 검수 완료")
    return {"pts": pts, "reports": reports}


def run_propose(bid_id=None, park_cb=None, choi_cb=None, oh_cb=None):
    """전체 파이프라인 일괄 실행 (수동 실행 탭에서 사용)."""
    a = run_draft(bid_id=bid_id, park_cb=park_cb)
    b = run_pt(bid_id=bid_id, choi_cb=choi_cb, oh_cb=oh_cb)
    return {"drafts": a["drafts"], "pts": b["pts"], "reports": b["reports"]}


def run_all(kim_cb=None, lee_cb=None, park_cb=None, choi_cb=None, oh_cb=None):
    """수집부터 검수까지 전체 파이프라인."""
    logger.info("=== 전체 파이프라인 시작 ===")
    collected = run_collect(progress_cb=kim_cb)
    evaluated = run_evaluate(progress_cb=lee_cb)
    pipeline = run_propose(park_cb=park_cb, choi_cb=choi_cb, oh_cb=oh_cb)
    return {
        "collected": len(collected),
        "evaluated": len(evaluated),
        "drafts": len(pipeline["drafts"]),
        "pts": len(pipeline["pts"]),
        "reports": len(pipeline["reports"]),
    }
