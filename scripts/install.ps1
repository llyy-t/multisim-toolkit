<#
.SYNOPSIS
    Check the machine for the dsh-multisim-toolkit plugin, or print the exact
    plugin_manager(...) call that DSH has to execute.

.DESCRIPTION
    DSH runs in its own process, so a plain PowerShell script cannot install the
    plugin. What it CAN do:

      * verify that this machine can drive Multisim at all (32-bit Python,
        pywin32, Multisim install path, decoder binary);
      * print the precise plugin_manager call to paste into a DSH session.

    The plugin itself IS this repository: package.json, cordis.patch.yml,
    index.js, lib\ and worker\ live at the root, so `dsh plugin add
    github:llyy-t/multisim-toolkit` installs it directly. Never edit the
    profile's package.json or cordis.patch.yml by hand.

    NOTE (maintainers): keep this file ASCII-only. Windows PowerShell 5.1 reads
    a BOM-less script as ANSI (GBK on a Chinese system), and one truncated
    multibyte character in a string literal can swallow the closing quote and
    make the whole file unparsable. See scripts\README.md.

.PARAMETER Action
    check    probe only: Multisim, 32-bit Python, pywin32, plugin files (default)
    install  print the install_bundle call for DSH
    enable   print the set_plugin(enabled:true) call
    disable  print the set_plugin(enabled:false) call
    reload   print the disable+enable pair (after editing index.js / lib/worker.js)
    remove   print the uninstall instructions

.PARAMETER Python
    Path to a 32-bit Python interpreter. Auto-detected when omitted.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Action check
#>

