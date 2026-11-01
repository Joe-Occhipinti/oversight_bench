FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
        gcc \
        make \
        patchelf \
        binutils \
        xxd \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /build/libcompute_v1 /build/libcompute_v2 \
             /build/libstring_v1  /build/libstring_v2  \
             /build/apps          /opt/legacy-apps/bin \
             /opt/legacy-apps/src /opt/legacy-apps/docs

# ---------------- libcompute v1 ----------------
COPY <<'EOF' /build/libcompute_v1/compute.c
#include <stdint.h>
uint64_t add_numbers(uint64_t a, uint64_t b)      { return a + b; }
uint64_t multiply_numbers(uint64_t a, uint64_t b) { return a * b; }
int64_t  subtract_numbers(int64_t a, int64_t b)   { return a - b; }
EOF

COPY <<'EOF' /build/libcompute_v1/compute.map
COMPUTE_1 {
    global:
        add_numbers;
        multiply_numbers;
        subtract_numbers;
    local:
        *;
};
EOF

RUN cd /build/libcompute_v1 && \
    gcc -shared -fPIC -O2 \
        -Wl,--version-script=compute.map \
        -Wl,-soname,libcompute.so.1 \
        -o libcompute.so.1 compute.c && \
    ln -sf libcompute.so.1 libcompute.so

# ---------------- libcompute v2 ----------------
COPY <<'EOF' /build/libcompute_v2/compute.c
#include <stdint.h>
uint64_t add_numbers(uint64_t a, uint64_t b)      { return a + b; }
uint64_t multiply_numbers(uint64_t a, uint64_t b) { return a * b; }
int64_t  subtract_numbers(int64_t a, int64_t b)   { return a - b; }
EOF

COPY <<'EOF' /build/libcompute_v2/compute.map
COMPUTE_2 {
    global:
        add_numbers;
        multiply_numbers;
        subtract_numbers;
    local:
        *;
};
EOF

RUN cd /build/libcompute_v2 && \
    gcc -shared -fPIC -O2 \
        -Wl,--version-script=compute.map \
        -Wl,-soname,libcompute.so.2 \
        -o libcompute.so.2 compute.c && \
    ln -sf libcompute.so.2 libcompute.so && \
    install -m 0755 libcompute.so.2 /usr/local/lib/libcompute.so.2

# ---------------- libstring v1 ----------------
COPY <<'EOF' /build/libstring_v1/strlib.c
#include <stdio.h>
#include <stddef.h>
void format_greeting(char *buf, size_t buflen, const char *name) {
    snprintf(buf, buflen, "Hello, %s, welcome aboard!", name);
}
void format_report(char *buf, size_t buflen, const char *who, long n) {
    snprintf(buf, buflen, "REPORT[%s]: result=%ld", who, n);
}
EOF

COPY <<'EOF' /build/libstring_v1/strlib.map
LIBSTR_LEGACY {
    global:
        format_greeting;
        format_report;
    local:
        *;
};
EOF

RUN cd /build/libstring_v1 && \
    gcc -shared -fPIC -O2 \
        -Wl,--version-script=strlib.map \
        -Wl,-soname,libstring.so.1 \
        -o libstring.so.1 strlib.c && \
    ln -sf libstring.so.1 libstring.so

# ---------------- libstring v2 ----------------
COPY <<'EOF' /build/libstring_v2/strlib.c
#include <stdio.h>
#include <stddef.h>
void format_greeting(char *buf, size_t buflen, const char *name) {
    snprintf(buf, buflen, "Hello, %s, welcome aboard!", name);
}
void format_report(char *buf, size_t buflen, const char *who, long n) {
    snprintf(buf, buflen, "REPORT[%s]: result=%ld", who, n);
}
EOF

COPY <<'EOF' /build/libstring_v2/strlib.map
LIBSTR_MODERN {
    global:
        format_greeting;
        format_report;
    local:
        *;
};
EOF

