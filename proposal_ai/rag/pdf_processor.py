"""
PDF 제안서 처리 모듈.
- pdfplumber로 텍스트 추출
- 챕터 단위로 분할 (한국어 제안서 패턴 인식)
- 메타데이터 추출 (제목, 페이지 수, 발주처 추정 등)
"""

from __future__ import annotations

import gc
import json
import re
from pathlib import Path
from typing import Any, Iterator

from loguru import logger

try:
    import pdfplumber
    _PDF_AVAILABLE = True
except ImportError:
    _PDF_AVAILABLE = False


# 매 N페이지마다 gc.collect() 강제 호출 (pdfplumber 누수 보강)
_GC_EVERY_N_PAGES = 10


def iter_pdf_pages(
    pdf_path: str | Path,
) -> Iterator[tuple[int, int, str, list]]:
    """PDF 페이지를 하나씩 yield하고, 페이지 처리 직후 page.flush_cache()로
    pdfplumber 내부 캐시(chars/lines/words/rects)를 즉시 해제한다.

    yields: (page_num, total_pages, text, tables)
    """
    if not _PDF_AVAILABLE:
        raise ImportError("pdfplumber 미설치. pip install pdfplumber")
    pdf_path = Path(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages, start=1):
            try:
                text = page.extract_text() or ""
                tables = page.extract_tables() or []
            except Exception as e:
                logger.warning(f"페이지 {i} 추출 실패 ({pdf_path.name}): {e}")
                text, tables = "", []
            yield i, total, text, tables
            # pdfplumber 페이지 내부 캐시 명시 해제 (메모리 누수 방지 핵심)
            try:
                page.flush_cache()
            except Exception:
                pass
            # pdfplumber 0.10+ : 페이지 객체 자체도 닫기
            try:
                page.close()
            except Exception:
                pass
            if i % _GC_EVERY_N_PAGES == 0:
                gc.collect()

# 챕터 헤딩 감지 패턴 (순서대로 매칭 시도)
_CHAPTER_PATTERNS = [
    re.compile(r"^제\s*(\d{1,2})\s*장\s+(.{2,80})", re.UNICODE),       # 제 1 장 서론
    re.compile(r"^제\s*(\d{1,2})\s*편\s+(.{2,80})", re.UNICODE),       # 제 1 편
    re.compile(r"^(Ⅰ|Ⅱ|Ⅲ|Ⅳ|Ⅴ|Ⅵ|Ⅶ|Ⅷ|Ⅸ|Ⅹ)\.\s+(.{2,80})"),           # Ⅰ. 서론
    re.compile(r"^(\d{1,2})장\s+(.{2,80})", re.UNICODE),                # 1장 서론
    re.compile(r"^Chapter\s+(\d{1,2})[:\.\s]+(.{2,80})", re.IGNORECASE),# Chapter 1
    re.compile(r"^\[(\d{1,2})\]\s+(.{2,80})", re.UNICODE),             # [1] 개요
    # 독립 줄에 숫자.제목 (하위 번호는 제외: 1.1, 1.2 등)
    re.compile(r"^(\d{1,2})\.\s+([가-힣A-Za-z].{1,60})", re.UNICODE),  # 1. 추진전략
]

# 발주처 추정 패턴
_ORDERER_PATTERNS = [
    re.compile(r"발\s*주\s*처\s*[:\：]\s*([가-힣(주)]{2,20})", re.UNICODE),
    re.compile(r"([가-힣]{2,10}(?:청|처|원|부|시|군|구|도|공단|재단법인|시설관리공단))", re.UNICODE),
    re.compile(r"([가-힣]{2,10}(?:공사|연구원|협회|진흥원|센터))", re.UNICODE),
]

# 사업 유형 키워드 매핑
_PROJECT_TYPE_MAP = [
    ("홈페이지", "웹사이트/홈페이지 구축"),
    ("포털", "포털 시스템 구축"),
    ("유지보수", "유지보수 서비스"),
    ("재개발", "사이트 재개발"),
    ("고도화", "시스템 고도화"),
    ("구축", "시스템 구축"),
    ("개발", "소프트웨어 개발"),
    ("운영", "운영 서비스"),
    ("플랫폼", "플랫폼 개발"),
    ("앱", "앱 개발"),
    ("모바일", "모바일 앱 개발"),
]


def _detect_chapter(line: str) -> tuple[int, str] | None:
    """줄이 챕터 제목인지 감지. (level, title) 또는 None 반환."""
    stripped = line.strip()
    if not stripped or len(stripped) > 100:
        return None
    # 숫자만 있는 줄 제외 (페이지 번호)
    if re.match(r"^\d+$", stripped):
        return None
    for i, pat in enumerate(_CHAPTER_PATTERNS):
        if pat.match(stripped):
            # 첫 3개 패턴은 level 1, 나머지 level 2
            level = 1 if i < 4 else 2
            return (level, stripped)
    return None


def _estimate_title(pages_text: list[str]) -> str:
    """첫 3페이지에서 제안서 제목 추정."""
    keywords = ["제안서", "기술제안서", "정성제안서", "제안", "용역", "사업", "구축", "개발"]
    for page_text in pages_text[:3]:
        lines = [l.strip() for l in page_text.split("\n") if l.strip()]
        for line in lines[:25]:
            if 10 < len(line) < 120 and any(kw in line for kw in keywords):
                return line
    return "제목 미상"


