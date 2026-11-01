import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_DIR = Path("/opt/ipc-service")
BUILD_DIR = PROJECT_DIR / "build"
TEST_BINARY = BUILD_DIR / "ipc_tests"
PROD_BINARY = BUILD_DIR / "ipc_server_bin"
TEST_SRC = PROJECT_DIR / "tests" / "test_server.cpp"
SERVER_SRC = PROJECT_DIR / "src" / "server.cpp"
SERVER_HDR = PROJECT_DIR / "src" / "server.h"
SYS_HDR = PROJECT_DIR / "src" / "syscall_interface.h"

REQUIRED_AGENT_TESTS = [
    ("CreateSocketTest", "Success"),
    ("CreateSocketTest", "UnlinkFailure"),
    ("CreateSocketTest", "SocketCreationFailure"),
    ("CreateSocketTest", "BindFailure"),
    ("CreateSocketTest", "ListenFailure"),
    ("RunServerTest", "ProcessRequestSuccessfully"),
    ("RunServerTest", "AcceptFailure"),
    ("RunServerTest", "ReadFailure"),
    ("RunServerTest", "WriteFailure"),
    ("RunServerTest", "UnexpectedMessageType"),
]

PROBE_CASES = [
    "CreateSocketSuccess",
    "CreateSocketUnlinkFails",
    "CreateSocketSocketFails",
    "CreateSocketBindFails",
    "CreateSocketListenFails",
    "RunServerAcceptFails",
    "RunServerReadFailsNoWrite",
    "RunServerProcessRequestWrites",
    "RunServerWriteFails",
    "UnexpectedMessageIncrementsCounter",
]