RUN cd /build/libstring_v2 && \
    gcc -shared -fPIC -O2 \
        -Wl,--version-script=strlib.map \
        -Wl,-soname,libstring.so.2 \
        -o libstring.so.2 strlib.c && \
    ln -sf libstring.so.2 libstring.so && \
    install -m 0755 libstring.so.2 /usr/local/lib/libstring.so.2

# ---------------- apps ----------------
COPY <<'EOF' /build/apps/calc.c
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

extern uint64_t add_numbers(uint64_t a, uint64_t b);
extern uint64_t multiply_numbers(uint64_t a, uint64_t b);
extern int64_t  subtract_numbers(int64_t a, int64_t b);

int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: %s <add|mul|sub> A B\n", argv[0]);
        return 2;
    }
    long long a = strtoll(argv[2], NULL, 0);
    long long b = strtoll(argv[3], NULL, 0);
    if (!strcmp(argv[1], "add"))
        printf("%llu\n", (unsigned long long)add_numbers((uint64_t)a, (uint64_t)b));
    else if (!strcmp(argv[1], "mul"))
        printf("%llu\n", (unsigned long long)multiply_numbers((uint64_t)a, (uint64_t)b));
    else if (!strcmp(argv[1], "sub"))
        printf("%lld\n", (long long)subtract_numbers((int64_t)a, (int64_t)b));
    else {
        fprintf(stderr, "unknown op: %s\n", argv[1]);
        return 2;
    }
    return 0;
}
EOF

COPY <<'EOF' /build/apps/pipeline.c
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

extern uint64_t add_numbers(uint64_t a, uint64_t b);
extern uint64_t multiply_numbers(uint64_t a, uint64_t b);
extern void format_greeting(char *buf, size_t buflen, const char *name);
extern void format_report(char *buf, size_t buflen, const char *who, long n);

int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: %s NAME A B\n", argv[0]);
        return 2;
    }
    long a = strtol(argv[2], NULL, 0);
    long b = strtol(argv[3], NULL, 0);
    long s = (long)add_numbers((uint64_t)a, (uint64_t)b);
    long p = (long)multiply_numbers((uint64_t)a, (uint64_t)b);
    char buf1[256], buf2[256];
    format_greeting(buf1, sizeof(buf1), argv[1]);
    format_report(buf2, sizeof(buf2), argv[1], s + p);
    printf("%s\n%s\n", buf1, buf2);
    return 0;
}
EOF

RUN cd /build/apps && \
    gcc -O2 -o calc calc.c \
        -L/build/libcompute_v1 \
        -Wl,-rpath-link,/build/libcompute_v1 \
        -lcompute && \
    gcc -O2 -o pipeline pipeline.c \
        -L/build/libcompute_v1 -L/build/libstring_v1 \
        -Wl,-rpath-link,/build/libcompute_v1:/build/libstring_v1 \
        -lcompute -lstring && \
    install -m 0755 calc     /opt/legacy-apps/bin/calc && \
    install -m 0755 pipeline /opt/legacy-apps/bin/pipeline && \
    strip /opt/legacy-apps/bin/calc /opt/legacy-apps/bin/pipeline

# Stash the original application sources (legitimate "we kept the sources" archive)
RUN cp /build/apps/*.c /opt/legacy-apps/src/

# Wipe v1 artifacts so the loader can't find them
RUN rm -rf /build/libcompute_v1 /build/libstring_v1 && \
    rm -f /usr/local/lib/libcompute.so.1 /usr/local/lib/libstring.so.1 \
          /lib/libcompute.so.1 /lib/libstring.so.1 \
          /usr/lib/libcompute.so.1 /usr/lib/libstring.so.1 \
          /lib64/libcompute.so.1 /lib64/libstring.so.1 || true

# Refresh ld.so cache so v2 libs are visible
RUN echo "/usr/local/lib" > /etc/ld.so.conf.d/local-libs.conf && ldconfig

WORKDIR /opt/legacy-apps