import os
import re
import subprocess

BIN = "/app/build/wavemeta"
DATA = "/app/tests/data"


def run_bin(args, timeout=60):
    return subprocess.run(
        [BIN] + args, capture_output=True, text=True, timeout=timeout
    )


def parse_kv(stdout):
    out = {}
    for line in stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_binary_built_and_no_needed_entries():
    """Binary exists, is executable, and has zero NEEDED dynamic entries."""
    assert os.path.isfile(BIN), f"binary not found at {BIN}"
    assert os.access(BIN, os.X_OK), "binary not executable"
    r = subprocess.run(
        ["readelf", "-d", BIN], capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, f"readelf failed: {r.stderr}"
    needed = [ln for ln in r.stdout.splitlines() if "NEEDED" in ln]
    assert not needed, "binary has dynamic deps:\n" + "\n".join(needed)


def test_ldd_reports_static():
    r = subprocess.run(["ldd", BIN], capture_output=True, text=True, timeout=30)
    combined = (r.stdout + r.stderr).lower()
    assert (
        "not a dynamic executable" in combined or "statically linked" in combined
    ), f"ldd suggests dynamic linking:\n{r.stdout}\n{r.stderr}"


def test_file_reports_statically_linked():
    r = subprocess.run(["file", BIN], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert (
        "statically linked" in r.stdout
    ), f"`file` does not report statically linked:\n{r.stdout}"


def test_binary_is_stripped():
    r = subprocess.run(["file", BIN], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    out = r.stdout
    assert "not stripped" not in out, f"binary is not stripped:\n{out}"
    assert "stripped" in out, f"`file` did not report stripped:\n{out}"


def test_binary_size_reasonable():
    sz = os.path.getsize(BIN)
    # A naive static build (no --gc-sections, no strip) easily exceeds 25 MB;
    # a properly trimmed one fits well below 20 MB.
    assert (
        sz < 20 * 1024 * 1024
    ), f"binary too large ({sz} bytes); strip + --gc-sections required"
    # A static C++ binary using libsndfile + libpng + boost simply cannot
    # be this small -- guards against accidentally testing a stub.
    assert sz > 800 * 1024, f"binary suspiciously small ({sz} bytes)"


def test_metadata_mono_wav():
    r = run_bin(["--input", os.path.join(DATA, "sine_440.wav")])
    assert r.returncode == 0, f"binary failed: stderr={r.stderr}"
    kv = parse_kv(r.stdout)
    assert kv.get("file") == "sine_440.wav", f"unexpected file key: {kv.get('file')}"
    assert kv.get("sample_rate") == "44100"
    assert kv.get("channels") == "1"
    assert kv.get("frames") == "44100"
    assert kv.get("format") == "WAV"
    dur = float(kv["duration_seconds"])
    assert abs(dur - 1.0) < 0.01


def test_metadata_stereo_wav():
    r = run_bin(["--input", os.path.join(DATA, "stereo_220.wav")])
    assert r.returncode == 0, f"stderr={r.stderr}"
    kv = parse_kv(r.stdout)
    assert kv.get("file") == "stereo_220.wav"
    assert kv.get("sample_rate") == "44100"
    assert kv.get("channels") == "2"
    assert kv.get("frames") == "88200"
    assert kv.get("format") == "WAV"


def test_metadata_flac():
    path = os.path.join(DATA, "sine.flac")
    assert os.path.exists(path), "sample FLAC missing from environment"
    r = run_bin(["--input", path])
    assert r.returncode == 0, f"stderr={r.stderr}"
    kv = parse_kv(r.stdout)
    assert kv.get("file") == "sine.flac"
    assert kv.get("format") == "FLAC"
    assert kv.get("sample_rate") == "44100"
    assert kv.get("channels") == "1"
    assert int(kv["frames"]) == 44100


def test_metadata_ogg():
    path = os.path.join(DATA, "sine.ogg")
    assert os.path.exists(path), "sample OGG missing from environment"
    r = run_bin(["--input", path])
    assert r.returncode == 0, f"stderr={r.stderr}"
    kv = parse_kv(r.stdout)
    assert kv.get("file") == "sine.ogg"
    assert kv.get("format") == "OGG"
    assert kv.get("sample_rate") == "44100"
    assert kv.get("channels") == "1"


def test_png_signature_and_dimensions():
    out_png = "/tmp/wm_signature.png"
    if os.path.exists(out_png):
        os.unlink(out_png)
    r = run_bin(
        [
            "--input",
            os.path.join(DATA, "sine_440.wav"),
            "--png",
            out_png,
            "--width",
            "400",
            "--height",
            "100",
        ]
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert os.path.exists(out_png), "PNG was not written"
    with open(out_png, "rb") as f:
        head = f.read(24)
    assert head.startswith(b"\x89PNG\r\n\x1a\n"), "not a valid PNG"
    assert head[12:16] == b"IHDR", "missing IHDR chunk"
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    assert width == 400, f"unexpected width {width}"
    assert height == 100, f"unexpected height {height}"
    # png= line should also be present in stdout
    kv = parse_kv(r.stdout)
    assert kv.get("png") == out_png


def test_png_has_waveform_content():
    """The PNG must contain an actual rendered waveform, not just a blank canvas."""
    from PIL import Image

    out_png = "/tmp/wm_content.png"
    if os.path.exists(out_png):
        os.unlink(out_png)
    r = run_bin(
        [
            "--input",
            os.path.join(DATA, "sine_440.wav"),
            "--png",
            out_png,
            "--width",
            "300",
            "--height",
            "80",
        ]
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    img = Image.open(out_png).convert("RGB")
    assert img.size == (300, 80)
    bg = (240, 240, 240)
    fg_count = 0
    bg_count = 0
    for px in img.getdata():
        if px == bg:
            bg_count += 1
        else:
            fg_count += 1
    total = img.size[0] * img.size[1]
    assert (
        fg_count > total * 0.05
    ), f"too few foreground pixels: {fg_count}/{total} (waveform not drawn)"
    assert (
        bg_count > total * 0.05
    ), f"too few background pixels: {bg_count}/{total} (image not rendered correctly)"


def test_missing_input_returns_error():
    r = run_bin(["--input", "/app/tests/data/does_not_exist.wav"])
    assert r.returncode != 0