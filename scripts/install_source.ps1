# install_source.ps1 - user-local AgentOS installer (no admin).
#
# Installer contract:
#   - installs into a user-owned prefix (never Program Files or system32)
#   - prefers uv tool install; falls back to pip --user; errors clearly if neither exists
#   - defaults to the "recommended" runtime profile (memory + bundled BGE
#     embedding assets) and allows `$env:AGENTOS_INSTALL_PROFILE="core"` to opt back down
#   - on Windows, best-effort installs Microsoft Visual C++ Redistributable
#     before the recommended profile because onnxruntime (local memory
#     embeddings) requires it
#   - prints a post-install banner documenting the default bind
#     (127.0.0.1:18791) and the explicit opt-in required to expose the gateway
#     on the network (-Listen 0.0.0.0 or $env:AGENTOS_LISTEN="0.0.0.0")
#   - adds an extra WARNING when the operator requested network exposure at
#     install time via $env:AGENTOS_LISTEN="0.0.0.0"
#
# Dry-run: set $env:AGENTOS_INSTALL_DRY_RUN="1" to print the install plan +
# banner without touching the system.

param(
    [string]$Profile = "",
    [string[]]$Extras = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- prefix resolution ------------------------------------------------------

if ($env:AGENTOS_PREFIX) {
    $prefix = $env:AGENTOS_PREFIX
} elseif ($env:LOCALAPPDATA) {
    $prefix = Join-Path $env:LOCALAPPDATA 'agentos'
} else {
    $prefix = Join-Path $HOME '.local'
}

$dryRun = $env:AGENTOS_INSTALL_DRY_RUN -eq '1'
$script:isWindowsHost = if (Get-Variable IsWindows -ErrorAction SilentlyContinue) {
    $IsWindows
} else {
    $env:OS -eq 'Windows_NT'
}
$profile = if ($Profile) {
    $Profile
} elseif ($env:AGENTOS_INSTALL_PROFILE) {
    $env:AGENTOS_INSTALL_PROFILE
} else {
    'recommended'
}

$validExtras = @(
    'matrix',
    'matrix-e2e',
    'document-extras'
)

function Split-InstallExtras {
    param([string[]]$Values)

    $items = New-Object System.Collections.Generic.List[string]
    foreach ($value in $Values) {
        if (-not $value) {
            continue
        }
        foreach ($part in ($value -split '[,\s]+')) {
            $item = $part.Trim()
            if ($item -and -not $items.Contains($item)) {
                $items.Add($item)
            }
        }
    }
    return $items.ToArray()
}

$extraInputs = @()
if ($env:AGENTOS_INSTALL_EXTRAS) {
    $extraInputs += $env:AGENTOS_INSTALL_EXTRAS
}
$extraInputs += $Extras
$installExtras = @(Split-InstallExtras $extraInputs)

$unknownExtras = @($installExtras | Where-Object { $_ -notin $validExtras })
if ($unknownExtras.Count -gt 0) {
    Write-Error "install_source.ps1: unsupported extras: $($unknownExtras -join ', '). Supported extras: $($validExtras -join ', ')."
    exit 1
}

switch ($profile) {
    'core' { $targetExtras = @() }
    'minimal' { $profile = 'core'; $targetExtras = @() }
    'recommended' { $targetExtras = @('recommended') }
    default {
        Write-Error "install_source.ps1: unsupported AGENTOS_INSTALL_PROFILE='$profile'. Supported profiles: core, recommended."
        exit 1
    }
}

$targetExtras += $installExtras
$installTarget = if ($targetExtras.Count -gt 0) {
    ".[$($targetExtras -join ',')]"
} else {
    '.'
}

function Test-EmbeddingAssets {
    param(
        [switch]$WarnOnly
    )

    if ($profile -ne 'recommended') {
        return
    }

    $modelRoot = 'src/agentos/memory/models/bge_onnx'
    $pilotRoot = 'src/agentos/agentos_router/models/pilot_v1'
    $miniLmRoot = 'src/agentos/memory/models/embeddings/all-MiniLM-L6-v2-int8'
    $required = @(
        "$modelRoot/model.onnx",
        "$pilotRoot/model.onnx",
        "$miniLmRoot/model.onnx"
    )
    $pointerLine = 'version https://git-lfs.github.com/spec/v1'
    $missing = New-Object System.Collections.Generic.List[string]
    $pointers = New-Object System.Collections.Generic.List[string]

    foreach ($path in $required) {
        if (-not (Test-Path $path -PathType Leaf)) {
            $missing.Add($path)
            continue
        }
        $firstLine = Get-Content -Path $path -TotalCount 1 -ErrorAction SilentlyContinue
        if ($firstLine -eq $pointerLine) {
            $pointers.Add($path)
        }
    }

    if ($missing.Count -gt 0 -or $pointers.Count -gt 0) {
        if ($WarnOnly) {
            Write-Host 'install_source.ps1: dry-run note — real recommended install would fail until the bundled BGE embedding assets are available in this checkout.'
        }
        else {
            Write-Error 'install_source.ps1: bundled BGE embedding assets are unavailable in this checkout.'
        }
        if ($missing.Count -gt 0) {
            $message = "install_source.ps1: missing embedding assets: $($missing -join ', ')"
            if ($WarnOnly) { Write-Host $message } else { Write-Error $message }
        }
        if ($pointers.Count -gt 0) {
            $message = "install_source.ps1: Git LFS pointer files detected: $($pointers -join ', ')"
            if ($WarnOnly) { Write-Host $message } else { Write-Error $message }
        }
        $lfsMessage = 'install_source.ps1: run `git lfs install` once, then `git lfs pull --include="src/agentos/memory/models/**"` and `git lfs pull --include="src/agentos/agentos_router/models/**"`.'
        $coreMessage = 'install_source.ps1: or retry with `$env:AGENTOS_INSTALL_PROFILE="core"` for the minimal runtime.'
        if ($WarnOnly) {
            Write-Host $lfsMessage
            Write-Host $coreMessage
            return
        }
        Write-Error $lfsMessage
        Write-Error $coreMessage
        exit 1
    }
}

function Test-WindowsVCRedistInstalled {
    if (-not $script:isWindowsHost) {
        return $true
    }

    $runtimeKeys = @(
        'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64'
    )
    foreach ($key in $runtimeKeys) {
        if (-not (Test-Path $key)) {
            continue
        }
        $runtime = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
        if ($runtime -and $runtime.Installed -eq 1 -and $runtime.Major -ge 14) {
            return $true
        }
    }
    return $false
}

function Install-WindowsVCRedistIfNeeded {
    if (-not $script:isWindowsHost -or $profile -ne 'recommended') {
        return
    }
    if ($env:AGENTOS_SKIP_VC_REDIST -eq '1') {
        Write-Host 'install_source.ps1: skipping Microsoft Visual C++ Redistributable check because AGENTOS_SKIP_VC_REDIST=1.'
        return
    }
    if (Test-WindowsVCRedistInstalled) {
        Write-Host 'install_source.ps1: Microsoft Visual C++ Redistributable is already installed.'
        return
    }

    $redistUrl = 'https://aka.ms/vs/17/release/vc_redist.x64.exe'
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host 'install_source.ps1: Microsoft Visual C++ Redistributable not detected; installing with winget.'
        $wingetArgs = @(
            'install',
            '--id',
            'Microsoft.VCRedist.2015+.x64',
            '--exact',
            '--silent',
            '--accept-package-agreements',
            '--accept-source-agreements'
        )
        & winget @wingetArgs
        if ($LASTEXITCODE -eq 0) {
            Write-Host 'install_source.ps1: Microsoft Visual C++ Redistributable installation completed.'
            return
        }
        Write-Warning "install_source.ps1: winget could not install Microsoft Visual C++ Redistributable (exit $LASTEXITCODE)."
    }

    Write-Warning 'AgentOS: Microsoft Visual C++ Redistributable 2015-2022 x64 is required for the bundled ONNX Runtime.'
    Write-Warning 'AgentOS can still start with safe embedding fallback, but the bundled local memory embedding model is disabled until this runtime is installed.'
    Write-Warning "If automatic installation fails, install it manually: $redistUrl"
    Write-Warning 'After installing, reopen PowerShell and restart AgentOS.'
}

