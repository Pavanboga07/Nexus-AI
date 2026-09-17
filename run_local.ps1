# Run AI Nexus locally against the Docker Postgres on port 5433.
#
# Usage:  .\run_local.ps1            # start the API on http://127.0.0.1:8000
#         .\run_local.ps1 -Migrate   # apply migrations first, then start
#
# Why this script exists rather than "just run uvicorn": the checked-in .env
# points DATABASE_URL at a hosted Neon instance. Running the app from a shell
# would therefore write demo data to a remote database - and, since the audit
# found a credential committed in this repository, that database must be treated
# as compromised anyway. The environment variables below override .env for this
# process only (pydantic-settings reads os.environ before the file), so nothing
# in the repository is modified.

param(
    [switch]$Migrate,
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot

# --- Local services -------------------------------------------------------
$env:DATABASE_URL = "postgresql+asyncpg://nexus:nexus@localhost:5433/nexus"
$env:NEXUS_ENV = "development"
$env:NEXUS_HOST = "127.0.0.1"
$env:NEXUS_PORT = "$Port"

# Log as JSON (the M11 default) but keep the level readable locally.
$env:NEXUS_LOG_FORMAT = "text"
$env:NEXUS_LOG_LEVEL = "INFO"

# Local development has no TLS, so a Secure-only cookie would be dropped by the
# browser and every request would come back 401 with no visible reason.
$env:NEXUS_COOKIE_SECURE = "false"

# The gateway in .env points at a hosted relay, which reconnects on a loop and
# floods the log when it is unreachable. Unset it locally so the app uses direct
# egress (D2 is gateway-first with a documented fallback, so this is a supported
# configuration) - set NEXUS_GATEWAY_URL to exercise the relay path.
# pydantic-settings reads os.environ before .env, so the variable must be
# REMOVED here; an empty string is falsy-but-present and does not override.
Remove-Item Env:NEXUS_GATEWAY_URL -ErrorAction SilentlyContinue

Write-Host "AI Nexus - local run" -ForegroundColor Cyan
Write-Host "  database : localhost:5433/nexus (Docker)" -ForegroundColor DarkGray
Write-Host "  api      : http://127.0.0.1:$Port" -ForegroundColor DarkGray
Write-Host "  docs     : http://127.0.0.1:$Port/docs" -ForegroundColor DarkGray
Write-Host "  readiness: http://127.0.0.1:$Port/readyz" -ForegroundColor DarkGray
Write-Host "  metrics  : http://127.0.0.1:$Port/metrics" -ForegroundColor DarkGray
Write-Host ""

Push-Location $repo
try {
    if ($Migrate) {
        Write-Host "Applying migrations..." -ForegroundColor Yellow
        python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) { throw "alembic upgrade failed" }
    }

    # `--no-access-log`: the ASGI access log duplicates what the trace
    # middleware already records through the structured logger, with the trace
    # id missing. One place to look.
    python -m uvicorn app.main:app --host 127.0.0.1 --port $Port --no-access-log
}
finally {
    Pop-Location
}
