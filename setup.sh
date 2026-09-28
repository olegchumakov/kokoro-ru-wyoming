#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${BASE_DIR}/wyoming.json"
VENV_DIR="${BASE_DIR}/.venv"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

echo
echo "=================================================="
echo " Kokoro RU - environment setup"
echo "=================================================="
echo
echo "Project: ${BASE_DIR}"
echo "Venv:    ${VENV_DIR}"
echo

# ---------------------------------------------------------------------------
# System dependencies
# ---------------------------------------------------------------------------

install_system_package() {
    local package="$1"

    if command -v apt-get >/dev/null 2>&1; then
        echo "Installing system package: ${package}"
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y "${package}"
        return
    fi

    if command -v dnf >/dev/null 2>&1; then
        echo "Installing system package: ${package}"
        dnf install -y "${package}"
        return
    fi

    if command -v yum >/dev/null 2>&1; then
        echo "Installing system package: ${package}"
        yum install -y "${package}"
        return
    fi

    if command -v apk >/dev/null 2>&1; then
        echo "Installing system package: ${package}"
        apk add "${package}"
        return
    fi

    echo "ERROR: no supported package manager found."
    echo "Cannot install required system package: ${package}"
    exit 1
}

ensure_espeak_ng() {
    if command -v espeak-ng >/dev/null 2>&1; then
        echo "espeak-ng: OK"
        return
    fi

    echo "espeak-ng not found."
    install_system_package "espeak-ng"

    if ! command -v espeak-ng >/dev/null 2>&1; then
        echo "ERROR: espeak-ng installation failed."
        exit 1
    fi

    echo "espeak-ng: installed"
}

ensure_espeak_ng

echo

# ---------------------------------------------------------------------------
# Basic checks
# ---------------------------------------------------------------------------

if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "ERROR: ${CONFIG_FILE} not found."
    exit 1
fi

# ---------------------------------------------------------------------------
# Read upstream information from wyoming.json
# ---------------------------------------------------------------------------

read_config() {
    python3 - "$CONFIG_FILE" "$1" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    cfg = json.load(f)

value = cfg

for part in sys.argv[2].split("."):
    value = value[part]

print(value)
PY
}

UPSTREAM_REPO="$(read_config "upstream.repo")"
UPSTREAM_REVISION="$(read_config "upstream.revision")"

echo "Upstream: ${UPSTREAM_REPO}"
echo "Revision: ${UPSTREAM_REVISION}"
echo

# ---------------------------------------------------------------------------
# Find Python compatible with the CURRENT Kokoro package.
#
# No Python version is hardcoded. Each available python3.X is tested by pip
# against Kokoro's own Requires-Python metadata.
# ---------------------------------------------------------------------------

echo "Searching for compatible Python..."

mapfile -t PYTHON_CANDIDATES < <(
    {
        command -v python3 2>/dev/null || true
        compgen -c 2>/dev/null | grep -E '^python3\.[0-9]+$' || true
    } | sort -Vu
)

SELECTED_PYTHON=""

for candidate in "${PYTHON_CANDIDATES[@]}"; do
    [[ -x "${candidate}" ]] || continue

    version="$(
        "${candidate}" -c \
        'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")'
    )"

    test_venv="${TMP_DIR}/venv-${RANDOM}"

    echo "  Testing ${candidate} (${version})..."

    if ! "${candidate}" -m venv "${test_venv}" >/dev/null 2>&1; then
        echo "    venv unavailable"
        continue
    fi

    test_python="${test_venv}/bin/python"

    if "${test_python}" -m pip install \
        --quiet \
        --disable-pip-version-check \
        --dry-run \
        --ignore-installed \
        --no-deps \
        kokoro >/dev/null 2>&1
    then
        SELECTED_PYTHON="${candidate}"
        echo "    compatible"
        rm -rf "${test_venv}"
        break
    fi

    echo "    incompatible"
    rm -rf "${test_venv}"
done

if [[ -z "${SELECTED_PYTHON}" ]]; then
    echo
    echo "ERROR: no compatible Python interpreter found."
    exit 1
fi

echo
echo "Selected Python:"
echo "  ${SELECTED_PYTHON}"
echo "  $(${SELECTED_PYTHON} --version)"
echo

# ---------------------------------------------------------------------------
# Create local venv
# ---------------------------------------------------------------------------

if [[ -d "${VENV_DIR}" ]]; then
    echo "Existing .venv found."
else
    echo "Creating .venv..."

    if ! "${SELECTED_PYTHON}" -m venv "${VENV_DIR}"; then
        echo
        echo "ERROR: unable to create virtual environment."
        echo "Install the OS package providing venv for this Python."
        exit 1
    fi
fi

PY="${VENV_DIR}/bin/python"
PIP="${PY} -m pip"

echo
echo "Venv Python:"
echo "  $(${PY} --version)"
echo

# ---------------------------------------------------------------------------
# Packaging tools
# ---------------------------------------------------------------------------

echo "Updating packaging tools..."

${PIP} install --upgrade \
    pip \
    setuptools \
    wheel

# ---------------------------------------------------------------------------
# Install Kokoro WITHOUT dependencies first.
#
# Then read Kokoro's own package metadata and install exactly what it declares.
# ---------------------------------------------------------------------------

