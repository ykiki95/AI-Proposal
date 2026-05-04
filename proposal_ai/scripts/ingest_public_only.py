"""공공 4건 학습 코퍼스만 reference_proposals 컬렉션에 인덱싱.

인덱싱 대상 화이트리스트:
  - [경찰청] 스마트워크시스템 고도화 사업 제안_발표자료_GD_완_v10.pdf
  - [정성제안서]KYWA 대표홈페이지 유지보수 용역_v1.0.pdf
  - 2026 서울 MICE 홈페이지 및 종합지원시스템 통합 유지보수 용역_기술제안서_원본.pdf
  - 2026년 내친구서울 누리집 재개발 용역_기술제안서_원본.pdf

PPTX는 처리하지 않음. 민간(SKT/T world)·발표자료 KYWA는 제외.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import VECTOR, WINNING_PROPOSALS_DIR
from rag.chunker import chunk_pdf_doc
from rag.embeddings import embed_documents, embed_query
from rag.pdf_processor import PDFProcessor
from rag.vector_store import VectorStore


WHITELIST = [
    "[경찰청] 스마트워크시스템 고도화 사업 제안_발표자료_GD_완_v10.pdf",
    "[정성제안서]KYWA 대표홈페이지 유지보수 용역_v1.0.pdf",
    "2026 서울 MICE 홈페이지 및 종합지원시스템 통합 유지보수 용역_기술제안서_원본.pdf",
    "2026년 내친구서울 누리집 재개발 용역_기술제안서_원본.pdf",
]

VERIFY_QUERIES = [
    "공공기관 유지보수 평가 기준",
    "보안 요구사항 대응 방안",
    "프로젝트 관리 방법론",
]


def main() -> int:
    pdf_dir = WINNING_PROPOSALS_DIR
    targets = []
    for name in WHITELIST:
        p = pdf_dir / name
        if not p.is_file():
            print(f"[ERROR] 화이트리스트 PDF 누락: {p}", file=sys.stderr)
            return 1
        targets.append(p)

    print("=" * 70)
    print(f"공공 4건 인덱싱 시작 ({pdf_dir})")
    for t in targets:
        print(f"  - {t.name}")
    print("=" * 70)

    store = VectorStore(collection_name=VECTOR.collection_proposals)
    processor = PDFProcessor()

    t_start = time.time()
    embed_calls = 0
    embed_items = 0
    total_chunks = 0
    per_file: dict[str, int] = {}

    for idx, pdf_path in enumerate(targets, start=1):
        print(f"\n[{idx}/{len(targets)}] {pdf_path.name}")
        f_start = time.time()

        doc = processor.process(pdf_path)
        pages = doc.get("metadata", {}).get("total_pages", 0)
        chunks = chunk_pdf_doc(doc)
        if not chunks:
            print("  ! 청크 없음 — 스킵")
            continue

        print(f"  ▷ 페이지 {pages} / 청크 {len(chunks)}")

        texts = [c["text"] for c in chunks]
        batch = VECTOR.embedding_batch_size
        embed_calls += (len(texts) + batch - 1) // batch
        embed_items += len(texts)
        embeddings = embed_documents(texts, batch_size=batch)

        added = store.add_chunks(chunks, embeddings)
        total_chunks += added
        per_file[pdf_path.name] = added
        print(f"  ✓ {added}청크 저장 ({time.time() - f_start:.1f}s)")

    elapsed = time.time() - t_start
    final_count = store.count()

    print("\n" + "=" * 70)
    print(f"인덱싱 완료: {sum(per_file.values())}청크 (컬렉션 총 {final_count})")
    print(f"임베딩 API 호출: {embed_calls} (총 {embed_items}건)")
    print(f"총 소요: {elapsed:.1f}s")
    for k, v in per_file.items():
        print(f"  - {k}: {v}청크")

    # ---- 검증 쿼리 ----
    print("\n" + "=" * 70)
    print("검증 쿼리 (top-3 / 출처 + 유사도)")
    print("=" * 70)

    accepted = {p.name for p in targets}
    failed = False
    for q in VERIFY_QUERIES:
        print(f"\n[Q] {q}")
        qvec = embed_query(q)
        results = store.query(qvec, n_results=3)
        for i, r in enumerate(results, 1):
            src = (
                r["metadata"].get("source_file")
                or r["metadata"].get("filename")
                or r["metadata"].get("source")
                or "(unknown)"
            )
            score = r.get("score", 0.0)
            print(f"  {i}. score={score:.4f}  src={src}")
            if src not in accepted:
                failed = True
                print(f"     ⚠ 화이트리스트 외 파일 감지!")

    print("\n" + "=" * 70)
    if failed:
        print("[FAIL] 검증 쿼리에서 화이트리스트 외 출처가 발견됨")
        return 1
    print("[OK] 모든 검증 쿼리가 4개 화이트리스트 안에서 결과 반환")
    return 0


if __name__ == "__main__":
    sys.exit(main())
