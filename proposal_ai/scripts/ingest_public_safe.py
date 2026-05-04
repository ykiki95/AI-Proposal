"""공공 PDF 안전 인덱싱 (B-1).

메모리 안전 모드:
  1. iter_pdf_chunks 제너레이터 사용 — 청크 list 누적 X
  2. STREAM_BATCH (기본 16) 청크가 모이면 즉시 임베딩 + ChromaDB upsert + 해제
  3. 매 파일 처리 후 gc.collect() + psutil로 RSS 메모리(MB) 출력
  4. 다음 파일 시작 전 sleep 5
  5. 한글 파일명 안전: sys.stdout.reconfigure(encoding='utf-8')
  6. Voyage embed: 90초 timeout + 3회 재시도 (지수 백오프)
  7. 모든 print에 flush=True

CLI:
  python scripts/ingest_public_safe.py                # 화이트리스트 4개 모두
  python scripts/ingest_public_safe.py --only "[경찰청]..."   # 특정 파일만
  python scripts/ingest_public_safe.py --limit 1     # 화이트리스트 앞 N개만
  python scripts/ingest_public_safe.py --skip-verify # 검증 쿼리 생략

⚠️ 백그라운드 실행 금지. 포그라운드만 사용.
"""

from __future__ import annotations

import argparse
import gc
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from pathlib import Path

# ─── 한글 출력 인코딩 안전 ────────────────────────────────────────────
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import psutil  # noqa: E402

from config.settings import KEYS, PROCESSED_DIR, VECTOR, WINNING_PROPOSALS_DIR  # noqa: E402
from rag.chunker import iter_pdf_chunks  # noqa: E402
from rag.pdf_processor import (  # noqa: E402
    PDFProcessor,
    _estimate_orderer,
    _estimate_project_type,
    _estimate_title,
    iter_pdf_pages,
)
from rag.vector_store import VectorStore  # noqa: E402


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

EMBED_BATCH_SIZE = 16        # ≤32, rate limit + timeout 안전
EMBED_TIMEOUT_SEC = 90.0
EMBED_MAX_RETRY = 3
STREAM_BATCH = 16            # 청킹→임베딩→upsert flush 단위
INTER_FILE_SLEEP_SEC = 5.0

_PROC = psutil.Process(os.getpid())


def _log(msg: str) -> None:
    print(msg, flush=True)


def _mem_mb() -> float:
    return _PROC.memory_info().rss / (1024 * 1024)


def _embed_batch_with_timeout(client, texts: list[str], model: str) -> list[list[float]]:
    """Voyage embed 단일 배치, timeout + 재시도."""
    last_err: Exception | None = None
    for attempt in range(1, EMBED_MAX_RETRY + 1):
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(
                lambda: client.embed(texts, model=model, input_type="document").embeddings
            )
            try:
                return fut.result(timeout=EMBED_TIMEOUT_SEC)
            except FutTimeout:
                last_err = TimeoutError(
                    f"Voyage embed timeout {EMBED_TIMEOUT_SEC}s (시도 {attempt}/{EMBED_MAX_RETRY})"
                )
                _log(f"    ! {last_err}")
            except Exception as e:
                last_err = e
                _log(f"    ! Voyage embed 실패 (시도 {attempt}/{EMBED_MAX_RETRY}): {e}")
        if attempt < EMBED_MAX_RETRY:
            wait = 2 ** attempt
            _log(f"    ↳ {wait}초 후 재시도")
            time.sleep(wait)
    raise RuntimeError(f"Voyage embed {EMBED_MAX_RETRY}회 모두 실패: {last_err}")


def _embed_documents_safe(client, model: str, texts: list[str]) -> list[list[float]]:
    """텍스트 배치 임베딩 (timeout/재시도 보강)."""
    valid_idx = [i for i, t in enumerate(texts) if t and t.strip()]
    valid_texts = [texts[i] for i in valid_idx]
    if not valid_texts:
        return [[] for _ in texts]

    embeddings: list[list[float]] = []
    total = len(valid_texts)
    for start in range(0, total, EMBED_BATCH_SIZE):
        batch = valid_texts[start : start + EMBED_BATCH_SIZE]
        batch_embs = _embed_batch_with_timeout(client, batch, model)
        embeddings.extend(batch_embs)
        if start + EMBED_BATCH_SIZE < total:
            time.sleep(0.3)  # rate limit 완화

    out: list[list[float]] = [[] for _ in texts]
    for k, orig in enumerate(valid_idx):
        out[orig] = embeddings[k]
    return out


