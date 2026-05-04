"""
제안서 자동화 AI 팀 v2 — CLI 진입점.

사용:
  python main.py --mode discover         # 공고 수집 + RFP 파싱 + RAG 인덱싱
  python main.py --mode analyze          # 1차 적격성 평가 (자동 게이트1 승급)
  python main.py --mode strategize       # APPROVED 공고 RfpStructured 산출
  python main.py --mode write            # STRATEGY_DONE 공고 초안 작성
  python main.py --mode review           # DRAFT_DONE 공고 검수 (수용표·TOC 검증)
  python main.py --mode graphics         # FINAL_APPROVED/UNDER_REVIEW 공고 PPTX 빌드
  python main.py --mode propose --bid <bid_id>   # 단일 공고 strategy→write→review→graphics
  python main.py --mode all              # 위 전 단계 일괄 (게이트는 자동 승급에 의존)

  python main.py --schedule              # 매일 09:00 자동 수집/평가
  python main.py --status                # 현재 시스템 상태
  python main.py --list-awaiting         # 게이트1 승인 대기 목록
  python main.py --approve <bid_id>      # 게이트1 수동 승인 (Notion 미사용 시)
"""

from __future__ import annotations

import argparse
import sys

from loguru import logger

from config.settings import SYS, summary
from tools.db import init_db
from workflows.human_gates import list_awaiting, manual_approve
from workflows.pipeline import (
    run_all,
    run_analysis,
    run_discovery,
    run_graphics,
    run_propose,
    run_review,
    run_strategy,
    run_write,
)


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=SYS.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}",
    )


def cmd_status() -> None:
    print("=" * 60)
    print(summary())
    print("=" * 60)


def cmd_schedule() -> None:
    """APScheduler — 매일 09:00 KST 자동 수집/평가."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    sched = BlockingScheduler(timezone="Asia/Seoul")

    @sched.scheduled_job("cron", hour=9, minute=0, id="daily_discover_analyze")
    def daily_job() -> None:
        logger.info("[스케줄] 일일 수집/평가 시작")
        run_discovery()
        run_analysis()

    logger.info("스케줄러 시작 (매일 09:00 KST). Ctrl+C로 종료.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("스케줄러 종료")


def main() -> None:
    setup_logging()
    init_db()

    parser = argparse.ArgumentParser(description="제안서 자동화 AI 팀 v2")
    parser.add_argument(
        "--mode",
        choices=[
            "discover", "analyze", "strategize", "write",
            "review", "graphics", "propose", "all",
        ],
        help="실행 모드",
    )
    parser.add_argument("--bid", type=str, help="단일 공고 ID (propose/write/review/graphics에서 사용)")
    parser.add_argument("--version", type=int, default=1, help="제안서 버전 (write 모드)")
    parser.add_argument("--schedule", action="store_true", help="스케줄러 모드")
    parser.add_argument("--status", action="store_true", help="현재 설정 상태")
    parser.add_argument("--list-awaiting", action="store_true", help="게이트1 대기 목록")
    parser.add_argument("--approve", type=str, help="게이트1 수동 승인할 공고 ID")
    args = parser.parse_args()

    if args.status:
        cmd_status()
        return

    if args.list_awaiting:
        ids = list_awaiting()
        print(f"게이트1 승인 대기 공고 ({len(ids)}건):")
        for i, bid_id in enumerate(ids, 1):
            print(f"  {i}. {bid_id}")
        return

    if args.approve:
        ok = manual_approve(args.approve)
        sys.exit(0 if ok else 1)

    if args.schedule:
        cmd_schedule()
        return

    bid_id = args.bid

    if args.mode == "discover":
        bids = run_discovery()
        print(f"수집 완료: {len(bids)}건")
    elif args.mode == "analyze":
        evs = run_analysis()
        print(f"평가 완료: {len(evs)}건")
        for ev in evs:
            print(f"  - {ev.bid_id}: {ev.fit_score}점 ({ev.recommendation})")
    elif args.mode == "strategize":
        out = run_strategy(bid_id=bid_id)
        print(f"전략 완료: {len(out)}건")
    elif args.mode == "write":
        drafts = run_write(bid_id=bid_id, version=args.version)
        print(f"초안 완료: {len(drafts)}건")
    elif args.mode == "review":
        reports = run_review(bid_id=bid_id)
        print(f"검수 완료: {len(reports)}건")
        for r in reports:
            print(
                f"  - {r.bid_id}: grade={r.overall_grade} "
                f"accept_valid={r.acceptance_table_valid} toc={r.toc_similarity_score:.2f}"
            )
    elif args.mode == "graphics":
        briefs = run_graphics(bid_id=bid_id)
        print(f"시각화 완료: {len(briefs)}건")
    elif args.mode == "propose":
        out = run_propose(bid_id=bid_id)
        print("propose 결과:", out)
    elif args.mode == "all":
        result = run_all()
        print("전체 파이프라인 결과:", result)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
