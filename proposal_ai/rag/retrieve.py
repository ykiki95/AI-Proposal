"""
RAG 검색 모듈.
- 자연어 쿼리 → 벡터 검색 → 관련 청크 반환
- 레이아웃 필터, 문서 유형 필터 지원
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from rag.embeddings import embed_query
from rag.vector_store import VectorStore

_store: VectorStore | None = None


def _get_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore()
    return _store


def retrieve(
    query: str,
    n_results: int = 5,
    doc_type: str | None = None,
    layout_mode: str | None = None,
) -> list[dict[str, Any]]:
    """
    자연어 쿼리로 관련 청크 검색.

    Args:
        query: 검색 쿼리 (한국어/영어)
        n_results: 반환할 최대 결과 수
        doc_type: "pdf" 또는 "pptx" (None이면 전체)
        layout_mode: "horizontal" 또는 "vertical" (PPTX 전용)

    Returns:
        [{"text": str, "metadata": dict, "score": float}, ...]
    """
    store = _get_store()

    if store.count() == 0:
        logger.warning("벡터 스토어가 비어 있습니다. ingest.py를 먼저 실행하세요.")
        return []

    query_emb = embed_query(query)

    # 메타데이터 필터 구성
    where: dict | None = None
    conditions = []
    if doc_type:
        conditions.append({"doc_type": {"$eq": doc_type}})
    if layout_mode:
        conditions.append({"layout_mode": {"$eq": layout_mode}})

    if len(conditions) == 1:
        where = conditions[0]
    elif len(conditions) > 1:
        where = {"$and": conditions}

    results = store.query(query_emb, n_results=n_results, where=where)
    logger.debug(f"검색 완료: '{query[:50]}' → {len(results)}건")
    return results


def retrieve_for_section(
    section_title: str,
    rfp_context: str = "",
    n_results: int = 8,
) -> list[dict[str, Any]]:
    """
    제안서 섹션 작성을 위한 참고 내용 검색.
    섹션 제목 + RFP 맥락을 조합한 쿼리 사용.
    """
    query_parts = [section_title]
    if rfp_context:
        query_parts.append(rfp_context[:300])
    query = " ".join(query_parts)
    return retrieve(query, n_results=n_results)


def format_context(results: list[dict[str, Any]], max_chars: int = 4000) -> str:
    """검색 결과를 LLM 프롬프트용 컨텍스트 문자열로 변환."""
    if not results:
        return ""

    parts: list[str] = []
    total_chars = 0

    for i, r in enumerate(results, 1):
        meta = r.get("metadata", {})
        source = meta.get("source_file", "")
        score = r.get("score", 0)
        text = r.get("text", "")

        header = f"[참고{i}] {source} (유사도: {score:.2f})"
        block = f"{header}\n{text}"

        if total_chars + len(block) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 200:
                parts.append(block[:remaining] + "...")
            break

        parts.append(block)
        total_chars += len(block)

    return "\n\n---\n\n".join(parts)
