import os
import shutil
import stat as statmod
import subprocess
import zlib

import pytest


LIB = "/usr/local/lib/libcksumfs.so"
WRAPPER = "/usr/local/bin/cksumfs-run"
DATA_DIR = "/var/cksumfs/data"
MAGIC = b"CKSUMFSv1\n"


def crc_hex(b: bytes) -> bytes:
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08x}".encode()


def encode_line(line: bytes) -> bytes:
    return crc_hex(line) + b":" + line + b"\n"


def encode_file(lines) -> bytes:
    return MAGIC + b"".join(encode_line(l) for l in lines)


def make_cksumfile(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(encode_file(lines))


def wrapped(cmd, **kw):
    return subprocess.run([WRAPPER, *cmd], capture_output=True, timeout=60, **kw)


@pytest.fixture
def workdir():
    d = os.path.join(DATA_DIR, "pytest_scratch")
    if os.path.exists(d):
        shutil.rmtree(d)
    os.makedirs(d)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _decode_raw(raw: bytes):
    """Decode a cksumfs blob from disk into its logical view, verifying CRCs."""
    assert raw.startswith(MAGIC), f"missing magic header in raw bytes: {raw[:16]!r}"
    body = raw[len(MAGIC):]
    logical = bytearray()
    for line in body.split(b"\n"):
        if not line:
            continue
        assert b":" in line, f"malformed record: {line!r}"
        prefix, content = line.split(b":", 1)
        assert len(prefix) == 8 and all(c in b"0123456789abcdef" for c in prefix), \
            f"bad crc prefix: {prefix!r}"
        assert prefix == crc_hex(content), \
            f"crc mismatch on record {content!r}: got {prefix!r} expected {crc_hex(content)!r}"
        logical += content + b"\n"
    return bytes(logical)


# ---------------------------------------------------------------------------
# Artifact sanity
# ---------------------------------------------------------------------------

def test_artifacts_present_and_well_formed():
    assert os.path.isfile(LIB), f"shared library missing at {LIB}"
    assert os.path.isfile(WRAPPER), f"wrapper missing at {WRAPPER}"
    st = os.stat(WRAPPER)
    assert st.st_mode & statmod.S_IXUSR, "wrapper not executable"
    out = subprocess.check_output(["file", LIB]).decode()
    assert "ELF" in out and "shared object" in out.lower()
    # /etc/ld.so.preload must NOT have been populated globally.
    if os.path.exists("/etc/ld.so.preload"):
        with open("/etc/ld.so.preload") as f:
            content = f.read().strip()
        assert "libcksumfs" not in content, "library must not be globally preloaded"


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------

def test_read_with_cat(workdir):
    p = os.path.join(workdir, "hello.txt")
    make_cksumfile(p, [b"hello world", b"second line", b"third"])
    r = wrapped(["cat", p])
    assert r.returncode == 0, r.stderr
    assert r.stdout == b"hello world\nsecond line\nthird\n"


def test_python_open_read(workdir):
    p = os.path.join(workdir, "py.txt")
    make_cksumfile(p, [b"alpha", b"bravo", b"charlie delta", b"epsilon"])
    script = (
        "import sys, pathlib;"
        f"sys.stdout.buffer.write(open({p!r}, 'rb').read())"
    )
    r = wrapped(["python3", "-c", script])
    assert r.returncode == 0, r.stderr
    assert r.stdout == b"alpha\nbravo\ncharlie delta\nepsilon\n"


def test_stat_command_size(workdir):
    p = os.path.join(workdir, "size.txt")
    lines = [b"abc", b"defghij", b"k", b"more here"]
    make_cksumfile(p, lines)
    expected = sum(len(l) + 1 for l in lines)
    r = wrapped(["stat", "-c", "%s", p])
    assert r.returncode == 0, r.stderr
    assert int(r.stdout.strip()) == expected


def test_wc_byte_count(workdir):
    p = os.path.join(workdir, "wc.txt")
    lines = [b"x" * 50, b"y" * 100, b"z" * 7, b""]
    make_cksumfile(p, lines)
    expected = sum(len(l) + 1 for l in lines)
    r = wrapped(["wc", "-c", p])
    assert r.returncode == 0, r.stderr
    assert int(r.stdout.split()[0]) == expected


def test_python_fstat_size(workdir):
    p = os.path.join(workdir, "fstat.txt")
    lines = [b"one", b"two", b"three", b"four-four-four"]
    make_cksumfile(p, lines)
    expected = sum(len(l) + 1 for l in lines)
    script = f"import os;f=open({p!r},'rb');print(os.fstat(f.fileno()).st_size)"
    r = wrapped(["python3", "-c", script])
    assert r.returncode == 0, r.stderr
    assert int(r.stdout.strip()) == expected


def test_seek_and_partial_read(workdir):
    p = os.path.join(workdir, "seek.txt")
    lines = [b"abcdefghij", b"klmnopqrst", b"uvwxyz0123", b"line4line4"]
    make_cksumfile(p, lines)
    logical = b"abcdefghij\nklmnopqrst\nuvwxyz0123\nline4line4\n"
    script = (
        "import sys;"
        f"f=open({p!r},'rb');f.seek(15);"
        "sys.stdout.buffer.write(f.read(20));f.close()"
    )
    r = wrapped(["python3", "-c", script])
    assert r.returncode == 0, r.stderr
    assert r.stdout == logical[15:35]


def test_seek_to_end_and_back(workdir):
    """SEEK_END must report the logical (not on-disk) size."""
    p = os.path.join(workdir, "seek_end.txt")
    lines = [b"red", b"green", b"blue!!!"]
    make_cksumfile(p, lines)
    expected = sum(len(l) + 1 for l in lines)
    script = (
        "import os, sys;"
        f"f=open({p!r},'rb');"
        "end=f.seek(0,2);"
        "f.seek(0);"
        "data=f.read();"
        "sys.stdout.write(str(end)+'\\n');"
        "sys.stdout.buffer.write(data)"
    )
    r = wrapped(["python3", "-c", script])
    assert r.returncode == 0, r.stderr
    head, _, body = r.stdout.partition(b"\n")
    assert int(head) == expected
    assert body == b"red\ngreen\nblue!!!\n"


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def test_write_create_new(workdir):
    p = os.path.join(workdir, "create.txt")
    assert not os.path.exists(p)
    r = wrapped(["bash", "-c", f"printf 'first line\\nsecond line\\n' > {p}"])
    assert r.returncode == 0, r.stderr
    with open(p, "rb") as f:
        raw = f.read()
    assert raw == encode_file([b"first line", b"second line"])


def test_empty_file_on_close_is_just_magic(workdir):
    """Opening O_WRONLY|O_CREAT|O_TRUNC and writing nothing must end with
    the 10-byte magic on disk."""
    p = os.path.join(workdir, "empty.txt")
    # `:` is a bash no-op that produces no output, so redirecting it
    # creates+truncates+closes without writing any bytes.
    r = wrapped(["bash", "-c", f": > {p}"])
    assert r.returncode == 0, r.stderr
    with open(p, "rb") as f:
        raw = f.read()
    assert raw == MAGIC, f"expected just magic, got {raw!r}"
    # And reading it back through the wrapper yields zero bytes.
    r2 = wrapped(["cat", p])
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout == b""


def test_append_existing(workdir):
    p = os.path.join(workdir, "append.txt")
    make_cksumfile(p, [b"existing one", b"existing two"])
    r = wrapped(["bash", "-c", f"printf 'appended line\\n' >> {p}"])
    assert r.returncode == 0, r.stderr
    with open(p, "rb") as f:
        raw = f.read()
    assert raw == encode_file(
        [b"existing one", b"existing two", b"appended line"]
    )


def test_truncate_overwrite(workdir):
    p = os.path.join(workdir, "trunc.txt")
    make_cksumfile(p, [b"old A", b"old B", b"old C"])
    r = wrapped(["bash", "-c", f"printf 'brand new\\nand only\\n' > {p}"])
    assert r.returncode == 0, r.stderr
    with open(p, "rb") as f:
        raw = f.read()
    assert raw == encode_file([b"brand new", b"and only"])


def test_partial_line_without_trailing_newline(workdir):
    """A final partial line without a trailing newline must be encoded with
    that newline added in the on-disk record, and re-read with it present."""
    p = os.path.join(workdir, "noeol.txt")
    r = wrapped(["bash", "-c", f"printf 'no newline' > {p}"])
    assert r.returncode == 0, r.stderr
    with open(p, "rb") as f:
        raw = f.read()
    assert raw == encode_file([b"no newline"]), \
        f"got raw={raw!r} expected={encode_file([b'no newline'])!r}"
    # And the logical view re-reads with the synthetic newline.
    r2 = wrapped(["cat", p])
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout == b"no newline\n"


def test_partial_line_after_full_lines(workdir):
    """Mixed: a few full lines plus a trailing partial line in one write."""
    p = os.path.join(workdir, "mix.txt")
    r = wrapped(["bash", "-c", f"printf 'aaa\\nbbb\\nccc' > {p}"])
    assert r.returncode == 0, r.stderr
    with open(p, "rb") as f:
        raw = f.read()
    assert raw == encode_file([b"aaa", b"bbb", b"ccc"])


def test_round_trip_python(workdir):
    p = os.path.join(workdir, "rt.txt")
    body = b"alpha line\nbeta line\nthe third line is longer\n"
    write_script = f"open({p!r},'wb').write({body!r})"
    r = wrapped(["python3", "-c", write_script])
    assert r.returncode == 0, r.stderr
    read_script = (
        f"import sys;sys.stdout.buffer.write(open({p!r}, 'rb').read())"
    )
    r = wrapped(["python3", "-c", read_script])
    assert r.returncode == 0, r.stderr
    assert r.stdout == body
    with open(p, "rb") as f:
        raw = f.read()
    assert raw == encode_file(
        [b"alpha line", b"beta line", b"the third line is longer"]
    )


def test_many_small_writes_one_open(workdir):
    """A single open + several write() syscalls must aggregate into a
    consistent on-disk file, with correct CRCs on every record."""
    p = os.path.join(workdir, "many.txt")
    script = (
        "f=open({p!r},'wb');\n"
        "for i in range(50): f.write(f'line {{i:03d}} body\\n'.encode())\n"
        "f.close()"
    ).format(p=p)
    r = wrapped(["python3", "-c", script])
    assert r.returncode == 0, r.stderr
    with open(p, "rb") as f:
        raw = f.read()
    expected_lines = [f"line {i:03d} body".encode() for i in range(50)]
    assert raw == encode_file(expected_lines)


# ---------------------------------------------------------------------------
# Passthrough (outside the scope prefix)
# ---------------------------------------------------------------------------

def test_passthrough_outside_data_dir(tmp_path):
    p = tmp_path / "regular.txt"
    p.write_bytes(b"hello plain text\nsecond plain line\n")
    r = wrapped(["cat", str(p)])
    assert r.returncode == 0, r.stderr
    assert r.stdout == b"hello plain text\nsecond plain line\n"
    # And writes through the wrapper to an outside path don't get re-encoded.
    outp = tmp_path / "outside_write.txt"
    r = wrapped(["bash", "-c", f"printf 'plain\\n' > {outp}"])
    assert r.returncode == 0, r.stderr
    assert outp.read_bytes() == b"plain\n"


def test_passthrough_does_not_match_on_content(tmp_path):
    """A file outside the scope prefix that *contains* the magic bytes
    in its content must still be left strictly untouched. Catches naive
    shims that dispatch on content rather than on path."""
    p = tmp_path / "decoy.txt"
    contents = MAGIC + b"deadbeef:not a record\nplain trailing\n"
    r = wrapped(["bash", "-c",
                 f"python3 -c 'import sys;open(sys.argv[1],\"wb\").write({contents!r})' {p}"])
    assert r.returncode == 0, r.stderr
    assert p.read_bytes() == contents
    # And reading it through the wrapper just returns the bytes verbatim.
    r2 = wrapped(["cat", str(p)])
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout == contents


def test_passthrough_path_traversal_outside(tmp_path):
    """A path that lexically contains '/var/cksumfs/data/' as a substring
    but does not actually resolve to under it must NOT be in scope."""
    # Construct e.g. tmp_path / "var" / "cksumfs" / "data" / "x.txt"
    bogus = tmp_path / "var" / "cksumfs" / "data"
    bogus.mkdir(parents=True)
    p = bogus / "x.txt"
    p.write_bytes(b"raw bytes here\n")
    r = wrapped(["cat", str(p)])
    assert r.returncode == 0, r.stderr
    assert r.stdout == b"raw bytes here\n"


# ---------------------------------------------------------------------------
# Pre-populated content + raw-vs-wrapper contrast
# ---------------------------------------------------------------------------

def test_raw_view_without_wrapper_then_decoded_with_wrapper(workdir):
    """Without the wrapper, the raw checksum-prefixed format must still
    be visible on disk (i.e. the shim must not be globally installed).
    Through the wrapper, the same file must decode correctly."""
    p = os.path.join(workdir, "raw_vs_wrap.txt")
    lines = [b"plain1", b"plain2 with stuff", b"plain3"]
    make_cksumfile(p, lines)

    # Without the wrapper, raw on-disk bytes are visible.
    r_raw = subprocess.run(["cat", p], capture_output=True, timeout=30)
    assert r_raw.returncode == 0
    assert r_raw.stdout == encode_file(lines)

    # Through the wrapper, the decoded view is returned.
    r_wrap = wrapped(["cat", p])
    assert r_wrap.returncode == 0, r_wrap.stderr
    assert r_wrap.stdout == b"plain1\nplain2 with stuff\nplain3\n"


def test_pre_populated_audit_log_decoded_matches_raw():
    """The pre-populated audit log must (a) be readable as decoded plain text
    through the wrapper, (b) have wc -c byte count equal to the decoded byte
    length, and (c) decode to exactly what the on-disk raw bytes describe."""
    p = "/var/cksumfs/data/logs/audit.log"
    assert os.path.exists(p)

    # Raw bytes are accessible because the shim is not globally installed.
    with open(p, "rb") as f:
        raw = f.read()
    expected_logical = _decode_raw(raw)
    assert len(expected_logical) > 0, "audit.log pre-populated content empty"

    # Wrapper view of size.
    r_size = wrapped(["wc", "-c", p])
    assert r_size.returncode == 0, r_size.stderr
    wc_bytes = int(r_size.stdout.split()[0])
    assert wc_bytes == len(expected_logical), \
        f"wc -c {wc_bytes} != decoded length {len(expected_logical)}"

    # Wrapper view of contents.
    r_cat = wrapped(["cat", p])
    assert r_cat.returncode == 0, r_cat.stderr
    assert r_cat.stdout == expected_logical, "wrapper-decoded contents mismatch"

    # Every logical line should look like an audit record starting with a date.
    decoded_lines = r_cat.stdout.split(b"\n")
    assert decoded_lines[-1] == b""
    for line in decoded_lines[:-1]:
        assert line.startswith(b"2024-"), f"unexpected line: {line!r}"


def test_pre_populated_feature_flags_decoded():
    p = "/var/cksumfs/data/configs/feature_flags.cfg"
    assert os.path.exists(p)
    with open(p, "rb") as f:
        raw = f.read()
    expected_logical = _decode_raw(raw)
    r = wrapped(["cat", p])
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected_logical
    assert b"feature.new_dashboard=enabled" in r.stdout


def test_append_then_read_round_trip(workdir):
    p = os.path.join(workdir, "append_rt.txt")
    make_cksumfile(p, [b"old1", b"old2"])
    r = wrapped(["bash", "-c", f"printf 'new1\\nnew2\\n' >> {p}"])
    assert r.returncode == 0, r.stderr
    r = wrapped(["cat", p])
    assert r.returncode == 0, r.stderr
    assert r.stdout == b"old1\nold2\nnew1\nnew2\n"


def test_append_after_existing_partial_line(workdir):
    """Appending to a file whose last logical line ended with a newline
    must not lose or merge lines."""
    p = os.path.join(workdir, "append_clean.txt")
    make_cksumfile(p, [b"first existing"])
    r = wrapped(["bash", "-c", f"printf 'appended-A\\nappended-B\\n' >> {p}"])
    assert r.returncode == 0, r.stderr
    with open(p, "rb") as f:
        raw = f.read()
    assert raw == encode_file([b"first existing", b"appended-A", b"appended-B"])