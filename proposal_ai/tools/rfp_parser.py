"""
RFP 파서 — DiscoveryAgent용 단건 진입점.

`rag.pdf_processor.PDFProcessor`(코퍼스 인제스트용 저수준 파서)를 감싸서
"공고 한 건의 첨부 PDF"를 빠르게 텍스트/구조화 결과로 변환한다.

DiscoveryAgent는 이 모듈만 호출하고, 결과를 BidNotice.rfp_full_text 및
RFP 벡터 인덱싱(rfp_documents 컬렉션)에 사용한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger


def parse_rfp(source: str | Path) -> Optional[str]:
    """단일 RFP 파일에서 전체 텍스트를 추출.

    Args:
        source: 로컬 PDF 경로. (URL 다운로드는 별도 단계에서 수행 — agency_crawler가 첨부를 받아 둠)

    Returns:
        RFP 본문 텍스트. 파싱 실패 시 None.
    """
    result = parse_rfp_full(source)
    return result["raw_text"] if result else None


def parse_rfp_full(source: str | Path) -> Optional[Dict[str, Any]]:
    """단일 RFP 파일의 전체 구조화 결과(텍스트 + 챕터 + 메타 + 표) 반환.

    내부적으로 rag.pdf_processor.PDFProcessor를 호출한다.
    Returns:
        {
          "filename": str,
          "metadata": {"title", "total_pages", ...},
          "chapters": [...],
          "raw_text": str,
        }
        파싱 실패 시 None.
    """
    src = Path(source)
    if not src.exists():
        logger.warning(f"RFP 파일 없음: {src}")
        return None

    try:
        from rag.pdf_processor import PDFProcessor
    except ImportError as e:
        logger.error(f"rag.pdf_processor 로드 실패: {e}")
        return None

    try:
        return PDFProcessor().process(src)
    except Exception as e:
        logger.error(f"RFP 파싱 실패 ({src.name}): {e}")
        return None