def _process_pdf_with_progress(pdf_path: Path) -> dict:
    """페이지 진행률 출력 + 캐시 사용 + iter_pdf_pages 스트리밍.

    pdfplumber 메모리 누수 방지: page.flush_cache + 매 10페이지 gc.collect (헬퍼 내부).
    챕터 분할 직후 pages_text/pages_tables 즉시 해제.
    """
    import json

    cache_path = PROCESSED_DIR / f"{pdf_path.stem}.json"
    if cache_path.is_file():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            _log(f"    캐시 히트: {cache_path.name}")
            return cached
        except Exception as e:
            _log(f"    캐시 읽기 실패 (무시하고 재파싱): {e}")

    pages_text: list[str] = []
    pages_tables: list[list] = []
    next_marker = 5
    total_pages = 0
    first = True

    for page_num, total, text, tables in iter_pdf_pages(pdf_path):
        total_pages = total
        if first:
            _log(f"    pdfplumber 열림 (페이지 {total})")
            first = False
        pages_text.append(text)
        pages_tables.append(tables)
        if page_num >= next_marker or page_num == total:
            _log(f"    페이지 진행: {page_num}/{total}  mem={_mem_mb():.0f}MB")
            while next_marker <= page_num:
                next_marker += 5

    full_text = "\n".join(pages_text)
    proc = PDFProcessor()
    chapters = proc._split_chapters(pages_text, pages_tables)

    result = {
        "filename": pdf_path.name,
        "metadata": {
            "title": _estimate_title(pages_text),
            "total_pages": total_pages,
            "estimated_orderer": _estimate_orderer(pages_text),
            "estimated_project_type": _estimate_project_type(full_text),
        },
        "chapters": chapters,
        "raw_text": full_text,
    }
    # 큰 임시 list 즉시 해제 (raw_text/chapters에 압축 보관됨)
    del pages_text, pages_tables
    gc.collect()

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    _log(f"    캐시 저장: {cache_path.name}  mem={_mem_mb():.0f}MB")
    return result


def _embed_query_safe(client, model: str, q: str) -> list[float]:
    last_err: Exception | None = None
    for attempt in range(1, EMBED_MAX_RETRY + 1):
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(
                lambda: client.embed([q], model=model, input_type="query").embeddings[0]
            )
            try:
                return fut.result(timeout=EMBED_TIMEOUT_SEC)
            except FutTimeout:
                last_err = TimeoutError("query embed timeout")
            except Exception as e:
                last_err = e
        if attempt < EMBED_MAX_RETRY:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"query embed 실패: {last_err}")


