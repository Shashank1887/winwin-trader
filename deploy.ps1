param(
    [string]$Message = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Resolve-GitPath {
    $cmd = Get-Command git -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        "C:\Program Files\Git\cmd\git.exe",
        "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }
    return $null
}

$git = Resolve-GitPath
if (-not $git) {
    Write-Host "[ERROR] Git is not installed. Run: winget install --id Git.Git -e --source winget" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path ".git")) {
    Write-Host "[ERROR] This folder is not initialized as a Git repository yet." -ForegroundColor Red
    exit 1
}

# Ensure .env and trades_log.csv are never tracked or committed
$trackedSecrets = & $git ls-files .env trades_log.csv .streamlit/secrets.toml
if ($trackedSecrets) {
    Write-Host "[BLOCKED] Sensitive file is tracked by Git: $trackedSecrets" -ForegroundColor Red
    Write-Host "Remove it from Git index first: git rm --cached .env trades_log.csv" -ForegroundColor Yellow
    exit 1
}

Write-Host "[1/4] Checking Python tests before deploy..." -ForegroundColor Cyan
python -m pytest -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "[BLOCKED] Tests failed! Fix errors before deploying to live." -ForegroundColor Red
    exit 1
}

Write-Host "[2/4] Checking local changes..." -ForegroundColor Cyan
& $git add -A

# Double-check staged files never include .env or trades_log.csv
$staged = & $git diff --cached --name-only
foreach ($f in $staged) {
    if ($f -match '(^\.env$|^trades_log\.csv$|secrets\.toml$)') {
        & $git reset HEAD -- $f | Out-Null
        Write-Host "[BLOCKED] Prevented staging sensitive file: $f" -ForegroundColor Red
        exit 1
    }
}

$hasStaged = ($staged | Measure-Object).Count -gt 0
if ($hasStaged) {
    if ([string]::IsNullOrWhiteSpace($Message)) {
        $Message = "Auto-deploy update $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
    }
    Write-Host "[3/4] Committing changes: $Message" -ForegroundColor Cyan
    & $git commit -m $Message
    if ($LASTEXITCODE -ne 0) { exit 1 }
} else {
    Write-Host "[3/4] No new uncommitted changes; checking unpushed commits..." -ForegroundColor Yellow
}

$remoteUrl = (& $git remote get-url origin 2>$null)
if (-not $remoteUrl) {
    Write-Host "[ERROR] Git remote 'origin' is not configured." -ForegroundColor Red
    exit 1
}

Write-Host "[4/4] Pushing to GitHub ($remoteUrl)..." -ForegroundColor Cyan
& $git push -u origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Push failed. If remote has newer commits, run: git pull --rebase origin main" -ForegroundColor Yellow
    exit 1
}

Write-Host "`n[SUCCESS] Pushed to GitHub! Streamlit Cloud / Render will auto-deploy in ~15-30 seconds." -ForegroundColor Green
