# hpclib's installer for Windows: hpclib runs in WSL (Linux on Windows), and this sets that up.
# In PowerShell (not as administrator):
#
#   irm https://raw.githubusercontent.com/Tabor-Research-Group/hpclib/master/install.ps1 | iex
#
# With options, run it as a script block instead:
#
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Tabor-Research-Group/hpclib/master/install.ps1))) -Distro Ubuntu-24.04 -NoMcp
#
#   -Distro NAME   the WSL distribution to install into (default: WSL's default one; $env:HPCLIB_WSL_DISTRO)
#   -NoMcp         skip the MCP SDK (only agents need it)
#   -NoShortcut    no Start menu shortcut
#   -Branch NAME   the branch to install (default: master)
#
# What it does:
#   1. With no WSL distribution yet, it asks Windows to install WSL with Ubuntu (an administrator prompt), then
#      stops: restart if Windows asks, open Ubuntu once from the Start menu to create your Linux user, and run
#      this again.
#   2. Runs install.sh inside the distribution: the clone in ~/hpclib, launch-tunnel-manager on PATH, hpclib's
#      shell functions in ~/.bashrc and the MCP SDK (see install.sh).
#   3. Adds "hpclib Tunnel Manager" to the Start menu: it starts launch-tunnel-manager in WSL and opens the
#      console in your browser. Closing its window stops the console.
# Running it again updates hpclib.

function Install-Hpclib {
    param(
        [string]$Distro = $env:HPCLIB_WSL_DISTRO,
        [switch]$NoMcp,
        [switch]$NoShortcut,
        [string]$Branch = "master"
    )
    $ErrorActionPreference = "Continue"   # "Stop" turns any stderr from wsl.exe into an error in Windows PowerShell 5.1
    $raw = "https://raw.githubusercontent.com/Tabor-Research-Group/hpclib/$Branch/install.sh"
    $env:WSL_UTF8 = "1"     # wsl.exe's own messages in UTF-8, not UTF-16
    # (arguments for wsl.exe avoid double quotes inside them: Windows PowerShell 5.1 passes those on unescaped)

    function Say($text) { Write-Host $text -ForegroundColor Cyan }
    function Fail($text) { Write-Host "hpclib installer: $text" -ForegroundColor Red }

    $wsl = Join-Path $env:WINDIR "System32\wsl.exe"
    if (-not (Test-Path $wsl)) {
        Fail "this Windows has no WSL. Windows 10 2004 (build 19041) or newer, or Windows 11, is needed."
        return
    }

    # the distributions WSL has (docker-desktop's own aren't for this)
    $names = @(& $wsl --list --quiet 2>$null | ForEach-Object { ($_ -replace "`0", "").Trim() } |
               Where-Object { $_ -and $_ -notmatch '^docker-desktop' })
    if ($LASTEXITCODE -ne 0 -or $names.Count -eq 0) {
        Say "WSL has no Linux distribution yet: installing WSL with Ubuntu (Windows asks for an administrator's OK)."
        try {
            Start-Process -FilePath $wsl -ArgumentList "--install", "-d", "Ubuntu" -Verb RunAs -Wait
        } catch {
            Fail "WSL wasn't installed ($($_.Exception.Message)). Run 'wsl --install' in an administrator PowerShell."
            return
        }
        Say "Next: restart Windows if it asks, open Ubuntu from the Start menu once to create your Linux user,"
        Say "then run this installer again."
        return
    }

    $target = @()
    if ($Distro) {
        if ($names -notcontains $Distro) {
            Fail "no WSL distribution called '$Distro'; you have: $($names -join ', ')"
            return
        }
        $target = @("-d", $Distro)
    }
    $name = (& $wsl @target -e sh -c 'printf %s $WSL_DISTRO_NAME' 2>$null)
    if (-not $name) {
        Fail "WSL didn't start the distribution. Open it once from the Start menu (to finish its setup) and rerun."
        return
    }
    if ($name -match '^docker-desktop') {
        Fail "WSL's default distribution is Docker Desktop's; choose yours with -Distro (you have: $($names -join ', '))"
        return
    }
    Say "Installing hpclib in WSL ($name)"

    $options = @()
    if ($NoMcp) { $options += "--no-mcp" }
    if ($Branch -ne "master") { $options += @("--branch", $Branch) }
    $fetch = "if command -v curl >/dev/null 2>&1; then curl -fsSL '$raw'; else wget -qO- '$raw'; fi"
    & $wsl -d $name -e bash -c "set -o pipefail; ($fetch) | bash -s -- $($options -join ' ')"
    if ($LASTEXITCODE -ne 0) {
        Fail "the installer in WSL stopped (exit $LASTEXITCODE); see its message above"
        return
    }

    if (-not $NoShortcut) {
        $folder = Join-Path $env:LOCALAPPDATA "hpclib"
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        # the icon, from the clone (WSL's files are under \\wsl.localhost, or \\wsl$ on older Windows)
        $linuxHome = (& $wsl -d $name -e sh -c 'printf %s $HOME')
        $icon = Join-Path $folder "hpclib.ico"
        foreach ($share in @("\\wsl.localhost\$name", "\\wsl$\$name")) {
            $source = $share + ($linuxHome -replace '/', '\') + "\hpclib\agent-console\icon.ico"
            if (Test-Path $source) { Copy-Item $source $icon -Force; break }
        }
        $programs = [Environment]::GetFolderPath("Programs")
        $link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $programs "hpclib Tunnel Manager.lnk"))
        $link.TargetPath = $wsl
        $link.Arguments = "-d $name -e bash -lc `"exec ~/.local/bin/launch-tunnel-manager`""
        $link.Description = "hpclib's tunnel manager (runs in WSL; closing this window stops it)"
        if (Test-Path $icon) { $link.IconLocation = $icon }
        $link.Save()
        Say "Added 'hpclib Tunnel Manager' to the Start menu."
    }
    Say ""
    Say "Done. Start the tunnel manager from the Start menu, or in a terminal:"
    Write-Host "    wsl -d $name -e bash -lc '~/.local/bin/launch-tunnel-manager'"
    Say "In WSL ($name), hpclib's commands (launch_tunnel, setup_agents, ...) are loaded in new shells."
}

Install-Hpclib @args
