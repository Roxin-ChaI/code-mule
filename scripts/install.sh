#!/usr/bin/env bash
#
# Code Mule persistent CLI installer.
#
# Installs the current repository into an isolated application environment:
#   ~/.local/share/code-mule/venv/
# and exposes a stable launcher:
#   ~/.local/bin/code-mule
#
# The repository-local .venv is never required. Shell configuration is
# modified only when the launcher directory is missing from PATH. Pass
# --no-configure-shell to keep this installer from touching a shell rc file.
#
set -euo pipefail

APP_NAME="code-mule"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
    cat <<'EOF'
Install Code Mule as a persistent CLI outside any repository virtualenv.

Usage:
  bash scripts/install.sh [options]

Options:
  --prefix DIR       application home (default: $HOME/.local/share/code-mule)
  --bin-dir DIR      launcher directory (default: $HOME/.local/bin)
  --python PATH      Python 3.12 executable to use (default: python3.12)
  --no-deps          install the CLI without the openai model dependency
                     (deterministic/air-gapped use; model commands will not run)
  --no-configure-shell
                     install the CLI without modifying any shell rc file
  --configure-shell  legacy: equivalent to the default automatic behavior
                     (append the launcher directory to the detected rc file)
  --reinstall        refresh an existing Code Mule installation in place
                     (default behavior is the same safe refresh; no deletion)
  -h, --help         show this help
EOF
}

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

warn() {
    printf 'Warning: %s\n' "$*" >&2
}

# Returns 0 when an rc file already carries the Code Mule managed block.
rc_has_code_mule_block() {
    [ -f "$1" ] && grep -Fq "# >>> code-mule >>>" "$1" 2>/dev/null \
        && grep -Fq "# <<< code-mule <<<" "$1" 2>/dev/null
}

# A managed block is only migrated/rewritten when it is well formed.
rc_has_closed_code_mule_block() {
    [ -f "$1" ] || return 1
    start_line="$(grep -n -F "# >>> code-mule >>>" "$1" 2>/dev/null | head -n 1 | cut -d: -f1 || true)"
    end_line="$(grep -n -F "# <<< code-mule <<<" "$1" 2>/dev/null | head -n 1 | cut -d: -f1 || true)"
    [ -n "$start_line" ] && [ -n "$end_line" ] && [ "$end_line" -gt "$start_line" ]
}

# Conservative idempotency detection for an equivalent existing PATH export.
rc_has_equivalent_path() {
    [ -f "$1" ] || return 1
    local literal
    grep -Fq "$export_line" "$1" && return 0
    if [ "$bin_dir" = "$HOME/.local/bin" ]; then
        literal="export PATH=\"$HOME/.local/bin:\$PATH\""
        grep -Fq "$literal" "$1" && return 0
        grep -Fq 'export PATH="~/.local/bin:$PATH"' "$1" && return 0
        literal="export PATH=$HOME/.local/bin:\$PATH"
        grep -Fq "$literal" "$1" && return 0
    else
        literal="export PATH=\"$bin_dir:\$PATH\""
        grep -Fq "$literal" "$1" && return 0
    fi
    return 1
}

# Creates one-time backup only when an existing non-empty file will change.
backup_rc_file() {
    local rc="$1"
    local backup="$rc.code-mule.bak"
    if [ -f "$rc" ] && [ -s "$rc" ] && [ ! -e "$backup" ]; then
        cp "$rc" "$backup" || return 1
        echo "Backup: $backup"
    fi
    return 0
}

# Removes only the exact Code Mule marker range; all other content is kept.
remove_code_mule_block() {
    local rc="$1"
    local temporary="$rc.code-mule.tmp.$$"
    if ! sed '/^# >>> code-mule >>>$/,/^# <<< code-mule <<<$/d' "$rc" \
        > "$temporary" 2>/dev/null; then
        rm -f -- "$temporary"
        return 1
    fi
    mv "$temporary" "$rc" || {
        rm -f -- "$temporary"
        return 1
    }
    return 0
}

# Builds one guarded Code Mule managed block. The guard avoids appending a
# duplicate directory when a login profile already initialized PATH.
build_managed_block() {
    local export_value
    if [ "$bin_dir" = "$HOME/.local/bin" ]; then
        export_value='$HOME/.local/bin'
    else
        export_value="$bin_dir"
    fi
    managed_block="# >>> code-mule >>>
case \":\$PATH:\" in
  *\":$export_value:\"*) ;;
  *) export PATH=\"$export_value:\$PATH\" ;;
esac
# <<< code-mule <<<"
}

