"""smoke_001 end-to-end 실행 wrapper.

단일 Python 프로세스에서 analyze → 게이트1 승인 → strategize → write → review →
게이트3 승인 → graphics를 순차 실행한다. 토큰 사용량은 같은 프로세스에 누적되므로
전체 실행이 끝난 시점에서 정확한 단일 비용 추정이 가능하다.

특징:
  - 게이트1·3은 자동 승급 (DB 직접 변경, interactive prompt 없음)
  - 단계 사이 print + timestamp, 상세 로그는 storage/logs/smoke_run_*.log에 동시 기록
  - 시작 시 기존 산출물 백업 (storage/outputs/_backup_<timestamp>/)
  - 동시 실행 방지 lock (storage/_runtime/<bid_id>.lock)
  - 60분 watchdog (초과 시 KeyboardInterrupt → finally 정리)
  - 어느 단계 실패해도 finally에서 누적 비용/산출물 요약 출력

사용:
  python scripts/run_smoke_full.py --bid-id smoke_001
  python scripts/run_smoke_full.py --bid-id smoke_001 --skip-graphics
  python scripts/run_smoke_full.py --bid-id smoke_001 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import traceback
import _thread
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger

from config.settings import (
    KEYS, OUTPUT_DIR, PROPOSALS_DIR, RFP_CACHE_DIR, STORAGE_DIR, SYS, VECTOR,
)
from schemas.models import BidStatus
from tools import db
from tools.db import BidRow, session_scope
from tools.llm_client import _BEDROCK_MODEL_MAP, get_usage_summary, reset_usage


# ---------------------------------------------------------------------------
# 단가 (USD per 1M tokens) — 2026-05 추정
# ---------------------------------------------------------------------------
_ANTHROPIC_RATES = {
    "claude-opus-4-7":           (15.0, 75.0),
    "claude-sonnet-4-6":         (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
# Bedrock 모드는 _BEDROCK_MODEL_MAP에 의해 다운그레이드되므로 실제 단가는 매핑 후 모델 기준
_BEDROCK_RATES = {
    "claude-opus-4-7":           (3.0, 15.0),   # → Sonnet 4.5
    "claude-sonnet-4-6":         (3.0, 15.0),   # → Sonnet 4.5
    "claude-haiku-4-5-20251001": (1.0, 5.0),    # → Haiku 4.5
}

_WATCHDOG_TIMEOUT_SEC = 60 * 60   # 60분
_LOCK_DIR = STORAGE_DIR / "_runtime"
_LOG_DIR = STORAGE_DIR / "logs"


# ---------------------------------------------------------------------------
# Tee logging
# ---------------------------------------------------------------------------
class _Tee:
    def __init__(self, console, file_):
        self._console = console
        self._file = file_

    def write(self, s):
        try:
            self._console.write(s)
        except Exception:
            pass
        try:
            self._file.write(s)
            self._file.flush()
        except Exception:
            pass

    def flush(self):
        for x in (self._console, self._file):
            try:
                x.flush()
            except Exception:
                pass


def _setup_tee_logging(log_path: Path):
    """sys.stdout/stderr를 console+file로 분기. loguru에도 file sink 추가."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(log_path, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, f)
    sys.stderr = _Tee(sys.__stderr__, f)
    logger.add(
        str(log_path), level="DEBUG", enqueue=True,
        format="{time:HH:mm:ss.SSS} | {level:<7} | {name}:{function}:{line} | {message}",
    )
    return f


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------
def _lock_path(bid_id: str) -> Path:
    return _LOCK_DIR / f"{bid_id}.lock"


def _acquire_lock(bid_id: str) -> Optional[Path]:
    p = _lock_path(bid_id)
    if p.exists():
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"pid={os.getpid()}\nstarted={datetime.now().isoformat()}\n",
        encoding="utf-8",
    )
    return p


