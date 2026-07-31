<#
.SYNOPSIS
    Point the persistent PR-testing worktree at a pull request.

.DESCRIPTION
    Repoints worktrees/field_admin/pr-testing at a PR's head, merges the
    current integration branch on top, refreshes the gitignored env file and
    reinstalls dependencies.

    The worktree is DETACHED on purpose. A branch checked out in one worktree
    cannot be checked out in another, and the daemon's clone already holds
    `dev` — a detached HEAD sidesteps that entirely and makes the state
    obviously throwaway.

    Only .env.development is copied, landing as .env.local. .env.production is
    deliberately never copied: its DATABASE_URL points at the production
    database.

.EXAMPLE
    .\scripts\use-pr.ps1 -Pr 334
#>
param(
    [Parameter(Mandatory = $true)][int]$Pr,
    [string]$Repo = "Accelevation/field_admin",
    [string]$Base = "dev",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

$Worktree = Join-Path $PSScriptRoot "..\worktrees\field_admin\pr-testing" | Resolve-Path
$EnvSource = "C:\Users\DerekNeely\Work\accelevation\field_admin\.env.development"

if (-not (Test-Path $Worktree)) {
    throw "Test worktree missing at $Worktree. Recreate it with:`n" +
          "  git -C repos\field_admin worktree add --detach ..\..\worktrees\field_admin\pr-testing origin/$Base"
}

Write-Host "==> Resolving PR #$Pr" -ForegroundColor Cyan
$head = (gh pr view $Pr --repo $Repo --json headRefName | ConvertFrom-Json).headRefName
if (-not $head) { throw "Could not resolve the head branch for PR #$Pr" }
Write-Host "    head: $head"

Set-Location $Worktree

Write-Host "==> Fetching" -ForegroundColor Cyan
git fetch origin --prune

Write-Host "==> Checking out PR head (detached)" -ForegroundColor Cyan
git checkout --detach "origin/$head"
if ($LASTEXITCODE -ne 0) { throw "checkout failed" }

Write-Host "==> Merging origin/$Base so you test the post-merge state" -ForegroundColor Cyan
git merge "origin/$Base" --no-edit
if ($LASTEXITCODE -ne 0) {
    Write-Warning "MERGE CONFLICT against $Base. That is a real review finding."
    Write-Warning "Inspect with 'git status', then 'git merge --abort' to back out."
    exit 1
}

Write-Host "==> Refreshing .env.local (from .env.development)" -ForegroundColor Cyan
if (Test-Path $EnvSource) {
    Copy-Item $EnvSource (Join-Path $Worktree ".env.local") -Force
} else {
    Write-Warning "No .env.development at $EnvSource - the dev server may not boot."
}

if (-not $SkipInstall) {
    Write-Host "==> Installing dependencies" -ForegroundColor Cyan
    npm install
    if (Test-Path (Join-Path $Worktree "functions\package.json")) {
        npm --prefix functions install
    }
    if (Test-Path (Join-Path $Worktree "prisma\schema.prisma")) {
        npx prisma generate
    }
}

Write-Host ""
Write-Host "Ready. PR #$Pr merged with $Base at:" -ForegroundColor Green
Write-Host "  $Worktree"
Write-Host ""
# `npm run dev` is hardcoded to `next dev --turbopack -p 9002`, so appending
# another -p would pass the flag twice. Invoke next directly instead.
Write-Host "Start it on a port that will not collide with your main dev server:"
Write-Host "  cd $Worktree" -ForegroundColor Yellow
Write-Host "  npx next dev --turbopack -p 9010" -ForegroundColor Yellow
Write-Host ""
Write-Host "Then follow the 'How to test' section of:"
Write-Host "  gh pr view $Pr --repo $Repo" -ForegroundColor Yellow
