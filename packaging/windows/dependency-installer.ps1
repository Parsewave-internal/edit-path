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
function Check($name, [scriptblock]$action) {
  try { $value=&$action; $checks[$name]=[ordered]@{passed=$true; result=$value}; return $true }
  catch { $checks[$name]=[ordered]@{passed=$false; error=$_.Exception.Message}; return $false }
}
New-Item -ItemType Directory -Force $InstallRoot | Out-Null
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
$passed=(Check 'microphone_directshow' { $devices=& $ffmpeg -hide_banner -list_devices true -f dshow -i dummy 2>&1; if ($devices -notmatch 'DirectShow audio devices|DirectShow audio devices') { throw 'DirectShow audio device enumeration failed' }; 'enumerated' }) -and $passed
if ($passed -and -not $Offline) { $passed=(Check 'model_download_and_self_test' { $probe=Join-Path $InstallRoot 'whisper-self-test.wav'; & $ffmpeg -hide_banner -loglevel error -f lavfi -i 'sine=frequency=440:duration=1' -y $probe; & $python -m whisper $probe --model $Model --output_format json --output_dir $InstallRoot --verbose False; if ($LASTEXITCODE) { throw 'Whisper self-test failed' }; Remove-Item $probe -Force -ErrorAction SilentlyContinue; 'passed' }) -and $passed }
$report=[ordered]@{schema='edit-path/dependency-installer@1'; generated_at_utc=[DateTime]::UtcNow.ToString('o'); model=$Model; install_root=$InstallRoot; offline=[bool]$Offline; passed=$passed; checks=$checks}
$report | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $ReportPath
Write-Output ($report | ConvertTo-Json -Depth 8)
if (-not $passed) { exit 1 }
