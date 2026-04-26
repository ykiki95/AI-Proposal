"""
Notion 보고/게이트 클라이언트.
- 키 미설정 시 모든 호출이 no-op으로 동작 (SQLite local gate로 대체)
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from config.settings import KEYS
from schemas.models import BidEvaluation, BidNotice, QualityReport

try:
    from notion_client import Client as NotionSDK  # type: ignore
except ImportError:
    NotionSDK = None  # type: ignore


_client: Optional[object] = None


def get_client():
    global _client
    if not KEYS.has_notion or NotionSDK is None:
        return None
    if _client is None:
        _client = NotionSDK(auth=KEYS.notion_api_key)
    return _client


def upsert_bid_to_notion(bid: BidNotice, ev: BidEvaluation, status: str) -> None:
    """공고 + 평가 결과를 Notion DB에 업로드."""
    c = get_client()
    if c is None:
        logger.debug(f"[notion-skip] {bid.bid_id} ({status})")
        return
    try:
        c.pages.create(
            parent={"database_id": KEYS.notion_db_bids},
            properties={
                "공고번호": {"title": [{"text": {"content": bid.bid_id}}]},
                "사업명": {"rich_text": [{"text": {"content": bid.title}}]},
                "발주기관": {"rich_text": [{"text": {"content": bid.agency}}]},
                "예산": {"number": bid.budget_krw or 0},
                "마감일": {"date": {"start": bid.deadline.isoformat()}},
                "적합도": {"number": ev.fit_score},
                "추천": {"select": {"name": ev.recommendation}},
                "상태": {"select": {"name": status}},
            },
        )
    except Exception as e:
        logger.warning(f"Notion 업로드 실패 ({bid.bid_id}): {e}")


def push_quality_report(bid: BidNotice, report: QualityReport) -> None:
    c = get_client()
    if c is None:
        logger.debug(f"[notion-skip] quality report {bid.bid_id}")
        return
    try:
        c.pages.create(
            parent={"database_id": KEYS.notion_db_proposals},
            properties={
                "공고번호": {"title": [{"text": {"content": bid.bid_id}}]},
                "버전": {"number": report.version},
                "등급": {"select": {"name": report.overall_grade}},
                "조치사항": {
                    "rich_text": [{"text": {"content": "\n".join(report.action_items[:5])}}]
                },
            },
        )
    except Exception as e:
        logger.warning(f"Notion 검수 리포트 업로드 실패 ({bid.bid_id}): {e}")
