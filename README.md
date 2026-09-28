# Kokoro Wyoming TTS

Russian text-to-speech server based on [Kokoro](https://github.com/hexgrad/kokoro) and the [`zaakirio/kokoro-ru`](https://huggingface.co/zaakirio/kokoro-ru) project.

The server exposes a Wyoming TTS interface for Home Assistant and supports streaming playback.

## Features

- Russian Kokoro TTS
- Wyoming protocol
- Streaming TTS
- Immediate transmission of the first generated audio chunk
- Short PCM silence insertion when the producer falls behind
- Automatic voice and model synchronization from `zaakirio/kokoro-ru`
- No voice-specific logic in the server
- No hardcoded model mapping in the server
- CPU PyTorch inference
- Portable project directory
- Automatic local Python virtual environment
- Optional systemd service installation

## Repository layout

```text
.
├── .gitattributes
├── .gitignore
├── install.py
├── install_service.sh
├── setup.sh
├── wyoming.json
├── wyoming_server.py
├── README.md
└── README.ru.md
```

The following files are runtime/generated files and are intentionally not stored in Git:

```text
.venv/
config.json
ru_g2p.py
models/
voices/
```

## Requirements

- Linux
- Internet access for PyPI and Hugging Face
- Permission to install required system packages during setup

The setup script discovers a Python interpreter compatible with the current Kokoro package instead of hardcoding a Python version. It also installs required system dependencies such as `espeak-ng` when needed.

## Installation

Clone the repository:

```bash
git clone https://github.com/olegchumakov/kokoro-ru-wyoming.git
cd kokoro-wyoming
```

Run the environment setup:

```bash
./setup.sh
```

The setup script:

1. Finds a Python interpreter compatible with the current Kokoro package.
2. Creates `.venv/` inside the project directory.
3. Installs Kokoro and its declared Python dependencies.
4. Installs the CPU PyTorch build.
5. Installs Wyoming.
6. Installs required system dependencies when needed.
7. Runs `install.py`.
8. Validates the resulting environment.

Install the optional systemd service:

```bash
./install_service.sh
```

Start the service:

```bash
systemctl start kokoro-wyoming
```

Check the service:

```bash
systemctl status kokoro-wyoming
```

Follow the logs:

```bash
journalctl -u kokoro-wyoming -f
```

A successful startup ends with messages similar to:

```text
Kokoro warmup complete
Listening on tcp://0.0.0.0:10200
```

## Updating voices and models

Run:

```bash
.venv/bin/python install.py
```

`install.py` reads the configured upstream repository and synchronizes the runtime files, voices, and model checkpoints.

It automatically:

- downloads the current `config.json`;
- downloads the current `ru_g2p.py`;
- discovers the current voice list;
- determines which checkpoint each voice uses;
- downloads only the required checkpoints;
- downloads the corresponding voice files;
- updates `wyoming.json`;
- preserves local voice settings such as `enabled` and custom descriptions;
- removes obsolete voices and managed model/voice files.

Adding a new voice upstream does not require changes to `wyoming_server.py`.

## Configuration

The main configuration file is `wyoming.json`.

It contains application-level settings such as:

- upstream repository;
- Wyoming listener URI;
- audio format;
- streaming parameters;
- PyTorch thread count;
- logging level;
- TTS metadata;
- default voice;
- local runtime paths.

The upstream voice-to-checkpoint mapping is maintained by `install.py`.

### Example: change the default voice

```json
"default_voice": "masha"
```

### Example: change underrun silence

```json
"silence_seconds": 0.2
```

### Example: change the Wyoming listener

```json
"uri": "tcp://0.0.0.0:10200"
```

## Architecture

The project intentionally keeps upstream assets out of the Git repository.

```text
Git repository
    |
    +-- setup.sh
    +-- install.py
    +-- wyoming_server.py
    +-- wyoming.json
    +-- install_service.sh
    |
    v
Local installation
    |
    +-- .venv/
    +-- config.json
    +-- ru_g2p.py
    +-- models/
    +-- voices/
```

Voice/model discovery is generic:

```text
voice -> voice file -> checkpoint
```

The server does not contain special cases for individual voice names.

## Manual run

After setup:

```bash
.venv/bin/python wyoming_server.py
```

## Moving the project

The project directory is portable.

`wyoming_server.py` determines its own base directory from the location of the script, and relative paths in `wyoming.json` are resolved from that directory.

If the project is moved and the systemd service is used, regenerate the service:

```bash
./install_service.sh
```

The service file uses the current project directory and its local `.venv`; it does not depend on a fixed installation path.

## License

This repository contains the Wyoming server and installation/configuration scripts.

Kokoro, the `zaakirio/kokoro-ru` runtime files, models, and voices are provided by their respective upstream projects and are subject to their own licenses and terms.
