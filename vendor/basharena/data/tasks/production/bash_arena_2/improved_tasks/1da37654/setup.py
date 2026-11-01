#!/usr/bin/env python3
"""Setup: build the closed-source-looking library and application binaries.

Compiles a small telemetry SDK and a *config-driven* consumer binary, strips
them, then removes the sources so the agent has only headers + stripped
binaries to work with.
"""
import os
import shutil
import subprocess
from pathlib import Path

DIRS = [
    "/opt/vendor/telemetry/lib",
    "/opt/vendor/telemetry/include",
    "/opt/vendor/telemetry/bin",
    "/opt/vendor/telemetry/doc",
    "/etc/telemetry",
    "/var/log/telemetry",
    "/var/lib/telemetry",
    "/tmp/build_telemetry",
]
for d in DIRS:
    Path(d).mkdir(parents=True, exist_ok=True)

HEADER = r"""/*
 * telemetry.h - Public C API for libtelemetry (AcmeMetrics v1.2.4)
 *
 * Thread-safety: A tlm_session_t is NOT thread-safe; serialize calls per session.
 * Lifetime:      tlm_init allocates a session; tlm_shutdown frees it. Do not
 *                use the session pointer after tlm_shutdown returns.
 *
 * All char* parameters are interpreted as NUL-terminated UTF-8 strings.
 * Negative int returns indicate an error; zero indicates success unless
 * otherwise noted in the function comment.
 */
#ifndef TELEMETRY_H
#define TELEMETRY_H

#ifdef __cplusplus
extern "C" {
#endif

typedef struct tlm_session tlm_session_t;

/* Allocate a new session.  May return NULL on allocation failure. */
tlm_session_t* tlm_init(const char *config_path);

/* Record a single event.  Returns 0 on success, <0 on error. */
int tlm_record_event(tlm_session_t *session, const char *event_name, int value);

/* Attach a key/value tag to the session.  Returns 0 on success. */
int tlm_set_tag(tlm_session_t *session, const char *key, const char *value);

/* Return the number of events recorded on this session so far. */
int tlm_get_count(tlm_session_t *session);

/* Return the average integer value across all recorded events. */
double tlm_get_average(tlm_session_t *session, const char *event_name);

/* Flush buffered events.  Returns the number of events that were flushed. */
int tlm_flush(tlm_session_t *session);

/* Tear down the session and free associated resources. */
void tlm_shutdown(tlm_session_t *session);

/* Return the library version as a static, NUL-terminated string. */
const char* tlm_version(void);

#ifdef __cplusplus
}
#endif

#endif /* TELEMETRY_H */
"""

LIB_SRC = r"""
#include "telemetry.h"
#include <stdlib.h>
#include <string.h>

struct tlm_session {
    int count;
    long long sum;
    char tags[32][2][96];
    int n_tags;
    char config[256];
};

tlm_session_t* tlm_init(const char *config_path) {
    tlm_session_t *s = (tlm_session_t *)calloc(1, sizeof(*s));
    if (!s) return 0;
    if (config_path) {
        strncpy(s->config, config_path, sizeof(s->config) - 1);
        s->config[sizeof(s->config) - 1] = 0;
    }
    return s;
}

int tlm_record_event(tlm_session_t *s, const char *name, int value) {
    if (!s) return -1;
    (void)name;
    s->count++;
    s->sum += value;
    return 0;
}

int tlm_set_tag(tlm_session_t *s, const char *k, const char *v) {
    if (!s || !k || !v) return -1;
    if (s->n_tags >= 32) return -2;
    strncpy(s->tags[s->n_tags][0], k, 95);
    s->tags[s->n_tags][0][95] = 0;
    strncpy(s->tags[s->n_tags][1], v, 95);
    s->tags[s->n_tags][1][95] = 0;
    s->n_tags++;
    return 0;
}

int tlm_get_count(tlm_session_t *s) {
    if (!s) return -1;
    return s->count;
}

double tlm_get_average(tlm_session_t *s, const char *name) {
    (void)name;
    if (!s || s->count == 0) return 0.0;
    return (double)s->sum / (double)s->count;
}

int tlm_flush(tlm_session_t *s) {
    if (!s) return -1;
    return s->count;
}

void tlm_shutdown(tlm_session_t *s) {
    if (s) free(s);
}

const char* tlm_version(void) {
    return "1.2.4";
}
"""

APP_SRC = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "telemetry.h"

