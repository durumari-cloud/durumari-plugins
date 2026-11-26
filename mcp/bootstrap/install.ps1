# Durumari one-line install for Windows PowerShell. It prepares uv when missing, then runs the
# verified installer (downloads/install.py) for every Agent found on this computer. It never
# connects a folder: after it, the person asks their Agent "두루마리 연결해 줘".
#   irm __DURUMARI_ORIGIN__/downloads/install.ps1 | iex
#   $env:DURUMARI_MCP_CLIENT = 'codex'; irm __DURUMARI_ORIGIN__/downloads/install.ps1 | iex
# The earlier $env:DURUMARI_CLIENT, DURUMARI_SERVER and DURUMARI_INSTALLER still work.
# Run through iex, so it never calls exit (that would close the person's shell), and everything
# runs inside one script block so no preference, variable or function stays in that shell.
& {
    $ErrorActionPreference = 'Stop'
    $server = if ($env:DURUMARI_MCP_SERVER) { $env:DURUMARI_MCP_SERVER } elseif ($env:DURUMARI_SERVER) { $env:DURUMARI_SERVER } else { '__DURUMARI_ORIGIN__' }
    # A release check runs an unpublished installer against the published release.
    $installer = if ($env:DURUMARI_MCP_INSTALLER) { $env:DURUMARI_MCP_INSTALLER } elseif ($env:DURUMARI_INSTALLER) { $env:DURUMARI_INSTALLER } else { "$server/downloads/install.py" }

    function Find-DurumariUv {
        $command = Get-Command uv -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
        # Where uv's own installer puts it, for a shell that has not reloaded PATH yet.
        foreach ($candidate in @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe")) {
            if (Test-Path $candidate) { return $candidate }
        }
        return $null
    }

    $uv = Find-DurumariUv
    if (-not $uv) {
        Write-Host '두루마리: 설치에 필요한 uv(Python 도구 관리자)를 먼저 설치합니다.'
        powershell -ExecutionPolicy ByPass -c 'irm https://astral.sh/uv/install.ps1 | iex'
        $uv = Find-DurumariUv
    }
    if (-not $uv) {
        Write-Host '두루마리: uv를 설치하지 못했습니다. https://docs.astral.sh/uv/ 안내로 설치한 뒤 다시 실행하세요.'
        return
    }
    Write-Host '두루마리: 이 컴퓨터의 Agent를 찾아 플러그인을 설치합니다.'
    # A uv just installed is not on this shell's PATH yet; the installer takes its absolute path.
    # Environment variables outlive the block, so the earlier value comes back afterwards.
    # Both spellings: the installer it runs may be an older release that reads the earlier one.
    $previousUv = $env:DURUMARI_UV_EXECUTABLE
    $previousMcpUv = $env:DURUMARI_MCP_UV_EXECUTABLE
    $env:DURUMARI_UV_EXECUTABLE = $uv
    $env:DURUMARI_MCP_UV_EXECUTABLE = $uv
    try {
        # --no-project keeps a pyproject.toml in the current folder out of the run.
        $arguments = @('run', '--quiet', '--no-project', '--python', '3.12', $installer, 'install', '--server', $server, '--summary')
        $client = if ($env:DURUMARI_MCP_CLIENT) { $env:DURUMARI_MCP_CLIENT } else { $env:DURUMARI_CLIENT }
        if ($client) { $arguments += @('--client', $client) }
        & $uv @arguments
    } finally {
        $env:DURUMARI_UV_EXECUTABLE = $previousUv
        $env:DURUMARI_MCP_UV_EXECUTABLE = $previousMcpUv
    }
}
