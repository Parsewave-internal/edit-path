# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only

[CmdletBinding()]
param(
    [string]$CraftRoot = "C:\CraftRoot",
    [string]$OutputDirectory = "",
    [string]$SigningCertificateThumbprint = $env:EDIT_PATH_SIGNING_CERT_THUMBPRINT,
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [switch]$RequireCodeSigning,
    [switch]$PreflightOnly,
    [switch]$SkipTestMedia
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$utf8NoBom = New-Object System.Text.UTF8Encoding -ArgumentList $false

function Stop-Build([string]$Message) {
    try { Stop-Transcript | Out-Null } catch { }
    throw "EditPath build prerequisite failed: $Message"
}

function Find-SignTool {
    $onPath = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }

    $candidates = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
        Sort-Object { [version]$_.Directory.Parent.Name } -Descending
    if ($candidates) { return $candidates[0].FullName }
    return $null
}

function Find-CodeSigningCertificate([string]$Thumbprint) {
    $normalizedThumbprint = ($Thumbprint -replace '\s', '').ToUpperInvariant()
    $certificate = Get-ChildItem -Path "Cert:\CurrentUser\My", "Cert:\LocalMachine\My" -CodeSigningCert -ErrorAction SilentlyContinue |
        Where-Object { $_.Thumbprint -eq $normalizedThumbprint } |
        Select-Object -First 1
    if (-not $certificate) {
        Stop-Build "code-signing certificate $normalizedThumbprint was not found in the CurrentUser or LocalMachine personal certificate store."
    }
    if (-not $certificate.HasPrivateKey) {
        Stop-Build "code-signing certificate $normalizedThumbprint does not have an accessible private key."
    }
    $now = Get-Date
    if ($certificate.NotBefore -gt $now -or $certificate.NotAfter -le $now) {
        Stop-Build "code-signing certificate $normalizedThumbprint is not currently valid."
    }
    return $certificate
}

function Sign-And-VerifyWindowsBinary([string]$SignTool, [System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate, [string]$Path) {
    Write-Host "Authenticode signing $Path"
    & $SignTool sign /sha1 $Certificate.Thumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 /v $Path
    if ($LASTEXITCODE -ne 0) { Stop-Build "Authenticode signing failed for $Path." }

    & $SignTool verify /pa /all /v $Path
    if ($LASTEXITCODE -ne 0) { Stop-Build "Authenticode verification failed for $Path." }
    $signature = Get-AuthenticodeSignature $Path
    if ($signature.Status -ne "Valid") {
        Stop-Build "Windows reported Authenticode status '$($signature.Status)' for $Path."
    }
}

if (-not [Environment]::Is64BitOperatingSystem) {
    Stop-Build "64-bit Windows 10 or 11 is required."
}

$sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not (Test-Path (Join-Path $sourceRoot "CMakeLists.txt"))) {
    Stop-Build "run this script from a complete EditPath repository checkout."
}
# WSL can launch PowerShell with a \\wsl.localhost UNC working directory.
# Craft invokes cmd.exe while importing the MSVC environment, and cmd.exe does
# not support UNC current directories. Always enter the native source path.
Set-Location $sourceRoot
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
    Stop-Build "download Visual Studio 2022 Build Tools from https://aka.ms/vs/17/release/vs_BuildTools.exe and select 'Desktop development with C++'."
}
$visualStudio = & $vswhere -latest -products * -version "[17.0,18.0)" -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $visualStudio) {
    Stop-Build "download Visual Studio 2022 Build Tools from https://aka.ms/vs/17/release/vs_BuildTools.exe and select 'Desktop development with C++'. Visual Studio 2026 is not supported by Craft yet."
}

$signToolPath = $null
$signingCertificate = $null
if ($SigningCertificateThumbprint) {
    $signToolPath = Find-SignTool
    if (-not $signToolPath) {
        Stop-Build "signtool.exe is required for code signing; install the Windows 10 or 11 SDK."
    }
    $signingCertificate = Find-CodeSigningCertificate $SigningCertificateThumbprint
    Write-Host "Code signing: $($signingCertificate.Subject) ($($signingCertificate.Thumbprint))"
} elseif ($RequireCodeSigning) {
    Stop-Build "-RequireCodeSigning was requested, but -SigningCertificateThumbprint (or EDIT_PATH_SIGNING_CERT_THUMBPRINT) was not provided."
} else {
    Write-Host "Code signing: disabled (engineering build only)"
}