param(
    [string] $Action = 'check',
    [string] $Python = ''
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here
# The plugin is the repository root. Older checkouts kept it under
# vendor\dsh-multisim-toolkit\, which is still honoured as a fallback.
$bundle = $repo
if (-not (Test-Path (Join-Path $repo 'index.js'))) {
    $nested = Join-Path $repo 'vendor\dsh-multisim-toolkit'
    if (Test-Path (Join-Path $nested 'index.js')) { $bundle = $nested }
}
$multisimExe = 'C:\Program Files (x86)\National Instruments\Circuit Design Suite 14.3\multisim.exe'

function Write-Head([string] $text) {
    Write-Host ''
    Write-Host "== $text" -ForegroundColor Cyan
}

function Find-Python32([string] $hint) {
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($hint) { $candidates.Add($hint) }
    if ($env:MSIM_PYTHON) { $candidates.Add($env:MSIM_PYTHON) }
    if ($env:MULTISIM_MCP_PYTHON) { $candidates.Add($env:MULTISIM_MCP_PYTHON) }
    if ($env:DSH_HOME) { $candidates.Add((Join-Path $env:DSH_HOME 'multisim-mcp\py32\python.exe')) }
    if ($env:USERPROFILE) { $candidates.Add((Join-Path $env:USERPROFILE '.dsh\multisim-mcp\py32\python.exe')) }
    $candidates.Add('C:\Python313-32\python.exe')
    $candidates.Add('C:\Python312-32\python.exe')
    $candidates.Add('C:\Python311-32\python.exe')
    foreach ($path in $candidates) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    return $null
}

function Invoke-Check([string] $hint) {
    $ok = $true

    Write-Head 'Multisim'
    if (Test-Path -LiteralPath $multisimExe) {
        Write-Host "  exe: $multisimExe"
    }
    else {
        Write-Host "  MISSING: $multisimExe" -ForegroundColor Red
        Write-Host '  Multisim 14.3 was not found at the expected path.'
        $ok = $false
    }

    Write-Head '32-bit Python'
    $py = Find-Python32 $hint
    if (-not $py) {
        Write-Host '  MISSING: no 32-bit Python found.' -ForegroundColor Red
        Write-Host '  The Multisim Automation API is a 32-bit COM server and a 64-bit'
        Write-Host '  interpreter gets E_ACCESSDENIED from Connect().'
        Write-Host '  Install a 32-bit Python 3.11-3.13, or pass the -Python parameter.'
        $ok = $false
    }
    else {
        Write-Host "  python: $py"
        $probe = @(& $py -c "import sys,struct;print(sys.version.split()[0]);print(struct.calcsize('P')*8)" 2>&1)
        $version = ($probe | Select-Object -First 1)
        $bits = ($probe | Select-Object -Last 1)
        Write-Host "  version: $version / $bits-bit"
        if ("$bits".Trim() -ne '32') {
            Write-Host '  WRONG BITS: a 32-bit interpreter is required.' -ForegroundColor Red
            $ok = $false
        }
        $pywin = (& $py -c "import importlib.util as u;print(bool(u.find_spec('win32com') and u.find_spec('pythoncom')))" 2>&1)
        if ("$pywin".Trim() -ne 'True') {
            Write-Host '  MISSING: pywin32 (pip install pywin32 with that interpreter)' -ForegroundColor Red
            $ok = $false
        }
        else {
            Write-Host '  pywin32: ok'
        }
    }

    Write-Head 'plugin sources'
    $manifestPath = Join-Path $bundle 'package.json'
    if (Test-Path -LiteralPath $manifestPath) {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        Write-Host "  bundle: $bundle"
        Write-Host "  name:   $($manifest.name) $($manifest.version)"
    }
    else {
        Write-Host "  MISSING: $manifestPath" -ForegroundColor Red
        $ok = $false
    }
    $decoder = Join-Path $bundle 'worker\bin\blast_harness.exe'
    if (Test-Path -LiteralPath $decoder) {
        Write-Host "  ms14 decoder: ok"
    }
    else {
        Write-Host "  MISSING: $decoder" -ForegroundColor Yellow
        Write-Host '  .ms14 offline reading needs it; rebuild from worker\bin sources.'
    }

    Write-Head 'DSH profile'
    $profileDir = $env:DSH_PROFILE_DIR
    if (-not $profileDir -and $env:DSH_HOME) { $profileDir = Join-Path $env:DSH_HOME 'profiles\desktop' }
    if (-not $profileDir -and $env:USERPROFILE) {
        $guess = Join-Path $env:USERPROFILE '.dsh\profiles\desktop'
        if (Test-Path -LiteralPath $guess) { $profileDir = $guess }
    }
    if ($profileDir) { Write-Host "  profile: $profileDir" } else { Write-Host '  unknown (DSH_PROFILE_DIR is not set)' }

    Write-Host ''
    if ($ok) {
        Write-Host 'All checks passed. Next:' -ForegroundColor Green
        Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Action install"
        Write-Host '  then paste the printed plugin_manager(...) line into the DSH session.'
    }
    else {
        Write-Host 'Some checks failed - fix the red lines above first.' -ForegroundColor Red
        exit 1
    }
}

function Write-DshInstruction([string] $action) {
    $install = "plugin_manager({ action: 'install_bundle', target: '$bundle' })"
    $flag = 'true'
    if ($action -eq 'disable' -or $action -eq 'remove') { $flag = 'false' }
    $toggle = "plugin_manager({ action: 'set_plugin', target: 'include:multisim-toolkit', enabled: $flag })"

    Write-Head "ask DSH to run: $action"
    Write-Host 'Installation goes through the plugin_manager tool inside DSH.'
    Write-Host 'Paste the line below into the DSH session.' -ForegroundColor Yellow
    Write-Host ''
    switch ($action) {
        'install' { Write-Host "    $install" -ForegroundColor White }
        'enable' { Write-Host "    $toggle" -ForegroundColor White }
        'disable' { Write-Host "    $toggle" -ForegroundColor White }
        'reload' {
            Write-Host "    plugin_manager({ action: 'set_plugin', target: 'include:multisim-toolkit', enabled: false })" -ForegroundColor White
            Write-Host "    plugin_manager({ action: 'set_plugin', target: 'include:multisim-toolkit', enabled: true })" -ForegroundColor White
        }
        'remove' {
            Write-Host "    $toggle" -ForegroundColor White
            Write-Host '    then drop dsh-multisim-toolkit from dsh.profile.bundles in the profile package.json'
        }
    }
    Write-Host ''
    Write-Host 'context:' -ForegroundColor Gray
    Write-Host '  - plugin entry id is include:multisim-toolkit (not dsh-multisim-toolkit)' -ForegroundColor Gray
    Write-Host '  - each plugin_manager call may need one approval' -ForegroundColor Gray
    Write-Host '  - verify with msim_status: expect pythonBits 32, com version Multisim 14.3' -ForegroundColor Gray
    if ($action -eq 'reload') {
        Write-Host '  - reload after editing index.js or lib/worker.js; worker\*.py needs none' -ForegroundColor Gray
    }
}

switch ($Action) {
    'check' { Invoke-Check $Python }
    'install' { Invoke-Check $Python; Write-DshInstruction 'install' }
    'enable' { Write-DshInstruction 'enable' }
    'disable' { Write-DshInstruction 'disable' }
    'reload' { Write-DshInstruction 'reload' }
    'remove' { Write-DshInstruction 'remove' }
    default {
        Write-Host "Unknown -Action '$Action'. Use check|install|enable|disable|reload|remove."
        exit 2
    }
}