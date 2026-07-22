# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only

[CmdletBinding()]
param(
    [string]$CraftRoot = "C:\CraftRoot",
    [string]$OutputDirectory = "",
    [switch]$PreflightOnly,
    [switch]$SkipTestMedia
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Stop-Build([string]$Message) {
    try { Stop-Transcript | Out-Null } catch { }
    throw "EditPath build prerequisite failed: $Message"
}

if (-not [Environment]::Is64BitOperatingSystem) {
    Stop-Build "64-bit Windows 10 or 11 is required."
}

$sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not (Test-Path (Join-Path $sourceRoot "CMakeLists.txt"))) {
    Stop-Build "run this script from a complete EditPath repository checkout."
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $sourceRoot "windows-output"
}
New-Item -ItemType Directory -Force $OutputDirectory | Out-Null
$buildLog = Join-Path $OutputDirectory "windows-build.log"
Start-Transcript -Path $buildLog -Append | Out-Null

$sourceDriveName = (Split-Path -Qualifier $sourceRoot).TrimEnd('\').TrimEnd(':')
$sourceDrive = Get-PSDrive -Name $sourceDriveName
if ($sourceDrive.Free -lt 40GB) {
    Stop-Build "at least 40 GB free space is required on $($sourceDrive.Name): (available: $([math]::Round($sourceDrive.Free / 1GB, 1)) GB)."
}

$git = Get-Command git.exe -ErrorAction SilentlyContinue
if (-not $git) {
    Stop-Build "Git for Windows is required: https://git-scm.com/download/win"
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) {
    Stop-Build "64-bit Python 3.11 or newer is required: https://www.python.org/downloads/windows/"
}
$pythonVersion = & $python.Source -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
if ([version]$pythonVersion -lt [version]"3.11") {
    Stop-Build "Python 3.11 or newer is required; found $pythonVersion."
}
$python64Bit = & $python.Source -c "import sys; print(sys.maxsize > 2**32)"
if ($python64Bit -ne "True") {
    Stop-Build "the installed Python must be 64-bit."
}

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) {
    Stop-Build "Visual Studio 2022 Build Tools with 'Desktop development with C++' is required: https://visualstudio.microsoft.com/downloads/"
}
$visualStudio = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $visualStudio) {
    Stop-Build "install the Visual Studio 2022 'Desktop development with C++' workload."
}

