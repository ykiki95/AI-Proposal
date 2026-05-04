"""
수주 제안서 인제스트 파이프라인.
PDF + PPTX → 처리 → 청킹 → 임베딩 → ChromaDB 저장

실행: python proposal_ai/rag/ingest.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger

try:
    from rich.console import Console
    from rich.table import Table
    from rich.progress import track
    _RICH = True
except ImportError:
    _RICH = False

from config.settings import (
    WINNING_PROPOSALS_DIR,
    WINNING_PROPOSALS_PPTX_DIR,
    VECTOR,
)
from rag.pdf_processor import PDFProcessor
from rag.pptx_processor import PPTXProcessor
from rag.chunker import chunk_pdf_doc, chunk_pptx_doc
from rag.embeddings import embed_documents
from rag.vector_store import VectorStore

console = Console() if _RICH else None


def _print(msg: str, style: str = "") -> None:
    if console:
        console.print(msg, style=style)
    else:
        print(msg)


def ingest_pdfs(
    pdf_dir: Path,
    store: VectorStore,
    batch_size: int,
    force: bool = False,
) -> dict[str, int]:
    """PDF 디렉토리의 모든 PDF 인제스트."""
    pdf_files = sorted(pdf_dir.glob("*.pdf"))
    if not pdf_files:
        _print(f"[yellow]PDF 파일 없음: {pdf_dir}[/yellow]")
        return {}

    _print(f"\n[bold cyan]▶ PDF 인제스트 ({len(pdf_files)}건)[/bold cyan]")
    processor = PDFProcessor()
    results: dict[str, int] = {}

    iter_files = track(pdf_files, description="PDF 처리 중...") if _RICH else pdf_files

    for pdf_path in iter_files:
        try:
            doc = processor.process(pdf_path)
            chunks = chunk_pdf_doc(doc)
            if not chunks:
                _print(f"  [yellow]청크 없음: {pdf_path.name}[/yellow]")
                continue

            texts = [c["text"] for c in chunks]
            embeddings = embed_documents(texts, batch_size=batch_size)
            added = store.add_chunks(chunks, embeddings)
            results[pdf_path.name] = added
            _print(f"  ✓ {pdf_path.name}: {len(chunks)}청크 → {added}건 저장")

        except Exception as e:
            logger.error(f"PDF 인제스트 실패 ({pdf_path.name}): {e}")
            _print(f"  [red]✗ {pdf_path.name}: {e}[/red]")

    return results


def ingest_pptx(
    pptx_dir: Path,
    store: VectorStore,
    batch_size: int,
) -> dict[str, dict]:
    """PPTX 디렉토리의 모든 PPTX 인제스트."""
    pptx_files = sorted(pptx_dir.glob("*.pptx"))
    if not pptx_files:
        _print(f"[yellow]PPTX 파일 없음: {pptx_dir}[/yellow]")
        return {}

    _print(f"\n[bold cyan]▶ PPTX 인제스트 ({len(pptx_files)}건)[/bold cyan]")
    processor = PPTXProcessor()
    results: dict[str, dict] = {}

    iter_files = track(pptx_files, description="PPTX 처리 중...") if _RICH else pptx_files

    for pptx_path in iter_files:
        try:
            doc = processor.process(pptx_path)
            layout = doc.get("layout_mode", "unknown")
            chunks = chunk_pptx_doc(doc)
            if not chunks:
                _print(f"  [yellow]청크 없음: {pptx_path.name}[/yellow]")
                continue

            texts = [c["text"] for c in chunks]
            embeddings = embed_documents(texts, batch_size=batch_size)
            added = store.add_chunks(chunks, embeddings)
            results[pptx_path.name] = {"layout": layout, "chunks": added}
            layout_label = "가로형" if layout == "horizontal" else "세로형"
            _print(
                f"  ✓ {pptx_path.name}: [{layout_label}] "
                f"{len(chunks)}슬라이드 → {added}건 저장"
            )

        except Exception as e:
            logger.error(f"PPTX 인제스트 실패 ({pptx_path.name}): {e}")
            _print(f"  [red]✗ {pptx_path.name}: {e}[/red]")

    return results


def print_layout_summary(pptx_results: dict[str, dict]) -> None:
    """가로형/세로형 분류 결과 출력."""
    horizontal = [(k, v) for k, v in pptx_results.items() if v.get("layout") == "horizontal"]
    vertical = [(k, v) for k, v in pptx_results.items() if v.get("layout") == "vertical"]

    _print("\n[bold green]=== 가로형/세로형 자동 분류 결과 ===[/bold green]")

    if _RICH and console:
        table = Table(title="PPTX 레이아웃 분류")
        table.add_column("파일명", style="cyan", no_wrap=False)
        table.add_column("분류", style="magenta")
        table.add_column("청크 수", justify="right")

        for fname, info in horizontal:
            table.add_row(fname, "가로형 (horizontal)", str(info["chunks"]))
        for fname, info in vertical:
            table.add_row(fname, "세로형 (vertical)", str(info["chunks"]))

        console.print(table)
    else:
        print(f"\n가로형 ({len(horizontal)}건):")
        for fname, info in horizontal:
            print(f"  - {fname} ({info['chunks']}청크)")
        print(f"\n세로형 ({len(vertical)}건):")
        for fname, info in vertical:
            print(f"  - {fname} ({info['chunks']}청크)")


def run_ingest(
    pdf_dir: Path | None = None,
    pptx_dir: Path | None = None,
    batch_size: int | None = None,
    force: bool = False,
) -> None:
    """메인 인제스트 실행."""
    pdf_dir = pdf_dir or WINNING_PROPOSALS_DIR
    pptx_dir = pptx_dir or WINNING_PROPOSALS_PPTX_DIR
    batch_size = batch_size or VECTOR.embedding_batch_size

    _print("[bold]=== 수주 제안서 RAG 인제스트 시작 ===[/bold]")
    _print(f"PDF 디렉토리: {pdf_dir}")
    _print(f"PPTX 디렉토리: {pptx_dir}")
    _print(f"임베딩 배치 크기: {batch_size}")

    store = VectorStore()

    pdf_results = ingest_pdfs(pdf_dir, store, batch_size, force)
    pptx_results = ingest_pptx(pptx_dir, store, batch_size)

    # 최종 통계
    stats = store.get_stats()
    _print("\n[bold green]=== 인제스트 완료 ===[/bold green]")
    _print(f"총 청크 수: {stats['total_chunks']}")
    _print(f"PDF 처리: {len(pdf_results)}건 ({sum(pdf_results.values())}청크)")
    _print(f"PPTX 처리: {len(pptx_results)}건 ({sum(v['chunks'] for v in pptx_results.values())}청크)")

    if pptx_results:
        print_layout_summary(pptx_results)

    _print("\n[bold]ChromaDB 저장 경로:[/bold]")
    from config.settings import CHROMA_DB_PATH
    _print(f"  {CHROMA_DB_PATH}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="제안서 RAG 인제스트")
    parser.add_argument("--force", action="store_true", help="기존 데이터 무시하고 재인제스트")
    parser.add_argument("--batch-size", type=int, default=None)
    args = parser.parse_args()

    run_ingest(force=args.force, batch_size=args.batch_size)