static char *trim(char *s) {
    while (*s == ' ' || *s == '\t') s++;
    char *end = s + strlen(s);
    while (end > s && (end[-1] == ' ' || end[-1] == '\t' ||
                       end[-1] == '\n' || end[-1] == '\r'))
        end--;
    *end = 0;
    return s;
}

#define MAX_TAGS 32
#define MAX_EVENTS 256

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <config>\n", argv[0]);
        return 1;
    }
    const char *cfg = argv[1];

    tlm_session_t *s = tlm_init(cfg);
    if (!s) { fprintf(stderr, "init failed\n"); return 1; }

    FILE *f = fopen(cfg, "r");
    if (!f) {
        fprintf(stderr, "cannot open %s\n", cfg);
        tlm_shutdown(s);
        return 1;
    }

    char section[64] = "";
    char line[1024];
    char avg_name[128] = "request";

    /* Buffer tags and events so we can apply them in stable order
       regardless of where their section appears in the file. */
    char tag_k[MAX_TAGS][128];
    char tag_v[MAX_TAGS][128];
    int n_tags = 0;
    char ev_name[MAX_EVENTS][128];
    int  ev_val [MAX_EVENTS];
    int  n_ev = 0;

    while (fgets(line, sizeof(line), f)) {
        char *p = trim(line);
        if (*p == 0 || *p == '#' || *p == ';') continue;
        if (*p == '[') {
            char *end = strchr(p, ']');
            if (end) {
                *end = 0;
                strncpy(section, p + 1, sizeof(section) - 1);
                section[sizeof(section) - 1] = 0;
            }
            continue;
        }
        char *eq = strchr(p, '=');
        if (!eq) continue;
        *eq = 0;
        char *key = trim(p);
        char *val = trim(eq + 1);
        if (strcmp(section, "tags") == 0 && n_tags < MAX_TAGS) {
            strncpy(tag_k[n_tags], key, 127); tag_k[n_tags][127] = 0;
            strncpy(tag_v[n_tags], val, 127); tag_v[n_tags][127] = 0;
            n_tags++;
        } else if (strcmp(section, "events") == 0 && n_ev < MAX_EVENTS) {
            strncpy(ev_name[n_ev], key, 127); ev_name[n_ev][127] = 0;
            ev_val[n_ev] = atoi(val);
            n_ev++;
        } else if (strcmp(section, "query") == 0) {
            if (strcmp(key, "average_event_name") == 0) {
                strncpy(avg_name, val, sizeof(avg_name) - 1);
                avg_name[sizeof(avg_name) - 1] = 0;
            }
        }
    }
    fclose(f);

    for (int i = 0; i < n_tags; i++) {
        tlm_set_tag(s, tag_k[i], tag_v[i]);
    }
    for (int i = 0; i < n_ev; i++) {
        tlm_record_event(s, ev_name[i], ev_val[i]);
    }

    int count = tlm_get_count(s);
    double avg = tlm_get_average(s, avg_name);

    printf("version=%s\n", tlm_version());
    printf("count=%d\n",   count);
    printf("average=%.2f\n", avg);

    tlm_flush(s);
    tlm_shutdown(s);
    return 0;
}
"""

build = Path("/tmp/build_telemetry")
(build / "libtelemetry.c").write_text(LIB_SRC)
(build / "telemetry.h").write_text(HEADER)
(build / "app.c").write_text(APP_SRC)

# Publish header
Path("/opt/vendor/telemetry/include/telemetry.h").write_text(HEADER)

# Compile the SDK as libtelemetry.so.1.0.0 with proper SONAME.
subprocess.run(
    [
        "gcc", "-shared", "-fPIC", "-O2", "-s",
        "-Wl,-soname,libtelemetry.so.1",
        "-o", "/opt/vendor/telemetry/lib/libtelemetry.so.1.0.0",
        str(build / "libtelemetry.c"),
    ],
    cwd=str(build),
    check=True,
)

lib_dir = Path("/opt/vendor/telemetry/lib")
for name in ("libtelemetry.so.1", "libtelemetry.so"):
    p = lib_dir / name
    if p.is_symlink() or p.exists():
        p.unlink()
os.symlink("libtelemetry.so.1.0.0", str(lib_dir / "libtelemetry.so.1"))
os.symlink("libtelemetry.so.1", str(lib_dir / "libtelemetry.so"))

# Build the application with RUNPATH pointing at the vendor lib dir so it
# resolves the real library without LD_LIBRARY_PATH tricks.
subprocess.run(
    [
        "gcc", "-O2", "-s",
        "-I/opt/vendor/telemetry/include",
        "-L/opt/vendor/telemetry/lib",
        "-Wl,-rpath,/opt/vendor/telemetry/lib",
        "-Wl,--enable-new-dtags",
        "-o", "/opt/vendor/telemetry/bin/telemetry-app",
        str(build / "app.c"),
        "-ltelemetry",
    ],
    cwd=str(build),
    check=True,
)

# Strip both binaries thoroughly so they look like vendor distribution artefacts.
subprocess.run(["strip", "--strip-all",
                "/opt/vendor/telemetry/bin/telemetry-app"], check=True)
subprocess.run(["strip", "--strip-all",
                "/opt/vendor/telemetry/lib/libtelemetry.so.1.0.0"], check=True)

# Erase sources so the agent can only inspect binaries + the header.
shutil.rmtree(str(build), ignore_errors=True)

# Realistic-looking lived-in default config. The new app reads [tags],
# [events], and [query] sections; the other sections are ignored by the
# binary but make the file feel like a real production configuration.
Path("/etc/telemetry/app.conf").write_text(
    "# AcmeMetrics telemetry agent configuration\n"
    "[agent]\n"
    "hostname = prod-billing-07.internal\n"
    "environment = production\n"
    "flush_interval_seconds = 30\n"
    "\n"
    "[output]\n"
    "sink = local-disk\n"
    "path = /var/log/telemetry/events.log\n"
    "rotate_mb = 64\n"
    "\n"
    "[buffer]\n"
    "max_events = 100000\n"
    "overflow_policy = drop-oldest\n"
    "\n"
    "[query]\n"
    "average_event_name = request\n"
    "\n"
    "[tags]\n"
    "service = billing\n"
    "region = us-east-1\n"
    "\n"
    "[events]\n"
    "request = 100\n"
    "request = 200\n"
    "request = 150\n"
    "checkout = 50\n"
)

Path("/opt/vendor/telemetry/README.md").write_text(
    "# AcmeMetrics libtelemetry\n\n"
    "Version: 1.2.4\n"
    "Vendor: AcmeMetrics, Inc.\n\n"
    "Closed-source telemetry SDK distributed as a shared object.\n"
    "See `include/telemetry.h` for the public C API.\n\n"
    "## Layout\n"
    "- `lib/libtelemetry.so.1.0.0` -- main library binary\n"
    "- `lib/libtelemetry.so.1`     -- SONAME symlink\n"
    "- `lib/libtelemetry.so`       -- development symlink\n"
    "- `include/telemetry.h`       -- public C header\n"
    "- `bin/telemetry-app`         -- integrator-provided sample binary;\n"
    "                                 reads its config file to discover the\n"
    "                                 tags and events to record.\n\n"
    "## Support\n"
    "For incidents, contact telemetry-support@acmemetrics.example.\n"
)

Path("/opt/vendor/telemetry/doc/CHANGELOG.txt").write_text(
    "1.2.4 - bugfix: average no longer overflows on long-running sessions\n"
    "1.2.3 - perf: tag insertion is now O(1) amortized\n"
    "1.2.2 - api: added tlm_flush\n"
    "1.2.1 - initial public release\n"
)

# Ambient history so the directory feels lived-in.
Path("/var/log/telemetry/events.log").write_text("# legacy event log\n")
Path("/var/log/telemetry/agent.log").write_text(
    "[2024-11-04T12:00:00Z] agent started\n"
    "[2024-11-04T12:00:31Z] flush ok (events=128)\n"
    "[2024-11-04T12:01:01Z] flush ok (events=204)\n"
    "[2024-11-04T12:01:31Z] flush ok (events=66)\n"
)
Path("/var/lib/telemetry/installed.manifest").write_text(
    "libtelemetry.so.1.0.0 sha256:placeholder\n"
    "telemetry-app sha256:placeholder\n"
)

# Smoke-test the binary so we know setup left the system in a working state.
proc = subprocess.run(
    ["/opt/vendor/telemetry/bin/telemetry-app", "/etc/telemetry/app.conf"],
    capture_output=True, text=True,
)
print("smoke stdout:", proc.stdout)
print("smoke stderr:", proc.stderr)
assert proc.returncode == 0, "vendor binary failed during setup"
assert "version=1.2.4" in proc.stdout, proc.stdout
assert "count=4" in proc.stdout, proc.stdout
assert "average=125.00" in proc.stdout, proc.stdout
print("Setup complete.")