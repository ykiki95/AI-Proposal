"""Mock 응답으로 6-에이전트 파이프라인 검증 (API 호출 0회).

목적:
  - 결제 풀린 직후 실제 API 호출 전, 코드 로직과 데이터 흐름을 검증한다.
  - tools.llm_client.chat을 monkey-patch해 가짜 응답을 반환하게 만든다.
  - 각 에이전트가 정상적으로 동작하면, 실제 API 호출 시 비용만 발생할 뿐 동작 자체는 안전하다.

사용:
  python scripts/dry_run_pipeline.py --bid-id smoke_001
  python scripts/dry_run_pipeline.py --bid-id smoke_001 --stage analyze
  python scripts/dry_run_pipeline.py --bid-id smoke_001 --stage all

주의:
  - 실제 산출물 품질은 검증하지 않는다 (응답이 가짜이므로).
  - 검증 항목: SQLite 상태 전환, 함수 인자 타입, 예외 발생 여부, 파일 생성 여부.
  - 실제 호출 시 발생할 비용을 추정해 출력한다.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger

from schemas.models import BidStatus
from tools import db


# ---------------------------------------------------------------------------
# Mock 응답 정의
# ---------------------------------------------------------------------------

_MOCK_CALL_COUNT = {"n": 0, "in_tokens": 0, "out_tokens": 0}

# Anthropic API 추정 단가 (USD per 1M tokens, 2026-05 기준 추정 — 실제 가격은 다를 수 있음)
_PRICE_TABLE = {
    "claude-opus-4-7":          {"in": 15.00, "out": 75.00},
    "claude-sonnet-4-6":        {"in":  3.00, "out": 15.00},
    "claude-haiku-4-5-20251001":{"in":  1.00, "out":  5.00},
}


def _mock_chat(model, system, user, max_tokens=4096, cache_system=False, temperature=None):
    """tools.llm_client.chat의 mock. 모델별로 그럴듯한 가짜 응답을 반환."""
    _MOCK_CALL_COUNT["n"] += 1

    # 입력 토큰 거칠게 추정 (한글 1자 ≈ 2토큰 가정)
    sys_text = system if isinstance(system, str) else " ".join(
        b.get("text", "") for b in system if isinstance(b, dict)
    )
    usr_text = user if isinstance(user, str) else " ".join(
        b.get("text", "") for b in user if isinstance(b, dict)
    )
    in_est = (len(sys_text) + len(usr_text)) // 2
    out_est = max_tokens // 2  # 보통 max의 절반 정도 사용한다고 가정
    _MOCK_CALL_COUNT["in_tokens"] += in_est
    _MOCK_CALL_COUNT["out_tokens"] += out_est

    logger.info(
        f"[MOCK] call#{_MOCK_CALL_COUNT['n']} model={model} "
        f"in≈{in_est} out_max={max_tokens} (cache={cache_system})"
    )

    # 응답 형태는 caller가 JSON을 기대할 수도, 평문을 기대할 수도 있음.
    # 안전하게 둘 다 그럴듯하게 보이는 응답을 반환.
    # JSON 의도는 보통 system 프롬프트에 명시되므로 system+user를 합쳐서 검사.
    combined = (sys_text + " " + usr_text).lower()
    if "json" in combined or "스키마" in combined or "structured" in combined:
        return (
            '{\n'
            '  "summary": "Mock dry-run 응답. 실제 API 호출이 아님.",\n'
            '  "fit_score": 75,\n'
            '  "recommendation": "PROCEED",\n'
            '  "key_points": ["mock point 1", "mock point 2"],\n'
            '  "chapters": [\n'
            '    {"title": "사업 이해", "content": "mock content 1"},\n'
            '    {"title": "솔루션", "content": "mock content 2"}\n'
            '  ]\n'
            '}'
        )
    # 글쓰기 단계 — writer_agent는 챕터별 본문을 받음
    return (
        "## Mock 챕터 본문\n\n"
        "이것은 dry-run 모드에서 생성된 가짜 응답입니다. "
        "실제 Claude API 호출이 아니므로 내용은 의미가 없습니다. "
        "실제 호출 시 이 자리에 전문가 페르소나에 맞는 2,500자 이상의 챕터 본문이 채워집니다.\n\n"
        "- 항목 1\n- 항목 2\n- 항목 3\n"
    )


def _estimate_cost() -> float:
    """누적 토큰을 기반으로 USD 비용 추정 (sonnet 단가 기준)."""
    in_tok = _MOCK_CALL_COUNT["in_tokens"]
    out_tok = _MOCK_CALL_COUNT["out_tokens"]
    # 보수적으로 sonnet 단가 사용 (실제는 모델별로 다름)
    p = _PRICE_TABLE["claude-sonnet-4-6"]
    cost = (in_tok / 1_000_000) * p["in"] + (out_tok / 1_000_000) * p["out"]
    return cost


def run_stage(stage: str, bid_id: str) -> int:
    """단일 스테이지 dry-run."""
    try:
        if stage == "analyze":
            from workflows.pipeline import run_analysis
            res = run_analysis()
            print(f"  결과: {len(res)}건 평가 완료")
        elif stage == "strategize":
            from workflows.pipeline import run_strategy
            # APPROVED 상태 가정 — 강제로 전환
            db.set_bid_status(bid_id, BidStatus.APPROVED)
            res = run_strategy(bid_id=bid_id)
            print(f"  결과: {len(res)}건 전략 완료")
        elif stage == "write":
            from workflows.pipeline import run_write
            db.set_bid_status(bid_id, BidStatus.STRATEGY_DONE)
            res = run_write(bid_id=bid_id)
            print(f"  결과: {len(res)}건 초안 완료")
        elif stage == "review":
            from workflows.pipeline import run_review
            db.set_bid_status(bid_id, BidStatus.DRAFT_DONE)
            res = run_review(bid_id=bid_id)
            print(f"  결과: {len(res)}건 검수 완료")
        elif stage == "graphics":
            from workflows.pipeline import run_graphics
            db.set_bid_status(bid_id, BidStatus.FINAL_APPROVED)
            res = run_graphics(bid_id=bid_id)
            print(f"  결과: {len(res)}건 시각화 완료")
        elif stage == "all":
            from workflows.pipeline import run_propose
            db.set_bid_status(bid_id, BidStatus.APPROVED)
            res = run_propose(bid_id=bid_id)
            print(f"  결과: {res}")
        else:
            print(f"  [ERROR] 알 수 없는 스테이지: {stage}")
            return 1
        return 0
    except Exception as e:
        print(f"  [ERROR] {stage} 실패: {e}")
        traceback.print_exc()
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Mock 기반 파이프라인 dry-run")
    ap.add_argument("--bid-id", required=True, help="검증 대상 bid_id (smoke_001 등)")
    ap.add_argument(
        "--stage",
        choices=["analyze", "strategize", "write", "review", "graphics", "all"],
        default="all",
        help="실행할 단계 (default: all)",
    )
    args = ap.parse_args()

    db.init_db()
    bid = db.get_bid(args.bid_id)
    if not bid:
        print(f"[ERROR] {args.bid_id} 존재하지 않음. register_local_rfp.py로 먼저 등록하세요.")
        return 1

    print("=" * 60)
    print(f" DRY-RUN: bid_id={args.bid_id}, stage={args.stage}")
    print(" (실제 Anthropic API 호출 없음. 모든 응답은 mock.)")
    print("=" * 60)

    # tools.llm_client.chat을 monkey-patch
    with patch("tools.llm_client.chat", side_effect=_mock_chat):
        # 다른 모듈에서 직접 import한 경우 대비
        import tools.llm_client
        tools.llm_client.chat_anthropic = _mock_chat

        rc = run_stage(args.stage, args.bid_id)

    print()
    print("=" * 60)
    print(" Dry-Run 통계")
    print("=" * 60)
    print(f"  Mock 호출 횟수      : {_MOCK_CALL_COUNT['n']}회")
    print(f"  누적 입력 토큰 (추정): {_MOCK_CALL_COUNT['in_tokens']:,}")
    print(f"  누적 출력 토큰 (추정): {_MOCK_CALL_COUNT['out_tokens']:,}")
    cost = _estimate_cost()
    print(f"  실제 호출 시 추정 비용: ~${cost:.2f} USD (sonnet 단가 기준)")
    print()
    if rc == 0:
        print("  ✅ 코드 로직 통과. 결제 해제 후 실제 호출 가능.")
    else:
        print("  ❌ 오류 발생. 실제 호출 전 수정 필요.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
