# Builds dist\texture_plugger-<version>.zip and validates it with Blender if available.
#   .\build.ps1                       # build + validate
#   .\build.ps1 -Blender "C:\path\blender.exe"
#   .\build.ps1 -Test                 # also run the pure-Python and headless Blender tests
param(
    [string]$Blender = "",
    [switch]$Test
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw "Python 3.9+ is required to run build.py" }

$buildArgs = @("$root\build.py")
if ($Blender) { $buildArgs += @("--blender", $Blender) }
& $python @buildArgs
if ($LASTEXITCODE -ne 0) { throw "Build/validation failed" }

if ($Test) {
    # unittest/Blender write progress to stderr; only exit codes decide success.
    $ErrorActionPreference = "Continue"
    & $python "$root\tests\test_detection.py" 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { throw "Detection tests failed" }
    & $python "$root\tests\test_scaffold.py" 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { throw "Scaffold tests failed" }
    $testArgs = @{}
    if ($Blender) { $testArgs["Blender"] = $Blender }
    & "$root\tests\run_blender_tests.ps1" @testArgs
    if ($LASTEXITCODE -ne 0) { throw "Blender tests failed" }
}
