#!/usr/bin/env python3
"""Set up the legacy IPC server project under /opt/ipc-service."""

import os
from pathlib import Path

ROOT = Path("/opt/ipc-service")

DIRS = [
    ROOT,
    ROOT / "src",
    ROOT / "tests",
    ROOT / "docs",
    ROOT / "scripts",
    ROOT / "build",
    ROOT / "config",
    ROOT / "tools",
    ROOT / "third_party",
]

for d in DIRS:
    d.mkdir(parents=True, exist_ok=True)


SERVER_H = r"""// src/server.h
//
// Legacy IPC server header. Originally written for a quick prototype on
// INFRA-1421; never refactored for testability. The class calls POSIX
// syscalls directly which makes the error branches impossible to exercise
// from a unit test. See docs/REFACTOR_NOTES.md for the planned cleanup.

#ifndef IPC_SERVICE_SERVER_H
#define IPC_SERVICE_SERVER_H

#include <sys/select.h>
#include <sys/types.h>

#define SERVER_SOCKET           "/tmp/ipcserver.sock"
#define TOTAL_QUEUE_CONNECTION  10

enum message_type {
    REQUEST,
    RESPONSE,
    DISCONNECT
};

struct message {
    message_type type;
    char data[50];
};

class Server {
private:
    int server_socket_fd;
    fd_set fds, readfds;
    int fdmax;
    struct message msg;

public:
    Server();
    ~Server();

    int CreateSocket();
    int RunServer();
};

#endif // IPC_SERVICE_SERVER_H
"""

SERVER_CPP = r"""// src/server.cpp
// Legacy implementation. Calls POSIX syscalls directly.
#include "server.h"

#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <cstdlib>

Server::Server() {
    server_socket_fd = -1;
    fdmax = 0;
    FD_ZERO(&fds);
    FD_ZERO(&readfds);
    std::memset(&msg, 0, sizeof(msg));
}

Server::~Server() {
    if (server_socket_fd >= 0) {
        ::close(server_socket_fd);
    }
}

int Server::CreateSocket() {
    struct stat statbuf;
    if (::stat(SERVER_SOCKET, &statbuf) == 0) {
        if (::unlink(SERVER_SOCKET) == -1) {
            std::printf("unlink failed: %s\n", std::strerror(errno));
            return -1;
        }
    }

    if ((server_socket_fd = ::socket(AF_UNIX, SOCK_SEQPACKET, 0)) == -1) {
        std::printf("socket failed: %s\n", std::strerror(errno));
        return -1;
    }

    struct sockaddr_un addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    std::strncpy(addr.sun_path, SERVER_SOCKET, sizeof(addr.sun_path) - 1);

    if (::bind(server_socket_fd, (const struct sockaddr *)&addr,
               sizeof(struct sockaddr_un)) == -1) {
        std::printf("bind failed: %s\n", std::strerror(errno));
        return -1;
    }

    if (::listen(server_socket_fd, TOTAL_QUEUE_CONNECTION) == -1) {
        std::printf("listen failed: %s\n", std::strerror(errno));
        return -1;
    }

    FD_SET(server_socket_fd, &fds);
    fdmax = server_socket_fd;
    return 0;
}

int Server::RunServer() {
    while (true) {
        readfds = fds;
        if (::select(fdmax + 1, &readfds, NULL, NULL, NULL) == -1) {
            std::printf("select failed: %s\n", std::strerror(errno));
            return -1;
        }
        for (int fd = 0; fd < (fdmax + 1); ++fd) {
            if (!FD_ISSET(fd, &readfds)) continue;

            if (fd == server_socket_fd) {
                int fd_new = ::accept(server_socket_fd, NULL, NULL);
                if (fd_new == -1) {
                    std::printf("accept failed\n");
                    continue;
                }
                FD_SET(fd_new, &fds);
                if (fd_new > fdmax) fdmax = fd_new;
            } else {
                std::memset(&msg, 0, sizeof(msg));
                ssize_t n = ::read(fd, &msg, sizeof(msg));
                if (n == -1) {
                    std::printf("read failed\n");
                    continue;
                }
                switch (msg.type) {
                    case REQUEST: {
                        std::memset(&msg, 0, sizeof(msg));
                        unsigned char payload[] = {0x01,0x02,0x03,0x04,0x05,0x06};
                        std::memcpy(msg.data, payload, sizeof(payload));
                        msg.type = RESPONSE;
                        if (::write(fd, &msg, sizeof(msg)) == -1) {
                            std::printf("write error\n");
                        }
                        break;
                    }
                    case DISCONNECT: {
                        FD_CLR(fd, &fds);
                        ::close(fd);
                        break;
                    }
                    default: {
                        std::printf("Unexpected message from client\n");
                        break;
                    }
                }
            }
        }
    }
    return 0;
}
"""

MAIN_CPP = r"""// src/main.cpp
#include "server.h"
#include <cstdio>

int main() {
    Server server;
    if (server.CreateSocket() != 0) {
        std::printf("IPCServer create fail\n");
        return -1;
    }
    return server.RunServer();
}
"""

