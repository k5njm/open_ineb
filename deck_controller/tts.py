"""
Offline Beni Voice (TTS) synthesizer & Opus encoder.

Synthesizes speech locally with zero cloud dependencies:
  1. `espeak-ng` or `espeak` (SteamOS / Linux / macOS)
  2. macOS `say` + `afconvert` (instant fallback on macOS)
  3. Pure-Python robotic synth fallback (always works even with no OS TTS binary)

Applies an optional robotic / WALL-E ring-modulator + metallic comb filter
and encodes 48 kHz mono S16 PCM into 20 ms Opus packets via PyAV (`libopus`)
matching Beni's `md_audio_processing_config.json` playback pipeline.
"""

from __future__ import annotations

import io
import math
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Iterator

import av
import numpy as np

SAMPLE_RATE = 48000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 960 samples per 20 ms

QUICK_PHRASES = [
    "Hello! I am Beni.",
    "Excuse me, coming through!",
    "Say cheese!",
    "Watch out, jumping!",
    "Time to recharge.",
    "Beep boop! Ready to roll.",
]


def _read_wav_mono_48k(wav_path: Path) -> np.ndarray:
    """Load a WAV file and resample to mono int16 @ 48 kHz."""
    with wave.open(str(wav_path), "rb") as wf:
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    if sw == 2:
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 1:
        pcm = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        pcm = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0

    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)

    if sr != SAMPLE_RATE and len(pcm) > 1:
        new_len = int(round(len(pcm) * SAMPLE_RATE / sr))
        x_old = np.linspace(0.0, 1.0, len(pcm), endpoint=False)
        x_new = np.linspace(0.0, 1.0, new_len, endpoint=False)
        pcm = np.interp(x_new, x_old, pcm).astype(np.float32)

    return np.clip(pcm * 32767.0, -32768.0, 32767.0).astype(np.int16)


def _synth_robot_chirp(text: str) -> np.ndarray:
    """Zero-dependency expressive WALL-E-style synth fallback when no OS TTS is installed."""
    chunks: list[np.ndarray] = []
    rng_seed = sum(ord(c) for c in text)
    words = [w for w in text.split() if w] or ["beep"]
    for idx, word in enumerate(words[:12]):
        dur = min(0.18, 0.06 + 0.015 * len(word))
        n = int(SAMPLE_RATE * dur)
        t = np.linspace(0.0, dur, n, endpoint=False, dtype=np.float32)
        base = 340.0 + ((ord(word[0]) * 37 + idx * 53 + rng_seed) % 380)
        end_f = base * (1.28 if word.endswith("?") else 0.85 if word.endswith(".") else 1.08)
        freq = np.linspace(base, end_f, n, dtype=np.float32)
        phase = 2.0 * math.pi * np.cumsum(freq) / SAMPLE_RATE
        env = np.sin(math.pi * np.linspace(0.0, 1.0, n, dtype=np.float32)) ** 0.5
        sig = (0.55 * np.sin(phase) + 0.25 * np.sin(2.0 * phase)) * env
        chunks.append((sig * 24000.0).astype(np.int16))
        chunks.append(np.zeros(int(SAMPLE_RATE * 0.04), dtype=np.int16))
    return np.concatenate(chunks)


def apply_robot_filter(pcm_i16: np.ndarray) -> np.ndarray:
    """Apply a lightweight ring-modulator + short metallic comb delay for a cute robot voice."""
    if len(pcm_i16) == 0:
        return pcm_i16
    x = pcm_i16.astype(np.float32) / 32768.0
    t = np.arange(len(x), dtype=np.float32) / SAMPLE_RATE
    # Gentle 95 Hz ring modulation blended with dry voice for intelligibility
    mod = 0.65 + 0.35 * np.sin(2.0 * math.pi * 95.0 * t)
    y = x * mod
    # 7 ms metallic comb echo
    delay = int(SAMPLE_RATE * 0.007)
    if len(y) > delay:
        y[delay:] = 0.78 * y[delay:] + 0.28 * y[:-delay]
    peak = float(np.max(np.abs(y))) or 1.0
    if peak > 0.95:
        y *= 0.95 / peak
    return np.clip(y * 32767.0, -32768.0, 32767.0).astype(np.int16)