def _release_lock(p: Optional[Path]) -> None:
    if p is None:
        return
    try:
        p.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"[lock] 해제 실패: {e}")


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
def _backup_existing_artifacts(bid_id: str) -> tuple[int, Optional[Path]]:
    """OUTPUT_DIR / PROPOSALS_DIR / RFP_CACHE_DIR에서 bid_id 매칭 파일 백업."""
    candidates: list[Path] = []
    for d in (OUTPUT_DIR, PROPOSALS_DIR, RFP_CACHE_DIR):
        if d.exists():
            for pat in (f"{bid_id}_*", f"{bid_id}.*"):
                candidates.extend(p for p in d.glob(pat) if p.is_file())
    if not candidates:
        return 0, None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = OUTPUT_DIR / f"_backup_{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for p in candidates:
        # parent dir 이름을 prefix로 충돌 방지 (outputs_xxx.docx, proposals_yyy.json)
        dest = backup_dir / f"{p.parent.name}_{p.name}"
        shutil.move(str(p), str(dest))
    return len(candidates), backup_dir


# ---------------------------------------------------------------------------
# Stage 결과
# ---------------------------------------------------------------------------
class StageResult:
    __slots__ = ("idx", "label", "ok", "started_at", "ended_at", "error", "value", "note")

    def __init__(self, idx: int, label: str):
        self.idx = idx
        self.label = label
        self.ok = False
        self.started_at: Optional[datetime] = None
        self.ended_at: Optional[datetime] = None
        self.error: Optional[str] = None
        self.value: Any = None
        self.note: Optional[str] = None

    def elapsed(self) -> float:
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return 0.0


def _format_duration(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s"
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m}m {s:.0f}s"


def _stage_header(idx: int, total: int, title: str, hint: str = "") -> None:
    print()
    print("=" * 70)
    line = f" [{idx}/{total}] {title}"
    if hint:
        line += f"   ({hint})"
    print(line)
    print("=" * 70)


def run_stage(
    idx: int, total: int, label: str, fn: Callable, hint: str = ""
) -> StageResult:
    """단일 stage 실행. 예외 발생 시 traceback 출력 + StageResult.ok=False로 반환."""
    r = StageResult(idx, label)
    _stage_header(idx, total, label, hint)
    r.started_at = datetime.now()
    try:
        r.value = fn()
        r.ok = True
        r.ended_at = datetime.now()
        print(f"  → 완료 ({_format_duration(r.elapsed())})")
    except KeyboardInterrupt:
        r.ended_at = datetime.now()
        r.error = "KeyboardInterrupt (사용자 중단 또는 watchdog)"
        raise
    except Exception as e:
        r.ended_at = datetime.now()
        r.error = f"{type(e).__name__}: {e}"
        print(f"  ❌ 실패 ({_format_duration(r.elapsed())}): {r.error}")
        traceback.print_exc()
    return r


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
def _bid_status(bid_id: str) -> Optional[str]:
    with session_scope() as s:
        r = s.get(BidRow, bid_id)
        return r.status if r else None


def preflight(bid_id: str) -> dict:
    print(f"  USE_BEDROCK         : {SYS.use_bedrock}")
    print(f"  has_bedrock         : {KEYS.has_bedrock}")
    print(f"  AWS region          : {KEYS.aws_region}")
    if SYS.use_bedrock:
        print(f"  Bedrock 매핑:")
        for k, v in _BEDROCK_MODEL_MAP.items():
            print(f"    {k:32s} → {v}")

    if not SYS.use_bedrock and not KEYS.has_anthropic:
        raise RuntimeError(
            "USE_BEDROCK=false인데 ANTHROPIC_API_KEY도 없음. .env 확인."
        )
    if SYS.use_bedrock and not KEYS.has_bedrock:
        raise RuntimeError(
            "USE_BEDROCK=true인데 AWS 키 누락. AWS_ACCESS_KEY_ID/SECRET/REGION 확인."
        )

    status = _bid_status(bid_id)
    print(f"  bid {bid_id:15s} : status={status or '(존재 X)'}")
    if status is None:
        raise RuntimeError(f"{bid_id} 공고가 DB에 없음 — register_local_rfp.py 먼저")
    if status != BidStatus.COLLECTED.value:
        raise RuntimeError(
            f"{bid_id} 상태가 COLLECTED가 아님 (현재: {status}). "
            f"이전 실행 잔여 가능성. cleanup 명령:\n"
            f"  python -c \"from tools import db; from schemas.models import BidStatus; "
            f"db.set_bid_status('{bid_id}', BidStatus.COLLECTED)\""
        )

    # ChromaDB 청크 수
    n = -1
    try:
        from rag.vector_store import VectorStore
        store = VectorStore(collection_name=VECTOR.collection_rfp)
        n = store.count()
        print(f"  ChromaDB ({VECTOR.collection_rfp}): {n}건")
        if 0 <= n < 230:
            logger.warning(f"청크 수가 기대치(230) 미달: {n}")
    except Exception as e:
        logger.warning(f"ChromaDB 조회 실패 (계속 진행): {e}")

    return {"status": status, "chunks": n}


