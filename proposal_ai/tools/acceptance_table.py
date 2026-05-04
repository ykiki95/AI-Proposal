"""
수용표(AcceptanceTable) 생성기.

한국 공공 제안서 제출 규정:
  - 모든 RFP 요구사항이 제안서에 100% "완전수용"으로 매핑되어야 함
  - 미수용 1건이라도 있으면 평가 탈락

파이프라인:
  1. StrategyAgent → RfpRequirement 목록 추출
  2. WriterAgent   → 각 섹션 작성 시 req_ids_covered 기록
  3. ReviewerAgent → build_table() 호출 → AcceptanceTable 완성 + 검증
  4. GraphicsAgent → to_slide_rows() 호출 → 슬라이드 렌더링

NOTE: LLM 호출 로직은 모듈 D~E에서 구현된다.
"""

from __future__ import annotations

from typing import List

from schemas.models import ProposalDraft
from schemas.rfp_schema import (
    AcceptanceItem,
    AcceptanceStatus,
    AcceptanceTable,
    RfpRequirement,
    RfpStructured,
)


def build_table(
    rfp: RfpStructured,
    draft: ProposalDraft,
) -> AcceptanceTable:
    """
    RFP 요구사항 + 제안서 초안 → AcceptanceTable 생성.

    req_ids_covered가 기록된 섹션은 자동 매핑.
    미커버 요구사항은 가장 유사한 섹션에 LLM으로 매핑 (모듈 E에서 구현).
    """
    covered_map: dict[str, str] = {}
    for section in draft.sections:
        for req_id in section.req_ids_covered:
            covered_map[req_id] = section.title

    items: List[AcceptanceItem] = []
    for req in rfp.requirements:
        section_title = covered_map.get(req.req_id, "")
        # 매핑 안 된 요구사항은 REJECTED로 마킹.
        # 의미: writer가 req_ids_covered에 안 적은 = 우리 제안서가 안 받아준 = 미수용.
        # is_valid가 rejected_count로 판정하므로 미매핑이 자연스럽게 fail 신호로 전파됨.
        # PARTIAL은 LLM 후처리로 부분 매핑된 케이스에 향후 사용 (모듈 E 예정).
        items.append(
            AcceptanceItem(
                req_id=req.req_id,
                requirement_summary=req.description[:100],
                acceptance_status=(
                    AcceptanceStatus.FULL if section_title else AcceptanceStatus.REJECTED
                ),
                proposal_section=section_title or "(매핑 필요)",
                response_summary="",
            )
        )

    return AcceptanceTable(bid_id=rfp.bid_id, items=items)


def validate_table(table: AcceptanceTable) -> list[str]:
    """수용표 검증. 문제 항목 설명 리스트 반환. 빈 리스트 = 통과."""
    issues: list[str] = []
    if table.rejected_count > 0:
        issues.append(f"미수용 항목 {table.rejected_count}건 발견 — 평가 탈락 위험")
    unmapped = [i for i in table.items if i.proposal_section == "(매핑 필요)"]
    if unmapped:
        issues.append(f"미매핑 요구사항 {len(unmapped)}건: {[i.req_id for i in unmapped[:5]]}")
    return issues


def to_slide_rows(table: AcceptanceTable) -> list[list[str]]:
    """수용표 → SlideSpec table_data rows 변환 (GraphicsAgent 입력용)."""
    header = ["요구사항 ID", "요구사항 요약", "수용 여부", "대응 섹션"]
    rows = [
        [
            item.req_id,
            item.requirement_summary,
            item.acceptance_status.value,
            item.proposal_section,
        ]
        for item in table.items
    ]
    return [header] + rows
