[CmdletBinding()]
param(
  [ValidateSet('turbo','small','medium')][string]$Model = 'turbo',
  [string]$InstallRoot = "$env:LOCALAPPDATA\EditPath",
  [string]$ReportPath = "",
  [switch]$Offline
)
$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest
if (-not $ReportPath) { $ReportPath = Join-Path $InstallRoot 'dependency-install-report.json' }
$checks = [ordered]@{}
$logPath = Join-Path (Split-Path $ReportPath -Parent) 'dependency-install.log'
function Log([string]$message) {
  $line = "[$([DateTime]::Now.ToString('s'))] $message"
  Write-Host $line
  Add-Content -Path $logPath -Value $line -Encoding UTF8
}
function Check($name, [scriptblock]$action, [int]$attempts=3) {
  Log "[$name] starting"
  for ($attempt=1; $attempt -le $attempts; $attempt++) {
    try { $value=&$action; $checks[$name]=[ordered]@{passed=$true; attempts=$attempt; result=$value}; Log "[$name] passed (attempt $attempt)"; return $true }
    catch { Log "[$name] failed (attempt $attempt/$attempts): $($_.Exception.Message)"; if ($attempt -lt $attempts) { Start-Sleep -Seconds ([Math]::Min(2*$attempt,8)) } }
  }
  $checks[$name]=[ordered]@{passed=$false; attempts=$attempts; error='all retry attempts failed'}; return $false
}
New-Item -ItemType Directory -Force $InstallRoot | Out-Null
$logParent = Split-Path $ReportPath -Parent; New-Item -ItemType Directory -Force $logParent | Out-Null
Log "EditPath dependency installer started; model=$Model"
$passed=$true
$passed = (Check 'architecture' { if (-not [Environment]::Is64BitOperatingSystem) { throw '64-bit Windows is required' }; 'x64' }) -and $passed
$passed = (Check 'disk_space' { $drive=(Get-Item $InstallRoot).PSDrive; $free=(Get-PSDrive $drive.Name).Free; if ($free -lt 10GB) { throw "at least 10 GB free space required; found $([math]::Round($free/1GB,2)) GB" }; "$([math]::Round($free/1GB,2)) GB free" }) -and $passed
$passed = (Check 'python' { $py=Get-Command py.exe -ErrorAction Stop; $py.Source }) -and $passed
$venv=Join-Path $InstallRoot 'python'; $python=Join-Path $venv 'Scripts\python.exe'
if ($passed) { $passed=(Check 'python_environment' { if (-not (Test-Path $python)) { & py.exe -3.11 -m venv $venv }; if (-not (Test-Path $python)) { throw 'venv creation failed' }; $python }) -and $passed }
if ($passed -and -not $Offline) { $passed=(Check 'whisper_install' { & $python -m pip install --disable-pip-version-check -U openai-whisper; if ($LASTEXITCODE) { throw 'pip install openai-whisper failed' }; 'installed' }) -and $passed }
$passed=(Check 'whisper_import' { & $python -c 'import whisper; print(whisper.__file__)'; if ($LASTEXITCODE) { throw 'Whisper import failed' } }) -and $passed
$bundleBin = Join-Path $PSScriptRoot 'bin\ffmpeg.exe'
$ffmpeg = if (Test-Path $bundleBin) { $bundleBin } else { (Get-Command ffmpeg.exe -ErrorAction Stop).Source }
$passed=(Check 'ffmpeg' { if (-not (Test-Path $ffmpeg)) { throw "FFmpeg not found: $ffmpeg" }; $ffmpeg }) -and $passed
$audioDevice = $null
$passed=(Check 'microphone_directshow' { $devices=& $ffmpeg -hide_banner -list_devices true -f dshow -i dummy 2>&1; $audioLine=$devices | Where-Object { $_ -match '"(.+)" \(audio\)' } | Select-Object -First 1; if (-not $audioLine) { throw 'No DirectShow audio device was found' }; $audioDevice=([regex]::Match([string]$audioLine, '"(.+)" \(audio\)')).Groups[1].Value; if (-not $audioDevice) { throw 'Could not parse DirectShow microphone name' }; $audioDevice }) -and $passed
if ($passed -and $audioDevice) { Set-Content (Join-Path $InstallRoot 'microphone-device.txt') $audioDevice -Encoding UTF8 }
if ($passed -and -not $Offline) { $passed=(Check 'model_download_and_self_test' { $probe=Join-Path $InstallRoot 'whisper-self-test.wav'; & $ffmpeg -hide_banner -loglevel error -f lavfi -i 'sine=frequency=440:duration=1' -y $probe; & $python -m whisper $probe --model $Model --output_format json --output_dir $InstallRoot --verbose False; if ($LASTEXITCODE) { throw 'Whisper self-test failed' }; Remove-Item $probe -Force -ErrorAction SilentlyContinue; 'passed' }) -and $passed }
$summary = [ordered]@{}
foreach ($name in $checks.Keys) {
  $check = $checks[$name]
  $summary[$name] = if ($check.passed) { "INSTALLED/PASS" } else { "MISSING/FAILED" }
}
Log "Dependency summary:"
foreach ($name in $summary.Keys) { Log ("  {0}: {1}" -f $name, $summary[$name]) }
$report=[ordered]@{schema='edit-path/dependency-installer@1'; generated_at_utc=[DateTime]::UtcNow.ToString('o'); model=$Model; install_root=$InstallRoot; offline=[bool]$Offline; passed=$passed; summary=$summary; checks=$checks}
$report | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $ReportPath
Log "Installer finished: passed=$passed; report=$ReportPath"
Write-Output ($report | ConvertTo-Json -Depth 8)
if (-not $passed) { exit 1 }
