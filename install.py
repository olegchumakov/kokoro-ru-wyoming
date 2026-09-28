#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

from huggingface_hub import HfApi, hf_hub_download


# =============================================================================
# Project
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent
WYOMING_CONFIG = BASE_DIR / "wyoming.json"

if not WYOMING_CONFIG.is_file():
    raise SystemExit(f"ERROR: {WYOMING_CONFIG} not found")


# =============================================================================
# Configuration
# =============================================================================

with WYOMING_CONFIG.open("r", encoding="utf-8") as f:
    CONFIG = json.load(f)

UPSTREAM = CONFIG["upstream"]
REPO_ID = UPSTREAM["repo"]
REVISION = UPSTREAM["revision"]


# =============================================================================
# Helpers
# =============================================================================

def save_config() -> None:
    WYOMING_CONFIG.write_text(
        json.dumps(CONFIG, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)

    temporary = target.with_name(
        target.name + ".tmp"
    )

    shutil.copy2(source, temporary)
    os.replace(temporary, target)


def download_upstream(filename: str, target: Path) -> None:
    print(f"Downloading {filename}...")

    cached = hf_hub_download(
        repo_id=REPO_ID,
        filename=filename,
        revision=REVISION,
    )

    atomic_copy(Path(cached), target)

    print(f"  -> {target}")


def model_target_path(filename: str) -> str:
    models_dir = CONFIG["paths"]["models_dir"]
    return f"{models_dir}/{PurePosixPath(filename).name}"


# =============================================================================
# Voice discovery
# =============================================================================

def parse_upstream_voices(readme: str) -> dict[str, dict]:
    """
    Read the current Voices table from upstream README.

    Current upstream format:

        ## Voices

        ```
        voice gender checkpoint
        ...
        ```
    """

    match = re.search(
        r"##\s+Voices\s+```(?:[^\n]*)\n(.*?)```",
        readme,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not match:
        raise RuntimeError(
            "Could not find '## Voices' table in upstream README"
        )

    voices: dict[str, dict] = {}

    for raw_line in match.group(1).splitlines():
        line = raw_line.strip()

        if not line:
            continue

        parts = line.split()

        if len(parts) < 3:
            continue

        if parts[0].lower() == "voice":
            continue

        voice = parts[0]
        gender = parts[1]
        checkpoint = parts[2]

        voices[voice] = {
            "gender": gender,
            "checkpoint": checkpoint,
        }

    if not voices:
        raise RuntimeError(
            "Voices table was found, but no voices were parsed"
        )

    return voices


# =============================================================================
# Wyoming configuration synchronization
# =============================================================================

def sync_voice_config(
    upstream_voices: dict[str, dict],
) -> None:
    tts = CONFIG.setdefault("tts", {})
    local_voices = tts.setdefault("voices", {})

    old_default = tts.get("default_voice")

    # -------------------------------------------------------------------------
    # Remove voices no longer present upstream.
    # -------------------------------------------------------------------------

    obsolete = set(local_voices) - set(upstream_voices)

    for voice in sorted(obsolete):
        print(f"Removing obsolete voice from config: {voice}")
        del local_voices[voice]

    # -------------------------------------------------------------------------
    # Add/update upstream voices.
    # -------------------------------------------------------------------------

    new_voice_config: dict[str, dict] = {}

    for voice, remote in upstream_voices.items():
        existing = local_voices.get(voice, {})

        checkpoint = remote["checkpoint"]
        previous_checkpoint = existing.get("model")

        if previous_checkpoint != checkpoint:
            if previous_checkpoint is None:
                print(f"New voice: {voice}")
            else:
                print(
                    f"Model changed for {voice}: "
                    f"{previous_checkpoint} -> {checkpoint}"
                )

        entry = dict(existing)

        # Managed fields
        entry["enabled"] = bool(entry.get("enabled", True))
        entry["file"] = f"{voice}.pt"
        entry["model"] = model_target_path(checkpoint)
        entry["gender"] = remote["gender"]

        # Keep custom description if user has one.
        entry.setdefault(
            "description",
            voice,
        )

        new_voice_config[voice] = entry

    tts["voices"] = new_voice_config

    # -------------------------------------------------------------------------
    # Preserve default voice if still available.
    # Otherwise choose the first enabled upstream voice.
    # -------------------------------------------------------------------------

    enabled_voice_names = tuple(
        voice
        for voice, cfg in new_voice_config.items()
        if cfg.get("enabled", True)
    )

    if old_default in enabled_voice_names:
        tts["default_voice"] = old_default
    elif enabled_voice_names:
        first_voice = enabled_voice_names[0]
        tts["default_voice"] = first_voice

        print(
            f"Default voice changed: "
            f"{old_default!r} -> {first_voice!r}"
        )
    else:
        tts["default_voice"] = None

        print(
            "No enabled voices; default voice set to None"
        )


# =============================================================================
# Asset cleanup
# =============================================================================

def cleanup_obsolete_assets(
    upstream_voices: dict[str, dict],
) -> None:
    voices_dir = BASE_DIR / CONFIG["paths"]["voice_dir"]
    models_dir = BASE_DIR / CONFIG["paths"]["models_dir"]

    desired_voice_files = {
        f"{voice}.pt"
        for voice in upstream_voices
    }

    desired_model_files = {
        PurePosixPath(info["checkpoint"]).name
        for info in upstream_voices.values()
    }

    if voices_dir.is_dir():
        for path in voices_dir.glob("*.pt"):
            if path.name not in desired_voice_files:
                print(f"Removing obsolete voice file: {path}")
                path.unlink()

    if models_dir.is_dir():
        for path in models_dir.glob("*.pth"):
            if path.name not in desired_model_files:
                print(f"Removing obsolete model file: {path}")
                path.unlink()


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    print()
    print("==================================================")
    print(" Kokoro RU - upstream synchronization")
    print("==================================================")
    print()
    print(f"Project:   {BASE_DIR}")
    print(f"Upstream:  {REPO_ID}")
    print(f"Revision:  {REVISION}")
    print()

    api = HfApi()

    print("Reading upstream repository...")
    files = set(
        api.list_repo_files(
            repo_id=REPO_ID,
            repo_type="model",
        )
    )

    # -------------------------------------------------------------------------
    # Required frontend files.
    # -------------------------------------------------------------------------

    for required in ("README.md", "config.json", "ru_g2p.py"):
        if required not in files:
            raise RuntimeError(
                f"Required upstream file missing: {required}"
            )

    # -------------------------------------------------------------------------
    # README is used only as metadata source.
    # -------------------------------------------------------------------------

    readme_cache = hf_hub_download(
        repo_id=REPO_ID,
        filename="README.md",
        revision=REVISION,
    )

    readme = Path(readme_cache).read_text(encoding="utf-8")

    upstream_voices = parse_upstream_voices(readme)

    print()
    print("Upstream voices:")

    for voice, info in upstream_voices.items():
        print(
            f"  {voice:<12} "
            f"{info['gender']:<8} "
            f"{info['checkpoint']}"
        )

    # -------------------------------------------------------------------------
    # Download runtime frontend.
    # -------------------------------------------------------------------------

    download_upstream(
        "config.json",
        BASE_DIR / "config.json",
    )

    download_upstream(
        "ru_g2p.py",
        BASE_DIR / "ru_g2p.py",
    )

    # -------------------------------------------------------------------------
    # Download all checkpoints referenced by current voices.
    # -------------------------------------------------------------------------

    models_dir = BASE_DIR / CONFIG["paths"]["models_dir"]
    models_dir.mkdir(parents=True, exist_ok=True)

    required_models = {
        info["checkpoint"]
        for info in upstream_voices.values()
    }

    for checkpoint in sorted(required_models):
        if checkpoint not in files:
            raise RuntimeError(
                f"Checkpoint referenced by voice is missing upstream: "
                f"{checkpoint}"
            )

        target = models_dir / PurePosixPath(checkpoint).name

        download_upstream(
            checkpoint,
            target,
        )

    # -------------------------------------------------------------------------
    # Download all .pt voicepacks.
    # -------------------------------------------------------------------------

    voices_dir = BASE_DIR / CONFIG["paths"]["voice_dir"]
    voices_dir.mkdir(parents=True, exist_ok=True)

    for voice in sorted(upstream_voices):
        filename = f"voices/{voice}.pt"

        if filename not in files:
            raise RuntimeError(
                f"Voice pack missing upstream: {filename}"
            )

        download_upstream(
            filename,
            voices_dir / f"{voice}.pt",
        )

    # -------------------------------------------------------------------------
    # Update paths in our config.
    # -------------------------------------------------------------------------

    paths = CONFIG.setdefault("paths", {})

    paths["config"] = "config.json"
    paths["voice_dir"] = "voices"
    paths["models_dir"] = "models"

    # -------------------------------------------------------------------------
    # Sync voice mapping.
    # -------------------------------------------------------------------------

    sync_voice_config(upstream_voices)

    # -------------------------------------------------------------------------
    # Ensure a few upstream values exist without overwriting local custom
    # settings.
    # -------------------------------------------------------------------------

    CONFIG.setdefault(
        "server",
        {
            "uri": "tcp://0.0.0.0:10200"
        },
    )

    CONFIG.setdefault(
        "logging",
        {
            "level": "INFO"
        },
    )

    CONFIG.setdefault("tts", {}).setdefault("version", "1.0")

    CONFIG["upstream"] = {
        "repo": REPO_ID,
        "revision": REVISION,
    }

    # -------------------------------------------------------------------------
    # Write config only after ALL downloads succeeded.
    # -------------------------------------------------------------------------

    save_config()

    # -------------------------------------------------------------------------
    # Remove old managed assets only after successful synchronization.
    # -------------------------------------------------------------------------

    cleanup_obsolete_assets(upstream_voices)

    print()
    print("==================================================")
    print(" SYNC COMPLETE")
    print("==================================================")
    print()
    print(
        "Voices:",
        ", ".join(upstream_voices),
    )
    print(
        "Default:",
        CONFIG["tts"]["default_voice"],
    )
    print()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        print("Interrupted.")
        raise SystemExit(130)
    except Exception as exc:
        print()
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