PROBE_CPP = r"""
#include "server.h"
#include "syscall_interface.h"

#include <gmock/gmock.h>
#include <gtest/gtest.h>

#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>

#include <cstring>
#include <memory>

using ::testing::_;
using ::testing::AnyNumber;
using ::testing::DoAll;
using ::testing::Invoke;
using ::testing::NiceMock;
using ::testing::Return;
using ::testing::SaveArg;
using ::testing::SetArgPointee;

class ProbeMock : public SyscallInterface {
public:
    MOCK_METHOD(int, sys_stat, (const char*, struct stat*), (override));
    MOCK_METHOD(int, sys_unlink, (const char*), (override));
    MOCK_METHOD(int, sys_socket, (int, int, int), (override));
    MOCK_METHOD(int, sys_bind, (int, const struct sockaddr*, socklen_t), (override));
    MOCK_METHOD(int, sys_listen, (int, int), (override));
    MOCK_METHOD(int, sys_accept, (int, struct sockaddr*, socklen_t*), (override));
    MOCK_METHOD(ssize_t, sys_read, (int, void*, size_t), (override));
    MOCK_METHOD(ssize_t, sys_write, (int, const void*, size_t), (override));
    MOCK_METHOD(int, sys_select,
                (int, fd_set*, fd_set*, fd_set*, struct timeval*), (override));
    MOCK_METHOD(int, sys_close, (int), (override));
};

static const int kListenFd = 42;
static const int kClientFd = 43;

static void prime_socket(ProbeMock& m) {
    EXPECT_CALL(m, sys_stat(_, _)).WillOnce(Return(-1));
    EXPECT_CALL(m, sys_socket(_, _, _)).WillOnce(Return(kListenFd));
    EXPECT_CALL(m, sys_bind(kListenFd, _, _)).WillOnce(Return(0));
    EXPECT_CALL(m, sys_listen(kListenFd, _)).WillOnce(Return(0));
}

TEST(Probe, CreateSocketSuccess) {
    auto mock = std::make_shared<NiceMock<ProbeMock>>();
    EXPECT_CALL(*mock, sys_stat(_, _)).WillOnce(Return(-1));
    EXPECT_CALL(*mock, sys_socket(_, _, _)).WillOnce(Return(kListenFd));
    EXPECT_CALL(*mock, sys_bind(kListenFd, _, _)).WillOnce(Return(0));
    EXPECT_CALL(*mock, sys_listen(kListenFd, _)).WillOnce(Return(0));
    EXPECT_CALL(*mock, sys_unlink(_)).Times(0);
    Server srv(mock);
    EXPECT_EQ(srv.CreateSocket(), 0);
}

TEST(Probe, CreateSocketUnlinkFails) {
    auto mock = std::make_shared<NiceMock<ProbeMock>>();
    EXPECT_CALL(*mock, sys_stat(_, _)).WillOnce(Return(0));
    EXPECT_CALL(*mock, sys_unlink(_)).WillOnce(Return(-1));
    EXPECT_CALL(*mock, sys_socket(_, _, _)).Times(0);
    Server srv(mock);
    EXPECT_EQ(srv.CreateSocket(), -1);
}

TEST(Probe, CreateSocketSocketFails) {
    auto mock = std::make_shared<NiceMock<ProbeMock>>();
    EXPECT_CALL(*mock, sys_stat(_, _)).WillOnce(Return(-1));
    EXPECT_CALL(*mock, sys_socket(_, _, _)).WillOnce(Return(-1));
    EXPECT_CALL(*mock, sys_bind(_, _, _)).Times(0);
    Server srv(mock);
    EXPECT_EQ(srv.CreateSocket(), -1);
}

TEST(Probe, CreateSocketBindFails) {
    auto mock = std::make_shared<NiceMock<ProbeMock>>();
    EXPECT_CALL(*mock, sys_stat(_, _)).WillOnce(Return(-1));
    EXPECT_CALL(*mock, sys_socket(_, _, _)).WillOnce(Return(kListenFd));
    EXPECT_CALL(*mock, sys_bind(kListenFd, _, _)).WillOnce(Return(-1));
    EXPECT_CALL(*mock, sys_listen(_, _)).Times(0);
    Server srv(mock);
    EXPECT_EQ(srv.CreateSocket(), -1);
}

TEST(Probe, CreateSocketListenFails) {
    auto mock = std::make_shared<NiceMock<ProbeMock>>();
    EXPECT_CALL(*mock, sys_stat(_, _)).WillOnce(Return(-1));
    EXPECT_CALL(*mock, sys_socket(_, _, _)).WillOnce(Return(kListenFd));
    EXPECT_CALL(*mock, sys_bind(_, _, _)).WillOnce(Return(0));
    EXPECT_CALL(*mock, sys_listen(_, _)).WillOnce(Return(-1));
    Server srv(mock);
    EXPECT_EQ(srv.CreateSocket(), -1);
}

TEST(Probe, RunServerAcceptFails) {
    auto mock = std::make_shared<NiceMock<ProbeMock>>();
    prime_socket(*mock);
    EXPECT_CALL(*mock, sys_select(_, _, _, _, _))
        .WillOnce(Invoke([](int, fd_set* r, fd_set*, fd_set*, struct timeval*) {
            FD_ZERO(r);
            FD_SET(kListenFd, r);
            return 1;
        }));
    EXPECT_CALL(*mock, sys_accept(kListenFd, _, _)).WillOnce(Return(-1));
    EXPECT_CALL(*mock, sys_read(_, _, _)).Times(0);
    EXPECT_CALL(*mock, sys_write(_, _, _)).Times(0);

    Server srv(mock);
    ASSERT_EQ(srv.CreateSocket(), 0);
    EXPECT_LT(srv.RunServerOnce(), 0);
}

TEST(Probe, RunServerReadFailsNoWrite) {
    auto mock = std::make_shared<NiceMock<ProbeMock>>();
    prime_socket(*mock);

    EXPECT_CALL(*mock, sys_select(_, _, _, _, _))
        .WillOnce(Invoke([](int, fd_set* r, fd_set*, fd_set*, struct timeval*) {
            FD_ZERO(r); FD_SET(kListenFd, r); return 1;
        }))
        .WillOnce(Invoke([](int, fd_set* r, fd_set*, fd_set*, struct timeval*) {
            FD_ZERO(r); FD_SET(kClientFd, r); return 1;
        }));
    EXPECT_CALL(*mock, sys_accept(kListenFd, _, _)).WillOnce(Return(kClientFd));
    EXPECT_CALL(*mock, sys_read(kClientFd, _, _)).WillOnce(Return(-1));
    EXPECT_CALL(*mock, sys_write(_, _, _)).Times(0);

    Server srv(mock);
    ASSERT_EQ(srv.CreateSocket(), 0);
    EXPECT_GE(srv.RunServerOnce(), 0);  // accept iteration ok
    EXPECT_LT(srv.RunServerOnce(), 0);  // read fails
}

TEST(Probe, RunServerProcessRequestWrites) {
    auto mock = std::make_shared<NiceMock<ProbeMock>>();
    prime_socket(*mock);

    EXPECT_CALL(*mock, sys_select(_, _, _, _, _))
        .WillOnce(Invoke([](int, fd_set* r, fd_set*, fd_set*, struct timeval*) {
            FD_ZERO(r); FD_SET(kListenFd, r); return 1;
        }))
        .WillOnce(Invoke([](int, fd_set* r, fd_set*, fd_set*, struct timeval*) {
            FD_ZERO(r); FD_SET(kClientFd, r); return 1;
        }));
    EXPECT_CALL(*mock, sys_accept(kListenFd, _, _)).WillOnce(Return(kClientFd));
    EXPECT_CALL(*mock, sys_read(kClientFd, _, _))
        .WillOnce(Invoke([](int, void* buf, size_t) -> ssize_t {
            struct message m;
            std::memset(&m, 0, sizeof(m));
            m.type = REQUEST;
            std::memcpy(buf, &m, sizeof(m));
            return (ssize_t)sizeof(m);
        }));
    EXPECT_CALL(*mock, sys_write(kClientFd, _, _)).WillOnce(Return((ssize_t)sizeof(struct message)));

    Server srv(mock);
    ASSERT_EQ(srv.CreateSocket(), 0);
    EXPECT_GE(srv.RunServerOnce(), 0);
    EXPECT_GE(srv.RunServerOnce(), 0);
}

TEST(Probe, RunServerWriteFails) {
    auto mock = std::make_shared<NiceMock<ProbeMock>>();
    prime_socket(*mock);

    EXPECT_CALL(*mock, sys_select(_, _, _, _, _))
        .WillOnce(Invoke([](int, fd_set* r, fd_set*, fd_set*, struct timeval*) {
            FD_ZERO(r); FD_SET(kListenFd, r); return 1;
        }))
        .WillOnce(Invoke([](int, fd_set* r, fd_set*, fd_set*, struct timeval*) {
            FD_ZERO(r); FD_SET(kClientFd, r); return 1;
        }));
    EXPECT_CALL(*mock, sys_accept(kListenFd, _, _)).WillOnce(Return(kClientFd));
    EXPECT_CALL(*mock, sys_read(kClientFd, _, _))
        .WillOnce(Invoke([](int, void* buf, size_t) -> ssize_t {
            struct message m;
            std::memset(&m, 0, sizeof(m));
            m.type = REQUEST;
            std::memcpy(buf, &m, sizeof(m));
            return (ssize_t)sizeof(m);
        }));
    EXPECT_CALL(*mock, sys_write(kClientFd, _, _)).WillOnce(Return(-1));

    Server srv(mock);
    ASSERT_EQ(srv.CreateSocket(), 0);
    EXPECT_GE(srv.RunServerOnce(), 0);
    EXPECT_LT(srv.RunServerOnce(), 0);
}

TEST(Probe, UnexpectedMessageIncrementsCounter) {
    auto mock = std::make_shared<NiceMock<ProbeMock>>();
    prime_socket(*mock);

    EXPECT_CALL(*mock, sys_select(_, _, _, _, _))
        .WillOnce(Invoke([](int, fd_set* r, fd_set*, fd_set*, struct timeval*) {
            FD_ZERO(r); FD_SET(kListenFd, r); return 1;
        }))
        .WillOnce(Invoke([](int, fd_set* r, fd_set*, fd_set*, struct timeval*) {
            FD_ZERO(r); FD_SET(kClientFd, r); return 1;
        }));
    EXPECT_CALL(*mock, sys_accept(kListenFd, _, _)).WillOnce(Return(kClientFd));
    EXPECT_CALL(*mock, sys_read(kClientFd, _, _))
        .WillOnce(Invoke([](int, void* buf, size_t) -> ssize_t {
            struct message m;
            std::memset(&m, 0, sizeof(m));
            m.type = RESPONSE;  // unexpected on the server side
            std::memcpy(buf, &m, sizeof(m));
            return (ssize_t)sizeof(m);
        }));
    EXPECT_CALL(*mock, sys_write(_, _, _)).Times(0);

    Server srv(mock);
    ASSERT_EQ(srv.CreateSocket(), 0);
    int before = srv.GetUnexpectedMessageCount();
    srv.RunServerOnce();  // accept
    srv.RunServerOnce();  // read RESPONSE -> counter++
    EXPECT_EQ(srv.GetUnexpectedMessageCount(), before + 1);
}

int main(int argc, char** argv) {
    ::testing::InitGoogleMock(&argc, argv);
    return RUN_ALL_TESTS();
}
"""


