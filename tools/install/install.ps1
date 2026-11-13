param([string]$Destination, [switch]$PrepareOnly)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$sourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if (-not $Destination) {
    $Destination = Read-Host 'Install folder (Enter = D:\opt\chat-collector)'
    if (-not $Destination) { $Destination = 'D:\opt\chat-collector' }
}
$Destination = [IO.Path]::GetFullPath($Destination)
if ($Destination -ne $sourceRoot -and (Test-Path -LiteralPath $Destination)) {
    if ((Get-ChildItem -LiteralPath $Destination -Force | Measure-Object).Count -gt 0) {
        throw 'Destination is not empty. Use Configure.cmd in the existing installation, or choose a new folder.'
    }
}
function Fetch-Verified($url, $output, $sha) {
    if ((Test-Path -LiteralPath $output) -and (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLower() -eq $sha) { return }
    $part = "$output.partial"
    if ($python -and (Test-Path -LiteralPath $python)) {
        & $python (Join-Path $Destination 'tools/install/download.py') $url $part
        if ($LASTEXITCODE -ne 0) { throw 'Download failed or timed out. Run Install.cmd again to resume.' }
    } else {
        Invoke-WebRequest -Uri $url -OutFile $part -UseBasicParsing -TimeoutSec 120
    }
    if ((Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash.ToLower() -ne $sha) { throw 'Download checksum mismatch. No downloaded code was executed.' }
    Move-Item -LiteralPath $part -Destination $output -Force
}
function Run-Checked($exe, $arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "Installation step failed (exit $LASTEXITCODE). Run Install.cmd again to resume." }
}
function Set-PythonPaths($directory) {
    $paths = @('python313.zip', '.', (Join-Path $Destination 'runtime-deps'), $Destination, 'import site')
    [IO.File]::WriteAllLines((Join-Path $directory 'python313._pth'), $paths, [Text.UTF8Encoding]::new($false))
}
try {
    $manifestPath = Join-Path $sourceRoot 'release-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) { throw 'Use the release ZIP built by tools/install/build_release.py; source checkout is not an installation bundle.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($file in $manifest.files) {
        $candidate = [IO.Path]::GetFullPath((Join-Path $sourceRoot $file.path))
        if (-not $candidate.StartsWith($sourceRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe manifest path.' }
        if ((Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLower() -ne $file.sha256) { throw "Package file verification failed: $($file.path)" }
    }
    if ($Destination -ne $sourceRoot) {
        New-Item -ItemType Directory -Path $Destination -Force | Out-Null
        foreach ($file in $manifest.files) {
            $out = Join-Path $Destination $file.path
            New-Item -ItemType Directory -Path (Split-Path -Parent $out) -Force | Out-Null
            Copy-Item -LiteralPath (Join-Path $sourceRoot $file.path) -Destination $out
        }
        Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $Destination 'release-manifest.json')
    }
    $runtime = Join-Path $Destination '.runtime'
    $python = Join-Path $runtime 'python/python.exe'
    $runtimeReady = Join-Path $runtime 'ready.txt'
    $lockHash = (Get-FileHash -LiteralPath (Join-Path $Destination 'tools/install/requirements.lock.txt') -Algorithm SHA256).Hash
    if ((Test-Path -LiteralPath $python) -and (Test-Path -LiteralPath $runtimeReady) -and (Get-Content -LiteralPath $runtimeReady -Raw).Trim() -eq $lockHash) {
        Set-PythonPaths (Split-Path -Parent $python)
        Run-Checked $python @((Join-Path $Destination 'tools/install/setup.py'), '--check-runtime')
        if (-not $PrepareOnly) { Run-Checked $python @((Join-Path $Destination 'tools/install/setup.py')) }
        Write-Host 'Existing runtime verified; no dependency reinstall needed.'
        exit 0
    }
    $cache = Join-Path $runtime 'downloads'
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
    $pythonZip = Join-Path $cache 'python.zip'
    Write-Host '[1/4] Preparing private Python 3.13.8 (does not change system Python)...'
    Fetch-Verified 'https://www.python.org/ftp/python/3.13.8/python-3.13.8-embed-amd64.zip' $pythonZip '3de305b550bdc582f7c31a0f286f5b08c453ae5628ef2800a1bb1f86a42b746c'
    $pythonDir = Join-Path $runtime 'python'
    Expand-Archive -LiteralPath $pythonZip -DestinationPath $pythonDir -Force
    Set-PythonPaths $pythonDir
    $python = Join-Path $pythonDir 'python.exe'
    $pip = Join-Path $cache 'pip.pyz'
    Fetch-Verified 'https://bootstrap.pypa.io/pip/pip.pyz' $pip 'be510f6d23d345eeabdd3eec2599a3ca2df3cbc98c4afbe2631561ca12e744d7'
    Write-Host '[2/4] Installing Python dependencies...'
    Run-Checked $python @($pip, 'install', '--disable-pip-version-check', '--only-binary=:all:', '--upgrade', '--no-deps', '--require-hashes', '--target', (Join-Path $Destination 'runtime-deps'), '-r', (Join-Path $Destination 'tools/install/requirements.lock.txt'))
    $wxWheel = Join-Path $cache 'wx_key-2.0.1-cp313-cp313-win_amd64.whl'
    Fetch-Verified 'https://raw.githubusercontent.com/LifeArchiveProject/WeChatDataAnalysis/22583491de87c574d26547e80ecbf9520419237f/tools/key_wheels/wx_key-2.0.1-cp313-cp313-win_amd64.whl' $wxWheel 'f4a78ee8525baf93f712f4603ac9f1e98763c824d3e292060d067926a0f4f8c4'
    Run-Checked $python @($pip, 'install', '--disable-pip-version-check', '--upgrade', '--no-deps', '--target', (Join-Path $Destination 'runtime-deps'), $wxWheel)
    Write-Host '[3/4] Preparing Node and Pi native runtime...'
    $nodeZip = Join-Path $cache 'node.zip'
    Fetch-Verified 'https://nodejs.org/dist/v22.21.1/node-v22.21.1-win-x64.zip' $nodeZip '3c624e9fbe07e3217552ec52a0f84e2bdc2e6ffa7348f3fdfb9fbf8f42e23fcf'
    Expand-Archive -LiteralPath $nodeZip -DestinationPath $cache -Force
    $pi = Join-Path $Destination 'processor/pi-runtime'
    $nodeDir = Join-Path $pi 'node'
    New-Item -ItemType Directory -Path $nodeDir -Force | Out-Null
    Copy-Item -Path (Join-Path $cache 'node-v22.21.1-win-x64/*') -Destination $nodeDir -Recurse -Force
    $env:PATH = "$nodeDir;$env:PATH"
    Push-Location $pi
    try { Run-Checked (Join-Path $nodeDir 'node.exe') @((Join-Path $nodeDir 'node_modules/npm/bin/npm-cli.js'), 'ci', '--no-audit', '--no-fund') }
    finally { Pop-Location }
    Write-Host '[4/4] Checking runtime...'
    Run-Checked $python @((Join-Path $Destination 'tools/install/setup.py'), '--check-runtime')
    $lockHash | Set-Content -LiteralPath $runtimeReady -Encoding ASCII
    if (-not $PrepareOnly) { Run-Checked $python @((Join-Path $Destination 'tools/install/setup.py')) }
    Write-Host "Runtime ready: $Destination. Configure.cmd resumes setup; Start.cmd starts configured services."
} catch {
    Write-Host ('Installation stopped: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
