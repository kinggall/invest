param([string]$DominoHome, [switch]$Diagnose)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not $DominoHome) { $DominoHome = $env:DOMINO_HOME }
if (-not $DominoHome) {
    $candidates = @((Join-Path $env:USERPROFILE 'projects\domino-code'), 'C:\domino')
    foreach ($candidate in $candidates) {
        if (Test-Path (Join-Path $candidate 'kis_config.json')) {
            $DominoHome = $candidate
            break
        }
    }
}
if (-not $DominoHome) {
    Write-Host 'Enter the existing Domino DATA folder containing kis_config.json.'
    Write-Host 'Do not enter an API key or a token.'
    $DominoHome = Read-Host 'Domino data folder'
}
if (-not (Test-Path (Join-Path $DominoHome 'kis_config.json'))) {
    throw 'kis_config.json was not found. Specify the existing Domino data folder.'
}
if (-not $env:KIS_DEMO_CANO) {
    $env:KIS_DEMO_CANO = Read-Host 'Demo account number (8 digits, no hyphen)'
}
if ($env:KIS_DEMO_CANO -notmatch '^\d{8}$') { throw 'Demo account must contain 8 digits.' }
if (-not $env:KIS_DEMO_PRODUCT) { $env:KIS_DEMO_PRODUCT = '01' }
if ($Diagnose) {
    python (Join-Path $PSScriptRoot 'trader.py') --domino-home $DominoHome diagnose
    exit $LASTEXITCODE
}
Write-Host 'Opening the invest demo dashboard at http://127.0.0.1:8766'
Write-Host 'This app never issues tokens. Restart always begins stopped.'
python (Join-Path $PSScriptRoot 'dashboard.py') --domino-home $DominoHome --state (Join-Path $PSScriptRoot 'runtime\orders.sqlite3')
