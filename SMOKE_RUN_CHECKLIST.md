# Smoke Run 체크리스트 — 결제 해제 직후 실행

작성일: 2026-05-04
대상: AI-Proposal v2 / smoke_001
상태: Anthropic 결제 활성화 대기 중

---

## 0. 사전 준비 (지금 미리 해둘 것)

### 0-1. dry-run 검증 (선택, 권장)

API 비용 0원으로 코드 동작 확인:

```powershell
cd C:\Users\ykiki\Projects\AI-Proposal\proposal_ai
..\venv\Scripts\Activate.ps1
$env:PYTHONIOENCODING = "utf-8"
python scripts\dry_run_pipeline.py --bid-id smoke_001 --stage all
```

기대 출력:
- "Mock 호출 횟수: N회"
- "✅ 코드 로직 통과"
- 에러 없이 종료

만약 에러 발생 시 → 결제 풀려도 진행 불가. 코드 수정 필요.

### 0-2. 현재 상태 진단

```powershell
python scripts\check_smoke_status.py --bid-id smoke_001
```

기대 출력:
- 공고명, 발주처, 상태(`COLLECTED` 추정) 표시
- ChromaDB 청크: 230건 (이미 인덱싱 완료)
- 다음 단계: `python main.py --mode analyze`

---

## 1. 결제 해제 확인 (이메일 수신 후)

| 항목 | 확인 방법 |
|---|---|
| Anthropic Console 결제 페이지 | https://platform.claude.com/settings/billing 접속, "크레딧 구매" 버튼 활성 여부 |
| 크레딧 USD 5 또는 USD 20 구매 | 가장 작은 금액으로 우선 결제 (smoke 1건 비용은 $1.5~2 추정) |
| 잔액 확인 | 결제 후 Console에서 잔액이 표시되는지 |

⚠️ 결제 후 즉시 다음 단계 진행 가능 (크레딧 즉시 반영됨).

---

## 2. 환경 활성화

```powershell
cd C:\Users\ykiki\Projects\AI-Proposal
.\venv\Scripts\Activate.ps1
$env:PYTHONIOENCODING = "utf-8"
cd proposal_ai
```

`.env` 파일 확인:

```powershell
Select-String -Path .env -Pattern "ANTHROPIC_API_KEY"
```

→ `ANTHROPIC_API_KEY=sk-ant-api03-...` 한 줄이 출력되어야 함.

---

## 3. 일괄 실행 (권장)

자동화 스크립트로 한 번에:

```powershell
.\scripts\run_smoke_e2e.ps1 -BidId smoke_001 -DryRunFirst
```

`-DryRunFirst`는 실제 API 호출 전에 dry-run을 한 번 더 돌려 검증함.

소요 시간: 15-30분 (write 단계가 가장 김)
예상 총 비용: **$1.5~2.5 USD**

---

## 4. 단계별 수동 실행 (자동화 스크립트 미사용 시)

### 4-1. analyze (게이트1 자동 승급)

```powershell
python main.py --mode analyze
```

| 항목 | 값 |
|---|---|
| 모델 | `claude-sonnet-4-6` |
| 예상 비용 | $0.05 ~ $0.20 |
| 예상 시간 | 30초 ~ 1분 |
| 확인 | `smoke_001`이 AWAITING_APPROVAL 상태로 전환됐는지 |

확인 명령:
```powershell
python main.py --list-awaiting
```

### 4-2. 게이트1 수동 승인

```powershell
python main.py --approve smoke_001
```

→ APPROVED로 전환됨.

### 4-3. strategize

```powershell
python main.py --mode strategize --bid smoke_001
```

| 항목 | 값 |
|---|---|
| 모델 | `claude-opus-4-7` |
| 예상 비용 | $0.20 ~ $0.50 |
| 예상 시간 | 1-3분 |
| 확인 | `STRATEGY_DONE` 상태, RfpStructured JSON 생성 |

### 4-4. write ⭐ 가장 비싼 단계

```powershell
python main.py --mode write --bid smoke_001
```

| 항목 | 값 |
|---|---|
| 모델 | `claude-opus-4-7` (writer는 opus) |
| 예상 비용 | **$1.0 ~ $2.0** (출력 8K~15K 토큰) |
| 예상 시간 | 5-15분 (8개 챕터 × 평균 1-2분) |
| 확인 | `DRAFT_DONE` 상태, `storage/outputs/smoke_001_proposal_v1.docx` 생성 |

