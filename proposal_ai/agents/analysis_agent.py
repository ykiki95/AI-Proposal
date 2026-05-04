"""
AnalysisAgent — 공고 적격성 1차 평가.

v1 LeeJudge에서 "RFP 정밀 분석(deep_review)" 책임을 떼어내고 평가에만 집중한다.
정밀 분석/전략은 StrategyAgent가 담당.

평가 정책:
  - 모든 신규(COLLECTED) 공고에 외적 메타데이터(예산·기간·자격·마감) 기반 점수 부여
  - 임계 통과 + 점수 상위 N건만 게이트1(AWAITING_APPROVAL)에 자동 진입
  - 점수 미달 공고도 REJECTED로 강제하지 않음 → 사용자가 ⭐로 직접 게이트1 승급 가능

성능:
  - system 프롬프트에 회사/rubric/스키마 등 정적 부분을 모아 prompt caching 활성화
  - 배치 모드(run)에서 N건 연속 평가 시 첫 호출만 캐시 쓰기, 이후는 캐시 읽기
"""

from __future__ import annotations

import json
import re
from typing import List, Optional

import yaml
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from agents.base_agent import BaseAgent, ProgressCallback
from config.settings import MODELS, ROOT_DIR, SYS, get_effective_company
from schemas.models import AuditLogEntry, BidEvaluation, BidNotice, BidStatus
from tools import db
from tools.llm_client import chat


RUBRIC_PATH = ROOT_DIR / "config" / "scoring_rubric.yaml"


def load_rubric() -> dict:
    with RUBRIC_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# 시스템 프롬프트 — 정적 부분(회사/rubric/스키마)을 모두 포함해 prompt caching 효과를 극대화
SYSTEM_TEMPLATE = """당신은 한국 웹에이전시의 시니어 사업개발 책임자입니다.
공공/민간 제안 공고를 외적 메타데이터(예산·기간·적합성·경쟁구도)만 보고 빠르게 0~100점으로 채점합니다.
설명/마크다운 없이 JSON 한 객체로만 응답하세요.

# 회사 정보
- 회사명: {company_name}
- 보유 기술스택: {tech_stack}
- 팀 규모: {team_size}명
- 적정 예산 범위: {budget_min} ~ {budget_max}원

# 가중치 (합계 100)
{weights_json}

# 채점 가이드
{guidance_json}

# 출력 JSON 스키마
{{
  "fit_score": int(0-100),
  "score_breakdown": {{"tech_fit": 0-100, "budget_fit": 0-100, "competition": 0-100, "schedule": 0-100, "experience_fit": 0-100}},
  "recommendation": "참여권장" | "조건부" | "비권장",
  "rationale": "한국어 5문장 이내, 외적 요소 기준만",
  "risk_factors": ["..."],
  "opportunity_factors": ["..."]
}}

규칙:
- fit_score는 score_breakdown을 가중치로 가중합한 값과 일치해야 한다.
- RFP 본문이 없으므로 '독소조항·파견 여부'는 평가 대상이 아니며, 판단되지 않는 항목은 보수적으로 60점 부근으로 추정한다."""


USER_TEMPLATE = """아래 공고를 평가하라.

# 공고 (메타데이터)
- 공고번호: {bid_id}
- 발주기관: {agency}
- 사업명: {title}
- 예산: {budget} 원
- 사업기간: {duration} 개월
- 마감일: {deadline}
- 참가자격: {qualifications}
- 한 줄 요약(공고 메타): {rfp_summary}"""


_RECOMMENDATION_VALUES = {"참여권장", "조건부", "비권장"}


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"JSON 추출 실패: {text[:200]}")
    return json.loads(m.group(0))


