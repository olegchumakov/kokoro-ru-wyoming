#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import asyncio
import logging
import re
import signal
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Optional

import numpy as np
import torch
from kokoro import KModel

from ru_g2p import RuG2P

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
WYOMING_CONFIG_PATH = BASE_DIR / "wyoming.json"

with WYOMING_CONFIG_PATH.open("r", encoding="utf-8") as f:
    APP_CONFIG = json.load(f)

PATHS_CONFIG = APP_CONFIG["paths"]
AUDIO_CONFIG = APP_CONFIG["audio"]
STREAMING_CONFIG = APP_CONFIG["streaming"]
TTS_CONFIG = APP_CONFIG["tts"]
LOGGING_CONFIG = APP_CONFIG.get("logging", {"level": "INFO"})

CONFIG_PATH = BASE_DIR / PATHS_CONFIG["config"]
VOICE_DIR = BASE_DIR / PATHS_CONFIG["voice_dir"]

SAMPLE_RATE = int(AUDIO_CONFIG["sample_rate"])
SAMPLE_WIDTH = int(AUDIO_CONFIG["sample_width"])
CHANNELS = int(AUDIO_CONFIG["channels"])
AUDIO_CHUNK_BYTES = int(AUDIO_CONFIG["chunk_bytes"])

VOICE_CONFIG = TTS_CONFIG["voices"]
VOICE_NAMES = tuple(
    name
    for name, cfg in VOICE_CONFIG.items()
    if cfg.get("enabled", True)
)

if not VOICE_NAMES:
    raise ValueError("No enabled voices configured")

_configured_default_voice = TTS_CONFIG.get("default_voice")

if _configured_default_voice in VOICE_NAMES:
    DEFAULT_VOICE = _configured_default_voice
else:
    DEFAULT_VOICE = VOICE_NAMES[0]

# One Kokoro inference at a time. This is intentional on CPU.
TORCH_THREADS = int(APP_CONFIG["torch"]["threads"])
TORCH_INTEROP_THREADS = int(APP_CONFIG["torch"]["interop_threads"])

MAX_PHONEME_TOKENS = int(STREAMING_CONFIG["max_phoneme_tokens"])

SILENCE_SECONDS = float(STREAMING_CONFIG["silence_seconds"])
UNDERRUN_WAIT_SECONDS = float(STREAMING_CONFIG["underrun_wait_seconds"])

START_BUFFER_SECONDS = float(STREAMING_CONFIG["start_buffer_seconds"])

# -----------------------------------------------------------------------------

LOG = logging.getLogger("kokoro-wyoming")

_log_level_name = str(LOGGING_CONFIG["level"]).upper()
_log_level = getattr(logging, _log_level_name, None)

if not isinstance(_log_level, int):
    raise ValueError(
        f"Invalid logging level in wyoming.json: {_log_level_name!r}"
    )

LOG.setLevel(_log_level)
logging.getLogger().setLevel(_log_level)



def configure_torch() -> None:
    torch.set_num_threads(TORCH_THREADS)
    try:
        torch.set_num_interop_threads(TORCH_INTEROP_THREADS)
    except RuntimeError:
        # PyTorch only allows this once during process startup.
        pass


def normalize_voice(name: Optional[str]) -> str:
    if not name:
        return DEFAULT_VOICE
    name = name.lower().strip()
    if name in VOICE_NAMES:
        return name
    LOG.warning("Unknown voice %r, using %s", name, DEFAULT_VOICE)
    return DEFAULT_VOICE


@dataclass
class StreamState:
    voice: str
    text_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    audio_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    text_buffer: str = ""
    producer_done: asyncio.Event = field(default_factory=asyncio.Event)
    error: Optional[BaseException] = None
    audio_started: bool = False
    producer_task: Optional[asyncio.Task] = None
    consumer_task: Optional[asyncio.Task] = None


# -----------------------------------------------------------------------------
# Kokoro backend
# -----------------------------------------------------------------------------