def synthesize_pcm(text: str, *, robot_voice: bool = True) -> tuple[np.ndarray, str]:
    """
    Synthesize `text` into mono int16 PCM @ 48 kHz.
    Returns `(pcm_int16, engine_name)`.
    """
    clean = text.strip()
    if not clean:
        return np.zeros(FRAME_SAMPLES, dtype=np.int16), "empty"

    pcm: np.ndarray | None = None
    engine = "synth"

    with tempfile.TemporaryDirectory(prefix="beni_tts_") as td:
        wav_path = Path(td) / "out.wav"

        espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        if espeak:
            res = subprocess.run(
                [espeak, "-s", "165", "-p", "60", "-w", str(wav_path), clean],
                capture_output=True,
                timeout=15,
            )
            if res.returncode == 0 and wav_path.exists() and wav_path.stat().st_size > 44:
                pcm = _read_wav_mono_48k(wav_path)
                engine = Path(espeak).name

        if pcm is None and shutil.which("say"):
            aiff_path = Path(td) / "out.aiff"
            res = subprocess.run(
                ["say", "-o", str(aiff_path), clean],
                capture_output=True,
                timeout=15,
            )
            if res.returncode == 0 and aiff_path.exists():
                if shutil.which("afconvert"):
                    c = subprocess.run(
                        [
                            "afconvert",
                            "-f",
                            "WAVE",
                            "-d",
                            "LEI16@48000",
                            "-c",
                            "1",
                            str(aiff_path),
                            str(wav_path),
                        ],
                        capture_output=True,
                        timeout=15,
                    )
                    if c.returncode == 0 and wav_path.exists():
                        pcm = _read_wav_mono_48k(wav_path)
                        engine = "say"

    if pcm is None:
        pcm = _synth_robot_chirp(clean)
        engine = "synth"

    if robot_voice and engine != "synth":
        pcm = apply_robot_filter(pcm)

    # Pad to exact 20 ms frame multiple
    rem = len(pcm) % FRAME_SAMPLES
    if rem:
        pcm = np.concatenate([pcm, np.zeros(FRAME_SAMPLES - rem, dtype=np.int16)])
    return pcm, engine


def pcm_to_wav_bytes(pcm_i16: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_i16.tobytes())
    return buf.getvalue()


def encode_opus_frames(pcm_i16: np.ndarray) -> Iterator[bytes]:
    """Encode 48 kHz mono int16 PCM into 20 ms Opus packets via PyAV `libopus`."""
    codec = av.CodecContext.create("libopus", "w")
    codec.sample_rate = SAMPLE_RATE
    codec.layout = "mono"
    codec.format = "s16"
    codec.bit_rate = 48000
    codec.open()

    pts = 0
    for off in range(0, len(pcm_i16), FRAME_SAMPLES):
        chunk = pcm_i16[off : off + FRAME_SAMPLES]
        if len(chunk) < FRAME_SAMPLES:
            chunk = np.pad(chunk, (0, FRAME_SAMPLES - len(chunk)))
        frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = pts
        pts += FRAME_SAMPLES
        for pkt in codec.encode(frame):
            yield bytes(pkt)
    for pkt in codec.encode(None):
        yield bytes(pkt)


def play_over_ssh(
    pcm_i16: np.ndarray,
    *,
    robot_ip: str = "192.168.7.1",
    ssh_port: int = 2222,
    ssh_user: str = "root",
) -> bool:
    """Fallback playback for custom `/md` firmware: pipe WAV directly to Beni's ALSA speaker."""
    wav_data = pcm_to_wav_bytes(pcm_i16)
    try:
        res = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=3",
                "-o",
                "StrictHostKeyChecking=no",
                "-p",
                str(ssh_port),
                f"{ssh_user}@{robot_ip}",
                "aplay -q -D plughw:1,1 -",
            ],
            input=wav_data,
            capture_output=True,
            timeout=15,
        )
        return res.returncode == 0
    except Exception:
        return False