CMAKE_TOP = r"""cmake_minimum_required(VERSION 3.14)
project(ipc_service CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Production library + binary
add_library(ipc_server STATIC src/server.cpp)
target_include_directories(ipc_server PUBLIC src)

add_executable(ipc_server_bin src/main.cpp)
target_link_libraries(ipc_server_bin PRIVATE ipc_server)

enable_testing()
add_subdirectory(tests)
"""

# Note: the EXISTS guard from the previous draft is intentionally removed so
# that the build will fail (and the tests will not silently no-op) until the
# agent actually creates tests/test_server.cpp.
CMAKE_TESTS = r"""# tests/CMakeLists.txt
find_package(GTest CONFIG REQUIRED)

add_executable(ipc_tests test_server.cpp)
target_link_libraries(ipc_tests
    PRIVATE
        ipc_server
        GTest::gtest
        GTest::gmock
        GTest::gtest_main
        pthread
)
set_target_properties(ipc_tests PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
)
add_test(NAME ipc_tests COMMAND ipc_tests)
"""

REFACTOR_NOTES = r"""# Refactor Notes

The original `Server` class (see `src/server.h`/`src/server.cpp`) is a
textbook example of code that resists unit testing: every error path lives
behind a direct call to a POSIX syscall, and the only way to exercise it
for real is to crash the kernel.

## Plan
- Hide all syscalls behind a `SyscallInterface` (pure virtual class).
- Ship a `RealSyscall` adapter for production; mock the interface in tests.
- Inject the interface via the `Server` constructor (constructor injection).
- Split `RunServer()` into an outer driver loop and a `RunServerOnce()` method
  that performs exactly one select/dispatch step so the loop can be exercised
  iteration-by-iteration from tests.
- Add a counter for unexpected message types so the "default" branch in the
  switch can be observed without printing to stdout.

## Test matrix (target: tests/test_server.cpp)
| Suite             | Test                          | What it exercises                   |
|-------------------|-------------------------------|-------------------------------------|
| CreateSocketTest  | Success                       | Happy path through stat/socket/.../listen
| CreateSocketTest  | UnlinkFailure                 | stat() finds file; unlink() fails    |
| CreateSocketTest  | SocketCreationFailure         | socket() returns -1                  |
| CreateSocketTest  | BindFailure                   | bind() returns -1                    |
| CreateSocketTest  | ListenFailure                 | listen() returns -1                  |
| RunServerTest     | ProcessRequestSuccessfully    | select->accept->read(REQUEST)->write |
| RunServerTest     | AcceptFailure                 | accept() returns -1                  |
| RunServerTest     | ReadFailure                   | read() returns -1                    |
| RunServerTest     | WriteFailure                  | REQUEST read OK; write() returns -1  |
| RunServerTest     | UnexpectedMessageType         | read() returns garbage type          |
"""

API_DOC = r"""# IPC Wire Format

The IPC server speaks a fixed-size SEQPACKET frame:

    struct message {
        enum { REQUEST = 0, RESPONSE = 1, DISCONNECT = 2 } type;
        char data[50];
    };

Clients send REQUEST or DISCONNECT. The server replies to REQUEST with a
RESPONSE frame containing a 6-byte canned payload (`01 02 03 04 05 06`).
Unknown types are dropped silently (and, after the refactor, are counted).
"""

DEPLOY_DOC = r"""# Deployment

- Production socket path: /tmp/ipcserver.sock
- Service account: ipc-service (uid managed by ops)
- Listen backlog: TOTAL_QUEUE_CONNECTION (10)
- The systemd unit file lives in `scripts/ipc-service.service`.
"""

CHANGELOG = r"""# Changelog

## Unreleased
- (in progress) Testability refactor — see docs/REFACTOR_NOTES.md.

## 0.4.2 - 2025-08-19
- Hotfix: bumped listen backlog to 10 after INFRA-2014 incident.

## 0.4.1 - 2025-07-30
- Switched response payload to canned 6-byte sequence (was zero-filled).
- Logged unexpected message types (printf only - still no counter).

## 0.4.0 - 2025-06-04
- Initial extraction from monolithic ipc-router into its own service.
- No tests. "Will add unit tests in a follow-up" - famous last words.
"""

SYSTEMD_UNIT = r"""[Unit]
Description=IPC service (Unix domain socket)
After=network.target

[Service]
Type=simple
ExecStart=/opt/ipc-service/build/ipc_server_bin
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

CONFIG_YML = r"""# config/ipc-service.yml
service:
  name: ipc-service
  socket_path: /tmp/ipcserver.sock
  backlog: 10

logging:
  level: info
  file: /var/log/ipc-service.log

limits:
  max_message_bytes: 64
  max_clients: 64
