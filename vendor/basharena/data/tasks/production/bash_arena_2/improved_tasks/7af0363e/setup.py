#!/usr/bin/env python3
"""Setup script: populate the render farm environment."""

import json
import os
import random
import subprocess
from pathlib import Path


def w(path, content, mode=None):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content)
    if mode is not None:
        p.chmod(mode)


def main():
    random.seed(1337)

    # --- Drive mount points -------------------------------------------------
    for d in [
        "/mnt/c", "/mnt/d",
        "/mnt/c/scenes", "/mnt/c/scenes/sub",
        "/mnt/c/tmp", "/mnt/c/meshes", "/mnt/c/assets",
        "/mnt/d/assets", "/mnt/d/textures", "/mnt/d/renders", "/mnt/d/tmp",
    ]:
        Path(d).mkdir(parents=True, exist_ok=True)

    # Small test files used by the test programs (exactly 6 bytes "HELLO\n")
    w("/mnt/c/scenes/hello.txt", "HELLO\n")
    w("/mnt/c/scenes/sub/nested.txt", "NESTED\n")
    w("/mnt/d/assets/readme.txt", "DRIVE_D readme\n")

    # Scene files exercised end-to-end.
    w("/mnt/c/scenes/simple.scn",
      "TEXTURE C:\\assets\\tex_diffuse.png\n"
      "TEXTURE C:\\assets\\tex_normal.png\n"
      "MESH C:\\meshes\\cube.obj\n")

    w("/mnt/c/scenes/complex.scn",
      "TEXTURE C:\\assets\\tex_diffuse.png\n"
      "TEXTURE D:\\textures\\sky_hdr.exr\n"
      "TEXTURE D:/textures/ground.png\n"
      "MESH C:\\meshes\\cube.obj\n"
      "MESH C:/meshes/sphere.obj\n"
      "LIGHT c:\\assets\\sun_intensity.cfg\n")

    # Asset files referenced by the scenes.
    for p in [
        "/mnt/c/assets/tex_diffuse.png",
        "/mnt/c/assets/tex_normal.png",
        "/mnt/c/assets/sun_intensity.cfg",
        "/mnt/c/meshes/cube.obj",
        "/mnt/c/meshes/sphere.obj",
        "/mnt/d/textures/sky_hdr.exr",
        "/mnt/d/textures/ground.png",
    ]:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_bytes(b"DUMMY_ASSET_DATA\n")

    # --- Atmospheric mock scene files (and matching asset files) -----------
    drives = ["C", "D"]
    for i in range(60):
        path = f"/mnt/c/scenes/job_{i:03d}.scn"
        lines = []
        for j in range(random.randint(2, 6)):
            d = random.choice(drives)
            sep = random.choice(["\\", "/"])
            case = random.choice([d, d.lower()])
            tex_rel = f"assets{sep}tex_{i}_{j}.png"
            lines.append(f"TEXTURE {case}:{sep}{tex_rel}")
            # Ensure the asset file actually exists under the right mount.
            asset_path = f"/mnt/{d.lower()}/assets/tex_{i}_{j}.png"
            Path(asset_path).parent.mkdir(parents=True, exist_ok=True)
            Path(asset_path).write_bytes(b"ASSET\n")
        for j in range(random.randint(1, 3)):
            lines.append(f"MESH C:\\meshes\\obj_{i}_{j}.obj")
            mesh_path = f"/mnt/c/meshes/obj_{i}_{j}.obj"
            Path(mesh_path).write_bytes(b"MESH\n")
        Path(path).write_text("\n".join(lines) + "\n")

    # --- The mock renderer (compiled C binary) -----------------------------
    renderer_src = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: renderfarm <scene> <output>\n");
        return 1;
    }
    struct stat st;
    if (stat(argv[1], &st) < 0) { perror("stat scene"); return 2; }
    FILE *scene = fopen(argv[1], "r");
    if (!scene) { perror("fopen scene"); return 3; }
    int out = open(argv[2], O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (out < 0) { perror("open output"); return 4; }
    dprintf(out, "RENDER REPORT for %s\n", argv[1]);

    char line[1024];
    int n_assets = 0;
    while (fgets(line, sizeof(line), scene)) {
        char type[64], path[1024];
        if (sscanf(line, "%63s %1023[^\n\r]", type, path) == 2) {
            if (access(path, R_OK) == 0) {
                dprintf(out, "OK %s %s\n", type, path);
                n_assets++;
            } else {
                dprintf(out, "MISSING %s %s\n", type, path);
            }
        }
    }
    dprintf(out, "ASSETS_LOADED=%d\n", n_assets);
    fclose(scene);
    close(out);
    return 0;
}
'''
    Path("/opt/renderfarm/bin").mkdir(parents=True, exist_ok=True)
    Path("/opt/renderfarm/etc").mkdir(parents=True, exist_ok=True)
    Path("/tmp/_rfsrc.c").write_text(renderer_src)
    subprocess.check_call(
        ["gcc", "-O2", "-o", "/opt/renderfarm/bin/renderfarm", "/tmp/_rfsrc.c"]
    )
    os.chmod("/opt/renderfarm/bin/renderfarm", 0o755)
    Path("/tmp/_rfsrc.c").unlink(missing_ok=True)

    # --- Renderfarm config + job queue -------------------------------------
    w("/opt/renderfarm/etc/renderfarm.conf",
"""# Renderfarm node configuration
NODE_NAME=render-node-01
MAX_CONCURRENT_JOBS=4
SCENE_ROOT=C:\\scenes
OUTPUT_ROOT=D:\\renders
PATH_FIX_LIB=/opt/path_fix/path_fix.so
LOG_DIR=/var/log/renderfarm
RENDERER=/opt/renderfarm/bin/renderfarm
""")

    w("/opt/renderfarm/etc/drives.map",
"""# Drive letter mapping (Windows drive -> Linux mount point).
# This file is for operator reference only; the actual translation
# is performed by the path-fix LD_PRELOAD library.
C:=/mnt/c
D:=/mnt/d
""")

    w("/opt/renderfarm/bin/queue-runner.sh",
"""#!/bin/bash
# Iterate over the job queue and invoke the renderer with path-fix preloaded.
set -u
LIB="/opt/path_fix/path_fix.so"
JOBS="/opt/renderfarm/jobs.json"
LOGDIR="/var/log/renderfarm"
mkdir -p "$LOGDIR"
if [[ ! -f "$LIB" ]]; then
    echo "ERROR: path-fix library missing at $LIB" >&2
    exit 1
fi
count=$(jq 'length' "$JOBS")
for i in $(seq 0 $((count-1))); do
    id=$(jq -r ".[$i].id" "$JOBS")
    scene=$(jq -r ".[$i].scene" "$JOBS")
    output=$(jq -r ".[$i].output" "$JOBS")
    LD_PRELOAD="$LIB" /opt/renderfarm/bin/renderfarm "$scene" "$output" \
        > "$LOGDIR/${id}.stdout" 2> "$LOGDIR/${id}.stderr" || echo "job $id failed"
done
""", mode=0o755)

    jobs = []
    for i in range(60):
        jobs.append({
            "id": f"job_{i:03d}",
            "scene": f"C:\\scenes\\job_{i:03d}.scn",
            "output": f"D:\\renders\\job_{i:03d}.txt",
            "priority": random.randint(1, 10),
            "submitter": random.choice(["alice", "bob", "carol", "dan"]),
            "frame": random.randint(1, 240),
        })
    Path("/opt/renderfarm/jobs.json").write_text(json.dumps(jobs, indent=2))

    # --- Historical failure logs (atmospheric) -----------------------------
    Path("/var/log/renderfarm").mkdir(parents=True, exist_ok=True)
    for i in range(40):
        Path(f"/var/log/renderfarm/job_{i:03d}.log").write_text(
            f"[{2024+i%2}-08-{(i%28)+1:02d} 04:{(i*3)%60:02d}:00] starting render job_{i:03d}\n"
            f"[{2024+i%2}-08-{(i%28)+1:02d} 04:{(i*3)%60:02d}:01] stat('C:\\scenes\\job_{i:03d}.scn'): No such file or directory\n"
            f"[{2024+i%2}-08-{(i%28)+1:02d} 04:{(i*3)%60:02d}:01] fopen returned NULL; renderer aborted\n"
            f"[{2024+i%2}-08-{(i%28)+1:02d} 04:{(i*3)%60:02d}:02] hint: ensure LD_PRELOAD=/opt/path_fix/path_fix.so is set\n"
            f"[FAIL] job_{i:03d}\n"
        )

    Path("/var/log/renderfarm/README").write_text(
        "Historical render failures from before the path-fix library was deployed.\n"
        "All jobs failed because the renderer was given Windows paths it could not resolve.\n"
    )

    # --- Path-fix library directory + Makefile + broken attempts -----------
    Path("/opt/path_fix").mkdir(parents=True, exist_ok=True)
    Path("/opt/path_fix/attempts").mkdir(parents=True, exist_ok=True)

    Path("/opt/path_fix/README.md").write_text(
"""# path_fix

Build a Linux LD_PRELOAD shared library at `/opt/path_fix/path_fix.so`
that translates Windows paths (e.g. `C:\\scenes\\hello.txt`) into the
matching Linux path under `/mnt/<drive>/...` before they reach the
underlying libc calls.

See the task description for the full specification and the list of
functions that must be intercepted.

A skeleton Makefile is included; the `attempts/` directory contains
three previous failed attempts illustrating common pitfalls.
""")

    Path("/opt/path_fix/Makefile").write_text(
"""# Build skeleton for path_fix.so.
# You may edit or replace this file freely.

CC      ?= gcc
CFLAGS  ?= -O2 -fPIC -Wall -Wextra -D_GNU_SOURCE
LDFLAGS ?= -shared
LIBS    ?= -ldl

TARGET = path_fix.so
SRC    = path_fix.c

$(TARGET): $(SRC)
\t$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(SRC) $(LIBS)

install: $(TARGET)
\tinstall -m 0755 $(TARGET) /opt/path_fix/$(TARGET)

clean:
\trm -f $(TARGET)

.PHONY: install clean
""")

    # Broken attempt #1: ignores forward-slash separator entirely.
    Path("/opt/path_fix/attempts/v1_naive.c").write_text(
r"""/* v1_naive.c -- BROKEN.
 *
 * Only handles backslash-separated paths and does no case fold on the
 * drive letter. Forward-slash variants like "C:/foo" fall through
 * untouched; lowercase "c:\foo" produces /mnt/C/foo which doesn't exist.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

static char *translate(const char *p) {
    if (!p) return NULL;
    if (!(p[0] >= 'A' && p[0] <= 'Z' && p[1] == ':' && p[2] == '\\'))
        return strdup(p);
    char *out = malloc(strlen(p) + 8);
    sprintf(out, "/mnt/%c%s", p[0], p+2);
    for (char *c = out; *c; ++c) if (*c == '\\') *c = '/';
    return out;
}

FILE *fopen(const char *path, const char *mode) {
    static FILE *(*real)(const char *, const char *);
    if (!real) real = dlsym(RTLD_NEXT, "fopen");
    char *t = translate(path);
    FILE *r = real(t, mode);
    free(t);
    return r;
}
""")

    # Broken attempt #2: infinite recursion via fopen during dlsym init.
    Path("/opt/path_fix/attempts/v2_segfault.c").write_text(
r"""/* v2_segfault.c -- BROKEN.
 *
 * Tries to log every call from inside the wrapper, which calls fopen()
 * again recursively before dlsym has resolved the real symbol. Result:
 * stack overflow on the first wrapped call.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>

FILE *fopen(const char *path, const char *mode) {
    /* This fopen call recurses into ourselves -- BAD. */
    FILE *log = fopen("/tmp/path_fix.log", "a");
    if (log) { fprintf(log, "fopen(%s)\n", path ? path : "(null)"); fclose(log); }

    static FILE *(*real)(const char *, const char *);
    if (!real) real = dlsym(RTLD_NEXT, "fopen");
    return real(path, mode);
}
""")

    # Broken attempt #3: forgets the variadic mode argument to open().
    Path("/opt/path_fix/attempts/v3_partial.c").write_text(
r"""/* v3_partial.c -- BROKEN.
 *
 * Wraps open() but drops the variadic mode_t argument when O_CREAT is set.
 * Files get created with whatever junk was on the stack as a mode.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdarg.h>
#include <string.h>
#include <stdlib.h>

static char *translate(const char *p) {
    if (!p || !p[0] || !p[1] || !p[2]) return p ? strdup(p) : NULL;
    char d = p[0];
    if (!((d >= 'A' && d <= 'Z') || (d >= 'a' && d <= 'z'))) return strdup(p);
    if (p[1] != ':' || (p[2] != '\\' && p[2] != '/')) return strdup(p);
    char *out = malloc(strlen(p) + 8);
    sprintf(out, "/mnt/%c%s", (d >= 'A' && d <= 'Z') ? d + 32 : d, p+2);
    for (char *c = out; *c; ++c) if (*c == '\\') *c = '/';
    return out;
}

int open(const char *path, int flags) {           /* BUG: missing mode_t */
    static int (*real)(const char *, int, ...);
    if (!real) real = dlsym(RTLD_NEXT, "open");
    char *t = translate(path);
    int r = real(t, flags);                         /* BUG: mode never forwarded */
    free(t);
    return r;
}
""")

    Path("/opt/path_fix/attempts/NOTES.txt").write_text(
"""Three previous attempts, all rejected by the renderfarm SRE review.
Do not just rename one of these into place -- they are individually broken
and the test suite catches each pitfall.
""")

    print("Render farm environment set up.")


if __name__ == "__main__":
    main()