# AI-Proposal v2 코드 점검 리포트

작성일: 2026-05-04
점검 범위: main.py, pipeline.py, human_gates.py, settings.py, llm_client.py, register_local_rfp.py, check_status.py, specialists.yaml, .env

---

## 종합 평가

전반적으로 잘 구조화된 프로젝트. 6-에이전트 분리, 게이트 1~4 체계, RAG 분리, 모델 차등 할당, Notion/Slack 옵셔널 통합이 깔끔하게 구현됨. 다만 batch 운영 시 비용 폭주 위험과 운영 편의성 측면에서 몇 가지 보완 권장.

---

## 발견 사항

### 🔴 중요 — 즉시 검토 권장

#### 1. `pipeline.run_write()` batch 비용 폭주 위험

**위치**: `workflows/pipeline.py:91-109`

**문제**: `bid_id`를 지정하지 않으면 `STRATEGY_DONE` 상태의 모든 공고가 한 번에 처리됨. 각 건당 $1.0~2.0 비용. N=10건이면 $10~20 즉시 발생. `.env`의 `MONTHLY_BUDGET_USD=200` 안전장치가 코드에 반영되어 있지 않음.

**현재 코드**:
```python
def run_write(bid_id=None, version=1, progress_cb=None):
    bids = db.list_bids(status=BidStatus.STRATEGY_DONE)
    if bid_id:
        bids = [b for b in bids if b.bid_id == bid_id]
    # ... 전체 bids 순회 호출
```

**권장 수정**:
```python
from config.settings import SYS

def run_write(bid_id=None, version=1, progress_cb=None, max_bids=None):
    bids = db.list_bids(status=BidStatus.STRATEGY_DONE)
    if bid_id:
        bids = [b for b in bids if b.bid_id == bid_id]
    elif max_bids:
        bids = bids[:max_bids]
    elif len(bids) > 5:
        # 명시적 max_bids 없이 5건 초과 시 안전장치
        logger.warning(
            f"[writer] {len(bids)}건이 대기 중. 비용 폭주 방지를 위해 max_bids=5로 제한. "
            f"전체 처리하려면 max_bids=N을 명시하세요."
        )
        bids = bids[:5]
    # ... 이후 동일
```

`run_review`, `run_graphics`도 동일 패턴 적용 권장 (writer만큼 비싸진 않지만 일관성).

---

#### 2. `llm_client.chat()`에 비용 누적 추적 없음

**위치**: `tools/llm_client.py:35-90`

**문제**: 호출별 입출력 토큰을 logger에 찍지만 누적 합계가 없음. 한 번의 batch 실행에서 총 비용을 알 방법이 호출 끝난 뒤 Console에서만 가능.

**권장 수정**: 모듈 레벨 카운터 추가

```python
# llm_client.py 상단
from threading import Lock
from collections import defaultdict

_USAGE_LOCK = Lock()
_USAGE_TRACKER = defaultdict(lambda: {"in": 0, "out": 0, "cache_read": 0, "cache_create": 0, "calls": 0})

def get_usage_summary() -> dict:
    """현재 프로세스의 누적 토큰 사용량을 반환."""
    with _USAGE_LOCK:
        return {k: dict(v) for k, v in _USAGE_TRACKER.items()}

def reset_usage():
    with _USAGE_LOCK:
        _USAGE_TRACKER.clear()
```

`chat()` 내부에서:
```python
usage = getattr(msg, "usage", None)
if usage is not None:
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    with _USAGE_LOCK:
        u = _USAGE_TRACKER[model]
        u["in"] += usage.input_tokens
        u["out"] += usage.output_tokens
        u["cache_read"] += cache_read
        u["cache_create"] += cache_create
        u["calls"] += 1
    # ... 기존 로깅
```

각 단계 종료 시 `print(get_usage_summary())`로 비용 확인 가능.

---

### 🟡 보통 — 운영 편의성

#### 3. `human_gates.manual_approve()` 후 진행 흐름 불명확

**위치**: `workflows/human_gates.py:51-58`

**문제**: 수동 승인 후 strategize 실행을 사용자가 별도로 입력해야 함. 게이트 흐름이 익숙하지 않은 운영자(우진님 본인 또는 팀원)가 헷갈릴 수 있음.

**권장 수정**: `manual_approve` 시그니처에 `auto_continue` 옵션 추가

```python
def manual_approve(bid_id: str, auto_continue: bool = False) -> bool:
    # ... 기존 승인 로직
    if auto_continue:
        from workflows.pipeline import run_strategy
        logger.info(f"[gate-1] auto-continue: strategize {bid_id}")
        run_strategy(bid_id=bid_id)
    return True
```

`main.py`에서:
```python
if args.approve:
    auto = args.continue_after_approve  # 새 플래그
    ok = manual_approve(args.approve, auto_continue=auto)
```

---

#### 4. `register_local_rfp.py`가 중복 등록 시 silent overwrite

**위치**: `scripts/register_local_rfp.py:50-67`

**문제**: 같은 `bid_id`로 두 번 실행하면 ChromaDB에 청크가 중복 누적될 가능성. `db.upsert_bid`는 dedup하지만 ChromaDB는 별도 처리 필요.

**확인 필요**: `DiscoveryAgent._enrich_and_index([bid])`가 중복 검사를 하는지. 안 하면 청크 2배가 됨.

