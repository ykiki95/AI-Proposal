"""
RFP 파일(PDF) 텍스트 추출기.

- PDF 페이지별 텍스트 + 표(table) 텍스트화 → 합쳐서 반환
- 핵심부 휴리스틱 추출:
    "사업 개요 / 추진 배경 / 추진 목적 / 사업 목적"
    "요구 사항 / 과업 내용 / 과업 범위 / 제안 요청 사항"
    "제안서 작성 목차 / 제안서 목차 / 제안서 작성 지침"
  → 시작 키워드 ~ 다음 챕터(또는 평가표/예시 키워드)까지 잘라낸다.
- 평가표, 예시 문서, 별첨 양식은 본문에서 잘라낸다.

다이어그램/이미지 내부 글자(스캔본)는 OCR을 별도로 적용하지 않는 한 추출되지 않는다.
표 내부 텍스트는 pdfplumber가 추출 가능한 범위에서 모두 텍스트로 변환한다.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Optional

import pdfplumber
from loguru import logger

# 본문 시작 키워드 (이걸 만나면 핵심부 시작)
_KEY_START_PATTERNS = [
    r"사업\s*개요",
    r"추진\s*배경",
    r"추진\s*목적",
    r"사업\s*목적",
    r"사업\s*추진\s*배경",
    r"제안\s*요청\s*사항",
    r"과업\s*내용",
    r"과업\s*범위",
    r"요구\s*사항",
    r"요구사항\s*상세",
    r"제안서\s*작성\s*목차",
    r"제안서\s*목차",
    r"제안서\s*작성\s*지침",
    r"제안서\s*구성",
]

# 본문 끝 키워드 (이걸 만나면 잘라낸다 — 평가표·예시·서식 영역)
_KEY_END_PATTERNS = [
    r"제안서\s*평가표",
    r"평가\s*기준",
    r"평가\s*항목",
    r"평가\s*위원",
    r"기술\s*평가\s*표",
    r"별첨\s*\d",
    r"별첨\s*양식",
    r"제안서\s*양식",
    r"서식\s*\d",
    r"입찰\s*공고문\s*양식",
    r"규격\s*서식",
    r"낙찰자\s*결정\s*방법",
    r"입찰\s*보증금",
    r"계약\s*조건",
    r"계약\s*특수\s*조건",
]


@dataclass
class RfpExtraction:
    full_text: str           # 전체 텍스트 (페이지·표 합본)
    core_text: str           # 핵심부만 (사업개요·요구사항·목차)
    page_count: int
    table_count: int
    truncated_from: Optional[str] = None  # 어떤 키워드에서 잘랐는지
    started_from: Optional[str] = None    # 어떤 키워드에서 시작했는지


def _table_to_lines(table: list[list]) -> list[str]:
    """pdfplumber 표를 마크다운 비슷한 줄로 변환."""
    lines = []
    for row in table:
        cells = [(c or "").strip().replace("\n", " ") for c in row]
        if not any(cells):
            continue
        lines.append(" | ".join(cells))
    return lines


def extract_pdf(file_bytes: bytes, max_chars: int = 200_000) -> RfpExtraction:
    """PDF 바이트 → 텍스트 + 표 합본 추출."""
    parts: list[str] = []
    table_count = 0
    page_count = 0
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            page_count = i
            try:
                text = page.extract_text() or ""
            except Exception as e:
                logger.warning(f"PDF p{i} 텍스트 추출 실패: {e}")
                text = ""
            if text.strip():
                parts.append(f"\n[page {i}]\n{text.strip()}")
            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []
            for ti, t in enumerate(tables, start=1):
                lines = _table_to_lines(t)
                if not lines:
                    continue
                table_count += 1
                parts.append(f"\n[표 p{i}-{ti}]\n" + "\n".join(lines))
            if sum(len(p) for p in parts) > max_chars:
                logger.warning(f"PDF 추출 {max_chars}자 초과 — 일부 페이지 생략 (p{i}/{len(pdf.pages)})")
                break
    full = "\n".join(parts).strip()
    core, started, truncated = _extract_core(full)
    return RfpExtraction(
        full_text=full,
        core_text=core,
        page_count=page_count,
        table_count=table_count,
        started_from=started,
        truncated_from=truncated,
    )


def _find_first(patterns: list[str], text: str) -> tuple[int, Optional[str]]:
    """가장 빠르게 매칭되는 패턴의 (위치, 패턴문자열). 없으면 (-1, None)."""
    best = -1
    best_pat = None
    for pat in patterns:
        m = re.search(pat, text)
        if m and (best == -1 or m.start() < best):
            best = m.start()
            best_pat = m.group(0)
    return best, best_pat


def _extract_core(full: str) -> tuple[str, Optional[str], Optional[str]]:
    """본문 시작·끝 키워드로 핵심부만 잘라낸다.
    핵심부가 너무 짧으면(<300자) 전체를 그대로 반환."""
    if not full:
        return "", None, None
    start_pos, start_pat = _find_first(_KEY_START_PATTERNS, full)
    if start_pos < 0:
        return full, None, None
    end_pos, end_pat = _find_first(_KEY_END_PATTERNS, full[start_pos:])
    if end_pos > 0:
        end_abs = start_pos + end_pos
        core = full[start_pos:end_abs].strip()
    else:
        core = full[start_pos:].strip()
        end_pat = None
    if len(core) < 300:
        return full, start_pat, None
    return core, start_pat, end_pat


def extract_text_from_upload(name: str, data: bytes) -> RfpExtraction:
    """파일명 확장자로 분기. 현재는 PDF만 정식 지원, 나머지는 명확한 안내."""
    lower = (name or "").lower()
    if lower.endswith(".pdf"):
        return extract_pdf(data)
    if lower.endswith(".txt"):
        text = data.decode("utf-8", errors="ignore")
        core, s, e = _extract_core(text)
        return RfpExtraction(
            full_text=text, core_text=core,
            page_count=1, table_count=0,
            started_from=s, truncated_from=e,
        )
    raise ValueError(
        f"지원하지 않는 파일 형식입니다: {name}\n"
        "현재는 PDF / TXT만 지원합니다. HWP는 한글에서 PDF로 저장 후 올려주세요."
    )
