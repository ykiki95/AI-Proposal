"""
오품질 - 검수 에이전트.
- 한국어 맞춤법/띄어쓰기 (hanspell or LLM fallback)
- 용어 일관성 검사 (LLM)
- RFP 요구사항 매핑 체크리스트 (LLM)
- 문체 통일 검사 (LLM)
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import select

from config.settings import MODELS
from schemas.models import (
    AuditLogEntry,
    BidNotice,
    BidStatus,
    ProposalDraft,
    ProposalSection,
    QualityReport,
)
from tools import db
from tools.db import ProposalRow, session_scope
from tools.korean_checker import check_spelling
from tools.llm_clients import chat_openai
from tools.notion_client import push_quality_report

REVIEW_SYSTEM = """당신은 한국어 제안서 품질 책임자입니다.
입력은 RFP와 제안서 본문입니다.
JSON 한 객체로만 응답하세요. 설명/마크다운 없이.
"""


def _build_review_prompt(bid: BidNotice, full_text: str) -> str:
    return f"""# RFP
{bid.rfp_full_text or bid.rfp_summary}

# 제안서 본문
{full_text[:8000]}

# 평가 항목
1. consistency_issues: 같은 개념에 다른 용어를 쓴 사례 (예: "사용자/유저" 혼용) 5개 이내
2. rfp_coverage: RFP의 주요 요구사항 8개를 추출해 각 키마다 제안서에 반영되었는지 true/false
3. style_issues: 경어체/평어체 혼용 등 문체 일관성 이슈 3개 이내
4. overall_grade: A/B/C (A=수정없이 제출 가능, B=경미수정, C=재작성 필요)
5. action_items: 박제안에게 전달할 수정 지시 5개 이내

# 출력 JSON
{{
  "consistency_issues": ["..."],
  "rfp_coverage": {{"요구사항A": true, "요구사항B": false, ...}},
  "style_issues": ["..."],
  "overall_grade": "A" | "B" | "C",
  "action_items": ["..."]
}}"""


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


class OhQuality:
    name = "오품질"
    role = "최종 산출물 한국어/요구사항 품질 검수"
    model = MODELS.oh

    def review(self, bid: BidNotice, draft: ProposalDraft) -> QualityReport:
        logger.info(f"[{self.name}] 검수 시작 ({bid.bid_id} v{draft.version})")

        full_text = "\n\n".join(
            f"# {sec.title}\n{sec.body}"
            for sec in sorted(draft.sections, key=lambda s: s.order)
        )

        # 1) 맞춤법
        spelling_issues = check_spelling(full_text)

        # 2) LLM 종합 검수
        try:
            raw = chat_openai(
                self.model,
                REVIEW_SYSTEM,
                _build_review_prompt(bid, full_text),
                max_tokens=2000,
            )
            data = _parse_json(raw)
        except Exception as e:
            logger.warning(f"[{self.name}] LLM 검수 실패: {e}")
            data = {}

        report = QualityReport(
            bid_id=bid.bid_id,
            version=draft.version,
            spelling_issues=spelling_issues[:20],
            consistency_issues=data.get("consistency_issues", []),
            rfp_coverage=data.get("rfp_coverage", {}),
            overall_grade=data.get("overall_grade", "B"),
            action_items=data.get("action_items", []),
        )

        db.save_quality_report(report)
        db.log_audit(AuditLogEntry(
            agent_name=self.name,
            model=self.model,
            bid_id=bid.bid_id,
            note=f"grade={report.overall_grade}",
        ))
        push_quality_report(bid, report)

        # 게이트 3: A=최종승인, B/C=검토중(재작업)
        if report.overall_grade == "A":
            db.set_bid_status(bid.bid_id, BidStatus.FINAL_APPROVED)
        else:
            db.set_bid_status(bid.bid_id, BidStatus.UNDER_REVIEW)

        logger.info(
            f"[{self.name}] 검수 완료 ({bid.bid_id}): {report.overall_grade}, "
            f"맞춤법 {len(spelling_issues)}건, 조치사항 {len(report.action_items)}개"
        )
        return report

    def run(self, bid_id: Optional[str] = None, progress_cb=None) -> List[QualityReport]:
        target_bids = db.list_bids(status=BidStatus.DRAFT_DONE)
        if bid_id:
            target_bids = [b for b in target_bids if b.bid_id == bid_id]

        total = len(target_bids)
        if progress_cb:
            progress_cb(0, total, f"검수 대상 {total}건")
        results: List[QualityReport] = []
        for idx, bid in enumerate(target_bids, start=1):
            with session_scope() as s:
                row = s.execute(
                    select(ProposalRow)
                    .where(ProposalRow.bid_id == bid.bid_id)
                    .order_by(ProposalRow.version.desc())
                ).scalar_one_or_none()
                if not row:
                    continue
                draft = ProposalDraft(
                    bid_id=row.bid_id,
                    version=row.version,
                    sections=[ProposalSection(**sec) for sec in (row.sections or [])],
                    docx_path=row.docx_path,
                )
            try:
                results.append(self.review(bid, draft))
            except Exception as e:
                logger.error(f"[{self.name}] 검수 실패 ({bid.bid_id}): {e}")
            if progress_cb:
                progress_cb(idx, total, f"{bid.title[:30]} 검수 완료")
        return results
