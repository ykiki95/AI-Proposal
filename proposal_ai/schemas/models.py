"""
Pydantic 데이터 스키마.
에이전트 간 주고받는 모든 데이터의 계약을 정의한다.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class BidStatus(str, Enum):
    """공고 상태 머신 - human gate 전환 추적용."""
    COLLECTED = "수집완료"
    EVALUATED = "평가완료"
    AWAITING_APPROVAL = "승인대기"
    APPROVED = "참여확정"
    DRAFT_DONE = "초안완료"
    UNDER_REVIEW = "검토중"
    FINAL_APPROVED = "최종승인"
    DONE = "완료"
    REJECTED = "비권장"


class BidNotice(BaseModel):
    """김탐정이 수집한 공고 단위."""
    bid_id: str = Field(..., description="공고번호 (중복 제거 키)")
    source: Literal["G2B", "지자체", "민간"] = "G2B"
    agency: str = Field(..., description="발주기관")
    title: str = Field(..., description="사업명")
    budget_krw: Optional[int] = Field(None, description="예산 (원)")
    duration_months: Optional[int] = Field(None, description="사업기간 (월)")
    qualifications: List[str] = Field(default_factory=list)
    deadline: datetime
    rfp_summary: str = ""
    rfp_url: str = ""
    rfp_full_text: Optional[str] = None
    collected_at: datetime = Field(default_factory=datetime.now)


class BidEvaluation(BaseModel):
    """이판단의 적격성 스코어링 결과."""
    bid_id: str
    fit_score: int = Field(..., ge=0, le=100)
    score_breakdown: Dict[str, int] = Field(default_factory=dict)
    recommendation: Literal["참여권장", "조건부", "비권장"]
    rationale: str
    risk_factors: List[str] = Field(default_factory=list)
    opportunity_factors: List[str] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=datetime.now)


class ProposalSection(BaseModel):
    """제안서 섹션 단위."""
    title: str
    body: str
    order: int = 0
    specialty: Optional[str] = None
    owner_name: Optional[str] = None
    # JSON 슬라이드 스펙 (SlideSpec 호환 dict 리스트). PPTX 빌더가 이걸 우선 사용.
    slides_json: List[Dict] = Field(default_factory=list)


class SlideSpec(BaseModel):
    """단일 슬라이드 스펙 — 정디자/PPTX 빌더가 사용.
    layout 종류:
      - cover, toc, section_divider, closing
      - title_bullets         : 제목 + 불릿 5~7개
      - two_column_compare    : 좌(AS-IS)/우(TO-BE) 2단 텍스트 비교
      - diagram_layered       : 계층형 박스 다이어그램
      - metric_cards          : 큰 정량 카드 1~4개
      - table                 : 일반 표
      - blank_placeholder     : 빈 페이지 (사람이 채움)
      - as_is_to_be_compare   : AS-IS/TO-BE 도형 (붉은 박스 → 화살표 → 파란 박스)
      - system_architecture   : 시스템 구성도 (레이어형 박스 + 외부 연계)
      - process_flow          : 단계별 프로세스 (가로 박스 + 화살표)
      - screen_mockup         : 화면 mockup 1장 (PC 또는 모바일 프레임 + 영역 박스)
      - screen_mockup_grid    : 화면 mockup 4~6장 그리드
    """
    layout: str
    title: str
    subtitle: Optional[str] = None
    bullets: List[str] = Field(default_factory=list)
    left: Optional[Dict] = None
    right: Optional[Dict] = None
    diagram: Optional[Dict] = None
    metrics: Optional[List[Dict]] = None
    table_data: Optional[Dict] = None
    speaker_notes: str = ""
    placeholder_reason: Optional[str] = None
    # 신규 layout용 데이터
    as_is_items: List[str] = Field(default_factory=list)        # as_is_to_be_compare 좌측
    to_be_items: List[str] = Field(default_factory=list)        # as_is_to_be_compare 우측
    arrow_label: Optional[str] = None                            # 화살표 위 한 줄
    architecture: Optional[Dict] = None                          # system_architecture: {layers, external}
    flow_steps: Optional[List[Dict]] = None                      # process_flow: [{step, title, desc}]
    mockup: Optional[Dict] = None                                # screen_mockup: {device, header, regions, footer}
    mockups: Optional[List[Dict]] = None                         # screen_mockup_grid: [{title, device, regions}]


class DesignBrief(BaseModel):
    """정디자가 산출하는 제안서 1건의 디자인 결정."""
    theme_key: str = "corporate_navy"   # tools/pptx_themes.py THEMES 키
    master_path: Optional[str] = None    # 사용자 업로드 마스터 경로 (있으면 우선)
    master_label: Optional[str] = None
    accent_hex: Optional[str] = None     # 발주처 키컬러 1색 액센트 오버라이드
    footer_text: Optional[str] = None
    layout_guide: Dict[str, str] = Field(default_factory=dict)
    rationale: str = ""


class ProposalDraft(BaseModel):
    """박제안의 제안서 초안."""
    bid_id: str
    version: int = 1
    sections: List[ProposalSection] = Field(default_factory=list)
    docx_path: Optional[str] = None
    pptx_path: Optional[str] = None
    generated_at: datetime = Field(default_factory=datetime.now)


class QualityReport(BaseModel):
    """오품질의 검수 리포트."""
    bid_id: str
    version: int = 1
    spelling_issues: List[Dict] = Field(default_factory=list)
    consistency_issues: List[str] = Field(default_factory=list)
    rfp_coverage: Dict[str, bool] = Field(default_factory=dict)
    overall_grade: Literal["A", "B", "C"] = "B"
    action_items: List[str] = Field(default_factory=list)
    reviewed_at: datetime = Field(default_factory=datetime.now)


class AuditLogEntry(BaseModel):
    """모든 LLM 호출 비용/사용량 기록."""
    agent_name: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    bid_id: Optional[str] = None
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
