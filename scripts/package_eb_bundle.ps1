#requires -Version 5.1
<#
.SYNOPSIS
  AWS Elastic Beanstalk 소스 번들을 만든다 (도현 파트: 트레이딩 + 회계·포트폴리오 + BFF).

.DESCRIPTION
  EB Docker 플랫폼은 **번들 루트의 `docker-compose.yml`** 을 실행한다. 저장소 루트의
  그 이름은 이미 재일님 소유 로컬 개발 스택이 쓰고 있어 같은 자리에 둘 수 없다.
  그래서 `deploy/eb/docker-compose.yml`을 번들 루트로 옮겨 담는 조립 단계가 필요하다.

  build context가 `.`(번들 루트)와 `./departments/02-trading`이라 Git이 추적하는 저장소
  내용이 번들에 들어가야 한다. 작업 디렉터리의 `.env`, 시장 데이터, 캐시는 절대 포함하지
  않는다. 이미지를 미리 만들어 ECR에 올리는 방식이 아니다(레지스트리·인증·태깅이
  늘어난다. Paper 단계에서는 인스턴스에서 빌드하는 쪽이 움직이는 부품이 적다).

.EXAMPLE
  pwsh scripts/package_eb_bundle.ps1
  eb deploy --staged   # 또는 AWS 콘솔에 dist/eb-bundle.zip 업로드
#>
param(
    [string]$OutFile = "dist/eb-bundle.zip"
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$ebDir = Join-Path $repo "deploy/eb"
$out = if ([System.IO.Path]::IsPathRooted($OutFile)) { $OutFile } else { Join-Path $repo $OutFile }

if (-not (Test-Path (Join-Path $ebDir "docker-compose.yml"))) {
    throw "deploy/eb/docker-compose.yml 이 없습니다"
}

# Git 추적 파일만 후보로 삼은 뒤 번들에서 더 뺄 것. 이 allowlist 경계가 로컬
# `.env`와 quant-data를 AWS 소스 번들로 유출하지 않는 핵심 통제다. ai-office(프론트)는
# BFF가 서빙하지 않고, apps/api/Dockerfile이 `COPY . .` 라 그대로 두면 이미지에
# 통째로 들어간다.
#
# ▶ 임시 디렉터리에 복사한 뒤 압축하지 않고 zip을 직접 조립한다. robocopy가 PATH에
#   없는 셸이 있고(System32 미포함), 저장소 전체를 한 번 더 복사할 이유도 없다.
$excludeDirs = @(
    ".git", ".venv", "node_modules", "ai-office", "test-results",
    ".gjc", "graphify-out", "dist", "__pycache__", ".pytest_cache"
)

function Test-Excluded([string]$relative) {
    foreach ($part in $relative.Split([char]'/')) {
        if ($excludeDirs -contains $part) { return $true }
    }
    return $false
}

$outDir = Split-Path -Parent $out
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
if (Test-Path $out) { Remove-Item $out -Force }

# 둘 다 필요하다 - ZipFile/ZipFileExtensions는 .FileSystem 에, ZipArchiveMode는
# System.IO.Compression 에 있다.
Add-Type -AssemblyName System.IO.Compression.FileSystem
Add-Type -AssemblyName System.IO.Compression
$archive = [System.IO.Compression.ZipFile]::Open($out, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    $added = 0
    $trackedFiles = @(& git -C $repo -c core.quotepath=false ls-files --cached)
    if ($LASTEXITCODE -ne 0) {
        throw "Git 추적 파일 목록을 읽지 못했습니다"
    }
    foreach ($trackedPath in $trackedFiles) {
        if ([string]::IsNullOrWhiteSpace($trackedPath)) { continue }
        $rel = $trackedPath.Replace('\', '/')
        if (Test-Excluded $rel) { continue }
        # 로컬 스택 파일은 번들에 넣지 않는다. 남겨두면 어느 쪽이 배포된 것인지
        # 번들만 보고는 알 수 없다 - 아래에서 EB용을 그 이름으로 넣는다.
        if ($rel -eq "docker-compose.yml") { continue }
        $fullPath = Join-Path $repo $trackedPath
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
            throw "Git 추적 파일이 작업 트리에 없습니다: $rel"
        }
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $archive, $fullPath, $rel) | Out-Null
        $added++
    }

    # EB가 읽는 자리로 넣는다.
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $archive, (Join-Path $ebDir "docker-compose.yml"), "docker-compose.yml") | Out-Null
    foreach ($cfg in Get-ChildItem -LiteralPath (Join-Path $ebDir ".ebextensions") -File -Force) {
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $archive, $cfg.FullName, ".ebextensions/$($cfg.Name)") | Out-Null
    }
    Write-Host "packed $added files + EB overlay"
}
finally { $archive.Dispose() }

# 조립이 맞았는지 zip 자체를 열어 확인한다. 이 스크립트가 조용히 틀리면 EB가
# "docker-compose.yml 없음"으로 배포를 거절하고, 그때는 원인이 여기라는 걸 알기 어렵다.
$zip = [System.IO.Compression.ZipFile]::OpenRead($out)
try {
    $names = $zip.Entries | ForEach-Object { $_.FullName }
    if ($names -contains ".env" -or
        ($names | Where-Object { $_ -like "quant-data/*" }).Count -gt 0) {
        throw "로컬 비밀 또는 런타임 시장 데이터가 번들에 들어갔습니다"
    }
    foreach ($required in @("docker-compose.yml", ".ebextensions/01_health.config",
                            "departments/02-trading/Dockerfile",
                            "departments/05-accounting-portfolio/Dockerfile",
                            "apps/api/Dockerfile", "requirements.txt")) {
        if ($names -notcontains $required) { throw "번들에 $required 가 없습니다" }
    }
    $composeEntry = $zip.GetEntry("docker-compose.yml")
    $reader = New-Object System.IO.StreamReader($composeEntry.Open())
    try { $compose = $reader.ReadToEnd() } finally { $reader.Dispose() }
    if ($compose -notmatch "trading-outbox-relay") {
        throw "번들 루트의 docker-compose.yml 이 EB용이 아니라 로컬 스택 파일입니다"
    }
    # 주석은 뺀다 - EB compose 상단이 "루트 스택은 ${USERPROFILE} 볼륨을 써서 Linux에서
    # 못 뜬다"고 설명하고 있어서, 파일 전체를 훑으면 그 설명이 걸린다.
    $active = ($compose -split "`n" | Where-Object { $_ -notmatch '^\s*#' }) -join "`n"
    if ($active -match "USERPROFILE") {
        throw "Windows 전용 볼륨 경로가 번들에 들어갔습니다"
    }
    if ($active -match "host\.docker\.internal") {
        throw "호스트 프록시 의존이 번들에 들어갔습니다 - EC2에는 그 프록시가 없습니다"
    }
}
finally { $zip.Dispose() }

$sizeMb = [Math]::Round((Get-Item $out).Length / 1MB, 1)
Write-Host "ok - $out ($sizeMb MB)"
Write-Host "다음: eb setenv DATABASE_URL=... 후 eb deploy (deploy/eb/README.md 3절)"
