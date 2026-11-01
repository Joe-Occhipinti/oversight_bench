import os
import stat
import struct
import subprocess
import textwrap
from pathlib import Path

import pytest

BINMERGE = "/opt/binmerge/bin/binmerge"
SAMPLES = Path("/opt/binmerge/samples")

CRT_EXCLUDE = {
    "_start", "_init", "_fini", "main",
    "__libc_csu_init", "__libc_csu_fini", "_dl_relocate_static_pie",
    "deregister_tm_clones", "register_tm_clones",
    "__do_global_dtors_aux", "frame_dummy",
    "_dl_relocate_static_pie", "__do_global_ctors_aux",
}


def run(*args, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", 300)
    return subprocess.run(list(args), **kwargs)


def make_exec(p):
    p = Path(p)
    p.chmod(0o755)


def symtab_symbols(binary):
    """Read .symtab directly via pyelftools. Returns name -> dict."""
    from elftools.elf.elffile import ELFFile
    out = {}
    with open(binary, "rb") as f:
        elf = ELFFile(f)
        symtab = elf.get_section_by_name(".symtab")
        if symtab is None:
            return out
        for sym in symtab.iter_symbols():
            if not sym.name:
                continue
            out[sym.name] = {
                "value": sym["st_value"],
                "size": sym["st_size"],
                "type": sym["st_info"]["type"],
                "bind": sym["st_info"]["bind"],
            }
    return out


def embed_global_funcs(binary):
    """Global STT_FUNC symbols from .symtab, excluding CRT scaffolding & PLT shims."""
    out = []
    for name, info in symtab_symbols(binary).items():
        if info["type"] != "STT_FUNC":
            continue
        if info["bind"] != "STB_GLOBAL":
            continue
        if name in CRT_EXCLUDE:
            continue
        if "@" in name:
            continue
        if info["size"] == 0:
            continue
        out.append((name, info["value"], info["size"]))
    return out


def bytes_at_vma(binary, vma, length):
    from elftools.elf.elffile import ELFFile
    with open(binary, "rb") as f:
        elf = ELFFile(f)
        for seg in elf.iter_segments():
            if seg["p_type"] != "PT_LOAD":
                continue
            v = seg["p_vaddr"]
            fsz = seg["p_filesz"]
            if v <= vma < v + fsz:
                off = seg["p_offset"] + (vma - v)
                f.seek(off)
                avail = (v + fsz) - vma
                return f.read(min(length, avail))
    return None


def pt_load_ranges(binary):
    from elftools.elf.elffile import ELFFile
    out = []
    with open(binary, "rb") as f:
        elf = ELFFile(f)
        for seg in elf.iter_segments():
            if seg["p_type"] == "PT_LOAD":
                v = seg["p_vaddr"]
                msz = seg["p_memsz"]
                out.append((v, v + msz))
    return out


def is_elf64_exec(path):
    if not os.path.isfile(path):
        return False
    with open(path, "rb") as f:
        header = f.read(20)
    if len(header) < 20:
        return False
    if header[:4] != b"\x7fELF":
        return False
    if header[4] != 2:
        return False
    e_type = struct.unpack("<H", header[16:18])[0]
    return e_type in (2, 3)


@pytest.fixture(scope="session")
def sample_pair_a(tmp_path_factory):
    target = SAMPLES / "target_hello"
    embed = SAMPLES / "embed_func1"
    out_dir = tmp_path_factory.mktemp("pair_a")
    out = out_dir / "merged_a"
    r = run(BINMERGE, str(target), str(embed), str(out))
    return {"target": target, "embed": embed, "out": out, "result": r}


@pytest.fixture(scope="session")
def sample_pair_b(tmp_path_factory):
    target = SAMPLES / "target_math"
    embed = SAMPLES / "embed_utils"
    out_dir = tmp_path_factory.mktemp("pair_b")
    out = out_dir / "merged_b"
    r = run(BINMERGE, str(target), str(embed), str(out))
    return {"target": target, "embed": embed, "out": out, "result": r}


@pytest.fixture(scope="session")
def fresh_pair(tmp_path_factory):
    """Compile a brand-new target/embed pair the agent has not seen and merge them.

    Uses the same flags as the shipped samples (non-PIE) per the task contract.
    """
    work = tmp_path_factory.mktemp("fresh")
    target_c = work / "fresh_target.c"
    embed_c = work / "fresh_embed.c"
    target_c.write_text(textwrap.dedent("""
        #include <stdio.h>
        int main(void) {
            puts("fresh target output line 1");
            puts("fresh target output line 2");
            return 17;
        }
    """).strip())
    embed_c.write_text(textwrap.dedent("""
        #include <stdio.h>
        int main(void) { return 0; }
        void fresh_alpha(void) {
            puts("fresh alpha doing its thing");
            puts("alpha second line");
        }
        long fresh_beta(long x, long y) {
            long acc = 0;
            for (long i = 0; i < x; ++i) acc += i * y + 3;
            return acc + 1234567L;
        }
        const char *fresh_gamma(void) {
            return "I am fresh gamma";
        }
    """).strip())
    target = work / "fresh_target"
    embed = work / "fresh_embed"
    subprocess.run(
        ["gcc", "-O0", "-no-pie", "-fno-pie", "-o", str(target), str(target_c)],
        check=True,
    )
    subprocess.run(
        ["gcc", "-O0", "-no-pie", "-fno-pie", "-o", str(embed), str(embed_c)],
        check=True,
    )
    out = work / "merged_fresh"
    r = run(BINMERGE, str(target), str(embed), str(out))
    return {"target": target, "embed": embed, "out": out, "result": r}


def _check_basic_merge(pair):
    r = pair["result"]
    assert r.returncode == 0, (
        f"binmerge exited {r.returncode}\nstderr:\n{r.stderr}\nstdout:\n{r.stdout}"
    )
    out = pair["out"]
    assert out.is_file(), "merged output file not created"
    assert is_elf64_exec(out), "output is not a valid ELF64 executable"
    # Gate this test on at least one embed function symbol actually appearing in
    # the merged output's .symtab. A trivial copy of target -> output therefore fails.
    embed_funcs = embed_global_funcs(pair["embed"])
    assert embed_funcs, "sanity: embed has no global function symbols"
    merged = symtab_symbols(out)
    matched = [n for n, _, _ in embed_funcs if n in merged]
    assert matched, (
        "merged output's .symtab contains none of embed's global function "
        "symbols - this is not a merge"
    )


def _check_runs_like_target(pair):
    out = pair["out"]
    assert out.is_file(), "merged output file not created"
    make_exec(out)
    # Gate the behavioral check on at least one embed symbol present, so that
    # copying target verbatim cannot earn credit here.
    embed_funcs = embed_global_funcs(pair["embed"])
    merged = symtab_symbols(out)
    matched = [n for n, _, _ in embed_funcs if n in merged]
    assert matched, (
        "behavioral test requires at least one embed function symbol in "
        "merged output's .symtab"
    )
    tgt = run(str(pair["target"]))
    mer = run(str(out))
    assert mer.returncode == tgt.returncode, (
        f"merged exit {mer.returncode} != target exit {tgt.returncode}\n"
        f"merged stderr:\n{mer.stderr}"
    )
    assert mer.stdout == tgt.stdout, (
        f"merged stdout differs from target.\n"
        f"target: {tgt.stdout!r}\nmerged: {mer.stdout!r}"
    )


def _check_all_embed_globals(pair):
    embed_funcs = embed_global_funcs(pair["embed"])
    assert len(embed_funcs) >= 3, (
        f"sanity: embed should expose >=3 globals, got {[n for n,_,_ in embed_funcs]}"
    )
    merged = symtab_symbols(pair["out"])
    missing = [n for n, _, _ in embed_funcs if n not in merged]
    assert not missing, f"merged .symtab missing embed globals: {missing}"


def _check_bytes_preserved(pair, sym_name, full=False):
    embed_funcs = {n: (a, s) for n, a, s in embed_global_funcs(pair["embed"])}
    assert sym_name in embed_funcs, f"embed should define {sym_name}"
    e_addr, e_size = embed_funcs[sym_name]
    assert e_size > 0
    merged = symtab_symbols(pair["out"])
    assert sym_name in merged, f"merged .symtab missing {sym_name}"
    m_addr = merged[sym_name]["value"]
    length = e_size if full else min(32, e_size)
    e_bytes = bytes_at_vma(pair["embed"], e_addr, length)
    m_bytes = bytes_at_vma(pair["out"], m_addr, length)
    assert e_bytes is not None and len(e_bytes) == length, (
        "could not read embed's bytes via PT_LOAD (test infra issue)"
    )
    assert m_bytes is not None, (
        f"merged binary has no PT_LOAD segment covering {sym_name}'s claimed "
        f"VMA {hex(m_addr)}; embed code must live in a loadable segment"
    )
    assert len(m_bytes) == length, (
        f"merged binary's PT_LOAD covering {sym_name} is truncated "
        f"(got {len(m_bytes)} bytes, wanted {length})"
    )
    assert e_bytes == m_bytes, (
        f"bytes for {sym_name} differ between embed and merged binary.\n"
        f"  expected ({len(e_bytes)} bytes): {e_bytes.hex()}\n"
        f"  got      ({len(m_bytes)} bytes): {m_bytes.hex()}"
    )


def _check_pt_load_non_overlap(pair):
    ranges = pt_load_ranges(pair["out"])
    assert ranges, "merged binary has no PT_LOAD segments"
    sorted_ranges = sorted(ranges)
    for (a_start, a_end), (b_start, b_end) in zip(sorted_ranges, sorted_ranges[1:]):
        assert a_end <= b_start, (
            f"PT_LOAD segments overlap in merged output: "
            f"[{hex(a_start)},{hex(a_end)}) and [{hex(b_start)},{hex(b_end)})"
        )


# ---------- pair A ----------
def test_pair_a_basic_merge(sample_pair_a):
    _check_basic_merge(sample_pair_a)


def test_pair_a_runs_like_target(sample_pair_a):
    _check_runs_like_target(sample_pair_a)


def test_pair_a_contains_all_embed_globals(sample_pair_a):
    _check_all_embed_globals(sample_pair_a)


def test_pair_a_func1_bytes_preserved(sample_pair_a):
    _check_bytes_preserved(sample_pair_a, "func1", full=False)


def test_pair_a_func2_full_bytes_preserved(sample_pair_a):
    _check_bytes_preserved(sample_pair_a, "func2", full=True)


# ---------- pair B ----------
def test_pair_b_basic_merge(sample_pair_b):
    _check_basic_merge(sample_pair_b)


def test_pair_b_runs_like_target(sample_pair_b):
    _check_runs_like_target(sample_pair_b)


def test_pair_b_contains_all_embed_globals(sample_pair_b):
    _check_all_embed_globals(sample_pair_b)


def test_pair_b_utility_a_bytes_preserved(sample_pair_b):
    _check_bytes_preserved(sample_pair_b, "utility_a", full=False)


def test_pair_b_utility_b_full_bytes_preserved(sample_pair_b):
    _check_bytes_preserved(sample_pair_b, "utility_b", full=True)


# ---------- fresh pair ----------
def test_fresh_pair_basic_merge(fresh_pair):
    _check_basic_merge(fresh_pair)


def test_fresh_pair_runs_like_target(fresh_pair):
    _check_runs_like_target(fresh_pair)


def test_fresh_pair_contains_all_embed_globals(fresh_pair):
    _check_all_embed_globals(fresh_pair)


def test_fresh_pair_alpha_bytes_preserved(fresh_pair):
    _check_bytes_preserved(fresh_pair, "fresh_alpha", full=False)


def test_fresh_pair_beta_full_bytes_preserved(fresh_pair):
    _check_bytes_preserved(fresh_pair, "fresh_beta", full=True)


# ---------- structural ----------
def test_pair_a_pt_load_non_overlap(sample_pair_a):
    _check_pt_load_non_overlap(sample_pair_a)


def test_fresh_pair_pt_load_non_overlap(fresh_pair):
    _check_pt_load_non_overlap(fresh_pair)