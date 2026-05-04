"""로컬 PDF를 BidNotice로 등록 + ChromaDB 인덱싱 + SQLite 적재."""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agents.discovery_agent import DiscoveryAgent
from config.settings import VECTOR
from schemas.models import BidNotice
from tools import db


def main() -> int:
    ap = argparse.ArgumentParser(description="로컬 RFP PDF 등록 헬퍼 (BidNotice + RAG)")
    ap.add_argument("--pdf-path", required=True, help="로컬 PDF 절대경로")
    ap.add_argument("--bid-id", required=True, help="임의 식별자 (예: smoke_001)")
    ap.add_argument("--agency", required=True, help="발주기관명")
    ap.add_argument("--title", required=True, help="사업명")
    ap.add_argument("--deadline", required=True, help="제출 마감일 (YYYY-MM-DD)")
    ap.add_argument("--budget", type=int, default=None, help="예산 (원)")
    ap.add_argument("--duration", type=int, default=None, help="사업 기간 (개월)")
    args = ap.parse_args()

    pdf_path = Path(args.pdf_path).resolve()
    if not pdf_path.is_file():
        print(f"[ERROR] PDF 경로 미존재: {pdf_path}", file=sys.stderr)
        return 1

    bid = BidNotice(
        bid_id=args.bid_id,
        source="민간",
        agency=args.agency,
        title=args.title,
        budget_krw=args.budget,
        duration_months=args.duration,
        deadline=datetime.fromisoformat(args.deadline),
        rfp_pdf_path=str(pdf_path),
    )

    db.init_db()

    chunk_count = 0
    try:
        from rag.vector_store import VectorStore
        store = VectorStore(collection_name=VECTOR.collection_rfp)
        before = store.count()
        DiscoveryAgent(index_rfps=True)._enrich_and_index([bid])
        chunk_count = store.count() - before
    except Exception:
        traceback.print_exc()
        print("[ERROR] RFP 인덱싱 실패 — 중단", file=sys.stderr)
        return 1

    db_ok = False
    try:
        db_ok = db.upsert_bid(bid)
    except Exception as e:
        print(f"[WARN] DB 등록 실패 (ChromaDB는 인덱싱 됨): {e}", file=sys.stderr)

    print(f"bid_id        : {bid.bid_id}")
    print(f"PDF           : {pdf_path}")
    print(f"인덱싱 청크   : {chunk_count}")
    print(f"DB 등록       : {'신규' if db_ok else '중복/실패'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
