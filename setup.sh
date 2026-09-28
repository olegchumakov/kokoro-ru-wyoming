#!/usr/bin/env bash
set -Eeuo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${BASE_DIR}/wyoming.json"

VENV_DIR="${BASE_DIR}/.venv"

# Everything managed by this script stays inside the project.
CACHE_DIR="${BASE_DIR}/.cache"
UV_DIR="${CACHE_DIR}/uv"
UV_PYTHON_DIR="${CACHE_DIR}/uv-python"
UV_CACHE_DIR="${CACHE_DIR}/uv-cache"
UV_BIN="${UV_DIR}/uv"

export UV_NO_MODIFY_PATH=1
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_DIR}"
export UV_CACHE_DIR="${UV_CACHE_DIR}"

log() {
    printf '\n==> %s\n' "$*"
}

die() {
    printf '\nERROR: %s\n' "$*" >&2
    exit 1
}

as_root() {
    if [[ "${EUID}" -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        die "This step requires root privileges. Run setup.sh as root or install sudo."
    fi
}

install_system_packages() {
    local packages=()

    command -v espeak-ng >/dev/null 2>&1 || packages+=("espeak-ng")

    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        packages+=("curl")
    fi

    if command -v python3 >/dev/null 2>&1; then
        :
    elif command -v apt-get >/dev/null 2>&1; then
        packages+=("python3")
    elif command -v dnf >/dev/null 2>&1; then
        packages+=("python3")
    elif command -v yum >/dev/null 2>&1; then
        packages+=("python3")
    elif command -v apk >/dev/null 2>&1; then
        packages+=("python3")
    else
        die "No supported package manager found and Python 3 is not installed."
    fi

    if ((${#packages[@]} == 0)); then
        return
    fi

    log "Installing required system packages: ${packages[*]}"

    if command -v apt-get >/dev/null 2>&1; then
        as_root apt-get update
        as_root apt-get install -y --no-install-recommends \
            ca-certificates \
            "${packages[@]}"

    elif command -v dnf >/dev/null 2>&1; then
        as_root dnf install -y ca-certificates "${packages[@]}"

    elif command -v yum >/dev/null 2>&1; then
        as_root yum install -y ca-certificates "${packages[@]}"

    elif command -v apk >/dev/null 2>&1; then
        as_root apk add --no-cache ca-certificates "${packages[@]}"

    else
        die "No supported package manager found."
    fi
}

find_system_python() {
    if command -v python3 >/dev/null 2>&1; then
        SYSTEM_PYTHON="$(command -v python3)"
        return
    fi

    if command -v python >/dev/null 2>&1; then
        SYSTEM_PYTHON="$(command -v python)"
        return
    fi

    die "Python 3 is not available."
}

read_kokoro_metadata() {
    log "Reading current Kokoro metadata from PyPI"

    local metadata

    metadata="$(
        "${SYSTEM_PYTHON}" - <<'PY'
import json
import urllib.request

url = "https://pypi.org/pypi/kokoro/json"

with urllib.request.urlopen(url, timeout=30) as response:
    data = json.load(response)

info = data["info"]

version = info.get("version") or ""
requires_python = info.get("requires_python") or ""

if not version:
    raise SystemExit("PyPI did not provide the current Kokoro version")

if not requires_python:
    raise SystemExit("PyPI did not provide Kokoro Requires-Python metadata")

print(version)
print(requires_python)
PY
    )" || die "Unable to read Kokoro metadata from PyPI."

    KOKORO_VERSION="$(printf '%s\n' "${metadata}" | sed -n '1p')"
    KOKORO_REQUIRES_PYTHON="$(printf '%s\n' "${metadata}" | sed -n '2p')"

    printf 'Current Kokoro: %s\n' "${KOKORO_VERSION}"
    printf 'Requires-Python: %s\n' "${KOKORO_REQUIRES_PYTHON}"
}

install_uv() {
    if [[ -x "${UV_BIN}" ]]; then
        printf 'uv: '
        "${UV_BIN}" --version
        return
    fi

    log "Installing uv locally"

    mkdir -p "${UV_DIR}"

    local installer
    installer="$(mktemp)"

    trap 'rm -f "${installer}"' EXIT

    if command -v curl >/dev/null 2>&1; then
        curl -fsSL \
            https://astral.sh/uv/install.sh \
            -o "${installer}"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "${installer}" \
            https://astral.sh/uv/install.sh
    else
        die "curl or wget is required to install uv."
    fi

    UV_INSTALL_DIR="${UV_DIR}" \
    UV_NO_MODIFY_PATH=1 \
    sh "${installer}"

    rm -f "${installer}"
    trap - EXIT

    [[ -x "${UV_BIN}" ]] || die "uv installation failed."

    printf 'uv: '
    "${UV_BIN}" --version
}

select_compatible_python() {
    log "Finding a Python version compatible with current Kokoro"

    # First search only system interpreters.
    # This deliberately ignores the project's existing .venv.
    if SELECTED_PYTHON="$(
        "${UV_BIN}" python find --system "${KOKORO_REQUIRES_PYTHON}" 2>/dev/null
    )" && [[ -n "${SELECTED_PYTHON}" ]]; then
        printf 'Selected compatible system Python: '
        "${SELECTED_PYTHON}" --version
        return
    fi

    # No compatible system Python exists.
    # Ask uv to install a managed CPython satisfying the exact
    # Requires-Python constraint reported by the current Kokoro.
    log "No compatible system Python found. Installing managed CPython with uv."

    "${UV_BIN}" python install "${KOKORO_REQUIRES_PYTHON}"

    SELECTED_PYTHON="$(
        "${UV_BIN}" python find --managed-python "${KOKORO_REQUIRES_PYTHON}"
    )"

    [[ -n "${SELECTED_PYTHON}" ]] || {
        die "Could not find a Python interpreter satisfying: ${KOKORO_REQUIRES_PYTHON}"
    }

    printf 'Selected uv-managed Python: '
    "${SELECTED_PYTHON}" --version
}

create_venv() {
    log "Creating project-local virtual environment"

    rm -rf "${VENV_DIR}"

    "${UV_BIN}" venv \
        --python "${SELECTED_PYTHON}" \
        --no-project \
        "${VENV_DIR}"

    [[ -x "${VENV_DIR}/bin/python" ]] || {
        die "Failed to create ${VENV_DIR}"
    }

    printf 'Venv Python:\n'
    "${VENV_DIR}/bin/python" --version
}

install_python_packages() {
    local python="${VENV_DIR}/bin/python"

    log "Updating packaging tools"

    "${UV_BIN}" pip install \
        --python "${python}" \
        --upgrade \
        pip \
        setuptools \
        wheel

    log "Installing current CPU PyTorch"

    "${UV_BIN}" pip install \
        --python "${python}" \
        --index-url "https://download.pytorch.org/whl/cpu" \
        torch

    log "Installing current Kokoro and Wyoming"

    # No Kokoro version is pinned.
    # All Kokoro dependencies are resolved from its package metadata.
    "${UV_BIN}" pip install \
        --python "${python}" \
        "kokoro==${KOKORO_VERSION}" \
        wyoming \
        ruaccent \
        phonemizer

    log "Installed package versions"

    "${python}" - <<'PY'
from importlib.metadata import version

for package in (
    "kokoro",
    "wyoming",
    "torch",
):
    try:
        print(f"{package}: {version(package)}")
    except Exception as exc:
        print(f"{package}: unavailable ({exc})")
PY
}

run_upstream_installer() {
    local python="${VENV_DIR}/bin/python"

    [[ -f "${CONFIG_FILE}" ]] || die "Missing ${CONFIG_FILE}"
    [[ -f "${BASE_DIR}/install.py" ]] || die "Missing ${BASE_DIR}/install.py"

    log "Downloading current upstream runtime files, models and voices"

    (
        cd "${BASE_DIR}"
        "${python}" install.py
    )
}

install_missing_ru_g2p_dependencies() {
    local python="${VENV_DIR}/bin/python"
    local attempt
    local missing

    for attempt in $(seq 1 10); do
        missing="$(
            "${python}" - <<'PY'
import sys

try:
    from ru_g2p import RuG2P
    RuG2P()
except ModuleNotFoundError as exc:
    print(exc.name or "")
    sys.exit(10)
PY
        )" || {
            if [[ -n "${missing}" ]]; then
                log "Installing Python module required by ru_g2p.py: ${missing}"

                "${UV_BIN}" pip install \
                    --python "${python}" \
                    "${missing}"

                continue
            fi

            die "ru_g2p.py could not be initialized."
        }

        return
    done

    die "Could not resolve ru_g2p.py dependencies automatically."
}
ensure_project_data() {
    local python="${VENV_DIR}/bin/python"

    log "Checking project data files"

    if [[ ! -f "${BASE_DIR}/espeak-data/ru_dict" || ! -f "${BASE_DIR}/kokoro-config.json" ]]; then
        log "Downloading espeak-data and kokoro-config.json from Hugging Face"

        BASE_DIR="${BASE_DIR}" "${python}" - <<'PY'
import os
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="zaakirio/kokoro-ru",
    local_dir=os.environ["BASE_DIR"],
    allow_patterns=["espeak-data/*", "kokoro-config.json"],
)
PY
    fi

    [[ -f "${BASE_DIR}/espeak-data/ru_dict" ]] || \
        die "Нет espeak-data/ru_dict после загрузки."
    [[ -f "${BASE_DIR}/kokoro-config.json" ]] || \
        die "Нет kokoro-config.json после загрузки."
}

