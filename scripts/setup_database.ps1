# One-time PostgreSQL setup for the Stock Inward application.
# Run in PowerShell from the project folder:   .\scripts\setup_database.ps1
# psql will ask for the PostgreSQL 'postgres' superuser password; this script asks for a NEW
# password for the application login 'inward_app'. Nothing is stored except in your local .env.

$ErrorActionPreference = "Stop"
$psql = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\psql.exe" | Sort-Object FullName -Descending | Select-Object -First 1
if (-not $psql) { throw "psql.exe not found under C:\Program Files\PostgreSQL" }

$secure = Read-Host "New password for database login 'inward_app' (min 12 chars)" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
if ($plain.Length -lt 12) { throw "Password too short" }

$sql = @'
SELECT 'CREATE ROLE inward_app LOGIN PASSWORD ' || quote_literal(:'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'inward_app') \gexec
SELECT 'CREATE DATABASE inward OWNER inward_app ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'inward') \gexec
SELECT 'CREATE DATABASE inward_test OWNER inward_app ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'inward_test') \gexec
'@
$sql | & $psql.FullName -U postgres -h localhost -v ON_ERROR_STOP=1 -v "pw=$plain" -f -
if ($LASTEXITCODE -ne 0) { throw "psql failed" }

$envFile = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"
if (-not (Test-Path $envFile)) {
    $escaped = [Uri]::EscapeDataString($plain)
    (Get-Content (Join-Path (Split-Path $PSScriptRoot -Parent) ".env.example")) `
        -replace "inward_app:CHANGE_ME", "inward_app:$escaped" | Set-Content -Encoding utf8 $envFile
    Write-Host "Created .env with the new connection strings."
} else {
    Write-Host ".env already exists - update INWARD_DATABASE_URL / INWARD_TEST_DATABASE_URL yourself if needed."
}
Write-Host "Done. Next:  .\.venv\Scripts\inward.exe migrate"
