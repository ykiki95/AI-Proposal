"""
이판단 - 적격성 판단 에이전트 (2단계 평가).

1단계 (외적 평가, evaluate_external):
  - 메타데이터(예산/기간/적합도/경쟁률/마감) 위주, RFP 본문 없이도 빠르게 채점
  - 모든 신규 공고에 일괄 적용 → 임계 통과 시 AWAITING_APPROVAL
2단계 (정밀 분석, deep_review):
  - 1단계 통과 + 점수 상위 N건만 RFP 본문 정밀 분석
  - 독소조항·파견·소재지·자격제약·숨은 단서 등 사장 관점 체크
  - 결과는 BidRow.rfp_deep_analysis에 저장됨
"""

from __future__ import annotations

import json
import re
from typing import List, Optional

import yaml
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import MODELS, ROOT_DIR, SYS, get_effective_company
from schemas.models import AuditLogEntry, BidEvaluation, BidNotice, BidStatus
from tools import db
from tools.llm_clients import chat_anthropic
from tools.rfp_analyzer import analyze_rfp

RUBRIC_PATH = ROOT_DIR / "config" / "scoring_rubric.yaml"


def load_rubric() -> dict:
    with RUBRIC_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


SYSTEM_PROMPT_EXTERNAL = """당신은 한국 웹에이전시의 시니어 사업개발 책임자입니다.
공공/민간 제안 공고를 외적 메타데이터(예산·기간·적합성·경쟁구도)만 보고
빠르게 0~100점으로 채점하고 JSON 한 객체로만 응답합니다. 설명/마크다운 금지."""


def _build_prompt_external(bid: BidNotice, rubric: dict) -> str:
    company = get_effective_company()
    weights = rubric["weights"]
    guidance = rubric["guidance"]
    return f"""아래 공고를 '외적 평가'만 수행하라. (RFP 본문은 보지 않는다.)

# 회사 정보
- 회사명: {company.name}
- 보유 기술스택: {', '.join(company.tech_stack)}
- 팀 규모: {company.team_size}명
- 적정 예산 범위: {company.typical_budget_min:,} ~ {company.typical_budget_max:,}원

# 공고 (메타데이터)
- 공고번호: {bid.bid_id}
- 발주기관: {bid.agency}
- 사업명: {bid.title}
- 예산: {bid.budget_krw or '미공개'} 원
- 사업기간: {bid.duration_months or '미상'} 개월
- 마감일: {bid.deadline.isoformat()}
- 참가자격: {', '.join(bid.qualifications) or '명시 없음'}
- 한 줄 요약(공고 메타): {bid.rfp_summary or '(없음)'}

# 가중치 (합계 100)
{json.dumps(weights, ensure_ascii=False)}

# 채점 가이드
{json.dumps(guidance, ensure_ascii=False)}

# 출력 JSON 스키마
{{
  "fit_score": int(0-100),
  "score_breakdown": {{"tech_fit": 0-100, "budget_fit": 0-100, "competition": 0-100, "schedule": 0-100, "experience_fit": 0-100}},
  "recommendation": "참여권장" | "조건부" | "비권장",
  "rationale": "한국어 5문장 이내, 외적 요소 기준만",
  "risk_factors": ["..."],
  "opportunity_factors": ["..."]
}}
fit_score는 score_breakdown을 가중치로 가중합한 값과 일치해야 한다.
RFP 본문이 없으므로 '독소조항·파견 여부'는 평가 대상이 아니며, 판단되지 않는 항목은 보수적으로 60점 부근으로 추정하라."""


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"JSON 추출 실패: {text[:200]}")
    return json.loads(m.group(0))