**권장 수정**: 등록 전 ChromaDB에 해당 bid_id 청크가 있는지 검사:

```python
from rag.vector_store import VectorStore
store = VectorStore(collection_name=VECTOR.collection_rfp)
existing = store.collection.get(where={"bid_id": args.bid_id}, limit=1)
if existing.get("ids"):
    print(f"[WARN] {args.bid_id} 청크 이미 존재. 재인덱싱하려면 먼저 삭제하세요.", file=sys.stderr)
    if not args.force:
        return 2
    # --force 옵션 추가 필요
```

---

#### 5. `.env.example`이 업로드되지 않음

**확인 필요**: `_env.example` 파일을 받았으나 본문 내용 미확인. 다음 항목들이 모두 example에 있는지 확인 필요:

- `ANTHROPIC_API_KEY`
- `VOYAGE_API_KEY`
- `MODEL_DISCOVERY`, `MODEL_ANALYSIS`, `MODEL_RFP_STRUCT`, `MODEL_STRATEGY`, `MODEL_WRITER`, `MODEL_REVIEWER`, `MODEL_GRAPHICS`
- `COMPANY_NAME`, `COMPANY_CEO`, `COMPANY_BIZ_NUM`, `COMPANY_TECH_STACK`, `COMPANY_TEAM_SIZE`
- `FIT_SCORE_THRESHOLD`, `TOC_SIMILARITY_THRESHOLD`, `TARGET_PAGES_MIN`, `TARGET_PAGES_MAX`
- `MONTHLY_BUDGET_USD`
- `G2B_SERVICE_KEY`, `NOTION_API_KEY`, `NOTION_DB_BIDS`, `SLACK_WEBHOOK_URL`

없으면 새 환경 셋업 시 누락 위험.

---

### 🟢 사소 — 정리 권장

#### 6. `scripts/` 폴더에 로그 파일 누적

`b2_step1_register.log`, `b2_step2_analyze.log`, `ingest_b1*.log` 등 로그가 코드와 같은 폴더에 쌓임.

**권장**: `storage/logs/` 폴더로 분리. `.gitignore`에 `*.log` 추가.

---

#### 7. `chroma_db_broken_20260430` 폴더 잔존

`data/chroma_db_broken_20260430/`이 그대로 남아있음. 4월 30일 이전 손상된 DB로 추정. 백업 목적이면 그대로 두되, 정리 시 외장으로 옮기길 권장 (용량 절약).

---

#### 8. `storage/outputs/`에 v1 시절 산출물 잔존

`R26BK01458535_proposal_v1.docx` 등 v1 시절 R26BK*로 시작하는 산출물 8개 존재. v2 검증 시 헷갈릴 수 있음.

**권장**: `storage/outputs/_archive_v1/`로 이동.

---

## 강점 (잘 된 부분)

1. ✅ **모델 매핑 명확**: settings.py의 `ModelConfig` 주석으로 각 에이전트가 어떤 모델을 쓰는지 한눈에 파악 가능
2. ✅ **prompt caching 지원**: `llm_client.chat(cache_system=True)` 옵션으로 비용 절감 가능 (현재 활용도는 호출자 측 확인 필요)
3. ✅ **specialists.yaml 외부화**: v1의 하드코딩 dict를 yaml로 분리해 비개발자도 수정 가능
4. ✅ **Notion fallback**: Notion 미설정 환경에서도 SQLite + CLI로 게이트 운영 가능
5. ✅ **3-단계 챕터 길이 보장**: writer가 챕터별 2,500자 미달 시 최대 3회 재시도하는 구조 (메모리에서 확인됨)
6. ✅ **상태 기반 워크플로우**: `BidStatus` enum으로 단계별 진행 추적 가능, 중단 후 재개 용이

---

## 권장 작업 순서

결제 풀린 직후:

1. **dry-run 검증**: `python scripts/dry_run_pipeline.py --bid-id smoke_001 --stage all` (비용 0)
2. **smoke E2E**: `.\scripts\run_smoke_e2e.ps1 -BidId smoke_001 -DryRunFirst` (비용 ~$2)
3. **결과 검증**: 산출물 PPTX/DOCX를 사람이 직접 검토 → 품질 평가
4. **이슈 1~4 수정** (선택, 시간 여유 있을 때): batch 비용 안전장치, 사용량 추적, manual_approve auto-continue, register 중복 검사
5. **추가 RFP batch 등록**: `.\scripts\register_additional_rfps.ps1 -CsvPath rfp_list.csv`
6. **batch 실행**: `python main.py --mode all --max-bids 3` (이슈 1 수정 후)

---

## 추정 vs 사실 구분

| 항목 | 구분 |
|---|---|
| Anthropic 모델 단가 | **추정** (2026-05 시점, 실제 가격은 https://platform.claude.com/docs/en/about-claude/pricing 에서 확인) |
| smoke 1건 비용 $1.5~3.5 | **추정** (writer가 8챕터 × 2,500자+ 출력 가정) |
| dry-run 통과 시 실제 호출 안전 | **부분적으로만 사실** (mock은 응답 형식만 흉내, 응답 파싱 로직의 깊은 검증은 못 함) |
| 6-에이전트 구조의 우수성 | **내 판단** (전반적으로 잘 짜였다는 평가) |
| 이슈 1~4의 우선순위 | **내 판단** (비즈니스 영향 기준 정렬) |
