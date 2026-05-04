"""
텍스트 청킹 모듈.
- PDF 챕터 → 오버랩 있는 고정 크기 청크 (제너레이터 스트리밍)
- PPTX 슬라이드 → 슬라이드 단위 청크
- 각 청크에 풍부한 메타데이터 부착

메모리 안전:
  - iter_split_text / iter_pdf_chunks 는 청크를 yield (list 누적 X)
  - 챕터별 full_text/tables_text 사용 직후 del
  - tables_text 는 += 누적이 아닌 join 1회 (O(N²) 방지)
  - _split_text 무한루프 가드 (최소 1자 전진)
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterator


def _make_chunk_id(doc_id: str, idx: int, text: str) -> str:
    """청크 고유 ID 생성."""
    h = hashlib.md5(f"{doc_id}_{idx}_{text[:50]}".encode()).hexdigest()[:8]
    return f"{doc_id}_c{idx:04d}_{h}"


def iter_split_text(
    text: str, max_chars: int = 1200, overlap: int = 200
) -> Iterator[str]:
    """텍스트를 max_chars 단위로 overlap 포함 분할 (제너레이터)."""
    if not text or not text.strip():
        return
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    if overlap < 0:
        overlap = 0
    if overlap >= max_chars:
        overlap = max_chars // 4  # 안전 가드: 진행 보장
    text = text.strip()
    n = len(text)
    if n <= max_chars:
        yield text
        return
    start = 0
    while start < n:
        end = min(start + max_chars, n)
        is_last = end == n
        chunk = text[start:end]
        # 문장 경계에서 자르기 시도 (마지막 청크는 컷하지 않음)
        if not is_last:
            for sep in ("다.\n", "다. ", ".\n", ". ", "\n\n", "\n"):
                idx = chunk.rfind(sep)
                if idx > max_chars // 2:
                    chunk = chunk[: idx + len(sep)]
                    break
        stripped = chunk.strip()
        if stripped:
            yield stripped
        # 끝 도달 시 즉시 종료 (마지막 chunk를 overlap만큼 다시 yield하지 않음)
        if is_last:
            return
        # chunk가 overlap 이하면 max_chars - overlap만큼 강제 전진 (무한 루프 + 잔여 1자 진행 방지)
        advance = len(chunk) - overlap
        if advance <= 0:
            advance = max_chars - overlap
        start += advance


def iter_pdf_chunks(
    doc: dict[str, Any],
    max_chars: int = 1200,
    overlap: int = 200,
) -> Iterator[dict[str, Any]]:
    """PDF 처리 결과 dict → 청크를 하나씩 yield (메모리 스트리밍)."""
    filename = doc.get("filename", "unknown")
    doc_id = filename.replace(".pdf", "").replace(" ", "_")[:40]
    meta_base = doc.get("metadata", {})

    idx = 0
    has_chapter = False

    for chapter in doc.get("chapters", []):
        has_chapter = True
        content = chapter.get("content", "") or ""

        # 표 누적: O(N²) str+= 회피 → 한 번에 join
        tables_rows: list[str] = []
        for table_rows in chapter.get("tables", []):
            tables_rows.extend(table_rows)
        tables_text = "\n".join(tables_rows)
        del tables_rows

        if tables_text.strip():
            full_text = content + "\n\n[표]\n" + tables_text
        else:
            full_text = content
        # 사용 후 즉시 해제
        del content, tables_text

        chapter_meta = {
            "source_file": filename,
            "doc_id": doc_id,
            "doc_type": "pdf",
            "chapter_id": chapter.get("chapter_id", ""),
            "chapter_title": chapter.get("title", ""),
            "page_start": chapter.get("page_start", 0),
            "page_end": chapter.get("page_end", 0),
            "title": meta_base.get("title", ""),
            "orderer": meta_base.get("estimated_orderer", ""),
            "project_type": meta_base.get("estimated_project_type", ""),
        }

        for part_idx, chunk_text in enumerate(
            iter_split_text(full_text, max_chars, overlap)
        ):
            chunk_id = _make_chunk_id(doc_id, idx, chunk_text)
            meta = dict(chapter_meta)
            meta["part_index"] = part_idx
            yield {
                "id": chunk_id,
                "text": chunk_text,
                "metadata": meta,
            }
            idx += 1

        # 챕터 끝나면 큰 텍스트 즉시 해제
        del full_text

    # 챕터가 없으면 raw_text 전체 청킹
    if not has_chapter:
        raw = doc.get("raw_text", "") or ""
        for chunk_text in iter_split_text(raw, max_chars, overlap):
            chunk_id = _make_chunk_id(doc_id, idx, chunk_text)
            yield {
                "id": chunk_id,
                "text": chunk_text,
                "metadata": {
                    "source_file": filename,
                    "doc_id": doc_id,
                    "doc_type": "pdf",
                    "chapter_id": "raw",
                    "chapter_title": "전체",
                    "page_start": 1,
                    "page_end": meta_base.get("total_pages", 0),
                    "part_index": idx,
                    "title": meta_base.get("title", ""),
                    "orderer": meta_base.get("estimated_orderer", ""),
                    "project_type": meta_base.get("estimated_project_type", ""),
                },
            }
            idx += 1


# ─── Backward-compatible wrappers ─────────────────────────────────────────
def _split_text(text: str, max_chars: int = 1200, overlap: int = 200) -> list[str]:
    """기존 호출자 호환용. 새 코드는 iter_split_text 사용 권장."""
    return list(iter_split_text(text, max_chars, overlap))


def chunk_pdf_doc(
    doc: dict[str, Any],
    max_chars: int = 1200,
    overlap: int = 200,
) -> list[dict[str, Any]]:
    """기존 호출자 호환용. 새 코드는 iter_pdf_chunks 사용 권장."""
    return list(iter_pdf_chunks(doc, max_chars, overlap))


def chunk_pptx_doc(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """PPTX 처리 결과 dict → 슬라이드 단위 청크 리스트."""
    filename = doc.get("filename", "unknown")
    doc_id = filename.replace(".pptx", "").replace(" ", "_")[:40]
    layout_mode = doc.get("layout_mode", "unknown")

    chunks: list[dict[str, Any]] = []

    for slide in doc.get("slides", []):
        slide_num = slide.get("slide_number", 0)
        title = slide.get("title", "") or ""
        texts = slide.get("texts", []) or []
        tables = slide.get("tables", []) or []

        # 슬라이드 텍스트 구성
        parts = []
        if title:
            parts.append(f"[제목] {title}")
        if texts:
            parts.extend(texts)
        for table in tables:
            for row in table:
                parts.append(" | ".join(row))

        text = "\n".join(parts).strip()
        if not text:
            continue

        chunk_id = _make_chunk_id(doc_id, slide_num, text)
        chunks.append({
            "id": chunk_id,
            "text": text,
            "metadata": {
                "source_file": filename,
                "doc_id": doc_id,
                "doc_type": "pptx",
                "layout_mode": layout_mode,
                "slide_number": slide_num,
                "slide_title": title,
                "pattern": slide.get("pattern", "content"),
            },
        })

    return chunks
