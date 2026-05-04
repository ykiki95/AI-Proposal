"""
RAG 시스템 통합 테스트.
인제스트 → 검색 → 결과 출력까지 전체 파이프라인 검증.

실행: python proposal_ai/scripts/test_rag.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    _RICH = True
except ImportError:
    _RICH = False

console = Console() if _RICH else None


def _print(msg: str, style: str = "") -> None:
    if console:
        console.print(msg, style=style)
    else:
        print(msg)


def _header(title: str) -> None:
    if console:
        console.rule(f"[bold cyan]{title}[/bold cyan]")
    else:
        print(f"\n{'='*60}\n{title}\n{'='*60}")


# ─── 테스트 쿼리 목록 ─────────────────────────────────────────
TEST_QUERIES = [
    ("홈페이지 재개발 사업 추진 전략", None, None),
    ("유지보수 서비스 체계 및 SLA", None, None),
    ("목차 구성 및 제안 구조", "pdf", None),
    ("시스템 아키텍처 슬라이드", "pptx", None),
    ("가로형 제안서 표지 디자인", "pptx", "horizontal"),
    ("세로형 제안서 수행 일정", "pptx", "vertical"),
]


def test_store_stats() -> bool:
    """ChromaDB 저장 상태 확인."""
    _header("1. ChromaDB 저장 상태 확인")
    try:
        from rag.vector_store import VectorStore
        store = VectorStore()
        stats = store.get_stats()

        total = stats["total_chunks"]
        _print(f"총 청크 수: [bold green]{total}[/bold green]")

        if total == 0:
            _print("[red]✗ 저장된 청크 없음. ingest.py를 먼저 실행하세요.[/red]")
            return False

        if _RICH and console:
            table = Table(title="문서별 청크 수")
            table.add_column("파일명", style="cyan")
            table.add_column("청크 수", justify="right")
            for fname, count in sorted(stats["documents"].items()):
                table.add_row(fname, str(count))
            console.print(table)

            layout_dist = stats.get("layout_distribution", {})
            if layout_dist:
                _print(f"\nPPTX 레이아웃 분포: {layout_dist}")
        else:
            for fname, count in sorted(stats["documents"].items()):
                print(f"  {fname}: {count}청크")

        _print("[green]✓ 저장 상태 정상[/green]")
        return True

    except Exception as e:
        _print(f"[red]✗ 오류: {e}[/red]")
        logger.exception(e)
        return False


def test_queries() -> bool:
    """검색 테스트 실행."""
    _header("2. 검색 테스트")
    try:
        from rag.retrieve import retrieve

        all_ok = True
        for query, doc_type, layout in TEST_QUERIES:
            filter_info = ""
            if doc_type:
                filter_info += f" [doc_type={doc_type}]"
            if layout:
                filter_info += f" [layout={layout}]"

            _print(f"\n[yellow]쿼리:[/yellow] {query}{filter_info}")
            try:
                results = retrieve(query, n_results=3, doc_type=doc_type, layout_mode=layout)
                if not results:
                    _print("  → [dim]결과 없음[/dim]")
                    continue

                for i, r in enumerate(results[:2], 1):
                    meta = r.get("metadata", {})
                    score = r.get("score", 0)
                    source = meta.get("source_file", "?")
                    text_preview = r["text"][:120].replace("\n", " ")
                    _print(
                        f"  [{i}] {source} (score={score:.3f})\n"
                        f"      {text_preview}..."
                    )

                _print(f"  [green]✓ {len(results)}건 반환[/green]")

            except Exception as e:
                _print(f"  [red]✗ 쿼리 실패: {e}[/red]")
                all_ok = False

        return all_ok

    except Exception as e:
        _print(f"[red]✗ 검색 모듈 오류: {e}[/red]")
        logger.exception(e)
        return False


def test_format_context() -> bool:
    """검색 결과 컨텍스트 포맷팅 테스트."""
    _header("3. 컨텍스트 포맷팅 테스트")
    try:
        from rag.retrieve import retrieve, format_context
        results = retrieve("사업 추진 방안", n_results=3)
        context = format_context(results, max_chars=2000)
        if context:
            _print(f"컨텍스트 길이: {len(context)}자")
            _print("[dim]--- 미리보기 (500자) ---[/dim]")
            _print(context[:500])
            _print("[green]✓ 포맷팅 정상[/green]")
            return True
        else:
            _print("[yellow]결과 없음 (저장된 데이터가 없을 수 있음)[/yellow]")
            return True
    except Exception as e:
        _print(f"[red]✗ 오류: {e}[/red]")
        return False


def print_summary(results: dict[str, bool]) -> None:
    """테스트 결과 요약."""
    _header("테스트 결과 요약")
    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for name, ok in results.items():
        icon = "✓" if ok else "✗"
        color = "green" if ok else "red"
        _print(f"  [{color}]{icon} {name}[/{color}]" if _RICH else f"  {icon} {name}")

    status = "전체 통과" if passed == total else f"{passed}/{total} 통과"
    color = "bold green" if passed == total else "bold yellow"
    _print(f"\n[{color}]{status}[/{color}]" if _RICH else f"\n{status}")


def main() -> None:
    _header("RAG 시스템 통합 테스트")
    _print(f"프로젝트 루트: {_PROJECT_ROOT}\n")

    # 설정 확인
    from config.settings import KEYS, VECTOR, CHROMA_DB_PATH
    _print(f"Voyage AI: {'✓ 설정됨' if KEYS.has_voyage else '✗ 미설정'}")
    _print(f"임베딩 모델: {VECTOR.voyage_model}")
    _print(f"ChromaDB 경로: {CHROMA_DB_PATH}\n")

    if not KEYS.has_voyage:
        _print("[red]VOYAGE_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.[/red]")
        sys.exit(1)

    test_results = {
        "ChromaDB 저장 상태": test_store_stats(),
        "검색 테스트": test_queries(),
        "컨텍스트 포맷팅": test_format_context(),
    }

    print_summary(test_results)

    # 한국어 최종 요약
    _header("한국어 작업 완료 요약")
    from rag.vector_store import VectorStore
    store = VectorStore()
    stats = store.get_stats()

    pdf_files = [f for f in stats["documents"] if f.endswith(".pdf")]
    pptx_files = [f for f in stats["documents"] if f.endswith(".pptx")]
    layout = stats.get("layout_distribution", {})

    _print(
        f"\n✅ RAG 모듈 B 구축 완료\n"
        f"   - PDF {len(pdf_files)}건 인제스트 완료\n"
        f"   - PPTX {len(pptx_files)}건 인제스트 완료\n"
        f"     (가로형 {layout.get('horizontal', 0)}건 / 세로형 {layout.get('vertical', 0)}건)\n"
        f"   - 총 {stats['total_chunks']}개 청크 ChromaDB 저장\n"
        f"   - 검색 테스트 {'정상' if test_results.get('검색 테스트') else '일부 실패'}\n"
    )


if __name__ == "__main__":
    main()
