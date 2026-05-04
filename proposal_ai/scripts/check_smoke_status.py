"""특정 bid_id의 SQLite 상태 + ChromaDB 청크 + 산출물 경로를 한 번에 진단.

사용:
  python scripts/check_smoke_status.py --bid-id smoke_001
  python scripts/check_smoke_status.py            # 전체 BidNotice 상태 요약

수정 이력:
  2026-05-04 v2: BidNotice에 status 속성 없음을 발견. db.list_bids(status=X)로 역조회.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import VECTOR, OUTPUT_DIR, RFP_CACHE_DIR
from rag.vector_store import VectorStore
from schemas.models import BidStatus
from tools import db


def _print_section(title: str) -> None:
    print()
    print("=" * 60)
    print(f" {title}")
    print("=" * 60)


def _find_status_for(bid_id: str) -> Optional[BidStatus]:
    """모든 BidStatus를 순회하며 해당 bid_id가 속한 상태를 찾음."""
    for status in BidStatus:
        for b in db.list_bids(status=status):
            if b.bid_id == bid_id:
                return status
    return None


def _check_chroma_chunks(bid_id: str) -> int:
    """ChromaDB rfp_documents 컬렉션에서 해당 bid_id 청크 수 카운트."""
    try:
        store = VectorStore(collection_name=VECTOR.collection_rfp)
        try:
            res = store.collection.get(
                where={"bid_id": bid_id},
                limit=10000,
                include=["metadatas"],
            )
            return len(res.get("ids", []) or [])
        except Exception:
            res = store.collection.get(limit=10000, include=["metadatas"])
            cnt = 0
            for m in res.get("metadatas", []) or []:
                if bid_id in str(m.get("source_file", "")) or bid_id in str(m.get("bid_id", "")):
                    cnt += 1
            return cnt
    except Exception as e:
        print(f"  [WARN] ChromaDB 조회 실패: {e}")
        return -1


def _check_outputs(bid_id: str) -> dict:
    """OUTPUT_DIR에서 bid_id 관련 산출물 파일 목록."""
    found = {"docx": [], "pptx": [], "rfp_cache": []}
    if OUTPUT_DIR.exists():
        for p in OUTPUT_DIR.iterdir():
            if bid_id in p.name:
                if p.suffix == ".docx":
                    found["docx"].append(p.name)
                elif p.suffix == ".pptx":
                    found["pptx"].append(p.name)
    cache_dir = RFP_CACHE_DIR / bid_id
    if cache_dir.exists():
        found["rfp_cache"] = [p.name for p in cache_dir.iterdir()]
    return found


def diagnose_bid(bid_id: str) -> int:
    """단일 bid_id 진단 리포트."""
    db.init_db()

    _print_section(f"BID 진단: {bid_id}")

    bid = db.get_bid(bid_id)
    if not bid:
        print(f"  [ERROR] SQLite에 BidNotice 없음. register_local_rfp.py 미실행 가능성.")
        return 1

    status = _find_status_for(bid_id)

    print(f"  공고명     : {bid.title}")
    print(f"  발주처     : {bid.agency}")
    print(f"  PDF 경로   : {bid.rfp_pdf_path or '(미지정)'}")
    print(f"  마감일     : {bid.deadline}")
    print(f"  예산       : {bid.budget_krw or '(미입력)'}")
    print(f"  현재 상태  : {status.value if status else '(미확인)'}")

    _print_section("ChromaDB 인덱싱")
    chunks = _check_chroma_chunks(bid_id)
    if chunks == -1:
        print("  조회 실패")
    elif chunks == 0:
        print(f"  [경고] {bid_id} 관련 청크 0건 — RFP 인덱싱이 안 된 상태.")
    else:
        print(f"  rfp_documents 컬렉션 내 {bid_id} 관련 청크: {chunks}건")

    _print_section("산출물 파일")
    out = _check_outputs(bid_id)
    print(f"  DOCX: {len(out['docx'])}개 — {', '.join(out['docx']) or '(없음)'}")
    print(f"  PPTX: {len(out['pptx'])}개 — {', '.join(out['pptx']) or '(없음)'}")
    print(f"  RFP 캐시: {len(out['rfp_cache'])}개")

    _print_section("다음 단계 안내")
    if status is None:
        print("  상태를 알 수 없음. SQLite 직접 확인 필요.")
        print("  python -c \"from tools import db; from schemas.models import BidStatus; \\")
        print("    [print(s.value, [b.bid_id for b in db.list_bids(status=s)]) for s in BidStatus]\"")
    elif status == BidStatus.COLLECTED:
        print("  → analyze 실행: python main.py --mode analyze")
    elif status == BidStatus.EVALUATED:
        print("  → 점수 임계 미달로 평가만 됨. is_pinned로 강제 진입하려면 DB 직접 수정.")
    elif status == BidStatus.AWAITING_APPROVAL:
        print(f"  → 게이트1 수동 승인: python main.py --approve {bid_id}")
    elif status == BidStatus.APPROVED:
        print(f"  → strategize 실행: python main.py --mode strategize --bid {bid_id}")
    elif status == BidStatus.STRATEGY_DONE:
        print(f"  → write 실행: python main.py --mode write --bid {bid_id}")
    elif status == BidStatus.DRAFT_DONE:
        print(f"  → review 실행: python main.py --mode review --bid {bid_id}")
    elif status == BidStatus.UNDER_REVIEW:
        print(f"  → 게이트3 수동 승인 후 graphics. (UNDER_REVIEW도 graphics가 처리)")
        print(f"     python main.py --mode graphics --bid {bid_id}")
    elif status == BidStatus.FINAL_APPROVED:
        print(f"  → graphics 실행: python main.py --mode graphics --bid {bid_id}")
    else:
        print(f"  현재 상태({status})에 대한 다음 단계 가이드 없음.")
    return 0


def summary_all() -> int:
    """전체 BidNotice 상태 요약."""
    db.init_db()
    _print_section("전체 BidNotice 상태별 카운트")

    total = 0
    for status in BidStatus:
        bids = db.list_bids(status=status)
        if bids:
            print(f"  {status.value:>22} : {len(bids)}건")
            total += len(bids)

    if total == 0:
        all_bids = db.list_bids()
        if not all_bids:
            print("  등록된 BidNotice 없음.")
            return 0
        print(f"  (상태 필터 없이) 총 {len(all_bids)}건")
    else:
        print(f"  {'합계':>22} : {total}건")

    _print_section("개별 목록 (각 상태별 최대 10건)")
    for status in BidStatus:
        bids = db.list_bids(status=status)
        for b in bids[:10]:
            print(f"  [{status.value:>22}] {b.bid_id} | {b.title[:40]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="bid_id 진단 헬퍼")
    ap.add_argument("--bid-id", help="특정 bid_id (없으면 전체 요약)")
    args = ap.parse_args()
    if args.bid_id:
        return diagnose_bid(args.bid_id)
    return summary_all()


if __name__ == "__main__":
    sys.exit(main())