# Ensures one rc file carries exactly the current managed block. Existing Code
# Mule marker ranges are migrated in place; user content outside the markers is
# never changed. Returns 0 on success or when already current.
ensure_rc_managed() {
    local rc="$1"
    if [ -e "$rc" ] || [ -L "$rc" ]; then
        if [ -L "$rc" ] || [ ! -f "$rc" ]; then
            return 1
        fi
    fi
    mkdir -p "$(dirname "$rc")" || return 1

    had_block=0
    if rc_has_code_mule_block "$rc"; then
        had_block=1
    elif [ -f "$rc" ] && grep -Fq "# >>> code-mule >>>" "$rc" 2>/dev/null; then
        if ! rc_has_closed_code_mule_block "$rc"; then
            return 1
        fi
        had_block=1
    fi

    if [ "$had_block" -eq 1 ]; then
        existing_block="$(
            sed -n '/^# >>> code-mule >>>$/,/^# <<< code-mule <<<$/p' "$rc" 2>/dev/null
        )"
        if [ "$existing_block" = "$managed_block" ]; then
            return 0
        fi
        backup_rc_file "$rc" || return 1
        remove_code_mule_block "$rc" || return 1
    fi

    if [ "$had_block" -eq 0 ] && rc_has_equivalent_path "$rc"; then
        return 0
    fi
    backup_rc_file "$rc" || return 1
    {
        printf '\n%s\n' "$managed_block"
    } >> "$rc" || return 1
    return 0
}

prefix=""
bin_dir=""
python_bin=""
no_deps=0
configure_shell=1
shell_rc="${CODE_MULE_SHELL_RC:-}"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix)
            [ "$#" -ge 2 ] || die "--prefix requires a value"
            prefix="$2"
            shift 2
            ;;
        --bin-dir)
            [ "$#" -ge 2 ] || die "--bin-dir requires a value"
            bin_dir="$2"
            shift 2
            ;;
        --python)
            [ "$#" -ge 2 ] || die "--python requires a value"
            python_bin="$2"
            shift 2
            ;;
        --no-deps)
            no_deps=1
            shift
            ;;
        --no-configure-shell)
            configure_shell=0
            shift
            ;;
        --configure-shell)
            configure_shell=1
            shift
            ;;
        --reinstall)
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1 (run bash scripts/install.sh --help)"
            ;;
    esac
done

[ -n "${HOME:-}" ] || die "HOME is not set"

[ -f "$SOURCE_ROOT/pyproject.toml" ] || die "Code Mule source was not found at: $SOURCE_ROOT"
[ -d "$SOURCE_ROOT/src/code_mule" ] || die "Code Mule source package was not found under $SOURCE_ROOT/src"

prefix="${prefix:-$HOME/.local/share/code-mule}"
bin_dir="${bin_dir:-$HOME/.local/bin}"
venv_dir="$prefix/venv"
launcher="$bin_dir/$APP_NAME"
marker="$prefix/CODE_MULE_INSTALL"
venv_entry="$venv_dir/bin/$APP_NAME"
dependencies_ok=1

if [ -z "$python_bin" ]; then
    python_bin="$(command -v python3.12 2>/dev/null || true)"
    [ -n "$python_bin" ] || die "Python 3.12 was not found. Install Python 3.12 or pass --python PATH."
fi

[ -x "$python_bin" ] || die "Python executable does not exist or is not executable: $python_bin"

python_version="$("$python_bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[ "$python_version" = "3.12" ] || die "Code Mule requires Python 3.12 (found $python_version): $python_bin"