# --- installer selection ----------------------------------------------------

$installer = $null
$installArgs = @()

if (Get-Command uv -ErrorAction SilentlyContinue) {
    $installer = 'uv'
    $installArgs = @('tool', 'install', '--force', '--reinstall-package', 'use-agent-os', $installTarget)
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $installer = 'pip'
    $installArgs = @('-m', 'pip', 'install', '--user', $installTarget)
} else {
    Write-Error "install_source.ps1: neither 'uv' nor 'python' is available on PATH. Install uv (https://docs.astral.sh/uv/) or Python 3.12+ and retry."
    exit 1
}

$installCmd = if ($installer -eq 'uv') {
    "uv $($installArgs -join ' ')"
} else {
    "python $($installArgs -join ' ')"
}

# --- banner -----------------------------------------------------------------

function Write-Banner {
    @"
----------------------------------------------------------------------------
AgentOS installed via $installer -> $prefix (profile: $profile)
Extras: $(if ($installExtras.Count -gt 0) { $installExtras -join ', ' } else { 'none' })

Default gateway bind: 127.0.0.1:18791 (loopback only)
Network exposure is opt-in only. To expose the gateway on the network you
must use one of:
  - CLI flag:  agentos gateway run --listen 0.0.0.0
  - Env var:   `$env:AGENTOS_LISTEN="0.0.0.0"; agentos gateway run

Reminder: only expose 0.0.0.0 behind a trusted reverse proxy or VPN. The
gateway's first-class auth assumes loopback-scope by default.
----------------------------------------------------------------------------
"@ | Write-Host
}

function Write-ListenWarning {
    @"
WARNING: you have selected network-exposed default - ensure you
   understand the blast radius. The gateway will bind to 0.0.0.0 and be
   reachable from every interface on this host.
"@ | Write-Host
}

if ($dryRun) {
    Write-Host "install_source.ps1: dry-run — would run: $installCmd"
    Write-Host "install_source.ps1: dry-run — prefix: $prefix"
    Test-EmbeddingAssets -WarnOnly
    Write-Banner
    if ($env:AGENTOS_LISTEN -eq '0.0.0.0') {
        Write-ListenWarning
    }
    exit 0
}

# --- execute ---------------------------------------------------------------

Install-WindowsVCRedistIfNeeded
Test-EmbeddingAssets

Write-Host "install_source.ps1: installing via $installer into prefix $prefix"
Write-Host "install_source.ps1: running: $installCmd"
if ($installer -eq 'uv') {
    & uv @installArgs
} else {
    & python @installArgs
}
if ($LASTEXITCODE -ne 0) {
    Write-Error "install_source.ps1: install command failed with exit code $LASTEXITCODE."
    Write-Error 'install_source.ps1: Close any running AgentOS gateway or shell using the existing tool environment, then retry.'
    exit $LASTEXITCODE
}

Write-Banner
if ($env:AGENTOS_LISTEN -eq '0.0.0.0') {
    Write-ListenWarning
}