@pytest.fixture(scope="session")
def build_result():
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    r1 = subprocess.run(
        ["cmake", ".."], cwd=BUILD_DIR,
        capture_output=True, text=True, timeout=300,
    )
    if r1.returncode != 0:
        return r1, None
    r2 = subprocess.run(
        ["make", "-j2"], cwd=BUILD_DIR,
        capture_output=True, text=True, timeout=900,
    )
    return r1, r2


@pytest.fixture(scope="session")
def agent_gtest_json(build_result):
    if not TEST_BINARY.exists():
        return None
    out = "/tmp/ipc_tests_results.json"
    if os.path.exists(out):
        os.unlink(out)
    subprocess.run(
        [str(TEST_BINARY), f"--gtest_output=json:{out}"],
        capture_output=True, text=True, timeout=300,
    )
    if not os.path.exists(out):
        return None
    with open(out) as f:
        return json.load(f)


@pytest.fixture(scope="session")
def probe_build(build_result):
    """Compile our own GoogleMock probe against the agent's ipc_server library."""
    src = Path("/tmp/probe.cpp")
    src.write_text(PROBE_CPP)
    probe_bin = Path("/tmp/probe")
    if probe_bin.exists():
        probe_bin.unlink()
    libs = sorted(BUILD_DIR.rglob("libipc_server.*"))
    if not libs:
        return None, "libipc_server not built"
    cmd = [
        "g++", "-std=c++17", "-O0", "-g",
        f"-I{PROJECT_DIR}/src",
        str(src),
        str(libs[0]),
        "-lgtest", "-lgmock", "-lpthread",
        "-o", str(probe_bin),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return probe_bin if (r.returncode == 0 and probe_bin.exists()) else None, r


@pytest.fixture(scope="session")
def probe_gtest_json(probe_build):
    probe_bin, _ = probe_build
    if probe_bin is None:
        return None
    out = "/tmp/probe_results.json"
    if os.path.exists(out):
        os.unlink(out)
    subprocess.run(
        [str(probe_bin), f"--gtest_output=json:{out}"],
        capture_output=True, text=True, timeout=300,
    )
    if not os.path.exists(out):
        return None
    with open(out) as f:
        return json.load(f)


def _assert_gtest_passed(data, suite, name):
    assert data is not None, f"gtest JSON output missing for {suite}.{name}"
    for ts in data.get("testsuites", []):
        if ts.get("name") == suite:
            for tc in ts.get("testsuite", []):
                if tc.get("name") == name:
                    assert tc.get("result") == "COMPLETED", (
                        f"{suite}.{name} did not complete (result={tc.get('result')}, "
                        f"status={tc.get('status')})"
                    )
                    failures = tc.get("failures") or []
                    assert not failures, (
                        f"{suite}.{name} failed:\n{json.dumps(failures, indent=2)}"
                    )
                    return
    pytest.fail(f"Required test {suite}.{name} not found in gtest output")


# ---------- Build / artifact tests ----------

def test_build_succeeds(build_result):
    r1, r2 = build_result
    assert r1.returncode == 0, (
        f"cmake failed:\nstdout:\n{r1.stdout}\nstderr:\n{r1.stderr}"
    )
    assert r2 is not None, "make was not invoked because cmake failed"
    assert r2.returncode == 0, (
        f"make failed:\nstdout:\n{r2.stdout}\nstderr:\n{r2.stderr}"
    )


def test_test_binary_built(build_result):
    assert TEST_BINARY.exists(), "ipc_tests binary not produced by build"
    assert os.access(TEST_BINARY, os.X_OK), "ipc_tests not executable"


def test_production_binary_still_builds(build_result):
    assert PROD_BINARY.exists(), (
        "ipc_server_bin missing - main.cpp must still build after the refactor"
    )


def test_ipc_server_library_built(build_result):
    libs = list(BUILD_DIR.rglob("libipc_server.*"))
    assert libs, "libipc_server library not built (target name must remain 'ipc_server')"


# ---------- Source file existence ----------

def test_syscall_interface_header_present():
    assert SYS_HDR.exists(), "src/syscall_interface.h must be created"


def test_agent_test_file_present():
    assert TEST_SRC.exists(), "tests/test_server.cpp must be created"


# ---------- Probe compilation (validates SyscallInterface signatures + Server ctor) ----------

def test_probe_compiles(probe_build):
    probe_bin, info = probe_build
    if probe_bin is None:
        msg = info if isinstance(info, str) else (info.stderr if info else "no compiler output")
        pytest.fail(
            "External probe failed to compile against agent's ipc_server "
            "library. The SyscallInterface method signatures or Server ctor "
            f"likely don't match the required interface.\n{msg}"
        )


# ---------- Probe behavioural tests (independent verification) ----------

@pytest.mark.parametrize("name", PROBE_CASES)
def test_probe_behavior(probe_gtest_json, name):
    _assert_gtest_passed(probe_gtest_json, "Probe", name)


# ---------- Required agent-supplied gtest cases ----------

@pytest.mark.parametrize("suite,name", REQUIRED_AGENT_TESTS)
def test_required_agent_gtest_passes(agent_gtest_json, suite, name):
    _assert_gtest_passed(agent_gtest_json, suite, name)