# ---------------------------------------------------------------------------
# Existing launcher: fail closed unless it clearly belongs to Code Mule.
# ---------------------------------------------------------------------------
launcher_owned=0
if [ -L "$launcher" ]; then
    launcher_target="$(readlink "$launcher" 2>/dev/null || true)"
    case "$launcher_target" in
        "$prefix"/*|"$venv_dir"/*) launcher_owned=1 ;;
        *) launcher_owned=0 ;;
    esac
elif [ -f "$launcher" ]; then
    launcher_header="$(head -n 1 "$launcher" 2>/dev/null || true)"
    case "$launcher_header" in
        "# Code Mule launcher (managed by scripts/install.sh)"*) launcher_owned=1 ;;
        *) launcher_owned=0 ;;
    esac
fi

if [ -e "$launcher" ] || [ -L "$launcher" ]; then
    if [ "$launcher_owned" -eq 0 ]; then
        die "Refusing to overwrite unrelated executable: $launcher

Remove or move it yourself, or choose a different --bin-dir, then rerun."
    fi
    echo "Existing Code Mule launcher detected; refreshing the installation in place."
fi

# ---------------------------------------------------------------------------
# Existing prefix: fail closed unless this directory is managed by Code Mule.
# ---------------------------------------------------------------------------
prefix_existed=0
if [ -e "$prefix" ]; then
    [ -d "$prefix" ] || die "Install prefix is not a directory: $prefix"
    if [ -n "$(ls -A "$prefix" 2>/dev/null || true)" ]; then
        prefix_existed=1
        if [ ! -f "$marker" ]; then
            die "Directory already exists but is not managed by Code Mule: $prefix

Move it aside yourself, or choose a different --prefix, then rerun."
        fi
        marker_header="$(head -n 1 "$marker" 2>/dev/null || true)"
        case "$marker_header" in
            "Code Mule installation in progress"|"Code Mule persistent installation") ;;
            *) die "Directory is managed by a different tool: $prefix" ;;
        esac
    fi
fi

if [ "$prefix_existed" -eq 0 ]; then
    mkdir -p "$prefix"
    printf 'Code Mule installation in progress\n' > "$marker"
fi

mkdir -p "$bin_dir"

# ---------------------------------------------------------------------------
# Isolated application environment (never the repository .venv).
# ---------------------------------------------------------------------------
if [ -x "$venv_dir/bin/python" ]; then
    echo "Reusing existing application environment: $venv_dir"
else
    echo "Creating isolated application environment: $venv_dir"
    "$python_bin" -m venv "$venv_dir" || die "Could not create the application environment.

Code Mule never deletes an existing environment. If a partial environment is in
the way, move it aside yourself and rerun."
fi

app_python="$venv_dir/bin/python"
[ -x "$app_python" ] || die "Application Python was not created at $app_python"
site_packages="$("$app_python" -c 'import site; print(site.getsitepackages()[0])')"
[ -n "$site_packages" ] || die "Could not locate the application site-packages directory"

# ---------------------------------------------------------------------------
# Install the current package into the isolated environment.
# The target directory is inside the owned application venv; refreshing it is
# a normal in-place upgrade and never touches the repository .venv.
# ---------------------------------------------------------------------------
package_target="$site_packages/code_mule"
mkdir -p "$site_packages"
if [ -e "$package_target" ]; then
    rm -rf -- "$package_target"
fi
cp -R "$SOURCE_ROOT/src/code_mule" "$package_target"
find "$package_target" -type d -name __pycache__ -prune -exec rm -rf -- {} +
find "$package_target" -type f -name '*.pyc' -delete

# ---------------------------------------------------------------------------
# Stable launcher.
# ---------------------------------------------------------------------------
cat > "$venv_entry" <<EOF
#!$app_python
# Code Mule launcher (managed by scripts/install.sh)
import sys
from code_mule.cli import main

if __name__ == "__main__":
    sys.exit(main())
EOF
chmod +x "$venv_entry"

if [ -e "$launcher" ] || [ -L "$launcher" ]; then
    rm -f -- "$launcher"
fi
ln -s "$venv_entry" "$launcher"

echo "Code Mule installed: $launcher"

# ---------------------------------------------------------------------------
# Verify the launcher without activating any virtualenv.
# ---------------------------------------------------------------------------
if ! "$launcher" --help >/dev/null 2>&1; then
    die "The installed launcher did not start. Rerun with --reinstall after fixing the reported issue."
fi
echo "Launcher verification passed: $launcher --help"

# ---------------------------------------------------------------------------
# Optional runtime dependency (openai). Local/offline commands do not need it.
# ---------------------------------------------------------------------------
if [ "$no_deps" -eq 0 ]; then
    echo "Installing the openai model dependency into the application environment..."
    if ! PIP_DEFAULT_TIMEOUT=20 "$app_python" -m pip install \
        --disable-pip-version-check \
        "openai>=3.6,<4" >/dev/null 2>&1; then
        dependencies_ok=0
        warn "The openai model dependency could not be installed (network may be unavailable)."
        warn "The local CLI is installed; rerun this script when network is available to enable model commands."
    fi
fi

# ---------------------------------------------------------------------------
# Final ownership marker.
# ---------------------------------------------------------------------------
version="$(
    cd / && "$app_python" -c 'import code_mule; print(code_mule.__version__)'
)"
schema_min="$(
    cd / && "$app_python" -c 'from code_mule.state import MIN_SUPPORTED_SCHEMA_VERSION; print(MIN_SUPPORTED_SCHEMA_VERSION)'
)"
schema_max="$(
    cd / && "$app_python" -c 'from code_mule.state import CURRENT_SCHEMA_VERSION; print(CURRENT_SCHEMA_VERSION)'
)"
source_revision="$(git -C "$SOURCE_ROOT" rev-parse HEAD 2>/dev/null || true)"
case "$source_revision" in
    *[!0-9a-f]*|'') source_revision="unknown" ;;
esac
installed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
dependency_state="installed"
[ "$no_deps" -eq 1 ] && dependency_state="skipped"
[ "$dependencies_ok" -eq 0 ] && dependency_state="missing"
marker_tmp="$prefix/.CODE_MULE_INSTALL.$$"
cat > "$marker_tmp" <<EOF
Code Mule persistent installation
version: $version
prefix: $prefix
source: $SOURCE_ROOT
launcher: $launcher
model_dependency: $dependency_state
source_revision: $source_revision
schema_min: $schema_min
schema_max: $schema_max
installed_at: $installed_at
status: ready
EOF
mv -f -- "$marker_tmp" "$marker"

# ---------------------------------------------------------------------------
# PATH configuration (automatic by default; opt out with --no-configure-shell).
# ---------------------------------------------------------------------------
path_included=0
case ":$PATH:" in
    *":$bin_dir:"*) path_included=1 ;;
esac

if [ "$bin_dir" = "$HOME/.local/bin" ]; then
    export_line='export PATH="$HOME/.local/bin:$PATH"'
else
    export_line="export PATH=\"$bin_dir:\$PATH\""
fi

configured_files=()
failed_rc_files=()
detect_rc_result=0
if [ "$path_included" -eq 1 ]; then
    echo "PATH already configured."
    echo
    echo "Code Mule installed successfully."
elif [ "$configure_shell" -eq 0 ]; then
    echo "Automatic PATH configuration skipped (--no-configure-shell)."
    echo
    echo "Code Mule installed successfully."
    echo
    echo "To use code-mule in a new terminal, add this line to your shell rc:"
    echo "  $export_line"
else
    rc_files=()
    if [ -z "$shell_rc" ]; then
        shell_name="$(basename "${SHELL:-}" 2>/dev/null || true)"
        case "$shell_name" in
            zsh)
                zsh_home="${ZDOTDIR:-$HOME}"
                if [ "$(uname -s 2>/dev/null || true)" = "Darwin" ]; then
                    # macOS Terminal windows are login shells. PATH is
                    # environment initialization, so .zprofile is the primary
                    # file; .zshrc remains as a guarded non-login fallback.
                    rc_files=("$zsh_home/.zprofile" "$zsh_home/.zshrc")
                else
                    rc_files=("$zsh_home/.zshrc")
                fi
                ;;
            bash)
                if [ "$(uname -s 2>/dev/null || true)" = "Darwin" ]; then
                    # macOS Terminal opens interactive login shells; bash reads
                    # .bash_profile there before .bashrc.
                    rc_files=("$HOME/.bash_profile")
                else
                    rc_files=("$HOME/.bashrc")
                fi
                ;;
            *)
                detect_rc_result=1
                ;;
        esac
    else
        rc_files=("$shell_rc")
    fi

    if [ "$detect_rc_result" -eq 1 ]; then
        echo "Code Mule installed successfully."
        echo
        echo "Automatic PATH configuration could not be completed"
        echo "(unsupported shell: ${SHELL:-unknown})."
        echo "Add this line to your shell rc:"
        echo "  $export_line"
    else
        build_managed_block
        for shell_rc in "${rc_files[@]}"; do
            if ensure_rc_managed "$shell_rc"; then
                if rc_has_code_mule_block "$shell_rc"; then
                    configured_files+=("$shell_rc")
                else
                    echo "PATH already configured in shell rc: $shell_rc"
                fi
            else
                failed_rc_files+=("$shell_rc")
            fi
        done
    fi
fi

if [ "${#failed_rc_files[@]}" -gt 0 ]; then
    echo "Code Mule installed successfully."
    echo
    echo "Automatic PATH configuration could not be completed for:"
    for shell_rc in "${failed_rc_files[@]}"; do
        echo "  $shell_rc"
    done
    echo "Add this line to your shell rc manually:"
    echo "  $export_line"
fi

if [ "${#configured_files[@]}" -gt 0 ]; then
    for shell_rc in "${configured_files[@]}"; do
        echo "Shell PATH configured: $shell_rc"
    done
    echo
    echo "Code Mule installed successfully."
    echo
    echo "Open a new terminal and run:"
    echo "  code-mule doctor"
elif [ "${#failed_rc_files[@]}" -eq 0 ] \
    && [ "$path_included" -eq 0 ] \
    && [ "$configure_shell" -eq 1 ] \
    && [ "$detect_rc_result" -eq 0 ]; then
    # Every target file already had an equivalent PATH export.
    echo
    echo "Code Mule installed successfully."
    echo
    echo "Open a new terminal and run:"
    echo "  code-mule doctor"
fi

[ "$dependencies_ok" -eq 1 ] || exit 1
exit 0
