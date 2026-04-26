"""
인간 승인 게이트.
Notion이 설정되어 있으면 Notion DB의 '참여확정' 체크 여부를 동기화한다.
미설정 시 SQLite 직접 조작 CLI(`python main.py --approve <bid_id>`)로 승인.
"""

from __future__ import annotations

from typing import List

from loguru import logger

from config.settings import KEYS
from schemas.models import BidStatus
from tools import db
from tools.notion_client import get_client


def sync_approvals_from_notion() -> int:
    """Notion에서 '참여확정' 체크된 공고를 APPROVED로 전환. 전환 건수 반환."""
    if not KEYS.has_notion:
        logger.debug("[gate-1] Notion 미설정 → 수동 승인 모드")
        return 0
    client = get_client()
    if client is None:
        return 0

    try:
        results = client.databases.query(
            database_id=KEYS.notion_db_bids,
            filter={
                "property": "상태",
                "select": {"equals": "참여확정"},
            },
        )
    except Exception as e:
        logger.warning(f"Notion 조회 실패: {e}")
        return 0

    converted = 0
    for page in results.get("results", []):
        try:
            title_prop = page["properties"]["공고번호"]["title"]
            bid_id = title_prop[0]["plain_text"] if title_prop else None
            if not bid_id:
                continue
            bid = db.get_bid(bid_id)
            if bid:
                db.set_bid_status(bid_id, BidStatus.APPROVED)
                converted += 1
        except Exception as e:
            logger.debug(f"Notion page 파싱 실패: {e}")
    logger.info(f"[gate-1] {converted}건 APPROVED 전환")
    return converted


def manual_approve(bid_id: str) -> bool:
    """CLI 수동 승인 (Notion 없는 환경 또는 즉시 승인)."""
    bid = db.get_bid(bid_id)
    if not bid:
        logger.error(f"공고 없음: {bid_id}")
        return False
    db.set_bid_status(bid_id, BidStatus.APPROVED)
    logger.info(f"[gate-1] 수동 승인: {bid_id}")
    return True


def list_awaiting() -> List[str]:
    """승인 대기중인 공고 ID 목록."""
    return [b.bid_id for b in db.list_bids(status=BidStatus.AWAITING_APPROVAL)]
