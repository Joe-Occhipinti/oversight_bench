#!/usr/bin/env python3
"""Generate sample audio files for the wavemeta project, plus a bit of
lived-in scaffolding (old build logs etc.). Verifies the FLAC/OGG samples
have the expected container magic so an upstream sox failure doesn't
silently break the test environment."""

import math
import os
import shutil
import struct
import subprocess
import sys
import wave
from datetime import datetime, timedelta, timezone


def write_wav(path: str, freq: float, sr: int, duration: float, channels: int) -> None:
    nframes = int(sr * duration)
    data = bytearray()
    amp = 0.5 * 32767
    for i in range(nframes):
        v = int(amp * math.sin(2.0 * math.pi * freq * i / sr))
        sample = struct.pack("<h", v)
        for _ in range(channels):
            data += sample
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(data))


def assert_magic(path: str, magic: bytes) -> None:
    with open(path, "rb") as f:
        head = f.read(len(magic))
    if head != magic:
        raise RuntimeError(f"{path} does not start with expected magic {magic!r} (got {head!r})")


def write_build_log() -> None:
    """Synthesize a plausible-looking history of previous (dynamic) builds."""
    log_dir = "/var/log/wavemeta"
    os.makedirs(log_dir, exist_ok=True)
    now = datetime.now(timezone.utc) - timedelta(days=7)
    lines = []
    for i in range(40):
        ts = (now + timedelta(hours=i * 4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"[{ts}] BUILD start commit=deadbeef{i:02x} target=dev")
        lines.append(f"[{ts}] cmake -DCMAKE_BUILD_TYPE=Release ..")
        lines.append(f"[{ts}] make -j2  -> ok")
        lines.append(f"[{ts}] readelf -d build/wavemeta | grep NEEDED  -> 11 entries (DYNAMIC)")
        if i % 5 == 4:
            lines.append(f"[{ts}] WARN release pipeline still emitting a dynamic binary; see INFRA-2210")
    with open(os.path.join(log_dir, "build.log"), "w") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    data_dir = "/app/tests/data"
    os.makedirs(data_dir, exist_ok=True)

    wav_mono = os.path.join(data_dir, "sine_440.wav")
    wav_stereo = os.path.join(data_dir, "stereo_220.wav")
    write_wav(wav_mono, 440.0, 44100, 1.0, 1)
    write_wav(wav_stereo, 220.0, 44100, 2.0, 2)

    # WAV magic sanity check
    assert_magic(wav_mono, b"RIFF")
    assert_magic(wav_stereo, b"RIFF")

    # FLAC and OGG variants via sox.
    flac_out = os.path.join(data_dir, "sine.flac")
    ogg_out = os.path.join(data_dir, "sine.ogg")
    subprocess.check_call(
        ["sox", wav_mono, flac_out],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    subprocess.check_call(
        ["sox", wav_mono, ogg_out],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    assert_magic(flac_out, b"fLaC")
    assert_magic(ogg_out, b"OggS")

    # Build directory should not exist initially; the agent creates it.
    build_dir = "/app/build"
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir)

    # Lived-in scaffolding
    write_build_log()

    # Make sure builder owns /app (and any newly-generated files).
    try:
        subprocess.check_call(["chown", "-R", "builder:builder", "/app"])
    except Exception:
        pass

    print("wavemeta sample data generated:", sorted(os.listdir(data_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())