param([string]$DominoHome, [switch]$Diagnose, [switch]$Reconfigure)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# Account settings stay on this PC, outside the Git checkout. No keys/tokens.
if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is unavailable.' }
$settingsDir = Join-Path $env:LOCALAPPDATA 'invest'
$settingsPath = Join-Path $settingsDir 'settings.json'
$saved = $null
if ((Test-Path -LiteralPath $settingsPath) -and -not $Reconfigure) {
    try {
        $saved = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw 'Cannot read local invest settings. Run with -Reconfigure to replace them.'
    }
}

if (-not $DominoHome) { $DominoHome = $env:DOMINO_HOME }
if (-not $DominoHome -and $saved) { $DominoHome = [string]$saved.domino_home }
if (-not $DominoHome -and -not $Reconfigure) {
    $candidates = @((Join-Path $env:USERPROFILE 'projects\domino-code'), 'C:\domino')
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $candidate 'kis_config.json')) {
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
if (-not (Test-Path -LiteralPath (Join-Path $DominoHome 'kis_config.json') -PathType Leaf)) {
    throw 'kis_config.json was not found. Specify -DominoHome with the existing data folder.'
}
$DominoHome = (Resolve-Path -LiteralPath $DominoHome).Path

$demoCano = $env:KIS_DEMO_CANO
if ($Reconfigure) { $demoCano = $null }
if (-not $demoCano -and $saved) { $demoCano = [string]$saved.demo_cano }
if (-not $demoCano) {
    $demoCano = Read-Host 'Demo account number (8 digits, no hyphen; saved on this PC)'
}
if ($demoCano -notmatch '^\d{8}$') { throw 'Demo account must contain 8 digits.' }
$demoProduct = $env:KIS_DEMO_PRODUCT
if ($Reconfigure) {
    $demoProduct = Read-Host 'Demo account product code (2 digits; Enter for 01)'
}
if (-not $demoProduct -and $saved) { $demoProduct = [string]$saved.demo_product }
if (-not $demoProduct) { $demoProduct = '01' }
if ($demoProduct -notmatch '^\d{2}$') { throw 'Demo product must contain 2 digits.' }

New-Item -ItemType Directory -Path $settingsDir -Force | Out-Null
$settings = [ordered]@{ domino_home = $DominoHome; demo_cano = $demoCano; demo_product = $demoProduct }
$tempPath = Join-Path $settingsDir ('settings-' + [guid]::NewGuid().ToString('N') + '.tmp')
try {
    [System.IO.File]::WriteAllText($tempPath, ($settings | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tempPath -Destination $settingsPath -Force
} finally {
    if (Test-Path -LiteralPath $tempPath) { Remove-Item -LiteralPath $tempPath -Force }
}
$env:KIS_DEMO_CANO = $demoCano
$env:KIS_DEMO_PRODUCT = $demoProduct
Write-Host 'Using local invest settings (account number is not displayed).'
if ($Diagnose) {
    python (Join-Path $PSScriptRoot 'trader.py') --domino-home $DominoHome diagnose
    exit $LASTEXITCODE
}
Write-Host 'Opening the invest demo dashboard at http://127.0.0.1:8766'
Write-Host 'This app never issues tokens. Restart always begins stopped.'
python (Join-Path $PSScriptRoot 'dashboard.py') --domino-home $DominoHome --state (Join-Path $PSScriptRoot 'runtime\orders.sqlite3')
