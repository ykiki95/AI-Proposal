"""
제안서 자동화 AI 팀 - 진입점.

사용:
  python main.py --mode collect          # G2B + 지자체 공고 수집
  python main.py --mode evaluate         # 수집된 공고 스코어링
  python main.py --mode propose          # 승인된 공고 → 제안서 + PT + 검수
  python main.py --mode all              # 위 3단계 순차 실행
  python main.py --schedule              # 매일 09:00 자동 수집/평가
  python main.py --status                # 현재 시스템 상태
  python main.py --list-awaiting         # 승인 대기중 공고 목록
  python main.py --approve <bid_id>      # 수동 승인 (Notion 미사용 시)
"""

from __future__ import annotations

import argparse
import sys

from loguru import logger

from config.settings import SYS, summary
from tools.db import init_db
from workflows.crew_definition import run_all, run_collect, run_evaluate, run_propose
from workflows.human_gates import list_awaiting, manual_approve


def setup_logging() -> None:
    logger.remove()
    logger.add(sys.stderr, level=SYS.log_level, format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}")


def cmd_status() -> None:
    print("=" * 60)
    print(summary())
    print("=" * 60)


def cmd_schedule() -> None:
    """APScheduler로 매일 오전 9시 수집+평가 자동 실행."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    sched = BlockingScheduler(timezone="Asia/Seoul")

    @sched.scheduled_job("cron", hour=9, minute=0, id="daily_collect_evaluate")
    def daily_job() -> None:
        logger.info("[스케줄] 일일 수집/평가 시작")
        run_collect()
        run_evaluate()

    logger.info("스케줄러 시작 (매일 09:00 KST). Ctrl+C로 종료.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("스케줄러 종료")


def main() -> None:
    setup_logging()
    init_db()

    parser = argparse.ArgumentParser(description="제안서 자동화 AI 팀")
    parser.add_argument(
        "--mode",
        choices=["collect", "evaluate", "propose", "all"],
        help="실행 모드",
    )
    parser.add_argument("--schedule", action="store_true", help="스케줄러 모드")
    parser.add_argument("--status", action="store_true", help="현재 설정 상태")
    parser.add_argument("--list-awaiting", action="store_true", help="승인 대기 목록")
    parser.add_argument("--approve", type=str, help="공고 ID 수동 승인")
    args = parser.parse_args()

    if args.status:
        cmd_status()
        return

    if args.list_awaiting:
        ids = list_awaiting()
        print(f"승인 대기 공고 ({len(ids)}건):")
        for i, bid_id in enumerate(ids, 1):
            print(f"  {i}. {bid_id}")
        return

    if args.approve:
        ok = manual_approve(args.approve)
        sys.exit(0 if ok else 1)

    if args.schedule:
        cmd_schedule()
        return

    if args.mode == "collect":
        bids = run_collect()
        print(f"수집 완료: {len(bids)}건")
    elif args.mode == "evaluate":
        evs = run_evaluate()
        print(f"평가 완료: {len(evs)}건")
        for ev in evs:
            print(f"  - {ev.bid_id}: {ev.fit_score}점 ({ev.recommendation})")
    elif args.mode == "propose":
        out = run_propose()
        print(
            f"제안 파이프라인: 초안 {len(out['drafts'])}건, "
            f"PT {len(out['pts'])}건, 검수 {len(out['reports'])}건"
        )
    elif args.mode == "all":
        result = run_all()
        print("전체 파이프라인 결과:", result)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