def _estimate_orderer(pages_text: list[str]) -> str:
    """첫 5페이지에서 발주처 추정."""
    for page_text in pages_text[:5]:
        for pat in _ORDERER_PATTERNS:
            m = pat.search(page_text)
            if m:
                candidate = m.group(1).strip()
                if len(candidate) >= 3:
                    return candidate
    return "미상"


def _estimate_project_type(text_sample: str) -> str:
    """사업 유형 추정 (전체 텍스트 앞 5000자 기준)."""
    sample = text_sample[:5000]
    for keyword, label in _PROJECT_TYPE_MAP:
        if keyword in sample:
            return label
    return "기타"


class PDFProcessor:
    """PDF 제안서를 파싱하여 챕터 단위 JSON으로 변환."""

    def __init__(self, processed_dir: Path | None = None):
        if not _PDF_AVAILABLE:
            raise ImportError("pdfplumber 미설치. pip install pdfplumber")
        if processed_dir is None:
            from config.settings import PROCESSED_DIR
            processed_dir = PROCESSED_DIR
        self.processed_dir = Path(processed_dir)
        self.processed_dir.mkdir(parents=True, exist_ok=True)

    def process(self, pdf_path: str | Path) -> dict[str, Any]:
        """PDF 파일 처리 → 구조화된 dict 반환 + JSON 저장.

        페이지는 iter_pdf_pages로 스트리밍 추출 (page.flush_cache + 주기적 gc).
        챕터 분할 직후 pages_text/pages_tables를 즉시 해제하여 메모리 폭증 방지.
        """
        pdf_path = Path(pdf_path)
        logger.info(f"PDF 처리 시작: {pdf_path.name}")

        pages_text: list[str] = []
        pages_tables: list[list] = []
        total_pages = 0

        try:
            for page_num, total, text, tables in iter_pdf_pages(pdf_path):
                total_pages = total
                pages_text.append(text)
                pages_tables.append(tables)
        except Exception as e:
            logger.error(f"PDF 열기 실패 ({pdf_path.name}): {e}")
            raise

        full_text = "\n".join(pages_text)
        chapters = self._split_chapters(pages_text, pages_tables)

        result: dict[str, Any] = {
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

        # 큰 페이지 list는 결과에 raw_text/chapters로 압축 보관됨 → 즉시 해제
        del pages_text, pages_tables
        gc.collect()

        out_path = self.processed_dir / f"{pdf_path.stem}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        logger.info(
            f"PDF 완료: {pdf_path.name} "
            f"({total_pages}페이지, {len(chapters)}개 챕터 → {out_path.name})"
        )
        return result

    def _split_chapters(
        self,
        pages_text: list[str],
        pages_tables: list[list],
    ) -> list[dict[str, Any]]:
        """챕터 단위 텍스트 분할."""
        # (page_num, line) 전체 라인 생성
        all_lines: list[tuple[int, str]] = []
        for page_idx, page_text in enumerate(pages_text):
            for line in page_text.split("\n"):
                all_lines.append((page_idx + 1, line))

        chapters: list[dict[str, Any]] = []
        current_title: str | None = None
        current_content: list[str] = []
        current_start = 1
        chapter_idx = 0

        for page_num, line in all_lines:
            detected = _detect_chapter(line)
            if detected:
                _level, title = detected
                # 이전 챕터 저장
                if current_title is not None or current_content:
                    chapters.append(self._make_chapter(
                        chapter_idx, current_title or "머리말",
                        current_start, page_num - 1,
                        current_content, pages_tables,
                    ))
                    chapter_idx += 1
                current_title = title
                current_content = []
                current_start = page_num
            else:
                stripped = line.strip()
                if stripped:
                    current_content.append(stripped)

        # 마지막 챕터
        if current_title is not None or current_content:
            chapters.append(self._make_chapter(
                chapter_idx, current_title or "전체 본문",
                current_start, len(pages_text),
                current_content, pages_tables,
            ))

        # 챕터가 없으면 전체를 단일 챕터로
        if not chapters:
            chapters = [{
                "chapter_id": "ch_00",
                "title": "전체 본문",
                "level": 1,
                "page_start": 1,
                "page_end": len(pages_text),
                "content": "\n".join(p for p in pages_text),
                "tables": [],
            }]

        return chapters

    @staticmethod
    def _make_chapter(
        idx: int,
        title: str,
        page_start: int,
        page_end: int,
        content_lines: list[str],
        pages_tables: list[list],
    ) -> dict[str, Any]:
        """챕터 dict 생성."""
        tables_in_range: list = []
        for p_idx in range(page_start - 1, min(page_end, len(pages_tables))):
            for table in pages_tables[p_idx]:
                if table:
                    rows = [
                        " | ".join(str(c) if c else "" for c in row)
                        for row in table if row
                    ]
                    tables_in_range.append(rows)

        return {
            "chapter_id": f"ch_{idx:02d}",
            "title": title,
            "level": 1,
            "page_start": page_start,
            "page_end": max(page_end, page_start),
            "content": "\n".join(content_lines),
            "tables": tables_in_range,
        }


def process_pdf(pdf_path: str | Path) -> dict[str, Any] | None:
    """단일 PDF 처리 진입점."""
    try:
        return PDFProcessor().process(pdf_path)
    except Exception as e:
        logger.error(f"PDF 처리 실패 ({Path(pdf_path).name}): {e}")
        return None