validate_installation() {
    local python="${VENV_DIR}/bin/python"

    log "Validating Python files"

    "${python}" -m py_compile \
        "${BASE_DIR}/wyoming_server.py" \
        "${BASE_DIR}/install.py" \
        "${BASE_DIR}/ru_g2p.py"

    log "Checking installed dependencies"

    "${UV_BIN}" pip check \
        --python "${python}"

    log "Validating generated configuration"

    (
        cd "${BASE_DIR}"

        "${python}" - <<'PY'
import json
from pathlib import Path

base = Path.cwd()

wyoming_path = base / "wyoming.json"
runtime_config_path = base / "config.json"

config = json.loads(
    wyoming_path.read_text(encoding="utf-8")
)

runtime_config = json.loads(
    runtime_config_path.read_text(encoding="utf-8")
)

tts = config.get("tts", {})
voices = tts.get("voices", {})

enabled = [
    name
    for name, item in voices.items()
    if item.get("enabled", True)
]

if not enabled:
    raise SystemExit(
        "install.py completed, but no enabled voices were configured."
    )

default_voice = tts.get("default_voice")

if default_voice not in enabled:
    raise SystemExit(
        f"Invalid default voice: {default_voice!r}; "
        f"enabled voices: {enabled!r}"
    )

paths = config.get("paths", {})

models_dir = base / paths["models_dir"]
voice_dir = base / paths["voice_dir"]

print(f"Configured default: {default_voice}")
print(f"Enabled voices: {tuple(enabled)}")
print(f"Runtime config: {runtime_config_path}")
print(f"Runtime G2P: {base / 'ru_g2p.py'}")
print(f"Models directory: {models_dir}")
print(f"Voices directory: {voice_dir}")
print(
    f"Upstream: "
    f"{config['upstream']['repo']} @ {config['upstream']['revision']}"
)
print(f"Runtime vocab entries: {len(runtime_config['vocab'])}")

if not models_dir.is_dir():
    raise SystemExit(f"Models directory does not exist: {models_dir}")

if not voice_dir.is_dir():
    raise SystemExit(f"Voices directory does not exist: {voice_dir}")
PY
    )
}

main() {
    [[ -f "${CONFIG_FILE}" ]] || {
        die "Missing ${CONFIG_FILE}"
    }

    log "Preparing Kokoro Russian Wyoming TTS"

    printf 'Project directory: %s\n' "${BASE_DIR}"

    install_system_packages
    find_system_python
    read_kokoro_metadata
    install_uv
    select_compatible_python
    create_venv
    install_python_packages
    run_upstream_installer
    ensure_project_data
    install_missing_ru_g2p_dependencies
    validate_installation

    log "Setup completed successfully"
}

main "$@"