def _ingest_one(pdf_path: Path, store: VectorStore, voyage_client, voyage_model: str) -> int:
    """단일 PDF를 스트리밍 청킹 → 작은 배치 임베딩 → 즉시 upsert.

    반환: 저장된 청크 수.
    """
    f_start = time.time()
    _log(f"  ▷ PDF 파싱… mem={_mem_mb():.0f}MB")
    doc = _process_pdf_with_progress(pdf_path)
    pages = doc.get("metadata", {}).get("total_pages", 0)
    _log(f"    페이지 {pages} / mem={_mem_mb():.0f}MB")

    _log(f"  ▷ 청킹 + 임베딩 + upsert (스트리밍, batch={STREAM_BATCH})…")
    file_total = 0
    batch_chunks: list[dict] = []

    def _flush():
        nonlocal file_total
        if not batch_chunks:
            return
        texts = [c["text"] for c in batch_chunks]
        embs = _embed_documents_safe(voyage_client, voyage_model, texts)
        added = store.add_chunks(batch_chunks, embs)
        file_total += added
        _log(
            f"    flush {added}청크 / 파일누적 {file_total} / "
            f"컬렉션 {store.count()} / mem={_mem_mb():.0f}MB"
        )
        batch_chunks.clear()
        del texts, embs

    for chunk in iter_pdf_chunks(doc):
        batch_chunks.append(chunk)
        if len(batch_chunks) >= STREAM_BATCH:
            _flush()
    _flush()  # 잔여

    del doc, batch_chunks
    collected = gc.collect()
    elapsed = time.time() - f_start
    _log(
        f"  ✓ 파일 완료: {file_total}청크 / {elapsed:.1f}s / "
        f"gc={collected}객체 / mem={_mem_mb():.0f}MB"
    )
    return file_total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", action="append", default=[],
                        help="화이트리스트 중 특정 파일명만 처리 (여러 번 사용 가능)")
    parser.add_argument("--limit", type=int, default=0,
                        help="화이트리스트 앞 N개만 처리 (0=전체)")
    parser.add_argument("--skip-verify", action="store_true",
                        help="검증 쿼리 생략")
    args = parser.parse_args()

    if not KEYS.voyage_api_key:
        _log("[ERROR] VOYAGE_API_KEY 누락")
        return 1

    pdf_dir = WINNING_PROPOSALS_DIR

    # 대상 결정
    if args.only:
        names = args.only
    else:
        names = list(WHITELIST)
    if args.limit > 0:
        names = names[: args.limit]

    targets: list[Path] = []
    for name in names:
        p = pdf_dir / name
        if not p.is_file():
            _log(f"[ERROR] PDF 누락: {p}")
            return 1
        targets.append(p)

    _log("=" * 70)
    _log(f"[B-1 SAFE] 인덱싱 시작 ({pdf_dir})")
    _log(f"  시작 메모리: {_mem_mb():.0f}MB")
    for t in targets:
        _log(f"  - {t.name}")
    _log("=" * 70)

    _log("ChromaDB 연결 중…")
    store = VectorStore(collection_name=VECTOR.collection_proposals)
    _log(f"  시작 시점 컬렉션 청크 수: {store.count()}")

    # voyage client 1회 생성, 재사용
    import voyageai
    voyage_client = voyageai.Client(api_key=KEYS.voyage_api_key)
    voyage_model = VECTOR.voyage_model

    t_global = time.time()
    per_file: dict[str, int] = {}

    for idx, pdf_path in enumerate(targets, start=1):
        _log("")
        _log("-" * 70)
        _log(f"[{idx}/{len(targets)}] {pdf_path.name}")
        _log("-" * 70)

        try:
            added = _ingest_one(pdf_path, store, voyage_client, voyage_model)
        except Exception as e:
            _log(f"  ✗ 실패: {e}")
            import traceback
            traceback.print_exc()
            return 1

        per_file[pdf_path.name] = added

        if idx < len(targets):
            _log(f"  ▷ {INTER_FILE_SLEEP_SEC:.0f}초 휴식 후 다음 파일…")
            time.sleep(INTER_FILE_SLEEP_SEC)

    elapsed = time.time() - t_global
    final_count = store.count()

    _log("")
    _log("=" * 70)
    _log(f"인덱싱 완료: 누적 {sum(per_file.values())}청크 (컬렉션 총 {final_count})")
    _log(f"총 소요: {elapsed:.1f}s / 종료 메모리: {_mem_mb():.0f}MB")
    for k, v in per_file.items():
        _log(f"  - {k}: {v}청크")

    if args.skip_verify:
        _log("[OK] --skip-verify, 검증 쿼리 생략")
        return 0

    _log("")
    _log("=" * 70)
    _log("검증 쿼리 (top-3 / 출처 + 유사도 + 미리보기)")
    _log("=" * 70)

    # 검증은 항상 WHITELIST 전체 기준 (--only로 일부만 ingest해도 누적 컬렉션 검증)
    accepted = set(WHITELIST)
    failed = False
    for q in VERIFY_QUERIES:
        _log(f"\n[Q] {q}")
        try:
            qvec = _embed_query_safe(voyage_client, voyage_model, q)
        except Exception as e:
            _log(f"  ! 쿼리 임베딩 실패: {e}")
            failed = True
            continue
        results = store.query(qvec, n_results=3)
        for i, r in enumerate(results, 1):
            src = (
                r["metadata"].get("source_file")
                or r["metadata"].get("filename")
                or r["metadata"].get("source")
                or "(unknown)"
            )
            score = r.get("score", 0.0)
            preview = (r.get("text") or r.get("document") or "")[:100].replace("\n", " ")
            _log(f"  {i}. score={score:.4f}  src={src}")
            _log(f"     ▸ {preview}")
            if src not in accepted:
                failed = True
                _log("     ⚠ 화이트리스트 외 파일 감지")

    _log("")
    _log("=" * 70)
    if failed:
        _log("[FAIL] 검증 쿼리에서 화이트리스트 외 출처 또는 오류 발생")
        return 1
    _log("[OK] 모든 검증 쿼리가 화이트리스트 안에서 결과 반환")
    return 0


if __name__ == "__main__":
    sys.exit(main())