class LeeJudge:
    name = "이판단"
    role = "공고 적격성 스코어링 (외적 1차 + 정밀 2차)"
    model = MODELS.lee

    def __init__(self) -> None:
        self.rubric = load_rubric()

    # ------------------------------------------------------------------
    # 1차 — 외적 평가
    # ------------------------------------------------------------------
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=8))
    def evaluate_external(self, bid: BidNotice) -> BidEvaluation:
        prompt = _build_prompt_external(bid, self.rubric)
        raw = chat_anthropic(self.model, SYSTEM_PROMPT_EXTERNAL, prompt, max_tokens=1500)
        data = _parse_json(raw)
        ev = BidEvaluation(
            bid_id=bid.bid_id,
            fit_score=int(data.get("fit_score", 0)),
            score_breakdown={k: int(v) for k, v in data.get("score_breakdown", {}).items()},
            recommendation=data.get("recommendation", "비권장"),
            rationale=data.get("rationale", ""),
            risk_factors=data.get("risk_factors", []),
            opportunity_factors=data.get("opportunity_factors", []),
        )
        db.save_evaluation(ev)
        db.log_audit(AuditLogEntry(
            agent_name=self.name,
            model=self.model,
            bid_id=bid.bid_id,
            note=f"[1차/외적] score={ev.fit_score} rec={ev.recommendation}",
        ))
        return ev

    # 호환성을 위한 별칭 (기존 evaluate 호출 유지)
    evaluate = evaluate_external

    # ------------------------------------------------------------------
    # 2차 — RFP 본문 정밀 분석 (상위 N개)
    # ------------------------------------------------------------------
    def deep_review(self, bid: BidNotice) -> str:
        """analyze_rfp 호출 결과를 BidRow.rfp_deep_analysis에 저장하고 반환."""
        if not (bid.rfp_full_text or "").strip():
            msg = "_RFP 본문이 없어 정밀 분석을 건너뜁니다. 공고 상세에 본문을 붙여넣어 주세요._"
            db.update_bid_deep_analysis(bid.bid_id, msg)
            return msg
        result = analyze_rfp(bid)
        db.update_bid_deep_analysis(bid.bid_id, result)
        db.log_audit(AuditLogEntry(
            agent_name=self.name,
            model=MODELS.park,  # 정밀 분석은 박제안 모델 사용
            bid_id=bid.bid_id,
            note=f"[2차/정밀] len={len(result)}",
        ))
        return result

    # ------------------------------------------------------------------
    # 배치 실행
    # ------------------------------------------------------------------
    def run(self, progress_cb=None, max_auto_promote: int = 20) -> list[BidEvaluation]:
        """1차 외적 평가만 일괄 수행. (정밀 분석은 run_deep_top_n 사용)

        규칙:
        - 모든 신규(COLLECTED) 공고에 1차 점수만 부여
        - **상위 max_auto_promote건 + 임계점수 통과** 둘 다 만족하는 공고만 게이트1 자동 진입
        - 점수 미달 공고도 REJECTED로 강제하지 않음 → 사장님이 ⭐ 찜으로 직접 게이트1에 올릴 수 있음
        - 사용자가 이미 ⭐ 찜한 공고는 set_bid_pinned에서 이미 AWAITING_APPROVAL이므로 그대로 둔다.
        """
        bids = db.list_bids(status=BidStatus.COLLECTED)
        total = len(bids)
        logger.info(f"[{self.name}] 1차 외적 평가 대상 {total}건 (자동 게이트1 상한 {max_auto_promote})")
        results: list[BidEvaluation] = []
        if progress_cb:
            progress_cb(0, total, f"1차 평가 대상 {total}건")
        for idx, bid in enumerate(bids, start=1):
            try:
                ev = self.evaluate_external(bid)
                logger.info(
                    f"[{self.name}] {bid.bid_id} → {ev.fit_score}점 ({ev.recommendation})"
                )
                results.append(ev)
            except Exception as e:
                logger.error(f"[{self.name}] 평가 실패 ({bid.bid_id}): {e}")
            if progress_cb:
                progress_cb(idx, total, f"{bid.title[:30]} 1차 평가 완료")

        # 임계점수 통과 + 점수 상위 N건만 자동 게이트1 진입
        passed = [r for r in results if r.fit_score >= SYS.fit_score_threshold]
        passed.sort(key=lambda x: x.fit_score, reverse=True)
        promoted = passed[:max_auto_promote]
        for ev in promoted:
            db.set_bid_status(ev.bid_id, BidStatus.AWAITING_APPROVAL)
        logger.info(
            f"[{self.name}] 게이트1 자동 진입 {len(promoted)}건 "
            f"(임계 {SYS.fit_score_threshold}점 / 상한 {max_auto_promote})"
        )
        if progress_cb:
            progress_cb(total, total, f"자동 게이트1 진입 {len(promoted)}건")
        return results

    def run_deep_top_n(self, n: int = 5, progress_cb=None) -> list[dict]:
        """1차 통과(AWAITING_APPROVAL) 점수 상위 N건에 정밀 분석.
        본문이 없는 공고는 안내 메시지만 저장하고 다음으로 넘어간다."""
        from sqlalchemy import select
        from tools.db import EvaluationRow, BidRow, session_scope

        # 점수 상위 N개 (AWAITING_APPROVAL 상태에 한해)
        with session_scope() as s:
            rows = s.execute(
                select(EvaluationRow.bid_id, EvaluationRow.fit_score, BidRow.title)
                .join(BidRow, EvaluationRow.bid_id == BidRow.bid_id)
                .where(BidRow.status == BidStatus.AWAITING_APPROVAL)
                .order_by(EvaluationRow.fit_score.desc())
                .limit(n)
            ).all()
            targets = [(r[0], r[1], r[2]) for r in rows]

        total = len(targets)
        logger.info(f"[{self.name}] 2차 정밀 분석 대상 {total}건 (상위 {n})")
        if progress_cb:
            progress_cb(0, total, f"정밀 분석 대상 {total}건")

        results = []
        for idx, (bid_id, score, title) in enumerate(targets, start=1):
            bid = db.get_bid(bid_id)
            if not bid:
                continue
            try:
                txt = self.deep_review(bid)
                results.append({"bid_id": bid_id, "title": title, "score": score, "ok": True, "len": len(txt)})
            except Exception as e:
                logger.error(f"[{self.name}] 정밀 분석 실패 ({bid_id}): {e}")
                results.append({"bid_id": bid_id, "title": title, "score": score, "ok": False, "error": str(e)})
            if progress_cb:
                progress_cb(idx, total, f"{title[:30]} 정밀 분석 완료")
        return results