class KokoroEngine:
    def __init__(self) -> None:
        configure_torch()

        if not CONFIG_PATH.is_file():
            raise FileNotFoundError(CONFIG_PATH)

        self.g2p = RuG2P()
        self.models: dict[str, KModel] = {}
        self.voices: dict[str, torch.Tensor] = {}
        self.model_paths: dict[str, Path] = {}
        self.infer_lock = asyncio.Lock()

        for voice in VOICE_NAMES:
            voice_cfg = VOICE_CONFIG[voice]
            voice_path = VOICE_DIR / voice_cfg["file"]

            if not voice_path.is_file():
                raise FileNotFoundError(voice_path)

            self.voices[voice] = torch.load(
                voice_path,
                map_location="cpu",
                weights_only=False,
            ).contiguous()

        for voice in VOICE_NAMES:
            voice_cfg = VOICE_CONFIG[voice]

            model_path = BASE_DIR / voice_cfg["model"]

            if not model_path.is_file():
                raise FileNotFoundError(model_path)

            self.model_paths[voice] = model_path

        # Load the default model immediately so the service is hot.
        self._load_model_sync(DEFAULT_VOICE)
        self._warmup_sync(DEFAULT_VOICE)

    def _load_model_sync(self, voice: str) -> KModel:
        if voice in self.models:
            return self.models[voice]

        model_path = self.model_paths[voice]
        LOG.info("Loading Kokoro model for voice=%s: %s", voice, model_path)

        model = KModel(
            config=str(CONFIG_PATH),
            model=str(model_path),
        ).to("cpu").eval()

        self.models[voice] = model
        LOG.info("Kokoro model ready for voice=%s", voice)
        return model

    def _warmup_sync(self, voice: str) -> None:
        model = self._load_model_sync(voice)
        pack = self.voices[voice]
        text = STREAMING_CONFIG["warmup_text"]
        ipa, _ = self.g2p(text)
        if not ipa:
            return

        # Warmup only. The result is discarded.
        with torch.inference_mode():
            _ = model(
                ipa,
                pack[min(len(ipa) - 1, pack.shape[0] - 1)],
                1.0,
                return_output=True,
            )
        LOG.info("Kokoro warmup complete")

    def _split_ipa(self, ipa: str, model: KModel) -> list[str]:
        """Split IPA at word boundaries while keeping <= MAX_PHONEME_TOKENS."""
        words = ipa.split()
        if not words:
            return []

        result: list[str] = []
        current: list[str] = []
        current_tokens = 0

        for word in words:
            word_tokens = sum(1 for ch in word if model.vocab.get(ch) is not None)

            # A single word should almost never exceed this limit. If it does,
            # split its IPA safely by character so the model context limit is
            # never hit.
            if word_tokens > MAX_PHONEME_TOKENS:
                if current:
                    result.append(" ".join(current))
                    current = []
                    current_tokens = 0

                piece: list[str] = []
                piece_tokens = 0
                for ch in word:
                    if model.vocab.get(ch) is None:
                        continue
                    if piece and piece_tokens >= MAX_PHONEME_TOKENS:
                        result.append("".join(piece))
                        piece = []
                        piece_tokens = 0
                    piece.append(ch)
                    piece_tokens += 1
                if piece:
                    result.append("".join(piece))
                continue

            if current and current_tokens + word_tokens > MAX_PHONEME_TOKENS:
                result.append(" ".join(current))
                current = []
                current_tokens = 0

            current.append(word)
            current_tokens += word_tokens

        if current:
            result.append(" ".join(current))

        return result

    def _synthesize_sync(
        self,
        text: str,
        voice: str,
        on_chunk=None,
    ) -> list[bytes]:
        voice = normalize_voice(voice)
        model = self._load_model_sync(voice)
        pack = self.voices[voice]

        started = time.perf_counter()
        ipa, oov = self.g2p(text)
        if not ipa:
            return []

        parts = self._split_ipa(ipa, model)
        LOG.info(
            "Synthesis: voice=%s chunks=%d text=%r",
            voice,
            len(parts),
            text,
        )

        pcm_parts: list[bytes] = []

        for idx, part in enumerate(parts, start=1):
            input_count = sum(1 for ch in part if model.vocab.get(ch) is not None)
            if input_count <= 0:
                continue

            style_index = min(len(part) - 1, pack.shape[0] - 1)
            if style_index < 0:
                continue

            chunk_start = time.perf_counter()
            with torch.inference_mode():
                output = model(
                    part,
                    pack[style_index],
                    1.0,
                    return_output=True,
                )

            audio = output.audio
            if not isinstance(audio, torch.Tensor):
                audio = torch.as_tensor(audio)
            audio = audio.detach().cpu().float().numpy().reshape(-1)
            audio = np.clip(audio, -1.0, 1.0)
            pcm = (audio * 32767.0).astype(np.int16).tobytes()
            pcm_parts.append(pcm)

            # Ключевой момент: не ждём окончания всего текста.
            # Каждый готовый Kokoro chunk сразу передаём streaming producer'у.
            if on_chunk is not None:
                on_chunk(pcm)

            duration = len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
            elapsed = time.perf_counter() - chunk_start
            rtf = elapsed / duration if duration > 0 else 0.0
            LOG.info(
                "Chunk %d/%d: %.3f sec, audio=%.3f sec, RTF=%.3f, tokens=%d",
                idx,
                len(parts),
                elapsed,
                duration,
                rtf,
                input_count,
            )

        total_elapsed = time.perf_counter() - started
        total_audio = sum(len(x) for x in pcm_parts) / (
            SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS
        )
        total_rtf = total_elapsed / total_audio if total_audio > 0 else 0.0
        LOG.info(
            "Synthesis done: %.3f sec, audio=%.3f sec, RTF=%.3f, oov=%s",
            total_elapsed,
            total_audio,
            total_rtf,
            bool(oov),
        )
        return pcm_parts

    async def synthesize(self, text: str, voice: str) -> list[bytes]:
        # Legacy/non-streaming path.
        async with self.infer_lock:
            return await asyncio.to_thread(self._synthesize_sync, text, voice)

    async def synthesize_stream(
        self,
        text: str,
        voice: str,
        audio_queue: asyncio.Queue,
    ) -> list[bytes]:
        """
        Synthesize one text item while immediately pushing every generated
        PCM chunk into the shared streaming audio queue.
        """
        loop = asyncio.get_running_loop()

        def emit_chunk(pcm: bytes) -> None:
            # This callback runs inside the worker thread, so enqueue through
            # the event loop instead of touching asyncio.Queue directly.
            loop.call_soon_threadsafe(audio_queue.put_nowait, pcm)

        async with self.infer_lock:
            return await asyncio.to_thread(
                self._synthesize_sync,
                text,
                voice,
                emit_chunk,
            )