echo
echo "Installing current Kokoro package..."

${PIP} install \
    --upgrade \
    --no-deps \
    kokoro

# ---------------------------------------------------------------------------
# Read Kokoro dependencies.
# ---------------------------------------------------------------------------

mapfile -t KOKORO_DEPS < <(
    "${PY}" <<'PY'
from importlib.metadata import requires
from packaging.requirements import Requirement

for req in requires("kokoro") or []:
    try:
        name = Requirement(req).name.lower()
    except Exception:
        name = req.split(";", 1)[0].split("[", 1)[0].strip().lower()

    if name == "torch":
        continue

    print(req)
PY
)

echo
echo "Kokoro dependencies:"
printf '  %s\n' "${KOKORO_DEPS[@]}"

if [[ "${#KOKORO_DEPS[@]}" -gt 0 ]]; then
    ${PIP} install --upgrade "${KOKORO_DEPS[@]}"
fi

# ---------------------------------------------------------------------------
# Install CPU PyTorch.
#
# The dependency name comes from Kokoro metadata; only the package source is
# specialized because this project intentionally uses CPU inference.
# ---------------------------------------------------------------------------

echo
echo "Installing CPU PyTorch..."

${PIP} install \
    --upgrade \
    torch \
    --index-url https://download.pytorch.org/whl/cpu

# ---------------------------------------------------------------------------
# Our direct application dependency.
# ---------------------------------------------------------------------------

echo
echo "Installing Wyoming..."

${PIP} install --upgrade wyoming

# ---------------------------------------------------------------------------
# Download/synchronize Kokoro-RU runtime assets.
#
# install.py obtains:
#   config.json
#   ru_g2p.py
#   models
#   voices
# and updates wyoming.json accordingly.
# ---------------------------------------------------------------------------

echo
echo "Synchronizing Kokoro-RU assets..."

"${PY}" "${BASE_DIR}/install.py"

# ---------------------------------------------------------------------------
# Resolve dependencies used by current ru_g2p.py.
#
# We do not hardcode ruaccent or any other package name here.
# If the current upstream frontend imports a missing Python module, install
# a distribution with that name and retry.
# ---------------------------------------------------------------------------

echo
echo "Checking ru_g2p dependencies..."

RUG2P_OK=0

for _ in $(seq 1 10); do
    output="$(
        "${PY}" - "${BASE_DIR}" <<'PY' 2>&1 || true
import sys

sys.path.insert(0, sys.argv[1])

from ru_g2p import RuG2P

print("RUG2P_IMPORT_OK")
PY
    )"

    if grep -q '^RUG2P_IMPORT_OK$' <<< "${output}"; then
        echo "  ru_g2p import: OK"
        RUG2P_OK=1
        break
    fi

    module="$(
        sed -n \
            "s/^ModuleNotFoundError: No module named ['\"]\\([^'\"]*\\)['\"].*$/\\1/p" \
            <<< "${output}" \
        | head -n 1
    )"

    if [[ -z "${module}" ]]; then
        echo
        echo "ERROR: ru_g2p.py could not be imported."
        echo
        echo "${output}"
        exit 1
    fi

    echo "  Missing module: ${module}"
    echo "  Installing: ${module}"

    ${PIP} install --upgrade "${module}"
done

if [[ "${RUG2P_OK}" -ne 1 ]]; then
    echo "ERROR: unable to resolve ru_g2p dependencies."
    exit 1
fi

# ---------------------------------------------------------------------------
# Validate our server module.
# ---------------------------------------------------------------------------

echo
echo "Checking wyoming_server.py..."

"${PY}" - "${BASE_DIR}" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])

import wyoming_server as w

print("  import: OK")
print("  BASE_DIR:", w.BASE_DIR)
print("  default voice:", w.DEFAULT_VOICE)
print("  voices:", w.VOICE_NAMES)
print("  sample rate:", w.SAMPLE_RATE)
print("  max tokens:", w.MAX_PHONEME_TOKENS)
print("  silence:", w.SILENCE_SECONDS)
print("  URI:", w.APP_CONFIG["server"]["uri"])
PY

# ---------------------------------------------------------------------------
# Final dependency check
# ---------------------------------------------------------------------------

echo
echo "Running pip check..."

${PIP} check

# ---------------------------------------------------------------------------
# Print versions for information only.
# Nothing is written into project configuration.
# ---------------------------------------------------------------------------

KOKORO_VERSION="$(
    "${PY}" -c \
    'from importlib.metadata import version; print(version("kokoro"))'
)"

WYOMING_VERSION="$(
    "${PY}" -c \
    'from importlib.metadata import version; print(version("wyoming"))'
)"

echo
echo "=================================================="
echo " SETUP COMPLETE"
echo "=================================================="
echo
echo "Python:  $(${PY} --version)"
echo "Kokoro: ${KOKORO_VERSION}"
echo "Wyoming: ${WYOMING_VERSION}"
echo
echo "Environment:"
echo "  ${VENV_DIR}"
echo
echo "Activate:"
echo "  source ${VENV_DIR}/bin/activate"
echo
echo "Update models/voices:"
echo "  ${PY} ${BASE_DIR}/install.py"
echo
echo "=================================================="
