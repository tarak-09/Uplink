"""Turn a WhatsApp voice note (OGG/Opus) into the WAV that Muse Voice Transcribe expects."""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

SAMPLE_RATE = 16_000
MAX_INPUT_BYTES = 16 * 1024 * 1024  # WhatsApp's own limit for audio


class AudioError(Exception):
    pass


def ffmpeg_available(ffmpeg: str = "ffmpeg") -> bool:
    return shutil.which(ffmpeg) is not None


def to_wav(audio: bytes, *, ffmpeg: str = "ffmpeg", sample_rate: int = SAMPLE_RATE, timeout: float = 60) -> bytes:
    """Convert any audio ffmpeg can read into mono 16-bit PCM WAV with a plain 44-byte header.

    Works through temporary files rather than pipes, because a WAV written to a pipe
    cannot have its length fields filled in afterwards.
    """
    if not audio:
        raise AudioError("the voice note was empty")
    if len(audio) > MAX_INPUT_BYTES:
        raise AudioError("the voice note is larger than 16 MB")
    if not ffmpeg_available(ffmpeg):
        raise AudioError(f"ffmpeg was not found ({ffmpeg}); install it to understand voice notes")
    with tempfile.TemporaryDirectory(prefix="uplink-audio-") as folder:
        source, target = Path(folder, "input"), Path(folder, "voice.wav")
        source.write_bytes(audio)
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            str(target),
        ]
        try:
            result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise AudioError("ffmpeg took too long to convert the voice note") from None
        if result.returncode != 0 or not target.exists():
            reason = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["unknown error"]
            raise AudioError(f"ffmpeg could not read the voice note: {reason[0]}")
        wav = target.read_bytes()
    _check_wav(wav, sample_rate)
    return wav


def _check_wav(wav: bytes, sample_rate: int) -> None:
    try:
        with wave.open(io.BytesIO(wav)) as reader:
            shape = (reader.getnchannels(), reader.getsampwidth(), reader.getframerate())
            frames = reader.getnframes()
    except (wave.Error, EOFError) as exc:
        raise AudioError(f"ffmpeg produced an unreadable WAV: {exc}") from None
    if shape != (1, 2, sample_rate):
        raise AudioError(f"unexpected WAV shape {shape}")
    if frames == 0:
        raise AudioError("the voice note had no sound")