# -----------------------------------------------------------------------------
# Text streaming
# -----------------------------------------------------------------------------

# A sentence ends on ., !, ?, … or a newline. Closing quotes/brackets are kept
# with the sentence.
SENTENCE_RE = re.compile(
    r"(.+?(?:[.!?…]+[\"'»”’\)\]]*|\n+))(?:\s+|$)",
    re.S,
)


def extract_sentences(buffer: str, final: bool = False) -> tuple[list[str], str]:
    sentences: list[str] = []
    consumed = 0

    for match in SENTENCE_RE.finditer(buffer):
        sentence = match.group(1).strip()
        if sentence:
            sentences.append(sentence)
        consumed = match.end()

    rest = buffer[consumed:]

    if final and rest.strip():
        sentences.append(rest.strip())
        rest = ""

    return sentences, rest


def silence_pcm(seconds: float) -> bytes:
    frames = int(SAMPLE_RATE * seconds)
    return b"\x00" * (frames * SAMPLE_WIDTH * CHANNELS)


# -----------------------------------------------------------------------------
# Wyoming handler
# -----------------------------------------------------------------------------

class KokoroEventHandler(AsyncEventHandler):
    def __init__(self, engine: KokoroEngine, info: Info, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.engine = engine
        self.info = info
        self.stream: Optional[StreamState] = None

    async def handle_event(self, event: Event) -> bool:
        try:
            if Describe.is_type(event.type):
                await self.write_event(self.info.event())
                return True

            # Streaming path -------------------------------------------------
            if SynthesizeStart.is_type(event.type):
                if self.stream is not None:
                    await self._stop_stream()

                req = SynthesizeStart.from_event(event)
                voice = normalize_voice(req.voice.name if req.voice else None)
                state = StreamState(voice=voice)
                self.stream = state

                state.producer_task = asyncio.create_task(
                    self._producer_loop(state),
                    name="kokoro-tts-producer",
                )
                state.consumer_task = asyncio.create_task(
                    self._consumer_loop(state),
                    name="kokoro-tts-consumer",
                )

                LOG.info(
                    "TTS STREAM START: voice=%s buffer=%.1fs silence=%.1fs",
                    voice,
                    START_BUFFER_SECONDS,
                    SILENCE_SECONDS,
                )
                return True

            if SynthesizeChunk.is_type(event.type):
                if self.stream is None:
                    return True

                state = self.stream
                chunk = SynthesizeChunk.from_event(event)
                state.text_buffer += chunk.text

                sentences, state.text_buffer = extract_sentences(state.text_buffer)
                for sentence in sentences:
                    LOG.info("Streaming sentence ready: %r", sentence)
                    await state.text_queue.put(sentence)

                return True

            if SynthesizeStop.is_type(event.type):
                if self.stream is None:
                    await self.write_event(SynthesizeStopped().event())
                    return True

                state = self.stream
                final_sentences, state.text_buffer = extract_sentences(
                    state.text_buffer,
                    final=True,
                )
                for sentence in final_sentences:
                    LOG.info("Streaming final sentence ready: %r", sentence)
                    await state.text_queue.put(sentence)

                await state.text_queue.put(None)

                await self._wait_stream(state)
                self.stream = None

                if state.error is not None:
                    raise state.error

                await self.write_event(SynthesizeStopped().event())
                LOG.info("TTS STREAM STOP")
                return True

            # HA sends the full Synthesize event as a compatibility message
            # during streaming. Ignore it, otherwise we'd synthesize twice.
            if Synthesize.is_type(event.type):
                if self.stream is not None:
                    return True

                req = Synthesize.from_event(event)
                voice = normalize_voice(req.voice.name if req.voice else None)
                await self._synthesize_legacy(req.text, voice)
                return True

            return True

        except Exception as err:
            LOG.exception("Wyoming TTS error")
            try:
                await self.write_event(
                    Error(text=str(err), code=err.__class__.__name__).event()
                )
            except Exception:
                LOG.exception("Failed to send Wyoming Error")
            raise

    async def _producer_loop(self, state: StreamState) -> None:
        try:
            while True:
                item = await state.text_queue.get()
                if item is None:
                    break

                await self.engine.synthesize_stream(
                    item,
                    state.voice,
                    state.audio_queue,
                )

        except asyncio.CancelledError:
            raise
        except BaseException as err:
            state.error = err
            LOG.exception("TTS producer failed")
        finally:
            state.producer_done.set()
            await state.audio_queue.put(None)

    async def _consumer_loop(self, state: StreamState) -> None:
        started = False
        silence_count = 0
        silence = silence_pcm(SILENCE_SECONDS)

        # Момент, до которого уже отправленный PCM должен проигрываться.
        # Например: первый чанк = 7.325 сек -> deadline = now + 7.325.
        playback_deadline = 0.0

        try:
            while True:
                if not started:
                    # До первого настоящего аудио просто ждём.
                    item = await state.audio_queue.get()
                else:
                    now = time.perf_counter()

                    # У нас уже есть отправленный клиенту аудиобуфер.
                    # Пока он не должен закончиться — НИЧЕГО дополнительно
                    # не отправляем, а ждём либо следующий чанк, либо deadline.
                    if now < playback_deadline:
                        wait_time = playback_deadline - now

                        queue_task = asyncio.create_task(
                            state.audio_queue.get()
                        )
                        timer_task = asyncio.create_task(
                            asyncio.sleep(wait_time)
                        )

                        done, pending = await asyncio.wait(
                            {queue_task, timer_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )

                        if queue_task in done:
                            item = queue_task.result()
                            timer_task.cancel()
                            try:
                                await timer_task
                            except asyncio.CancelledError:
                                pass
                        else:
                            queue_task.cancel()
                            try:
                                await queue_task
                            except asyncio.CancelledError:
                                pass

                            # Время уже вышло. Сначала ещё раз проверяем
                            # очередь без ожидания: настоящий чанк мог прийти
                            # ровно сейчас.
                            try:
                                item = state.audio_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                item = "__UNDERRUN__"
                    else:
                        # Аудио уже должно было закончиться.
                        try:
                            item = state.audio_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            item = "__UNDERRUN__"

                # ---------------------------------------------------------
                # UNDERRUN: следующий настоящий чанк ещё не готов.
                # Отправляем РОВНО 1 секунду PCM silence.
                # Но только после того, как предыдущий отправленный
                # аудиобуфер уже должен был закончиться.
                # ---------------------------------------------------------
                if item == "__UNDERRUN__":
                    if state.producer_done.is_set():
                        break

                    silence_count += 1

                    LOG.warning(
                        "AUDIO UNDERRUN -> inserting %.1fs silence (#%d)",
                        SILENCE_SECONDS,
                        silence_count,
                    )

                    await self._send_audio_bytes(
                        silence,
                        state,
                        started=True,
                    )

                    # Мы мгновенно отправили 1 секунду PCM, поэтому эта
                    # секунда теперь считается частью клиентского буфера.
                    now = time.perf_counter()
                    playback_deadline = max(
                        playback_deadline,
                        now,
                    ) + SILENCE_SECONDS

                    continue

                if item is None:
                    break

                if not item:
                    continue

                # ---------------------------------------------------------
                # Первый настоящий аудиочанк
                # ---------------------------------------------------------
                if not started:
                    await self.write_event(
                        AudioStart(
                            rate=SAMPLE_RATE,
                            width=SAMPLE_WIDTH,
                            channels=CHANNELS,
                        ).event()
                    )
                    started = True
                    state.audio_started = True
                    LOG.info("AUDIO STREAM START")

                    # До этого ничего не было отправлено клиенту.
                    playback_deadline = time.perf_counter()

                silence_count = 0

                # Отправляем настоящий чанк сразу.
                await self._send_audio_bytes(
                    item,
                    state,
                    started=True,
                )

                # Узнаём реальную длительность отправленного PCM.
                duration = len(item) / (
                    SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS
                )

                # Добавляем её к уже запланированному времени
                # воспроизведения.
                now = time.perf_counter()
                playback_deadline = max(
                    playback_deadline,
                    now,
                ) + duration

                LOG.info(
                    "AUDIO BUFFER +%.3fs -> deadline in %.3fs",
                    duration,
                    max(0.0, playback_deadline - now),
                )

            if started:
                await self.write_event(AudioStop().event())
                LOG.info("AUDIO STREAM END")

        except asyncio.CancelledError:
            raise
        except BaseException as err:
            state.error = state.error or err
            LOG.exception("TTS consumer failed")

    async def _send_audio_bytes(
        self,
        pcm: bytes,
        state: StreamState,
        started: bool,
    ) -> None:
        if not pcm:
            return

        for offset in range(0, len(pcm), AUDIO_CHUNK_BYTES):
            chunk = pcm[offset:offset + AUDIO_CHUNK_BYTES]
            await self.write_event(
                AudioChunk(
                    audio=chunk,
                    rate=SAMPLE_RATE,
                    width=SAMPLE_WIDTH,
                    channels=CHANNELS,
                ).event()
            )

    async def _wait_stream(self, state: StreamState) -> None:
        tasks = [t for t in (state.producer_task, state.consumer_task) if t]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _stop_stream(self) -> None:
        state = self.stream
        if state is None:
            return

        for task in (state.producer_task, state.consumer_task):
            if task and not task.done():
                task.cancel()

        await self._wait_stream(state)
        self.stream = None

    async def _synthesize_legacy(self, text: str, voice: str) -> None:
        pcm_parts = await self.engine.synthesize(text, voice)
        if not pcm_parts:
            return

        await self.write_event(
            AudioStart(
                rate=SAMPLE_RATE,
                width=SAMPLE_WIDTH,
                channels=CHANNELS,
            ).event()
        )

        for pcm in pcm_parts:
            await self._send_audio_bytes(pcm, StreamState(voice=voice), started=True)

        await self.write_event(AudioStop().event())


# -----------------------------------------------------------------------------
# Server
# -----------------------------------------------------------------------------


def build_info() -> Info:
    attribution_cfg = TTS_CONFIG.get("attribution", {})

    attribution = Attribution(
        name=attribution_cfg["name"],
        url=attribution_cfg["url"],
    )

    voices = [
        TtsVoice(
            name=voice,
            description=VOICE_CONFIG[voice]["description"],
            version="",
            attribution=attribution,
            installed=True,
            languages=[TTS_CONFIG["language"]],
        )
        for voice in VOICE_NAMES
    ]

    return Info(
        tts=[
            TtsProgram(
                name=TTS_CONFIG["name"],
                description=TTS_CONFIG["description"],
                attribution=attribution,
                installed=True,
                voices=voices,
                version=TTS_CONFIG["version"],
                supports_synthesize_streaming=True,
            )
        ]
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--uri",
        default=None,
        help="Override Wyoming URI from wyoming.json",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    LOG.info("Starting Kokoro Wyoming server")
    LOG.info("CPU threads=%d interop=%d", TORCH_THREADS, TORCH_INTEROP_THREADS)
    LOG.info("Streaming: immediate audio + %.1fs silence on underrun", SILENCE_SECONDS)

    engine = KokoroEngine()
    info = build_info()
    server = AsyncServer.from_uri(args.uri or APP_CONFIG["server"]["uri"])

    LOG.info("Listening on %s", args.uri or APP_CONFIG["server"]["uri"])

    server_task = asyncio.create_task(
        server.run(partial(KokoroEventHandler, engine, info))
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, server_task.cancel)

    try:
        await server_task
    except asyncio.CancelledError:
        LOG.info("Server stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

