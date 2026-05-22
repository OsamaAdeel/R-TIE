# Pre-warmed Redis image publisher for RTIE.
#
# Captures the current rtie-redis state into a fresh dump.rdb, builds the
# `r-tie-redis-prewarmed` image with it, and pushes to GitHub Container
# Registry (ghcr.io/toheedasghar/r-tie-redis-prewarmed).
#
# Run from any directory; the script resolves paths relative to itself.
#
# Prerequisites:
#   - rtie-redis container is running with the indexed corpus loaded.
#   - `docker login ghcr.io` has succeeded recently with a PAT that has
#     write:packages scope (skip if running with -NoPush).
#
# Usage:
#   .\scripts\publish_redis_image.ps1                # build + push :latest + :YYYYMMDD-HHmm
#   .\scripts\publish_redis_image.ps1 -NoPush        # build only, skip push
#   .\scripts\publish_redis_image.ps1 -Tag v1        # also tag :v1
#
# Maintainer workflow when the corpus changes:
#   1. Re-index locally: `python cli.py index --force` then restart backend.
#   2. Run this script. Teammates pick up the new image on their next
#      `docker compose pull` or `docker compose up -d`.

param(
    [string]$Owner = "toheedasghar",
    [string]$ImageName = "r-tie-redis-prewarmed",
    [string]$Container = "rtie-redis",
    [string]$Tag,
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"

$registry      = "ghcr.io"
$image         = "$registry/$Owner/$ImageName"
$repoRoot      = Split-Path -Parent $PSScriptRoot
$dockerfileDir = Join-Path $repoRoot "deploy/redis"
$dumpFile      = Join-Path $dockerfileDir "dump.rdb"
$timestamp     = (Get-Date).ToString("yyyyMMdd-HHmm")

if (-not (Test-Path (Join-Path $dockerfileDir "Dockerfile"))) {
    throw "Dockerfile not found at $dockerfileDir. Expected at deploy/redis/Dockerfile."
}

Write-Host "==> Verifying $Container is running..." -ForegroundColor Cyan
$state = docker inspect --format '{{.State.Status}}' $Container 2>$null
if ($LASTEXITCODE -ne 0 -or $state -ne "running") {
    throw "Container '$Container' is not running (state=$state). Bring it up with 'docker compose up -d redis' before publishing."
}

Write-Host "==> Triggering BGSAVE on $Container..." -ForegroundColor Cyan
$prevSave = (docker exec $Container redis-cli LASTSAVE).Trim()
docker exec $Container redis-cli BGSAVE | Out-Null
$timeout = 120
$elapsed = 0
while ($true) {
    Start-Sleep -Seconds 2
    $elapsed += 2
    $now = (docker exec $Container redis-cli LASTSAVE).Trim()
    if ($now -ne $prevSave) { break }
    if ($elapsed -ge $timeout) {
        throw "BGSAVE did not complete within ${timeout}s (LASTSAVE unchanged)."
    }
}
Write-Host "    BGSAVE completed (${elapsed}s)."

Write-Host "==> Copying dump.rdb from container to build context..." -ForegroundColor Cyan
docker cp "${Container}:/data/dump.rdb" $dumpFile
if (-not (Test-Path $dumpFile)) {
    throw "docker cp did not produce $dumpFile."
}
$sizeMB = [math]::Round((Get-Item $dumpFile).Length / 1MB, 2)
Write-Host "    dump.rdb captured (${sizeMB} MB)."

Write-Host "==> Building $image (tags: latest, $timestamp$(if ($Tag) { ", $Tag" }))..." -ForegroundColor Cyan
$buildArgs = @("build")
$tags = @("latest", $timestamp)
if ($Tag) { $tags += $Tag }
foreach ($t in $tags) { $buildArgs += @("-t", "${image}:$t") }
$buildArgs += $dockerfileDir
& docker @buildArgs
if ($LASTEXITCODE -ne 0) { throw "docker build failed." }

if ($NoPush) {
    Write-Host ""
    Write-Host "==> Build complete. Push skipped (-NoPush)." -ForegroundColor Yellow
    Write-Host "    Local tags:"
    $tags | ForEach-Object { Write-Host "      ${image}:$_" }
    return
}

Write-Host "==> Pushing to $registry..." -ForegroundColor Cyan
foreach ($t in $tags) {
    & docker push "${image}:$t"
    if ($LASTEXITCODE -ne 0) {
        throw "docker push ${image}:$t failed. If this is the first push, ensure you have run: docker login $registry (PAT scope: write:packages)."
    }
}

Write-Host ""
Write-Host "==> Done." -ForegroundColor Green
Write-Host "    Published tags:"
$tags | ForEach-Object { Write-Host "      ${image}:$_" }
Write-Host ""
Write-Host "    Teammates will pick this up on their next 'docker compose pull' or"
Write-Host "    'docker compose up -d redis' (only fresh volumes get repopulated; existing"
Write-Host "    'rtie_redis_data' volumes keep their current contents)."