$conflictingToolDirectories = @()
foreach ($toolName in @("sh.exe", "gcc.exe", "g++.exe", "cpp.exe")) {
    $tool = Get-Command $toolName -ErrorAction SilentlyContinue
    if ($tool) {
        $conflictingToolDirectories += (Split-Path $tool.Source -Parent).TrimEnd('\')
    }
}
$conflictingToolDirectories = $conflictingToolDirectories | Sort-Object -Unique
if ($conflictingToolDirectories) {
    $env:Path = (($env:Path -split ';') | Where-Object {
        $_ -and $conflictingToolDirectories -notcontains $_.TrimEnd('\')
    }) -join ';'
    Write-Host "Ignoring incompatible build tools in PATH: $($conflictingToolDirectories -join ', ')"
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
    private static extern uint SetThreadExecutionState(uint flags);

    public static uint PreventSleep() {
        return SetThreadExecutionState(0x80000001u);
    }

    public static uint RestoreDefaults() {
        return SetThreadExecutionState(0x80000000u);
    }
}
'@
[EditPathPower]::PreventSleep() | Out-Null

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

# Windows PowerShell 5's `Set-Content -Encoding UTF8` writes a BOM. Python's
# configparser does not accept that BOM before the first INI section header, so
# normalize the file before craftenv.ps1 imports Craft.
$settings = Join-Path $CraftRoot "etc\CraftSettings.ini"
$settingsText = Get-Content $settings -Raw
if ($settingsText.Contains('#PackageType = SevenZipPackager')) {
    $settingsText = $settingsText.Replace('#PackageType = SevenZipPackager', 'PackageType = PortablePackager')
} elseif ($settingsText.Contains('PackageType = SevenZipPackager')) {
    $settingsText = $settingsText.Replace('PackageType = SevenZipPackager', 'PackageType = PortablePackager')
} elseif (-not $settingsText.Contains('PackageType = PortablePackager')) {
    Stop-Build "the Craft package type setting changed; update this script before building."
}
[IO.File]::WriteAllText($settings, $settingsText, $utf8NoBom)

# Craft's environment script invokes Python and older Craft releases emit
# informational diagnostics on stderr. PowerShell 7 turns native stderr into
# ErrorRecords under `Stop`, which aborts before Craft has initialized. Keep
# strict failure handling for our checks, but do not treat that bootstrap
# chatter as a build failure.
$previousErrorActionPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"
    . $craftEnvironment
} finally {
    $ErrorActionPreference = $previousErrorActionPreference
}

$craftPython = $env:CRAFT_PYTHON
$craftScript = Join-Path (Split-Path $craftEnvironment -Parent) "bin\craft.py"
if (-not $craftPython -or -not (Test-Path $craftPython) -or -not (Test-Path $craftScript)) {
    Stop-Build "the Craft Python launcher is incomplete."
}

# Do not continue if Craft ignored the local source override and resolved its
# own cached Kdenlive checkout.  That produces a valid-looking package whose
# EditPath supervisor predates the source tree being tested.
$craftSource = (& $craftPython $craftScript -q --ci-mode --options "kde/kdemultimedia/kdenlive.srcDir=$sourceRoot" --get "sourceDir()" "kde/kdemultimedia/kdenlive" | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $craftSource) {
    Stop-Build "could not query Craft's resolved Kdenlive source directory."
}
$resolvedCraftSource = [IO.Path]::GetFullPath($craftSource).TrimEnd('\')
$resolvedLocalSource = [IO.Path]::GetFullPath($sourceRoot).TrimEnd('\')
if (-not [String]::Equals($resolvedCraftSource, $resolvedLocalSource, [StringComparison]::OrdinalIgnoreCase)) {
    Stop-Build "Craft resolved Kdenlive source to '$resolvedCraftSource' instead of the requested local checkout '$resolvedLocalSource'."
}
Write-Host "Craft source verified: $resolvedCraftSource"

$blueprint = Get-ChildItem $CraftRoot -Recurse -Filter kdenlive.py |
    Where-Object { $_.FullName -match 'craft-blueprints-kde.*kdenlive' } |
    Select-Object -First 1
if (-not $blueprint) {
    Stop-Build "the Kdenlive Craft blueprint was not found."
}
$blueprintText = Get-Content $blueprint.FullName -Raw
$oldFilter = 'bin/(?!(ff|kdenlive|kioworker|melt|update-mime-database|snoretoast|drmingw|data/kdenlive)).*'
$editPathOnlyFilter = 'bin/(?!(ff|kdenlive|EditPath|kioworker|melt|update-mime-database|snoretoast|drmingw|data/kdenlive)).*'
$newFilter = 'bin/(?!(ff|kdenlive|EditPath|kioworker|melt|update-mime-database|snoretoast|drmingw|data/kdenlive|video-path-pilot)).*'
if ($blueprintText.Contains($oldFilter)) {
    $blueprintText = $blueprintText.Replace($oldFilter, $newFilter)
    [IO.File]::WriteAllText($blueprint.FullName, $blueprintText, $utf8NoBom)
} elseif ($blueprintText.Contains($editPathOnlyFilter)) {
    $blueprintText = $blueprintText.Replace($editPathOnlyFilter, $newFilter)
    [IO.File]::WriteAllText($blueprint.FullName, $blueprintText, $utf8NoBom)
} else {
    # Craft periodically reformats this regex (line wrapping, escaping, or
    # additional retained runtime binaries).  Requiring one exact string
    # makes otherwise compatible Craft releases fail before the build starts.
    # Validate the safety properties instead: this must be a bin exclusion
    # filter and it must retain the executables/data EditPath needs.
    $hasBinFilter = $blueprintText -match 'bin/\(\?!'
    $requiredFilterEntries = @('ff', 'kdenlive', 'EditPath', 'kioworker', 'melt', 'data/kdenlive', 'video-path-pilot')
    $missingFilterEntries = @($requiredFilterEntries | Where-Object { $blueprintText -notmatch [regex]::Escape($_) })
    if (-not $hasBinFilter -or $missingFilterEntries.Count -gt 0) {
        $missing = if ($missingFilterEntries.Count) { $missingFilterEntries -join ', ' } else { 'bin exclusion filter' }
        Stop-Build "the Craft Kdenlive executable filter is incompatible; missing: $missing"
    }
}

Write-Host "Building EditPath and all required Kdenlive dependencies..."
# This repository supplies a local Kdenlive/EditPath source tree.  A Craft
# binary cache can satisfy the package with an older EditPath.exe even though
# the local source contains newer supervisor UI.  Dependencies remain cached
# by Craft, but the direct application target must always be compiled from the
# source checkout passed above.
& $craftPython $craftScript --ci-mode --no-cache --ignoreInstalled --options "kde/kdemultimedia/kdenlive.srcDir=$sourceRoot" kde/kdemultimedia/kdenlive
if ($LASTEXITCODE -ne 0) { Stop-Build "Craft compilation failed." }

Write-Host "Creating dependency-complete portable package..."
& $craftPython $craftScript --ci-mode --no-cache --ignoreInstalled --options "kde/kdemultimedia/kdenlive.srcDir=$sourceRoot" --package kde/kdemultimedia/kdenlive
if ($LASTEXITCODE -ne 0) { Stop-Build "Craft packaging failed." }

$archive = Get-ChildItem $CraftRoot -Recurse -File -Filter '*kdenlive*.7z' |
    Where-Object { $_.Name -notmatch '(-dbg|-logs|debug|symbols|src)' } |
    Sort-Object LastWriteTimeUtc -Descending |
    Select-Object -First 1
if (-not $archive) { Stop-Build "Craft did not produce a Kdenlive 7z package." }

$sevenZipPath = $null
foreach ($commandName in @("7z.exe", "7za.exe")) {
    $sevenZip = Get-Command $commandName -ErrorAction SilentlyContinue
    if ($sevenZip) {
        $sevenZipPath = $sevenZip.Source
        break
    }
}
if (-not $sevenZipPath) {
    foreach ($sevenZipCandidate in @(
        (Join-Path $CraftRoot "bin\7z.exe"),
        (Join-Path $CraftRoot "bin\7za.exe"),
        (Join-Path $CraftRoot "dev-utils\bin\7za.exe")
    )) {
        if (Test-Path $sevenZipCandidate) {
            $sevenZipPath = $sevenZipCandidate
            break
        }
    }
}
if (-not $sevenZipPath) { Stop-Build "7z.exe or 7za.exe was not found after Craft packaging." }

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
$audioHelper = Get-ChildItem $portable -Recurse -File -Filter EditPathAudio.exe | Select-Object -First 1
$kdenlive = Get-ChildItem $portable -Recurse -File -Filter kdenlive.exe | Select-Object -First 1
if (-not $editPath) { Stop-Build "EditPath.exe is missing from the portable package." }
if (-not $audioHelper) { Stop-Build "EditPathAudio.exe is missing from the portable package." }
if (-not $kdenlive) { Stop-Build "kdenlive.exe is missing from the portable package." }
if ($editPath.Directory.FullName -ne $kdenlive.Directory.FullName -or $editPath.Directory.FullName -ne $audioHelper.Directory.FullName) {
    Stop-Build "EditPath.exe, EditPathAudio.exe, and kdenlive.exe were not packaged together."
}

# Guard against a successful-looking Craft package that silently contains an
# executable from an earlier cache/image.  QStringLiteral text is embedded as
# UTF-16; decode both byte alignments so this check is independent of the PE
# section offset.
$editPathBytes = [IO.File]::ReadAllBytes($editPath.FullName)
$editPathTextEven = [Text.Encoding]::Unicode.GetString($editPathBytes)
$editPathTextOdd = if ($editPathBytes.Length -gt 1) {
    [Text.Encoding]::Unicode.GetString($editPathBytes, 1, $editPathBytes.Length - 1)
} else {
    ""
}
$kdenliveBytes = [IO.File]::ReadAllBytes($kdenlive.FullName)
$kdenliveTextEven = [Text.Encoding]::Unicode.GetString($kdenliveBytes)
$kdenliveTextOdd = if ($kdenliveBytes.Length -gt 1) {
    [Text.Encoding]::Unicode.GetString($kdenliveBytes, 1, $kdenliveBytes.Length - 1)
} else {
    ""
}
$sourceRevision = (& git -C $sourceRoot rev-parse --short HEAD).Trim()
if (-not $sourceRevision -or
    (-not $kdenliveTextEven.Contains($sourceRevision) -and -not $kdenliveTextOdd.Contains($sourceRevision))) {
    Stop-Build "the packaged kdenlive.exe does not contain this checkout's source revision ($sourceRevision); refusing a stale recorder binary."
}
foreach ($featureMarker in @(
    "Configure microphone",
    "Test microphone and play it back",
    "Stop Reasoning (recording"
)) {
    if (-not $editPathTextEven.Contains($featureMarker) -and -not $editPathTextOdd.Contains($featureMarker)) {
        Stop-Build "the packaged EditPath.exe is stale; required recorder UI marker is missing: $featureMarker"
    }
}
# These strings live in the instrumented editor, not the supervisor. Checking
# kdenlive.exe prevents a cached pre-PR8 recorder from passing merely because
# an independently fresh supervisor has the current button labels.
foreach ($featureMarker in @(
    "effect.parameter.change",
    "keyframe.value.change",
    "command_registered",
    "generated."
)) {
    if (-not $kdenliveTextEven.Contains($featureMarker) -and -not $kdenliveTextOdd.Contains($featureMarker)) {
        Stop-Build "the packaged kdenlive.exe is stale; required recorder taxonomy marker is missing: $featureMarker"
    }
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

$signedFiles = @()
if ($signingCertificate) {
    foreach ($binary in @($editPath, $audioHelper, $kdenlive)) {
        Sign-And-VerifyWindowsBinary $signToolPath $signingCertificate $binary.FullName
        $signedFiles += $binary.FullName.Substring($portable.Length + 1)
    }
}

$selfTestReport = Join-Path $portable "SELF-TEST.json"
$env:EDIT_PATH_SELF_TEST_REPORT = $selfTestReport
$savedPath = $env:PATH
$env:PATH = "$bin;$pythonDirectory;$env:SystemRoot\System32"
$selfTestProcess = Start-Process -FilePath $editPath.FullName -ArgumentList '--self-test' -Wait -PassThru -NoNewWindow
$selfTestExitCode = $selfTestProcess.ExitCode
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
$portablePrefix = [IO.Path]::GetFullPath($portable).TrimEnd('\') + '\'
foreach ($checkName in @('application_root', 'audio_helper', 'edit_path_module', 'ffmpeg', 'ffprobe', 'kdenlive', 'melt', 'pipeline', 'python', 'qt_multimedia_qml', 'validator')) {
    $checkPath = [string]$selfTest.checks.$checkName.path
    if (-not $checkPath -or -not [IO.Path]::GetFullPath($checkPath).StartsWith($portablePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        $env:PATH = $savedPath
        Stop-Build "the packaged runtime self-test resolved '$checkName' outside the portable bundle: $checkPath"
    }
}
$embeddedPython = Join-Path $pythonDirectory "python.exe"
$savedPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = "$bin;$sourceRoot"
Push-Location $portable
try {
    $importedEditPath = (& $embeddedPython -c "import edit_path; print(edit_path.__file__)" | Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0 -or -not [IO.Path]::GetFullPath($importedEditPath).StartsWith($portablePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Build "embedded Python imported edit_path outside the portable bundle: $importedEditPath"
    }
    # Verbose unittest writes progress/status to stderr on Windows. Do not let
    # PowerShell promote that normal diagnostic stream into a terminating error.
    $testPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $embeddedPython -m unittest -v tests.edit_path.test_reconstruction_pipeline.MediaIntegrationTests.test_real_checkpoint_and_final_ssim_pipeline
    } finally {
        $ErrorActionPreference = $testPreference
    }
    $mediaTestExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
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
Copy-Item (Join-Path $sourceRoot "packaging\windows\dependency-installer.ps1") (Join-Path $portable "dependency-installer.ps1")
Copy-Item (Join-Path $sourceRoot "packaging\windows\DependencyInstaller.cs") (Join-Path $portable "DependencyInstaller.cs")
Copy-Item (Join-Path $sourceRoot "packaging\windows\install-dependencies.bat") (Join-Path $portable "install-dependencies.bat")
$csc = Get-ChildItem "${env:ProgramFiles}\Microsoft Visual Studio", "${env:ProgramFiles(x86)}\Microsoft Visual Studio" -Recurse -Filter csc.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $csc) { Stop-Build "C# compiler csc.exe is required to build dependency-installer.exe." }
$dependencyInstaller = Join-Path $portable "dependency-installer.exe"
& $csc.FullName /nologo /target:exe /out:$dependencyInstaller (Join-Path $portable "DependencyInstaller.cs")
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $dependencyInstaller)) { Stop-Build "could not compile dependency-installer.exe." }
Remove-Item (Join-Path $portable "DependencyInstaller.cs") -Force
if (-not (Test-Path $dependencyInstaller)) { Stop-Build "dependency-installer.exe is missing from the portable bundle." }
@"
EditPath first-run dependency setup

Double-click install-dependencies.bat (internet required for Whisper/model download).
It launches the bundled dependency-installer.exe and does not change the
machine or user PowerShell execution policy.

Command-line alternative:
.\dependency-installer.exe -Model turbo -InstallRoot `"$env:LOCALAPPDATA\EditPath`"

Then start .\bin\EditPath.exe
"@ | Set-Content (Join-Path $portable "INSTALL-DEPENDENCIES.txt") -Encoding UTF8

$outputZip = Join-Path $OutputDirectory "EditPath-Windows-x64.zip"
if (Test-Path $outputZip) {
    Move-Item $outputZip "$outputZip.previous.$(Get-Date -Format 'yyyyMMdd-HHmmss')"
}
Compress-Archive -Path (Join-Path $portable '*') -DestinationPath $outputZip -CompressionLevel Optimal
$archiveSha256 = (Get-FileHash $outputZip -Algorithm SHA256).Hash.ToLowerInvariant()
$checksumFile = "$outputZip.sha256"
"$archiveSha256  $([IO.Path]::GetFileName($outputZip))" | Set-Content $checksumFile -Encoding ASCII

$manifest = [ordered]@{
    built_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    source_commit = (& git -C $sourceRoot rev-parse HEAD).Trim()
    archive = $outputZip
    editpath_exe = $editPath.FullName.Substring($portable.Length + 1)
    audio_helper_exe = $audioHelper.FullName.Substring($portable.Length + 1)
    kdenlive_exe = $kdenlive.FullName.Substring($portable.Length + 1)
    test_media_included = -not $SkipTestMedia
    code_signed = [bool]$signingCertificate
    signed_files = $signedFiles
    signer_subject = if ($signingCertificate) { $signingCertificate.Subject } else { $null }
    signer_thumbprint = if ($signingCertificate) { $signingCertificate.Thumbprint } else { $null }
    signer_certificate_expires_utc = if ($signingCertificate) { $signingCertificate.NotAfter.ToUniversalTime().ToString("o") } else { $null }
    timestamp_url = if ($signingCertificate) { $TimestampUrl } else { $null }
    archive_sha256 = $archiveSha256
    checksum_file = $checksumFile
}
$manifest | ConvertTo-Json | Set-Content (Join-Path $OutputDirectory "build-manifest.json") -Encoding UTF8

Write-Host ""
Write-Host "BUILD COMPLETE" -ForegroundColor Green
Write-Host "Portable folder: $portable"
Write-Host "Shareable ZIP:    $outputZip"
Write-Host "SHA-256 file:     $checksumFile"
Write-Host "Start executable: $($editPath.FullName)"
[EditPathPower]::RestoreDefaults() | Out-Null
Stop-Transcript | Out-Null