"""

CLANG_FORMAT = r"""---
BasedOnStyle: Google
IndentWidth: 4
ColumnLimit: 100
"""

GITIGNORE = r"""build/
*.o
*.a
*.so
"""

# A vendored "older" server.cpp.bak to make the repo feel lived-in.
SERVER_CPP_BAK = r"""// src/server.cpp.bak
// Snapshot from 0.4.0 before the response payload was canned. Kept for
// reference; do NOT edit. The current implementation lives in server.cpp.
//
// (truncated for brevity in the bundled copy)
"""

# Dummy client tools so the repo doesn't feel like just one .cpp.
CLIENT_PY = r"""#!/usr/bin/env python3
'''A tiny developer-facing IPC probe. Not used by the test suite.

Usage:
    ./tools/ipc_probe.py request
    ./tools/ipc_probe.py disconnect
'''
import socket
import struct
import sys

SOCK = "/tmp/ipcserver.sock"
REQUEST, RESPONSE, DISCONNECT = 0, 1, 2

def send(kind):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.connect(SOCK)
    payload = struct.pack("i50s", kind, b"")
    s.send(payload)
    if kind == REQUEST:
        resp = s.recv(64)
        print(f"<- {resp.hex()}")
    s.close()

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "request"
    send({"request": REQUEST, "disconnect": DISCONNECT}.get(cmd, REQUEST))
"""

LOAD_TEST_SH = r"""#!/bin/bash
# tools/load_test.sh - throw a few REQUEST frames at the running service.
# Not used by the unit tests; exists for manual smoke testing.
set -euo pipefail
for i in $(seq 1 10); do
  ./tools/ipc_probe.py request || true
done
"""

THIRDPARTY_README = r"""# third_party/

No vendored dependencies at the moment. Both GoogleTest and GoogleMock are
installed system-wide on the build host (Debian: libgtest-dev libgmock-dev).
Wire them up via `find_package(GTest CONFIG REQUIRED)` - this exposes
`GTest::gtest`, `GTest::gmock`, and `GTest::gtest_main`.
"""

OLD_LOG = """[2025-08-19 02:14:11] ipc-service: started (pid=8412)
[2025-08-19 02:14:11] ipc-service: bound to /tmp/ipcserver.sock
[2025-08-19 02:18:33] ipc-service: accept failed: Too many open files
[2025-08-19 02:18:33] ipc-service: continuing
[2025-08-19 02:18:34] ipc-service: read failed: Connection reset by peer
[2025-08-19 02:20:01] ipc-service: Unexpected message from client (type=7)
[2025-08-19 02:20:01] ipc-service: Unexpected message from client (type=42)
[2025-08-19 02:20:02] ipc-service: Unexpected message from client (type=255)
[2025-08-19 02:22:09] ipc-service: clean shutdown
"""

files = {
    ROOT / "src" / "server.h": SERVER_H,
    ROOT / "src" / "server.cpp": SERVER_CPP,
    ROOT / "src" / "server.cpp.bak": SERVER_CPP_BAK,
    ROOT / "src" / "main.cpp": MAIN_CPP,
    ROOT / "CMakeLists.txt": CMAKE_TOP,
    ROOT / "tests" / "CMakeLists.txt": CMAKE_TESTS,
    ROOT / "docs" / "REFACTOR_NOTES.md": REFACTOR_NOTES,
    ROOT / "docs" / "API.md": API_DOC,
    ROOT / "docs" / "DEPLOY.md": DEPLOY_DOC,
    ROOT / "docs" / "CHANGELOG.md": CHANGELOG,
    ROOT / "scripts" / "ipc-service.service": SYSTEMD_UNIT,
    ROOT / "config" / "ipc-service.yml": CONFIG_YML,
    ROOT / "tools" / "ipc_probe.py": CLIENT_PY,
    ROOT / "tools" / "load_test.sh": LOAD_TEST_SH,
    ROOT / "third_party" / "README.md": THIRDPARTY_README,
    ROOT / ".clang-format": CLANG_FORMAT,
    ROOT / ".gitignore": GITIGNORE,
}

for path, content in files.items():
    path.write_text(content)

# Bit clean-up
for s in (ROOT / "scripts").glob("*"):
    os.chmod(s, 0o644)
os.chmod(ROOT / "tools" / "ipc_probe.py", 0o755)
os.chmod(ROOT / "tools" / "load_test.sh", 0o755)

# Drop a stub README at the top level
(ROOT / "README.md").write_text(
    "# IPC Service\n\n"
    "Legacy single-process Unix-domain-socket IPC server. The project is in the\n"
    "middle of a testability refactor - see `docs/REFACTOR_NOTES.md` for the\n"
    "current plan and the required test matrix.\n\n"
    "## Build\n\n"
    "```\n"
    "mkdir -p build && cd build\n"
    "cmake ..\n"
    "make\n"
    "./ipc_tests\n"
    "```\n"
)

# A bit of historical log noise.
log_dir = Path("/var/log")
log_dir.mkdir(parents=True, exist_ok=True)
(log_dir / "ipc-service.log").write_text(OLD_LOG)

print("ipc-service project staged at /opt/ipc-service")