class AnalysisAgent(BaseAgent):
    agent_name = "analysis"

    def __init__(self, progress_cb: Optional[ProgressCallback] = None) -> None:
        super().__init__(progress_cb)
        self.model = MODELS.analysis
        self.rubric = load_rubric()
        self._system_prompt: Optional[str] = None  # lazy build → 인스턴스 내 재사용

    # ------------------------------------------------------------------
    # 단건 평가
    # ------------------------------------------------------------------
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=8))
    def evaluate(self, bid: BidNotice) -> BidEvaluation:
        system = self._build_system()
        user = USER_TEMPLATE.format(
            bid_id=bid.bid_id,
            agency=bid.agency,
            title=bid.title,
            budget=f"{bid.budget_krw:,}" if bid.budget_krw else "미공개",
            duration=bid.duration_months or "미상",
            deadline=bid.deadline.isoformat(),
            qualifications=", ".join(bid.qualifications) or "명시 없음",
            rfp_summary=bid.rfp_summary or "(없음)",
        )

        raw = chat(
            self.model,
            system,
            user,
            max_tokens=1500,
            cache_system=True,  # 동일 system 프롬프트 → 두 번째 호출부터 캐시 히트
        )
        data = _parse_json(raw)

        recommendation = data.get("recommendation", "비권장")
        if recommendation not in _RECOMMENDATION_VALUES:
            logger.warning(
                f"[{self.agent_name}] 비표준 recommendation '{recommendation}' → 비권장 처리"
            )
            recommendation = "비권장"

        ev = BidEvaluation(
            bid_id=bid.bid_id,
            fit_score=int(data.get("fit_score", 0)),
            score_breakdown={
                k: int(v) for k, v in data.get("score_breakdown", {}).items()
            },
            recommendation=recommendation,
            rationale=data.get("rationale", ""),
            risk_factors=data.get("risk_factors", []),
            opportunity_factors=data.get("opportunity_factors", []),
        )

        db.save_evaluation(ev)
        db.log_audit(AuditLogEntry(
            agent_name=self.agent_name,
            model=self.model,
            bid_id=bid.bid_id,
            note=f"score={ev.fit_score} rec={ev.recommendation}",
        ))
        return ev

    # ------------------------------------------------------------------
    # 배치 실행 + 자동 게이트1 진입
    # ------------------------------------------------------------------
    def run(self, max_auto_promote: int = 20) -> List[BidEvaluation]:
        """COLLECTED 상태 공고 일괄 평가.

        규칙:
          - 모든 COLLECTED 공고에 점수 부여
          - fit_score >= SYS.fit_score_threshold 이면서 점수 상위 max_auto_promote 건만
            AWAITING_APPROVAL로 자동 승급
          - 미달 공고도 REJECTED로 강제하지 않음 (사용자 ⭐ 찜으로 수동 승급 가능)
        """
        bids = db.list_bids(status=BidStatus.COLLECTED)
        total = len(bids)
        self.progress(
            f"평가 대상 {total}건 (model={self.model}, 자동 승급 상한 {max_auto_promote})"
        )

        results: List[BidEvaluation] = []
        for idx, bid in enumerate(bids, start=1):
            try:
                ev = self.evaluate(bid)
                self.progress(
                    f"({idx}/{total}) {bid.title[:30]} → {ev.fit_score}점 ({ev.recommendation})"
                )
                results.append(ev)
            except Exception as e:
                logger.error(f"[{self.agent_name}] 평가 실패 ({bid.bid_id}): {e}")

        promoted = self._promote_top(results, max_auto_promote)
        self.progress(
            f"평가 완료: {len(results)}건 채점, 게이트1 자동 진입 {len(promoted)}건 "
            f"(임계 {SYS.fit_score_threshold}점)"
        )
        return results

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _build_system(self) -> str:
        if self._system_prompt is None:
            company = get_effective_company()
            self._system_prompt = SYSTEM_TEMPLATE.format(
                company_name=company.name,
                tech_stack=", ".join(company.tech_stack),
                team_size=company.team_size,
                budget_min=f"{company.typical_budget_min:,}",
                budget_max=f"{company.typical_budget_max:,}",
                weights_json=json.dumps(self.rubric["weights"], ensure_ascii=False),
                guidance_json=json.dumps(self.rubric["guidance"], ensure_ascii=False),
            )
        return self._system_prompt

    def _promote_top(
        self, results: List[BidEvaluation], limit: int
    ) -> List[BidEvaluation]:
        passed = [r for r in results if r.fit_score >= SYS.fit_score_threshold]
        passed.sort(key=lambda x: x.fit_score, reverse=True)
        promoted = passed[:limit]
        for ev in promoted:
            db.set_bid_status(ev.bid_id, BidStatus.AWAITING_APPROVAL)
        return promoted
