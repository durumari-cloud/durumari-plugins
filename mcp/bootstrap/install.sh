#!/bin/sh
# Durumari one-line install for macOS and Linux. It prepares uv when missing, then runs the
# verified installer (downloads/install.py) for every Agent found on this computer. It never
# connects a folder: after it, the person asks their Agent "두루마리 연결해 줘".
#   curl -fsSL __DURUMARI_ORIGIN__/downloads/install.sh | sh
#   curl -fsSL __DURUMARI_ORIGIN__/downloads/install.sh | sh -s -- --client codex
set -eu
# DURUMARI_MCP_* names are the plugin's own; the earlier DURUMARI_SERVER/DURUMARI_INSTALLER still work.
server="${DURUMARI_MCP_SERVER:-${DURUMARI_SERVER:-__DURUMARI_ORIGIN__}}"
# A release check runs an unpublished installer against the published release.
installer="${DURUMARI_MCP_INSTALLER:-${DURUMARI_INSTALLER:-$server/downloads/install.py}}"

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    # Where uv's own installer puts it, for a shell that has not reloaded PATH yet.
    for candidate in "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

if ! uv="$(find_uv)"; then
    echo "두루마리: 설치에 필요한 uv(Python 도구 관리자)를 먼저 설치합니다."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    else
        wget -qO- https://astral.sh/uv/install.sh | sh
    fi
    if ! uv="$(find_uv)"; then
        echo "두루마리: uv를 설치하지 못했습니다. https://docs.astral.sh/uv/ 안내로 설치한 뒤 다시 실행하세요." >&2
        exit 1
    fi
fi

echo "두루마리: 이 컴퓨터의 Agent를 찾아 플러그인을 설치합니다."
# A uv just installed is not on this shell's PATH yet; the installer takes its absolute path.
# Both spellings: the installer it runs may be an older release that reads the earlier one.
DURUMARI_MCP_UV_EXECUTABLE="$uv"
DURUMARI_UV_EXECUTABLE="$uv"
export DURUMARI_MCP_UV_EXECUTABLE DURUMARI_UV_EXECUTABLE
# --no-project keeps a pyproject.toml in the current folder out of the run. The installer reads
# the terminal rather than this piped script, so Gemini CLI can ask its own question.
if (: </dev/tty) 2>/dev/null; then
    exec "$uv" run --quiet --no-project --python 3.12 "$installer" \
        install --server "$server" --summary "$@" </dev/tty
fi
exec "$uv" run --quiet --no-project --python 3.12 "$installer" \
    install --server "$server" --summary "$@"