⚠️ 중간 중단하지 말 것. opus가 챕터별 2,500자 이상 보장 + 부족 시 최대 3회 재시도하는 구조.

### 4-5. review

```powershell
python main.py --mode review --bid smoke_001
```

| 항목 | 값 |
|---|---|
| 모델 | `claude-sonnet-4-6` |
| 예상 비용 | $0.10 ~ $0.30 |
| 예상 시간 | 1-3분 |
| 확인 | `UNDER_REVIEW` 상태, QualityReport 생성, 수용표 검증, TOC 일치율 ≥ 0.90 |

### 4-6. 게이트3 수동 승인 (대시보드 또는 SQLite)

`UNDER_REVIEW` → `FINAL_APPROVED` 전환은 사람이 검수 후 결정.

빠르게 진행하려면 (smoke 검증용):
```powershell
python -c "from tools import db; from schemas.models import BidStatus; db.set_bid_status('smoke_001', BidStatus.FINAL_APPROVED); print('OK')"
```

또는 대시보드 사용:
```powershell
streamlit run dashboard\app.py
```

### 4-7. graphics

```powershell
python main.py --mode graphics --bid smoke_001
```

| 항목 | 값 |
|---|---|
| 모델 | `claude-sonnet-4-6` |
| 예상 비용 | $0.20 ~ $0.50 |
| 예상 시간 | 2-5분 |
| 확인 | `storage/outputs/smoke_001_pt.pptx` 생성 |

---

## 5. 최종 산출물 확인

```powershell
Get-ChildItem storage\outputs\*smoke_001* | Format-Table Name, Length, LastWriteTime
python scripts\check_smoke_status.py --bid-id smoke_001
```

기대 산출물:
- `smoke_001_proposal_v1.docx` (DOCX 제안서)
- `smoke_001_pt.pptx` 또는 `smoke_001_pt_v1.pptx` (PPTX 발표자료)

---

## 6. 비용 합산 (smoke 1건)

| 단계 | 모델 | 추정 비용 |
|---|---|---|
| analyze | sonnet | $0.05~0.20 |
| strategize | opus | $0.20~0.50 |
| **write** | **opus** | **$1.00~2.00** |
| review | sonnet | $0.10~0.30 |
| graphics | sonnet | $0.20~0.50 |
| **합계** | | **$1.55~3.50** |

→ 월 예산 USD 200 (`.env`의 `MONTHLY_BUDGET_USD`) 대비 smoke 1건은 1~2% 수준.

---

## 7. 트러블슈팅

| 증상 | 원인 추정 | 대응 |
|---|---|---|
| `RuntimeError: ANTHROPIC_API_KEY가 설정되지 않았습니다` | .env 미반영 | venv 재활성화 + `Get-Content .env`로 키 확인 |
| `credit balance is too low` | 결제 안 됐거나 잔액 0 | Console에서 잔액 확인 후 충전 |
| write 단계가 1시간 이상 멈춤 | opus 응답 지연 또는 무한 재시도 | Ctrl+C 중단 → 로그 확인 → 챕터별 분할 실행 검토 |
| TOC 유사도 < 0.90 | RFP 목차 추출 실패 | `data/processed/smoke_001*.json` 확인 → 재인덱싱 |
| PPTX 빈 슬라이드 | DRAFT_DONE의 sections JSON 비어있음 | `tools.db`에서 ProposalRow 직접 조회 |

---

## 8. 다음 단계 (smoke 통과 후)

1. 추가 RFP 등록: `.\scripts\register_additional_rfps.ps1`
2. Batch 실행: `python main.py --mode all` (게이트는 수동 승인 필요)
3. 비용 모니터링: Console 사용량 페이지 또는 `MONTHLY_BUDGET_USD` 알림 설정

---

## 9. 만약 결제가 며칠 더 안 풀리면

대안 검토:
- AWS Bedrock의 Claude (`anthropic` SDK 대신 `boto3` 사용)
- 코드 수정 위치: `proposal_ai/tools/llm_client.py:get_anthropic_client()`
- Bedrock에서 미지원: prompt caching (cache_control), Claude Skills 일부

이 경우 별도 가이드 작성 필요.
