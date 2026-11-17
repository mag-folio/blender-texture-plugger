# Runs the headless Blender integration test in an isolated user-config directory
# (your real Blender preferences/extensions are never touched).
#   .\tests\run_blender_tests.ps1                         # newest Blender in Program Files
#   .\tests\run_blender_tests.ps1 -Blender "C:\...\blender.exe"
#   .\tests\run_blender_tests.ps1 -Legacy                 # test as a legacy add-on instead
param(
    [string]$Blender = "",
    [switch]$Legacy,
    [string]$Script = "blender_test.py"   # or adversarial_test.py (extension mode only)
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

if (-not $Blender) {
    $cmd = Get-Command blender -ErrorAction SilentlyContinue
    if ($cmd) { $Blender = $cmd.Source }
    else {
        $cand = Get-ChildItem "C:\Program Files\Blender Foundation" -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName "blender.exe" } |
            Where-Object { Test-Path $_ } | Select-Object -First 1
        $Blender = $cand
    }
}
if (-not $Blender -or -not (Test-Path $Blender)) { Write-Error "Blender not found; pass -Blender"; exit 2 }

python "$root\build.py" --no-validate
if ($LASTEXITCODE -ne 0) { exit 1 }
$zip = Get-ChildItem "$root\dist\texture_plugger-*.zip" | Sort-Object LastWriteTime -Descending | Select-Object -First 1

$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) ("tp_blender_user_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $sandbox | Out-Null
$env:BLENDER_USER_RESOURCES = $sandbox
# Never let a test write Painter's real link folder or installed plugin: tests that
# press Edit in Painter / Send to Painter would overwrite the user's scene lighting.
$env:TP_PAINTER_USER_DIR = Join-Path $sandbox "painter_user"
$setLink = -not $env:TP_LINK_DIR
if ($setLink) { $env:TP_LINK_DIR = Join-Path $sandbox "painter_user\texture_plugger_link" }
try {
    if ($Legacy) {
        $addons = Join-Path $sandbox "scripts\addons"
        New-Item -ItemType Directory -Force $addons | Out-Null
        Copy-Item -Recurse "$root\texture_plugger" (Join-Path $addons "texture_plugger")
        $testArgs = @("--legacy")
    } else {
        $testArgs = @("--zip", $zip.FullName)
    }
    # Blender writes warnings to stderr; don't let PowerShell treat that as fatal.
    $ErrorActionPreference = "Continue"
    & $Blender --background --factory-startup --python-exit-code 1 `
        --python "$root\tests\$Script" -- @testArgs 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
} finally {
    Remove-Item Env:\BLENDER_USER_RESOURCES -ErrorAction SilentlyContinue
    Remove-Item Env:\TP_PAINTER_USER_DIR -ErrorAction SilentlyContinue
    if ($setLink) { Remove-Item Env:\TP_LINK_DIR -ErrorAction SilentlyContinue }
    Remove-Item -Recurse -Force $sandbox -ErrorAction SilentlyContinue
}
exit $code
