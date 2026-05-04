# register_additional_rfps.ps1
# 추가 RFP를 batch로 등록하는 헬퍼.
# smoke_001 통과 후, 실제 사업 RFP를 여러 건 등록할 때 사용.
#
# 사용 1: CSV 파일로 일괄 등록
#   .\scripts\register_additional_rfps.ps1 -CsvPath .\rfp_list.csv
#
# 사용 2: 단일 RFP 등록 (대화형)
#   .\scripts\register_additional_rfps.ps1
#
# CSV 형식 (UTF-8, 헤더 포함):
#   bid_id,pdf_path,agency,title,deadline,budget,duration
#   rfp_001,C:\path\rfp1.pdf,행정안전부,소통24 구축,2026-06-30,800900000,12
#   rfp_002,C:\path\rfp2.pdf,한국테크노파크진흥회,SME 클러스터,2026-07-15,310000000,8

[CmdletBinding()]
param(
    [string]$CsvPath
)

$env:PYTHONIOENCODING = "utf-8"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectDir = Split-Path -Parent $ScriptDir
Set-Location $ProjectDir

function Register-OneRfp {
    param(
        [string]$BidId,
        [string]$PdfPath,
        [string]$Agency,
        [string]$Title,
        [string]$Deadline,
        [string]$Budget,
        [string]$Duration
    )
    Write-Host ""
    Write-Host "→ 등록: [$BidId] $Title" -ForegroundColor Cyan

    $args = @(
        "scripts\register_local_rfp.py",
        "--bid-id", $BidId,
        "--pdf-path", $PdfPath,
        "--agency", $Agency,
        "--title", $Title,
        "--deadline", $Deadline
    )
    if ($Budget) { $args += @("--budget", $Budget) }
    if ($Duration) { $args += @("--duration", $Duration) }

    python @args
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ✅ $BidId 등록 완료" -ForegroundColor Green
        return $true
    } else {
        Write-Host "  ❌ $BidId 등록 실패" -ForegroundColor Red
        return $false
    }
}

# -----------------------------------------------------------------------------
# CSV 모드
# -----------------------------------------------------------------------------
if ($CsvPath) {
    if (-not (Test-Path $CsvPath)) {
        Write-Host "❌ CSV 파일 없음: $CsvPath" -ForegroundColor Red
        exit 1
    }

    $rfps = Import-Csv -Path $CsvPath -Encoding UTF8
    $total = $rfps.Count
    $success = 0
    $failed = @()

    Write-Host ""
    Write-Host "=" * 60 -ForegroundColor Cyan
    Write-Host " Batch 등록 시작: $total건" -ForegroundColor Cyan
    Write-Host "=" * 60 -ForegroundColor Cyan

    foreach ($rfp in $rfps) {
        $ok = Register-OneRfp `
            -BidId $rfp.bid_id `
            -PdfPath $rfp.pdf_path `
            -Agency $rfp.agency `
            -Title $rfp.title `
            -Deadline $rfp.deadline `
            -Budget $rfp.budget `
            -Duration $rfp.duration
        if ($ok) {
            $success++
        } else {
            $failed += $rfp.bid_id
        }
    }

    Write-Host ""
    Write-Host "=" * 60 -ForegroundColor Cyan
    Write-Host " 완료: 성공 $success / $total" -ForegroundColor $(if ($success -eq $total) { "Green" } else { "Yellow" })
    if ($failed.Count -gt 0) {
        Write-Host " 실패 목록: $($failed -join ', ')" -ForegroundColor Red
    }
    Write-Host "=" * 60 -ForegroundColor Cyan
    exit 0
}

# -----------------------------------------------------------------------------
# 대화형 모드 (단일)
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "단일 RFP 등록 (대화형)" -ForegroundColor Cyan
Write-Host "-" * 40
$BidId    = Read-Host "bid_id (예: rfp_001)"
$PdfPath  = Read-Host "PDF 절대 경로"
$Agency   = Read-Host "발주기관명"
$Title    = Read-Host "사업명"
$Deadline = Read-Host "마감일 (YYYY-MM-DD)"
$Budget   = Read-Host "예산 (원, 선택)"
$Duration = Read-Host "기간 (개월, 선택)"

$ok = Register-OneRfp `
    -BidId $BidId -PdfPath $PdfPath -Agency $Agency `
    -Title $Title -Deadline $Deadline -Budget $Budget -Duration $Duration

if ($ok) { exit 0 } else { exit 1 }
