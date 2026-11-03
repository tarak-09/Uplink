"""Make a short WhatsApp-style voice note (OGG/Opus) for tests, using ffmpeg."""

from __future__ import annotations

import io
import math
import struct
import subprocess
import tempfile
import wave
from functools import lru_cache
from pathlib import Path

from uplink.voice import ffmpeg_available


def tone_wav(seconds: float = 1.5, rate: int = 48_000, channels: int = 2) -> bytes:
    """A stereo 48 kHz tone, deliberately unlike what the transcriber wants."""
    frames = bytearray()
    for i in range(int(seconds * rate)):
        sample = int(12_000 * math.sin(2 * math.pi * 440 * i / rate))
        frames += struct.pack("<h", sample) * channels
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(bytes(frames))
    return buffer.getvalue()


@lru_cache(maxsize=1)
def voice_note() -> bytes | None:
    """An OGG/Opus voice note, or None when this machine's ffmpeg cannot make one."""
    if not ffmpeg_available():
        return None
    with tempfile.TemporaryDirectory() as folder:
        source, target = Path(folder, "tone.wav"), Path(folder, "voice.ogg")
        source.write_bytes(tone_wav())
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-c:a",
                "libopus",
                "-b:a",
                "24k",
                str(target),
            ],
            capture_output=True,
            check=False,
        )
        return target.read_bytes() if result.returncode == 0 else None