if ($PreflightOnly) {
    Write-Host "PREFLIGHT PASSED" -ForegroundColor Green
    Write-Host "Windows, disk space, Git, 64-bit Python, and Visual Studio C++ tools are ready."
    Write-Host "Next command: .\packaging\windows\build-editpath.ps1"
    Stop-Transcript | Out-Null
    return
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class EditPathPower {
    [DllImport("kernel32.dll")]
    public static extern uint SetThreadExecutionState(uint flags);
}
'@
[EditPathPower]::SetThreadExecutionState(0x80000001) | Out-Null

Write-Host "Source: $sourceRoot"
Write-Host "Craft:  $CraftRoot"
Write-Host "Output: $OutputDirectory"
Write-Host "The first build can take several hours. Do not close this window."

$craftEnvironment = Join-Path $CraftRoot "craft\craftenv.ps1"
if (-not (Test-Path $craftEnvironment)) {
    if (Test-Path $CraftRoot) {
        $existing = Get-ChildItem $CraftRoot -Force -ErrorAction SilentlyContinue
        if ($existing) {
            Stop-Build "$CraftRoot exists but is not a valid Craft installation. Rename it or choose -CraftRoot with an empty path."
        }
    }
    New-Item -ItemType Directory -Force $CraftRoot | Out-Null
    $bootstrap = Join-Path $env:TEMP "install_craft.ps1"
    Invoke-WebRequest https://raw.githubusercontent.com/KDE/craft/master/setup/install_craft.ps1 -OutFile $bootstrap
    & $bootstrap -root $CraftRoot -python $python.Source -use-defaults
}
if (-not (Test-Path $craftEnvironment)) {
    Stop-Build "Craft bootstrap did not create $craftEnvironment."
}

. $craftEnvironment

$blueprint = Get-ChildItem $CraftRoot -Recurse -Filter kdenlive.py |
    Where-Object { $_.FullName -match 'craft-blueprints-kde.*kdenlive' } |
    Select-Object -First 1
if (-not $blueprint) {
    Stop-Build "the Kdenlive Craft blueprint was not found."
}
$blueprintText = Get-Content $blueprint.FullName -Raw
$oldFilter = 'bin/(?!(ff|kdenlive|kioworker|melt|update-mime-database|snoretoast|drmingw|data/kdenlive)).*'
$newFilter = 'bin/(?!(ff|kdenlive|EditPath|kioworker|melt|update-mime-database|snoretoast|drmingw|data/kdenlive)).*'
if ($blueprintText.Contains($oldFilter)) {
    $blueprintText = $blueprintText.Replace($oldFilter, $newFilter)
    Set-Content $blueprint.FullName $blueprintText -Encoding UTF8
} elseif (-not $blueprintText.Contains($newFilter)) {
    Stop-Build "the Craft Kdenlive executable filter changed; update this script before building."
}

Write-Host "Building EditPath and all required Kdenlive dependencies..."
craft --ci-mode --src-dir $sourceRoot kde/kdemultimedia/kdenlive
if ($LASTEXITCODE -ne 0) { Stop-Build "Craft compilation failed." }

$settings = Join-Path $CraftRoot "etc\CraftSettings.ini"
$settingsText = Get-Content $settings -Raw
if ($settingsText.Contains('#PackageType = SevenZipPackager')) {
    $settingsText = $settingsText.Replace('#PackageType = SevenZipPackager', 'PackageType = SevenZipPackager')
    Set-Content $settings $settingsText -Encoding UTF8
}

Write-Host "Creating dependency-complete portable package..."
craft --ci-mode --src-dir $sourceRoot --package kde/kdemultimedia/kdenlive
if ($LASTEXITCODE -ne 0) { Stop-Build "Craft packaging failed." }

$archive = Get-ChildItem $CraftRoot -Recurse -File -Filter '*kdenlive*.7z' |
    Where-Object { $_.Name -notmatch '(debug|symbols|src)' } |
    Sort-Object LastWriteTimeUtc -Descending |
    Select-Object -First 1
if (-not $archive) { Stop-Build "Craft did not produce a Kdenlive 7z package." }

$sevenZip = Get-Command 7z.exe -ErrorAction SilentlyContinue
if (-not $sevenZip) {
    $sevenZipCandidate = Join-Path $CraftRoot "bin\7z.exe"
    if (Test-Path $sevenZipCandidate) { $sevenZip = Get-Item $sevenZipCandidate }
}
if (-not $sevenZip) { Stop-Build "7z.exe was not found after Craft packaging." }
$sevenZipPath = if ($sevenZip -is [IO.FileInfo]) { $sevenZip.FullName } else { $sevenZip.Source }

$portable = Join-Path $OutputDirectory "EditPath-Windows-x64"
if (Test-Path $portable) {
    $backup = "$portable.previous.$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Move-Item $portable $backup
    Write-Host "Previous output preserved at $backup"
}
New-Item -ItemType Directory -Force $portable | Out-Null
& $sevenZipPath x $archive.FullName "-o$portable" -y
if ($LASTEXITCODE -ne 0) { Stop-Build "could not extract the Craft package." }

$editPath = Get-ChildItem $portable -Recurse -File -Filter EditPath.exe | Select-Object -First 1
$kdenlive = Get-ChildItem $portable -Recurse -File -Filter kdenlive.exe | Select-Object -First 1
if (-not $editPath) { Stop-Build "EditPath.exe is missing from the portable package." }
if (-not $kdenlive) { Stop-Build "kdenlive.exe is missing from the portable package." }
if ($editPath.Directory.FullName -ne $kdenlive.Directory.FullName) {
    Stop-Build "EditPath.exe and kdenlive.exe were not packaged together."
}

$bin = $editPath.Directory.FullName
$pythonZip = Join-Path $env:TEMP "python-3.11.9-embed-amd64.zip"
if (-not (Test-Path $pythonZip)) {
    Invoke-WebRequest https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip -OutFile $pythonZip
}
$pythonDirectory = Join-Path $bin "python"
New-Item -ItemType Directory -Force $pythonDirectory | Out-Null
Expand-Archive $pythonZip $pythonDirectory -Force
Remove-Item (Join-Path $pythonDirectory "python311._pth") -ErrorAction SilentlyContinue
$sitePackages = Join-Path $pythonDirectory "Lib\site-packages"
New-Item -ItemType Directory -Force $sitePackages | Out-Null
& $python.Source -m pip install --disable-pip-version-check --no-deps --target $sitePackages "zstandard==0.23.0"
if ($LASTEXITCODE -ne 0) { Stop-Build "could not install the pinned zstandard runtime into embedded Python." }

$packagedEditPath = Join-Path $bin "edit_path"
if (-not (Test-Path (Join-Path $packagedEditPath "__main__.py"))) {
    Stop-Build "the edit_path reconstruction package is missing from the portable build."
}

$selfTestReport = Join-Path $portable "SELF-TEST.json"
$env:EDIT_PATH_SELF_TEST_REPORT = $selfTestReport
$savedPath = $env:PATH
$env:PATH = "$bin;$pythonDirectory;$env:SystemRoot\System32"
& $editPath.FullName --self-test
$selfTestExitCode = $LASTEXITCODE
Remove-Item Env:\EDIT_PATH_SELF_TEST_REPORT -ErrorAction SilentlyContinue
if ($selfTestExitCode -ne 0 -or -not (Test-Path $selfTestReport)) {
    $env:PATH = $savedPath
    Stop-Build "the packaged EditPath runtime self-test failed."
}
$selfTest = Get-Content $selfTestReport -Raw | ConvertFrom-Json
if (-not $selfTest.passed) {
    $env:PATH = $savedPath
    Stop-Build "the packaged runtime reported a failed dependency check."
}

$embeddedPython = Join-Path $pythonDirectory "python.exe"
$savedPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = "$bin;$sourceRoot"
& $embeddedPython -m unittest -v tests.edit_path.test_reconstruction_pipeline.MediaIntegrationTests.test_real_checkpoint_and_final_ssim_pipeline
$mediaTestExitCode = $LASTEXITCODE
if ($null -eq $savedPythonPath) {
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
} else {
    $env:PYTHONPATH = $savedPythonPath
}
$env:PATH = $savedPath
if ($mediaTestExitCode -ne 0) { Stop-Build "the packaged real-media reconstruction test failed." }

& $kdenlive.FullName --version
if ($LASTEXITCODE -ne 0) { Stop-Build "kdenlive.exe could not start for its version check." }

if (-not $SkipTestMedia) {
    $ffmpeg = Get-ChildItem $portable -Recurse -File -Filter ffmpeg.exe | Select-Object -First 1
    if (-not $ffmpeg) { Stop-Build "ffmpeg.exe is missing; synthetic test media cannot be generated." }
    $testMedia = Join-Path $portable "test-media"
    New-Item -ItemType Directory -Force $testMedia | Out-Null
    & $ffmpeg.FullName -hide_banner -loglevel error -y -f lavfi -i "testsrc2=size=1280x720:rate=30:duration=8" `
        -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=8" -c:v mpeg4 -q:v 4 -c:a aac -shortest `
        (Join-Path $testMedia "test-video-1.mp4")
    if ($LASTEXITCODE -ne 0) { Stop-Build "failed to generate test-video-1.mp4." }
    & $ffmpeg.FullName -hide_banner -loglevel error -y -f lavfi -i "smptebars=size=1280x720:rate=30:duration=8" `
        -f lavfi -i "sine=frequency=660:sample_rate=48000:duration=8" -c:v mpeg4 -q:v 4 -c:a aac -shortest `
        (Join-Path $testMedia "test-video-2.mp4")
    if ($LASTEXITCODE -ne 0) { Stop-Build "failed to generate test-video-2.mp4." }
    & $ffmpeg.FullName -hide_banner -loglevel error -y -f lavfi -i "sine=frequency=220:sample_rate=48000:duration=12" `
        -c:a pcm_s16le (Join-Path $testMedia "test-audio.wav")
    if ($LASTEXITCODE -ne 0) { Stop-Build "failed to generate test-audio.wav." }
    $generatedMedia = Get-ChildItem $testMedia -File
    if ($generatedMedia.Count -ne 3 -or ($generatedMedia | Where-Object Length -eq 0)) {
        Stop-Build "synthetic test-media verification failed."
    }
}

@"
EditPath portable MVP

START: bin\EditPath.exe
DO NOT start bin\kdenlive.exe directly; that bypasses recording.
Sessions: %USERPROFILE%\Videos\EditPathSessions
Test instructions: WINDOWS_TEST_PLAN.md in the source repository.
"@ | Set-Content (Join-Path $portable "START-HERE.txt") -Encoding UTF8
Copy-Item (Join-Path $sourceRoot "WINDOWS_TEST_PLAN.md") (Join-Path $portable "WINDOWS_TEST_PLAN.md")

$outputZip = Join-Path $OutputDirectory "EditPath-Windows-x64.zip"
if (Test-Path $outputZip) {
    Move-Item $outputZip "$outputZip.previous.$(Get-Date -Format 'yyyyMMdd-HHmmss')"
}
Compress-Archive -Path (Join-Path $portable '*') -DestinationPath $outputZip -CompressionLevel Optimal

$manifest = [ordered]@{
    built_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    source_commit = (& git -C $sourceRoot rev-parse HEAD).Trim()
    archive = $outputZip
    editpath_exe = $editPath.FullName.Substring($portable.Length + 1)
    kdenlive_exe = $kdenlive.FullName.Substring($portable.Length + 1)
    test_media_included = -not $SkipTestMedia
}
$manifest | ConvertTo-Json | Set-Content (Join-Path $OutputDirectory "build-manifest.json") -Encoding UTF8

Write-Host ""
Write-Host "BUILD COMPLETE" -ForegroundColor Green
Write-Host "Portable folder: $portable"
Write-Host "Shareable ZIP:    $outputZip"
Write-Host "Start executable: $($editPath.FullName)"
[EditPathPower]::SetThreadExecutionState(0x80000000) | Out-Null
Stop-Transcript | Out-Null
