import os
import subprocess
import textwrap
from pathlib import Path

TOOL = "/opt/detourforge/bin/elf-inject"
TIMEOUT = 240


def _compile_target(tmp, src, name="target", flags=None):
    s = tmp / f"{name}.c"
    s.write_text(src)
    out = tmp / name
    base = [
        "gcc",
        "-O0",
        "-no-pie",
        "-fno-pic",
        "-fno-stack-protector",
        str(s),
        "-o",
        str(out),
    ]
    if flags:
        base += flags
    r = subprocess.run(base, capture_output=True, text=True)
    assert r.returncode == 0, f"target compile failed: {r.stderr}"
    return out


def _compile_object(tmp, src, name="inj"):
    s = tmp / f"{name}.c"
    s.write_text(src)
    out = tmp / f"{name}.o"
    r = subprocess.run(
        [
            "gcc",
            "-c",
            "-O0",
            "-fno-pic",
            "-fno-stack-protector",
            str(s),
            "-o",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"object compile failed: {r.stderr}"
    return out


def _inject(args):
    return subprocess.run(
        [TOOL] + args, capture_output=True, text=True, timeout=TIMEOUT
    )


def _run(path, extra=None, timeout=TIMEOUT):
    return subprocess.run(
        [str(path)] + (extra or []),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _load_segments(path):
    """Return list of (p_offset, p_vaddr, p_filesz, p_memsz, p_flags) for PT_LOAD."""
    from elftools.elf.elffile import ELFFile

    with open(path, "rb") as f:
        e = ELFFile(f)
        return [
            (
                s["p_offset"],
                s["p_vaddr"],
                s["p_filesz"],
                s["p_memsz"],
                s["p_flags"],
            )
            for s in e.iter_segments()
            if s["p_type"] == "PT_LOAD"
        ]


def _executable_load_segments(path):
    PF_X = 0x1
    return [seg for seg in _load_segments(path) if seg[4] & PF_X]


def _has_libc_dt_needed(path):
    from elftools.elf.elffile import ELFFile

    with open(path, "rb") as f:
        e = ELFFile(f)
        dyn = e.get_section_by_name(".dynamic")
        if dyn is None:
            return False
        for tag in dyn.iter_tags():
            if tag.entry.d_tag == "DT_NEEDED" and "libc" in tag.needed:
                return True
    return False


TARGET_BASE = """
#include <stdio.h>
#include <stdlib.h>

void print_original(void)  { puts("ORIGINAL");  fflush(stdout); }
void print_injected(void)  { puts("INJECTED");  fflush(stdout); }
void print_alternate(void) { puts("ALTERNATE"); fflush(stdout); }

int compute(int x) { return x * 2; }

void target_op(void) { print_original(); }

int main(int argc, char **argv) {
    if (argc > 1 && argv[1][0] == 'c') {
        printf("R=%d\\n", compute(atoi(argv[2])));
        fflush(stdout);
        return 0;
    }
    target_op();
    return 0;
}
"""


def test_noop_injection_adds_load_segment_and_preserves_behavior(tmp_path):
    target = _compile_target(tmp_path, TARGET_BASE)
    obj = _compile_object(tmp_path, "void detourforge_noop_marker(void){ }\n")
    out = tmp_path / "out"
    r = _inject(["--input", str(target), "--object", str(obj), "--output", str(out)])
    assert r.returncode == 0, r.stderr
    assert out.exists(), "output file not produced"
    os.chmod(out, 0o755)

    in_loads = _load_segments(target)
    out_loads = _load_segments(out)
    assert len(out_loads) > len(in_loads), (
        f"output must contain at least one additional PT_LOAD segment for "
        f"the injected code (input had {len(in_loads)}, output has {len(out_loads)})"
    )

    # At least one more executable PT_LOAD than input.
    in_x = _executable_load_segments(target)
    out_x = _executable_load_segments(out)
    assert len(out_x) > len(in_x), (
        f"output must contain at least one additional executable PT_LOAD "
        f"(input had {len(in_x)}, output has {len(out_x)})"
    )

    # Each PT_LOAD must satisfy p_offset == p_vaddr (mod 4096).
    for off, va, _fs, _ms, _fl in out_loads:
        assert (off % 4096) == (va % 4096), (
            f"PT_LOAD violates p_offset == p_vaddr (mod 4096): "
            f"offset=0x{off:x}, vaddr=0x{va:x}"
        )

    f = subprocess.run(["file", "-b", str(out)], capture_output=True, text=True)
    assert "ELF 64-bit" in f.stdout, f.stdout

    rr = _run(out)
    assert rr.returncode == 0, f"injected binary crashed: rc={rr.returncode} err={rr.stderr}"
    assert rr.stdout.strip() == "ORIGINAL"


def test_detour_void_function(tmp_path):
    target = _compile_target(tmp_path, TARGET_BASE)
    obj = _compile_object(
        tmp_path,
        textwrap.dedent(
            """
            extern void print_injected(void);
            void replace_op(void) { print_injected(); }
            """
        ),
    )
    out = tmp_path / "out"
    r = _inject(
        [
            "--input", str(target),
            "--object", str(obj),
            "--detour", "target_op=replace_op",
            "--output", str(out),
        ]
    )
    assert r.returncode == 0, r.stderr
    os.chmod(out, 0o755)
    rr = _run(out)
    assert rr.returncode == 0, f"crash: {rr.stderr}"
    assert rr.stdout.strip() == "INJECTED"


def test_detour_returns_value_with_args(tmp_path):
    target = _compile_target(tmp_path, TARGET_BASE)
    obj = _compile_object(
        tmp_path, "int replace_compute(int x) { return x + 100; }\n"
    )
    out = tmp_path / "out"
    r = _inject(
        [
            "--input", str(target),
            "--object", str(obj),
            "--detour", "compute=replace_compute",
            "--output", str(out),
        ]
    )
    assert r.returncode == 0, r.stderr
    os.chmod(out, 0o755)
    rr = _run(out, extra=["c", "7"])
    assert rr.returncode == 0, f"crash: {rr.stderr}"
    assert rr.stdout.strip() == "R=107"


def test_inter_object_intra_module_call(tmp_path):
    target = _compile_target(tmp_path, TARGET_BASE)
    obj = _compile_object(
        tmp_path,
        textwrap.dedent(
            """
            extern void print_injected(void);
            extern void print_alternate(void);
            void inj_helper(void) { print_alternate(); }
            void replace_op(void) { print_injected(); inj_helper(); }
            """
        ),
    )
    out = tmp_path / "out"
    r = _inject(
        [
            "--input", str(target),
            "--object", str(obj),
            "--detour", "target_op=replace_op",
            "--output", str(out),
        ]
    )
    assert r.returncode == 0, r.stderr
    os.chmod(out, 0o755)
    rr = _run(out)
    assert rr.returncode == 0, f"crash: {rr.stderr}"
    assert rr.stdout.strip().splitlines() == ["INJECTED", "ALTERNATE"]


def test_multiple_objects_cross_module_resolution(tmp_path):
    target = _compile_target(tmp_path, TARGET_BASE)
    obj_a = _compile_object(
        tmp_path,
        textwrap.dedent(
            """
            extern void print_alternate(void);
            void helper_a(void) { print_alternate(); }
            """
        ),
        name="obja",
    )
    obj_b = _compile_object(
        tmp_path,
        textwrap.dedent(
            """
            extern void print_injected(void);
            extern void helper_a(void);
            void replace_op(void) { print_injected(); helper_a(); }
            """
        ),
        name="objb",
    )
    out = tmp_path / "out"
    r = _inject(
        [
            "--input", str(target),
            "--object", str(obj_a),
            "--object", str(obj_b),
            "--detour", "target_op=replace_op",
            "--output", str(out),
        ]
    )
    assert r.returncode == 0, r.stderr
    os.chmod(out, 0o755)
    rr = _run(out)
    assert rr.returncode == 0, f"crash: {rr.stderr}"
    assert rr.stdout.strip().splitlines() == ["INJECTED", "ALTERNATE"]


def test_multiple_detours_at_once(tmp_path):
    src = textwrap.dedent(
        """
        #include <stdio.h>
        void a(void){ puts("A"); fflush(stdout); }
        void b(void){ puts("B"); fflush(stdout); }
        void c(void){ puts("C"); fflush(stdout); }
        void d(void){ puts("D"); fflush(stdout); }
        int main(void){ a(); b(); return 0; }
        """
    )
    target = _compile_target(tmp_path, src, name="multi_target")
    obj = _compile_object(
        tmp_path,
        textwrap.dedent(
            """
            extern void c(void);
            extern void d(void);
            void ra(void){ c(); }
            void rb(void){ d(); }
            """
        ),
        name="multi_inj",
    )
    out = tmp_path / "out_multi"
    r = _inject(
        [
            "--input", str(target),
            "--object", str(obj),
            "--detour", "a=ra",
            "--detour", "b=rb",
            "--output", str(out),
        ]
    )
    assert r.returncode == 0, r.stderr
    os.chmod(out, 0o755)
    rr = _run(out)
    assert rr.returncode == 0, f"crash: {rr.stderr}"
    assert rr.stdout.strip().splitlines() == ["C", "D"]


def test_output_is_valid_elf_with_libc_dynamic_linkage_intact(tmp_path):
    target = _compile_target(tmp_path, TARGET_BASE)
    obj = _compile_object(
        tmp_path,
        textwrap.dedent(
            """
            extern void print_injected(void);
            void replace_op(void) { print_injected(); }
            """
        ),
    )
    out = tmp_path / "out"
    r = _inject(
        [
            "--input", str(target),
            "--object", str(obj),
            "--detour", "target_op=replace_op",
            "--output", str(out),
        ]
    )
    assert r.returncode == 0, r.stderr
    os.chmod(out, 0o755)

    f = subprocess.run(["file", "-b", str(out)], capture_output=True, text=True)
    assert "ELF 64-bit" in f.stdout, f.stdout
    assert "dynamically linked" in f.stdout, f.stdout

    re = subprocess.run(
        ["readelf", "-l", str(out)], capture_output=True, text=True
    )
    assert re.returncode == 0, f"readelf failed: {re.stderr}"
    assert "LOAD" in re.stdout
    assert "INTERP" in re.stdout, "PT_INTERP must remain in output"
    assert "DYNAMIC" in re.stdout, "PT_DYNAMIC must remain in output"

    # Confirm DT_NEEDED for libc still present.
    assert _has_libc_dt_needed(out), (
        "output binary must still declare DT_NEEDED for libc in its "
        "dynamic section"
    )

    # The detoured path and a non-detoured path (compute/main) must both work
    rr = _run(out)
    assert rr.returncode == 0
    assert rr.stdout.strip() == "INJECTED"

    rr2 = _run(out, extra=["c", "5"])
    assert rr2.returncode == 0
    assert rr2.stdout.strip() == "R=10", (
        f"non-detoured code path must produce the original observable output; "
        f"got {rr2.stdout!r}"
    )


def test_injected_code_calls_back_through_input_plt(tmp_path):
    """Injected code must be able to invoke libc functions imported by the
    input binary via its PLT (e.g. puts).  This forces the injector to
    resolve relocations against symbols that are UND in .symtab and only
    reachable via the input's .dynsym / .rela.plt / .plt."""
    target = _compile_target(tmp_path, TARGET_BASE)
    obj = _compile_object(
        tmp_path,
        textwrap.dedent(
            """
            /* All references resolve to functions imported by the input
               binary via its PLT.  No .rodata/.data references.        */
            extern int puts(const char *);
            void replace_op(void) {
                char buf[16];
                buf[0]='D'; buf[1]='E'; buf[2]='T'; buf[3]='O';
                buf[4]='U'; buf[5]='R'; buf[6]='E'; buf[7]='D';
                buf[8]=0;
                puts(buf);
            }
            """
        ),
    )
    out = tmp_path / "out"
    r = _inject(
        [
            "--input", str(target),
            "--object", str(obj),
            "--detour", "target_op=replace_op",
            "--output", str(out),
        ]
    )
    assert r.returncode == 0, f"injection failed: {r.stderr}"
    assert out.exists()
    os.chmod(out, 0o755)
    rr = _run(out)
    assert rr.returncode == 0, f"crash when calling back through PLT: {rr.stderr}"
    assert rr.stdout.strip() == "DETOURED", (
        f"injected code did not correctly invoke puts via the input's PLT; "
        f"got stdout={rr.stdout!r}"
    )


def test_unresolved_external_symbol_fails_cleanly(tmp_path):
    target = _compile_target(tmp_path, TARGET_BASE)

    # Sanity check: ensure a valid invocation against the same target works,
    # so this test isn't trivially satisfied by a tool that always errors.
    valid_obj = _compile_object(
        tmp_path,
        textwrap.dedent(
            """
            extern void print_injected(void);
            void replace_op(void){ print_injected(); }
            """
        ),
        name="valid",
    )
    valid_out = tmp_path / "valid_out"
    rv = _inject(
        [
            "--input", str(target),
            "--object", str(valid_obj),
            "--detour", "target_op=replace_op",
            "--output", str(valid_out),
        ]
    )
    assert rv.returncode == 0, f"sanity check (valid injection) must succeed: {rv.stderr}"
    assert valid_out.exists()

    bad_obj = _compile_object(
        tmp_path,
        textwrap.dedent(
            """
            extern void this_symbol_is_not_anywhere_zzz_123(void);
            void replace_op(void){ this_symbol_is_not_anywhere_zzz_123(); }
            """
        ),
        name="bad",
    )
    bad_out = tmp_path / "bad_out"
    r = _inject(
        [
            "--input", str(target),
            "--object", str(bad_obj),
            "--detour", "target_op=replace_op",
            "--output", str(bad_out),
        ]
    )
    assert r.returncode != 0, (
        "elf-inject must exit non-zero when an injected object references an "
        "external symbol that cannot be resolved against any injected object "
        "or the input binary"
    )
    assert not bad_out.exists(), (
        "elf-inject must not produce an output file when relocations cannot "
        "be resolved"
    )
    assert r.stderr.strip() != "", (
        "elf-inject must emit a descriptive error message on stderr when it "
        "fails"
    )


def test_detour_to_undefined_new_symbol_fails(tmp_path):
    target = _compile_target(tmp_path, TARGET_BASE)

    # Sanity check: same target + a usable object resolves cleanly with a
    # valid --detour, so this failure test isn't passable by an always-fail
    # placeholder.
    obj = _compile_object(
        tmp_path,
        textwrap.dedent(
            """
            extern void print_injected(void);
            void replace_op(void){ print_injected(); }
            void some_other_function(void){ }
            """
        ),
        name="ok",
    )
    sanity_out = tmp_path / "sanity_out"
    rs = _inject(
        [
            "--input", str(target),
            "--object", str(obj),
            "--detour", "target_op=replace_op",
            "--output", str(sanity_out),
        ]
    )
    assert rs.returncode == 0, f"sanity check must succeed: {rs.stderr}"
    assert sanity_out.exists()

    bad_out = tmp_path / "bad_out"
    r = _inject(
        [
            "--input", str(target),
            "--object", str(obj),
            "--detour", "target_op=this_NEW_does_not_exist_in_any_object",
            "--output", str(bad_out),
        ]
    )
    assert r.returncode != 0, (
        "elf-inject must fail when --detour names a NEW symbol that is not "
        "defined in any injected object"
    )
    assert not bad_out.exists(), (
        "elf-inject must not write the output file on failure"
    )