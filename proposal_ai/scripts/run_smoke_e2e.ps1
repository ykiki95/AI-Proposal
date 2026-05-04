# run_smoke_e2e.ps1
# 결제 해제 직후 smoke_001 end-to-end 실행 스크립트.
#
# 사용:
#   cd C:\Users\ykiki\Projects\AI-Proposal
#   .\venv\Scripts\Activate.ps1
#   .\proposal_ai\scripts\run_smoke_e2e.ps1 -BidId smoke_001
#
# 옵션:
#   -SkipAnalyze    : analyze 단계 건너뛰기 (이미 완료된 경우)
#   -DryRunFirst    : 실제 호출 전 dry-run 검증 먼저 수행
#   -StopOnError    : 단계별 에러 발생 시 즉시 중단

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string]$BidId,
    [switch]$SkipAnalyze,
    [switch]$DryRunFirst,
    [switch]$StopOnError = $true
)

$ErrorActionPreference = if ($StopOnError) { "Stop" } else { "Continue" }
$env:PYTHONIOENCODING = "utf-8"

# 작업 디렉토리는 proposal_ai/ 로 이동
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectDir = Split-Path -Parent $ScriptDir
Set-Location $ProjectDir

function Write-Step {
    param([string]$Title, [string]$Cmd)
    Write-Host ""
    Write-Host ("=" * 60) -ForegroundColor Cyan
    Write-Host " $Title" -ForegroundColor Cyan
    Write-Host ("=" * 60) -ForegroundColor Cyan
    Write-Host "  실행: $Cmd" -ForegroundColor DarkGray
    Write-Host ""
}

function Test-StepResult {
    param([string]$StepName)
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "❌ [$StepName] 실패 (ExitCode=$LASTEXITCODE)" -ForegroundColor Red
        if ($StopOnError) { exit $LASTEXITCODE }
    } else {
        Write-Host ""
        Write-Host "✅ [$StepName] 완료" -ForegroundColor Green
    }
}

# -----------------------------------------------------------------------------
# 0. 사전 검증
# -----------------------------------------------------------------------------
Write-Step "Step 0: 사전 검증" "환경변수 + .env + bid 상태 확인"

# .env에 ANTHROPIC_API_KEY가 비어있지 않은지 확인
$EnvFile = Join-Path $ProjectDir ".env"
if (-not (Test-Path $EnvFile)) {
    Write-Host "❌ .env 파일 없음: $EnvFile" -ForegroundColor Red
    exit 1
}
$EnvContent = Get-Content $EnvFile -Raw
if ($EnvContent -match 'ANTHROPIC_API_KEY\s*=\s*sk-ant-api') {
    Write-Host "  ✅ ANTHROPIC_API_KEY 설정 확인됨" -ForegroundColor Green
} else {
    Write-Host "❌ .env에 유효한 ANTHROPIC_API_KEY 없음" -ForegroundColor Red
    exit 1
}

python scripts\check_smoke_status.py --bid-id $BidId
Test-StepResult "사전 진단"

# -----------------------------------------------------------------------------
# (선택) Dry-Run 검증
# -----------------------------------------------------------------------------
if ($DryRunFirst) {
    Write-Step "Step 0.5: Dry-Run 검증 (Mock)" "python scripts\dry_run_pipeline.py --bid-id $BidId --stage all"
    python scripts\dry_run_pipeline.py --bid-id $BidId --stage all
    Test-StepResult "Dry-Run"

    Write-Host ""
    $continue = Read-Host "Dry-run 통과. 실제 API 호출로 진행할까요? (y/N)"
    if ($continue -ne "y" -and $continue -ne "Y") {
        Write-Host "사용자 취소" -ForegroundColor Yellow
        exit 0
    }
}

# -----------------------------------------------------------------------------
# 1. analyze (게이트1 자동 승급까지)
# -----------------------------------------------------------------------------
if (-not $SkipAnalyze) {
    Write-Step "Step 1: analyze (예상 비용: $0.05-0.20)" "python main.py --mode analyze"
    python main.py --mode analyze
    Test-StepResult "analyze"
}

# -----------------------------------------------------------------------------
# 2. 게이트1 수동 승인
# -----------------------------------------------------------------------------
Write-Step "Step 2: 게이트1 수동 승인" "python main.py --approve $BidId"
python main.py --approve $BidId
Test-StepResult "게이트1 승인"

# -----------------------------------------------------------------------------
# 3. strategize
# -----------------------------------------------------------------------------
Write-Step "Step 3: strategize (예상 비용: $0.20-0.50)" "python main.py --mode strategize --bid $BidId"
python main.py --mode strategize --bid $BidId
Test-StepResult "strategize"

# -----------------------------------------------------------------------------
# 4. write (가장 비싼 단계)
# -----------------------------------------------------------------------------
Write-Step "Step 4: write (예상 비용: $1.0-2.0, 가장 비싼 단계)" "python main.py --mode write --bid $BidId"
Write-Host "⚠️  이 단계는 5-15분 소요될 수 있습니다. 중단하지 마세요." -ForegroundColor Yellow
python main.py --mode write --bid $BidId
Test-StepResult "write"

# -----------------------------------------------------------------------------
# 5. review
# -----------------------------------------------------------------------------
Write-Step "Step 5: review (예상 비용: $0.10-0.30)" "python main.py --mode review --bid $BidId"
python main.py --mode review --bid $BidId
Test-StepResult "review"

# -----------------------------------------------------------------------------
# 6. 게이트3 수동 승인 (UNDER_REVIEW or FINAL_APPROVED 전환은 dashboard에서)
# -----------------------------------------------------------------------------
Write-Step "Step 6: 상태 확인 (게이트2/3 확인)" "python scripts\check_smoke_status.py --bid-id $BidId"
python scripts\check_smoke_status.py --bid-id $BidId
Test-StepResult "상태 확인"

Write-Host ""
Write-Host "⚠️  게이트3 수동 승인이 필요할 수 있습니다." -ForegroundColor Yellow
Write-Host "   대시보드에서 FINAL_APPROVED로 상태 전환 후 graphics 단계 실행:" -ForegroundColor Yellow
Write-Host "   python main.py --mode graphics --bid $BidId" -ForegroundColor White
Write-Host ""
$proceedGraphics = Read-Host "지금 graphics 단계를 강제로 실행할까요? (UNDER_REVIEW도 처리됨) (y/N)"

# -----------------------------------------------------------------------------
# 7. graphics (선택)
# -----------------------------------------------------------------------------
if ($proceedGraphics -eq "y" -or $proceedGraphics -eq "Y") {
    Write-Step "Step 7: graphics (예상 비용: $0.20-0.50)" "python main.py --mode graphics --bid $BidId"
    python main.py --mode graphics --bid $BidId
    Test-StepResult "graphics"
}

# -----------------------------------------------------------------------------
# 8. 최종 산출물 확인
# -----------------------------------------------------------------------------
Write-Step "Step 8: 최종 산출물 확인" "Get-ChildItem storage\outputs\*$BidId*"
Get-ChildItem (Join-Path $ProjectDir "storage\outputs") -Filter "*$BidId*" | Format-Table Name, Length, LastWriteTime

Write-Host ""
Write-Host ("=" * 60) -ForegroundColor Green
Write-Host " ✅ Smoke E2E 완료" -ForegroundColor Green
Write-Host ("=" * 60) -ForegroundColor Green
Write-Host "  최종 진단:" -ForegroundColor White
python scripts\check_smoke_status.py --bid-id $BidId