# ---------------------------------------------------------------------------
# 비용 추정
# ---------------------------------------------------------------------------
def estimate_cost(usage: dict) -> float:
    rates = _BEDROCK_RATES if SYS.use_bedrock else _ANTHROPIC_RATES
    total = 0.0
    for m, u in usage.items():
        r_in, r_out = rates.get(m, (3.0, 15.0))
        total += (u["in"] / 1e6) * r_in
        total += (u["out"] / 1e6) * r_out
        total += (u.get("cache_read", 0) / 1e6) * (r_in * 0.1)    # 캐시 히트는 ~10%
        total += (u.get("cache_create", 0) / 1e6) * (r_in * 1.25)  # 캐시 쓰기는 ~125%
    return total


# ---------------------------------------------------------------------------
# 산출물 list
# ---------------------------------------------------------------------------
def _list_artifacts(bid_id: str) -> list[Path]:
    paths: list[Path] = []
    for d in (OUTPUT_DIR, PROPOSALS_DIR, RFP_CACHE_DIR):
        if d.exists():
            for pat in (f"{bid_id}_*", f"{bid_id}.*"):
                paths.extend(sorted(p for p in d.glob(pat) if p.is_file()))
    return paths


# ---------------------------------------------------------------------------
# 최종 보고
# ---------------------------------------------------------------------------
def print_final_summary(
    started: datetime,
    results: list[StageResult],
    bid_id: str,
    backup_count: int,
    backup_dir: Optional[Path],
    aborted: bool,
) -> None:
    ended = datetime.now()
    total_sec = (ended - started).total_seconds()

    print()
    print("=" * 70)
    print(" 실행 결과 요약")
    print("=" * 70)
    print(f"  bid_id              : {bid_id}")
    print(f"  시작                 : {started.isoformat()}")
    print(f"  종료                 : {ended.isoformat()}")
    print(f"  총 소요               : {_format_duration(total_sec)}")
    print(f"  USE_BEDROCK         : {SYS.use_bedrock}")
    print(f"  중단 발생             : {aborted}")
    if backup_count:
        print(f"  백업                 : {backup_count}개 → {backup_dir}")
    print()
    print("  단계별 결과:")
    for r in results:
        if r.ended_at is None:
            mark = "⏸"
            elapsed = "-"
        else:
            mark = "✅" if r.ok else "❌"
            elapsed = _format_duration(r.elapsed())
        line = f"    {mark} [{r.idx}] {r.label:30s}  {elapsed}"
        if not r.ok and r.error:
            line += f"  ({r.error[:80]})"
        if r.note:
            line += f"  {r.note}"
        print(line)
    print()

    usage = get_usage_summary()
    print("  누적 토큰:")
    if usage:
        print(json.dumps(usage, indent=2, ensure_ascii=False))
        print(
            f"  추정 비용 ({'Bedrock 매핑' if SYS.use_bedrock else 'Anthropic 직결'}): "
            f"${estimate_cost(usage):.4f}"
        )
    else:
        print("    (LLM 호출 0회)")
    print()

    artifacts = _list_artifacts(bid_id)
    print("  산출물:")
    if artifacts:
        for p in artifacts:
            try:
                size_kb = p.stat().st_size / 1024
            except Exception:
                size_kb = 0.0
            print(f"    {str(p):60s}  {size_kb:>8.1f} KB")
    else:
        print("    (없음)")
    print()

    print("  다음 액션:")
    all_ok = (not aborted) and all(r.ok for r in results)
    if all_ok:
        print(f"    explorer.exe {OUTPUT_DIR}")
        print("    또는 산출물 경로 직접 열어 검토")
    else:
        print(f"    cleanup 후 재실행: python scripts/run_smoke_full.py --bid-id {bid_id}")
        print(f"    부분 재시작     : python main.py --mode <stage> --bid {bid_id}")
        print(f"    상태 직접 확인   : python scripts/check_smoke_status.py --bid-id {bid_id}")
    print()


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------
def _start_watchdog(timeout_sec: int) -> threading.Timer:
    def _kick():
        print(f"\n[WATCHDOG] {timeout_sec}초 ({timeout_sec//60}분) 초과 — main thread interrupt")
        try:
            _thread.interrupt_main()
        except Exception:
            pass
    t = threading.Timer(timeout_sec, _kick)
    t.daemon = True
    t.start()
    return t


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="smoke run wrapper")
    ap.add_argument("--bid-id", default="smoke_001")
    ap.add_argument("--skip-graphics", action="store_true",
                    help="graphics 단계 스킵 (비용 절감 테스트용)")
    ap.add_argument("--dry-run", action="store_true",
                    help="실제 LLM 호출 없이 단계 헤더만 출력")
    args = ap.parse_args()

    bid_id = args.bid_id
    started = datetime.now()
    ts = started.strftime("%Y%m%d_%H%M%S")
    log_path = _LOG_DIR / f"smoke_run_{bid_id}_{ts}.log"
    log_file_handle = _setup_tee_logging(log_path)

    print("=" * 70)
    print(f" smoke run wrapper — {bid_id}")
    print(f" 시작: {started.isoformat()}")
    print(f" 로그: {log_path}")
    if args.dry_run:
        print(" [DRY-RUN] 실제 LLM 호출 안 함, 단계 헤더만 출력")
    print("=" * 70)

    # Lock
    lock = _acquire_lock(bid_id)
    if lock is None:
        lp = _lock_path(bid_id)
        print(f"\n[ABORT] {lp} 이미 존재")
        print("  이전 실행이 비정상 종료됐을 수 있음. 강제 진행하려면 .lock 삭제 후 재실행:")
        print(f"  rm '{lp}'")
        log_file_handle.close()
        return 2

    db.init_db()

    results: list[StageResult] = []
    backup_count = 0
    backup_dir: Optional[Path] = None
    aborted = False
    watchdog: Optional[threading.Timer] = None

    try:
        if args.dry_run:
            steps = [
                ("preflight", "환경/DB/ChromaDB 검증"),
                ("analyze + 게이트1 승인", "예상 ~$0.01-0.05"),
                ("strategize", "예상 ~$0.05-0.15"),
                ("write", "예상 ~$0.20-0.40 ⭐"),
                ("review", "예상 ~$0.05-0.10"),
                ("게이트3 자동 승인", "DB 직접 변경"),
                ("graphics" + (" (SKIP)" if args.skip_graphics else ""),
                 "예상 ~$0.05-0.10"),
                ("결과 보고", "토큰/산출물/비용 출력"),
            ]
            total = len(steps) - 1
            for i, (label, hint) in enumerate(steps):
                _stage_header(i, total, label, hint)
                print("  (dry-run — 실제 작업 없음)")
            return 0

        # 백업
        backup_count, backup_dir = _backup_existing_artifacts(bid_id)
        if backup_count:
            print(f"\n기존 산출물 {backup_count}개 백업 → {backup_dir}")

        # 누적 카운터 0부터 시작
        reset_usage()

        # 60분 watchdog
        watchdog = _start_watchdog(_WATCHDOG_TIMEOUT_SEC)

        # 실제 단계
        from workflows.pipeline import (
            run_analysis, run_graphics, run_review, run_strategy, run_write,
        )

        TOTAL = 7

        # [0] preflight
        r0 = run_stage(0, TOTAL, "preflight", lambda: preflight(bid_id),
                       "환경/DB/ChromaDB 검증")
        results.append(r0)
        if not r0.ok:
            return 1

        # [1] analyze + 게이트1 자동 승인
        def _do_analyze():
            evs = run_analysis()
            n_match = sum(1 for e in evs if e.bid_id == bid_id)
            print(f"  평가 {len(evs)}건 (대상 {bid_id}: {n_match}건)")
            if n_match == 0:
                raise RuntimeError(
                    f"analyze에서 {bid_id}가 평가되지 않음 — 상태 확인 필요"
                )
            status_now = _bid_status(bid_id)
            print(f"  {bid_id} 상태 (analyze 후): {status_now}")
            if status_now != BidStatus.AWAITING_APPROVAL.value:
                logger.warning(
                    f"점수 미달로 AWAITING_APPROVAL 미진입 → 강제 승급 (smoke 통과 우선)"
                )
                db.set_bid_status(bid_id, BidStatus.AWAITING_APPROVAL)
            db.set_bid_status(bid_id, BidStatus.APPROVED)
            print(f"  게이트1 자동 승인: {bid_id} → APPROVED")
            return evs
        r1 = run_stage(1, TOTAL, "analyze + 게이트1 승인", _do_analyze,
                       "예상 ~$0.01-0.05, 1-2분")
        results.append(r1)
        if not r1.ok:
            return 1

        # [2] strategize
        def _do_strategy():
            out = run_strategy(bid_id=bid_id)
            print(f"  전략 산출 {len(out)}건")
            if not out:
                raise RuntimeError(
                    "strategize 처리 0건 — pipeline.run_strategy 내부 에러 가능성. "
                    "storage/logs/strategize_failed_*.txt 확인."
                )
            return out
        r2 = run_stage(2, TOTAL, "strategize", _do_strategy,
                       "예상 ~$0.05-0.15, 2-5분")
        results.append(r2)
        if not r2.ok:
            return 1

        # [3] write
        def _do_write():
            out = run_write(bid_id=bid_id)
            print(f"  초안 작성 {len(out)}건")
            if not out:
                raise RuntimeError(
                    "write 처리 0건 — STRATEGY_DONE 공고 없음 또는 writer 내부 에러."
                )
            for d in out:
                print(f"    sections={len(d.sections)}, docx={d.docx_path}")
            return out
        r3 = run_stage(3, TOTAL, "write", _do_write,
                       "예상 ~$0.20-0.40, 5-15분 ⭐ 가장 비싼 단계")
        results.append(r3)
        if not r3.ok:
            return 1

        # [4] review
        def _do_review():
            out = run_review(bid_id=bid_id)
            print(f"  검수 {len(out)}건")
            if not out:
                raise RuntimeError(
                    "review 처리 0건 — DRAFT_DONE 공고 없음 또는 reviewer 내부 에러."
                )
            for q in out:
                print(
                    f"    grade={q.overall_grade}, "
                    f"acceptance_valid={q.acceptance_table_valid}, "
                    f"toc_sim={q.toc_similarity_score:.2f}"
                )
            return out
        r4 = run_stage(4, TOTAL, "review", _do_review,
                       "예상 ~$0.05-0.10, 1-3분")
        results.append(r4)
        if not r4.ok:
            return 1

        # [5] 게이트3 자동 승인
        def _do_gate3():
            db.set_bid_status(bid_id, BidStatus.FINAL_APPROVED)
            print(f"  {bid_id} → FINAL_APPROVED")
            return True
        r5 = run_stage(5, TOTAL, "게이트3 자동 승인", _do_gate3, "DB 직접 변경")
        results.append(r5)
        if not r5.ok:
            return 1

        # [6] graphics (또는 SKIP)
        if args.skip_graphics:
            r6 = StageResult(6, "graphics (SKIP)")
            r6.ok = True
            r6.started_at = r6.ended_at = datetime.now()
            r6.note = "사용자 요청으로 SKIP"
            _stage_header(6, TOTAL, "graphics", "SKIP (--skip-graphics)")
            print("  → SKIP (--skip-graphics)")
            results.append(r6)
        else:
            def _do_graphics():
                out = run_graphics(bid_id=bid_id)
                print(f"  PPTX/스토리보드 {len(out)}건")
                if not out:
                    raise RuntimeError(
                        "graphics 처리 0건 — FINAL_APPROVED/UNDER_REVIEW 공고 없음."
                    )
                return out
            r6 = run_stage(6, TOTAL, "graphics", _do_graphics,
                           "예상 ~$0.05-0.10, 1-3분")
            results.append(r6)
            if not r6.ok:
                return 1

        return 0

    except KeyboardInterrupt:
        print("\n[INTERRUPT] 중단됨 (Ctrl+C 또는 watchdog 60분 초과)")
        aborted = True
        return 130
    except SystemExit:
        raise
    except Exception:
        print("\n[FATAL] 예상치 못한 예외")
        traceback.print_exc()
        aborted = True
        return 1
    finally:
        if watchdog:
            watchdog.cancel()
        try:
            print_final_summary(started, results, bid_id, backup_count, backup_dir, aborted)
        except Exception:
            traceback.print_exc()
        _release_lock(lock)
        try:
            log_file_handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
