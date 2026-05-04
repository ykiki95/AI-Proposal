"""B-1 진행 상태 점검."""
from __future__ import annotations
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import VECTOR
from rag.vector_store import VectorStore


def main() -> int:
    store = VectorStore(collection_name=VECTOR.collection_proposals)
    cnt = store.count()
    print(f"[reference_proposals] 청크 수: {cnt}", flush=True)

    store2 = VectorStore(collection_name=VECTOR.collection_rfp)
    cnt2 = store2.count()
    print(f"[rfp_documents] 청크 수: {cnt2}", flush=True)

    if cnt > 0:
        try:
            res = store.collection.get(limit=10000, include=["metadatas"])
            files = {}
            for m in res.get("metadatas", []) or []:
                src = (m.get("source_file") or m.get("filename") or m.get("source") or "(unknown)")
                files[src] = files.get(src, 0) + 1
            print("\n파일별 청크 수:", flush=True)
            for k, v in sorted(files.items()):
                print(f"  - {k}: {v}", flush=True)
        except Exception as e:
            print(f"파일별 집계 실패: {e}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
