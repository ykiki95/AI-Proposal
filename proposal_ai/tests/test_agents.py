"""
기본 단위 테스트.
LLM 호출 없이 동작하는 부분(스키마, DB, 샘플 G2B, DOCX/PPTX)에 집중.
실행: python -m pytest tests/ -v   또는   python tests/test_agents.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas.models import (
    BidEvaluation,
    BidNotice,
    ProposalDraft,
    ProposalSection,
)
from tools import db, g2b_api
from tools.docx_generator import render_proposal_docx
from tools.pptx_generator import render_pptx


def test_schema_roundtrip() -> None:
    bid = BidNotice(
        bid_id="TEST-001",
        agency="테스트청",
        title="테스트 사업",
        deadline=datetime.now() + timedelta(days=7),
    )
    assert bid.bid_id == "TEST-001"
    print("[ok] schema roundtrip")


def test_g2b_sample_fallback() -> None:
    bids = g2b_api.search_bids("홈페이지")
    assert len(bids) >= 1
    assert all(isinstance(b, BidNotice) for b in bids)
    print(f"[ok] g2b sample fallback ({len(bids)}건)")


def test_db_init_and_upsert() -> None:
    db.init_db()
    bid = BidNotice(
        bid_id="TEST-DB-001",
        agency="DB청",
        title="DB 테스트",
        deadline=datetime.now() + timedelta(days=3),
    )
    inserted = db.upsert_bid(bid)
    again = db.upsert_bid(bid)
    assert inserted is True
    assert again is False  # 중복 제거
    print("[ok] db upsert + dedup")


def test_docx_pptx_generation() -> None:
    draft = ProposalDraft(
        bid_id="TEST-DOCX",
        version=1,
        sections=[
            ProposalSection(title="사업 이해", body="본 사업은 테스트 사업이다.\n\n- 항목1\n- 항목2", order=0),
            ProposalSection(title="기대 효과", body="기대 효과 본문", order=1),
        ],
    )
    docx = render_proposal_docx(draft, "테스트 제안서")
    assert docx.exists()
    pptx = render_pptx("TEST-PPTX", "테스트 사업명", [
        ("핵심 메시지", "메시지1\n메시지2\n메시지3"),
        ("일정", "1단계\n2단계\n3단계"),
    ])
    assert pptx.exists()
    print(f"[ok] docx={docx.name}, pptx={pptx.name}")


if __name__ == "__main__":
    test_schema_roundtrip()
    test_g2b_sample_fallback()
    test_db_init_and_upsert()
    test_docx_pptx_generation()
    print("\n모든 테스트 통과 ✅")
