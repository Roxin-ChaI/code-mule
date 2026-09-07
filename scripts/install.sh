#!/usr/bin/env bash
#
# Code Mule persistent CLI installer.
#
# Installs the current repository into an isolated application environment:
#   ~/.local/share/code-mule/venv/
# and exposes a stable launcher:
#   ~/.local/bin/code-mule
#
# The repository-local .venv is never required and no shell configuration is
# modified unless the Boss explicitly passes --configure-shell.
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
  --configure-shell  explicitly append the launcher directory to the detected
                     shell rc file (zshrc/bashrc); never done by default
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

prefix=""
bin_dir=""
python_bin=""
no_deps=0
configure_shell=0
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
installed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
dependency_state="installed"
[ "$no_deps" -eq 1 ] && dependency_state="skipped"
[ "$dependencies_ok" -eq 0 ] && dependency_state="missing"
cat > "$marker" <<EOF
Code Mule persistent installation
version: $version
prefix: $prefix
source: $SOURCE_ROOT
launcher: $launcher
model_dependency: $dependency_state
installed_at: $installed_at
status: ready
EOF

# ---------------------------------------------------------------------------
# PATH guidance (never silently edits shell configuration).
# ---------------------------------------------------------------------------
path_included=0
case ":$PATH:" in
    *":$bin_dir:"*) path_included=1 ;;
esac

if [ "$path_included" -eq 1 ]; then
    echo "PATH is already configured for: $bin_dir"
else
    if [ "$bin_dir" = "$HOME/.local/bin" ]; then
        export_suggestion='export PATH="$HOME/.local/bin:$PATH"'
    else
        export_suggestion="export PATH=\"$bin_dir:\$PATH\""
    fi
    echo
    echo "Add this directory to PATH:"
    echo
    echo "  $export_suggestion"
    echo
    echo "To make it permanent, add that line to ~/.zshrc or ~/.bashrc."
    echo "You can also run: bash scripts/install.sh --configure-shell"
fi

# ---------------------------------------------------------------------------
# Explicit-only shell configuration.
# ---------------------------------------------------------------------------
if [ "$configure_shell" -eq 1 ]; then
    if [ -z "$shell_rc" ]; then
        case "${SHELL:-}" in
            */zsh) shell_rc="${ZDOTDIR:-$HOME}/.zshrc" ;;
            *) shell_rc="$HOME/.bashrc" ;;
        esac
    fi
    if [ "$bin_dir" = "$HOME/.local/bin" ]; then
        export_line='export PATH="$HOME/.local/bin:$PATH"'
    else
        export_line="export PATH=\"$bin_dir:\$PATH\""
    fi
    mkdir -p "$(dirname "$shell_rc")"
    if [ -f "$shell_rc" ] && grep -Fq "$export_line" "$shell_rc" 2>/dev/null; then
        echo "Shell configuration already contains the PATH export: $shell_rc"
    else
        {
            printf '\n# Code Mule PATH (added by scripts/install.sh --configure-shell)\n'
            printf '%s\n' "$export_line"
        } >> "$shell_rc"
        echo "Appended the PATH export to: $shell_rc"
        echo "Open a new terminal (or source $shell_rc) before running code-mule."
    fi
fi

echo
echo "Done. Verify from any directory in a new terminal:"
echo "  command -v code-mule"
echo "  code-mule --help"

[ "$dependencies_ok" -eq 1 ] || exit 1
exit 0
