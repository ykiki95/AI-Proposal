"""
RFP 전용 스키마.
StrategyAgent가 RFP 원문을 구조화하고 수용표를 만들 때 사용하는 데이터 계약.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class RequirementCategory(str, Enum):
    """RFP 요구사항 분류."""
    FUNCTIONAL = "기능요구사항"
    TECHNICAL = "기술요구사항"
    MANAGEMENT = "관리요구사항"
    QUALITY = "품질요구사항"
    SECURITY = "보안요구사항"
    INTERFACE = "인터페이스요구사항"
    CONSTRAINT = "제약사항"
    EVALUATION = "평가기준"


class AcceptanceStatus(str, Enum):
    FULL = "완전수용"
    PARTIAL = "부분수용"
    ALTERNATIVE = "대안제시"
    REJECTED = "미수용"


class RfpRequirement(BaseModel):
    """RFP 단일 요구사항."""
    req_id: str = Field(..., description="예: REQ-001")
    category: RequirementCategory
    section: str = Field(..., description="RFP 원문 섹션 번호/제목")
    description: str = Field(..., description="요구사항 원문")
    mandatory: bool = True
    keyword_tags: List[str] = Field(default_factory=list)


class AcceptanceItem(BaseModel):
    """수용표 한 행."""
    req_id: str
    requirement_summary: str
    acceptance_status: AcceptanceStatus = AcceptanceStatus.FULL
    proposal_section: str = Field(..., description="제안서 내 대응 섹션 제목")
    proposal_page_hint: Optional[str] = None
    response_summary: str = ""
    reviewer_note: str = ""


class AcceptanceTable(BaseModel):
    """전체 수용표. ReviewerAgent가 완성하고 GraphicsAgent가 슬라이드로 변환."""
    bid_id: str
    version: int = 1
    items: List[AcceptanceItem] = Field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def full_count(self) -> int:
        return sum(1 for i in self.items if i.acceptance_status == AcceptanceStatus.FULL)

    @property
    def rejected_count(self) -> int:
        return sum(1 for i in self.items if i.acceptance_status == AcceptanceStatus.REJECTED)

    @property
    def is_valid(self) -> bool:
        """공공 제안서 제출 기준: 미수용 0건."""
        return self.rejected_count == 0 and self.total > 0


class RfpTocChapter(BaseModel):
    """RFP가 제안서에 요청한 목차 한 챕터."""
    chapter_no: str
    title: str
    sub_chapters: List["RfpTocChapter"] = Field(default_factory=list)
    evaluation_weight: Optional[float] = None
    required_pages: Optional[int] = None
    specialty: Optional[str] = Field(
        None,
        description="WriterAgent가 매핑할 specialist key (business/solution/methodology/...). StrategyAgent가 RFP 분석 시 채움."
    )


RfpTocChapter.model_rebuild()


class RfpStructured(BaseModel):
    """
    StrategyAgent가 RFP 원문 → 구조화한 결과.
    WriterAgent의 입력으로 사용된다.
    """
    bid_id: str
    title: str
    agency: str

    # RFP가 요청하는 목차 (목차 일치율 계산 기준)
    requested_toc: List[RfpTocChapter] = Field(default_factory=list)

    # 추출된 요구사항 전체
    requirements: List[RfpRequirement] = Field(default_factory=list)

    # 평가 기준 (배점표)
    evaluation_criteria: List[dict] = Field(default_factory=list)

    # 전략 메모 (StrategyAgent 산출)
    differentiators: List[str] = Field(default_factory=list)
    win_themes: List[str] = Field(default_factory=list)
    risk_notes: List[str] = Field(default_factory=list)
    competitor_hints: List[str] = Field(default_factory=list)

    # RFP 메타
    layout_hint: Literal["가로형", "세로형"] = "세로형"
    target_pages: int = 90


class TocSimilarity(BaseModel):
    """제안서 목차 ↔ RFP 요청 목차 유사도 검사 결과."""
    bid_id: str
    similarity_score: float = Field(..., ge=0.0, le=1.0)
    matched_chapters: List[dict] = Field(default_factory=list)
    unmatched_rfp_chapters: List[str] = Field(default_factory=list)
    unmatched_proposal_chapters: List[str] = Field(default_factory=list)
    is_passing: bool = False

    @classmethod
    def passing_threshold(cls) -> float:
        return 0.90
