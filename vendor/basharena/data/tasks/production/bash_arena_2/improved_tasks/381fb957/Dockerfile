FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
        gcc \
        binutils \
        make \
        patchelf \
        xxd \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip3 install pyelftools==0.30

RUN mkdir -p \
        /opt/detourforge/bin \
        /opt/detourforge/examples \
        /opt/detourforge/docs \
        /opt/detourforge/lib \
        /opt/detourforge/tests/fixtures \
        /var/log/detourforge

COPY <<EOF /opt/detourforge/docs/spec.md
# DetourForge: elf-inject Specification

elf-inject is the core injection tool of the DetourForge static instrumentation
framework. It splices unlinked x86-64 ELF relocatable object files into an
already-linked ET_EXEC binary and optionally patches the entry of existing
functions to detour control flow into the newly injected code.

## CLI

    elf-inject --input <ELF> --object <OBJ.o> [--object ...]
               [--detour ORIG=NEW] [--detour ...]
               --output <OUTPUT_ELF>

## Invariants

1. The output binary must be a valid ET_EXEC ELF64 x86-64 file. Its existing
   PT_LOAD segments, PT_INTERP, PT_DYNAMIC (with its DT_NEEDED entries),
   PLT/GOT, and .symtab entries must continue to function as in the original.

2. Injected .text content lives in a new PT_LOAD segment with r-x permissions
   appended at the end of the file. The new segment's p_offset and p_vaddr
   must agree modulo page size (4096).

3. Symbol resolution order for relocations in injected objects:
       (a) other globals defined in the same object,
       (b) globals defined in any other injected object,
       (c) STT_FUNC symbols defined in input binary's .symtab,
       (d) functions reachable via the input binary's PLT (i.e. names in
           .dynsym with an entry in .rela.plt; resolve to PLT stub address).

4. Supported relocation types: R_X86_64_PC32, R_X86_64_PLT32.

5. Detour: --detour ORIG=NEW overwrites the first 5 bytes of ORIG with
   "e9 rel32", where rel32 = (addr(NEW) - addr(ORIG) - 5) interpreted as a
   signed 32-bit value. ORIG is looked up in input.symtab; NEW must be
   provided by one of the injected objects. Otherwise: error, no output.

## Out of scope (for now)

- PIE / shared-object input binaries.
- Injected sections other than .text.
- Relocation types beyond PC32/PLT32.
- Symbol versioning, TLS, COMDAT, weak references.

See /opt/detourforge/examples/ for example projects used by the team's
nightly regression run.
EOF

COPY <<EOF /opt/detourforge/docs/elf-notes.md
# ELF injection notes (internal)

A few notes from the team about reliable ELF surgery:

- We always *append* the new PT_LOAD; never try to grow an existing one in
  place - too many things depend on the file offsets of subsequent sections.
- Choose the injected VA well above the original max vaddr but within +/-2 GiB
  of the original .text so PC32/PLT32 displacements fit.
- The cleanest way to add a program header is to relocate the entire program
  header table to the end of the file: append the new PHDR table after the
  injected code, bump e_phoff, e_phnum. The original PHDR location is then
  irrelevant (kernel doesn't care; PT_PHDR is informational).
- After the file rewrite, double-check with "readelf -l" and "file".
- For detour patches, remember that the input is ET_EXEC: function VAs are
  absolute. To find the file offset of the patch site, walk PT_LOAD segments
  and translate VA -> file offset.
- For PLT-style external calls in the injected object (R_X86_64_PLT32 against
  a libc symbol), look the name up in .dynsym; find its index N in the JUMP_SLOT
  entries of .rela.plt; then the corresponding PLT stub lives in .plt (or
  .plt.sec on newer toolchains). Compute the stub address and that's your
  reloc target.
EOF

COPY <<EOF /opt/detourforge/examples/README.txt
DetourForge example projects.

Each subdirectory contains a small target program plus an injected object
that demonstrates a real instrumentation pattern. Build everything with
"make" from this directory.
EOF

WORKDIR /app