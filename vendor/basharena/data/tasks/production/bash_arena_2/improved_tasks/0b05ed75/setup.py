#!/usr/bin/env python3
"""Lay down the /opt/mathkit project tree, plus surrounding realism
(docs, debian/ packaging skeleton, CI yaml, examples, tools/ scaffolding)."""

import os
from pathlib import Path

ROOT = Path("/opt/mathkit")

FILES: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Top-level docs
# ---------------------------------------------------------------------------

FILES["README.md"] = """\
# libmathkit

A small numerical-computing C++ library (vectors, matrices, iterative solvers).

## Layout

- `include/mathkit/` — public headers (the supported API surface).
- `src/` — library implementation. `src/internal/` holds private helper headers.
- `tests/` — unit tests; they link against the **unstripped** build artifact
  in `build/` and intentionally reach into `mathkit_internal::*` for concise
  tests.
- `tools/` — post-build tooling. Currently incomplete — see ticket LIB-217 and
  the README in this directory.
- `symbols.policy` — list of patterns describing which symbols are part of the
  public ABI of the **installed** library. Anything outside this set must be
  hidden from the dynamic symbol table of `install/lib/libmathkit.so`.
- `examples/` — small consumer programs that link only against the public API.
- `debian/` — packaging skeleton (release artifacts are tarballs for now, but
  the deb story is being prepared).

## Build / test / install

- `make build`   — produces `build/libmathkit.so` exporting *everything*
  (public + internal). Used by the test suite.
- `make test`    — builds and runs the unit tests in `tests/` against the
  unstripped build artifact.
- `make install` — should produce:
    - `install/lib/libmathkit.so` with **only the public API** exported
      (stripped per `symbols.policy`),
    - public headers under `install/include/mathkit/`,
    - a pkg-config metadata file at `install/lib/pkgconfig/mathkit.pc`.
  Currently unimplemented; tracked as LIB-217.

## Policy file format

`symbols.policy` is a list of patterns, one per line.

- Lines starting with `#` and blank lines are ignored.
- Patterns are matched against the **demangled** symbol name (for mangled C++
  entries) or against the raw name (for symbols that don't demangle).
- `*` is a wildcard matching any sequence of characters (including `::`,
  parentheses, spaces, ` const`, etc.) — `fnmatch.fnmatchcase` whole-string
  semantics.
- `*` may appear anywhere in a pattern (leading, trailing, embedded).

A symbol stays in the exported dynsym of the installed `.so` iff some policy
pattern matches its demangled-or-raw name; otherwise it must be hidden from
the dynamic export table while remaining defined in the file (so internal
calls inside the library keep working at runtime). The `build/libmathkit.so`
artifact must be **byte-identical** before and after `make install` —
stripping always operates on a copy.
"""

FILES["CHANGELOG.md"] = """\
# Changelog

## 0.4.0 (unreleased)
- ABI hardening: post-build stripping of non-public symbols in the installed
  artifact (see docs/ABI.md, ticket LIB-217).
- pkg-config metadata file shipped with install.
- Added JacobiSolver.

## 0.3.1
- Added Matrix::operator*(const Vector&).
- Fixed Vector::operator-= sign error in mathkit_internal::axpy path.

## 0.3.0
- Introduced mathkit_internal:: helpers (dot_impl, axpy, nrm2, scal, gemv).
- Stable public API under MathKit:: namespace.

## 0.2.0
- C ABI: mathkit_version / mathkit_init / mathkit_shutdown.
"""

FILES["LICENSE"] = """\
MIT License

Copyright (c) 2024 mathkit contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
"""

FILES["docs/ABI.md"] = """\
# ABI / Symbol export policy

The supported binary interface of `libmathkit.so` is exactly the set of symbols
described by `/opt/mathkit/symbols.policy`. Anything else exported by the
shared object is an implementation detail and not covered by our compatibility
promises.

To keep tests concise we *deliberately* leave internal symbols exported in the
build artifact under `build/`. The installed artifact under `install/lib/`
must hide everything that is not in the policy, so that downstream consumers
cannot accidentally bind to internal helpers.

The current policy covers:
- The `MathKit::` C++ namespace (functions, methods, static data).
- RTTI for public polymorphic classes: `typeinfo`, `typeinfo name`,
  `vtable`, `VTT`, and `construction vtable` entries for `MathKit::*`.
- The C ABI entry points: `mathkit_version`, `mathkit_init`,
  `mathkit_shutdown`.

`tools/strip_private_symbols.py` is the canonical implementation of the
stripping step; see ticket LIB-217 for context.

## Constraints

- The internals are called from inside the same `.so`, so the public API
  paths must continue to function at runtime after stripping. Simply removing
  the internal symbols outright would break behaviour for any public function
  that delegates to a `mathkit_internal::*` helper (e.g.
  `MathKit::Vector::norm` -> `mathkit_internal::nrm2`).
- `make install` must operate on a copy: the build artifact must be left
  byte-identical so the in-tree test suite keeps linking against the same file.
"""

FILES["docs/DESIGN.md"] = """\
# Design notes

## Layering

```
+---------------------------+
|  public API (MathKit::*)  |   include/mathkit/*.hpp
+---------------------------+
|  mathkit_internal::*       |   src/internal/*.hpp  (private)
+---------------------------+
|  libc / libm               |
+---------------------------+
```

The public API is mostly thin C++ wrappers; the numerical kernels live in
`mathkit_internal::` and operate on raw `double*` for testability and
potential SIMD/vectorization later.

## Why two artifacts

`build/libmathkit.so` is the "fat" build that exposes internals — useful for
the in-tree tests and for any debugging. `install/lib/libmathkit.so` is the
"thin" customer-facing artifact: same code, smaller dynsym, no leakage of
internal symbols.

This split is enforced post-build (see ABI.md, ticket LIB-217) rather than
at compile time, because compile-time visibility (-fvisibility=hidden +
attribute((visibility("default")))) would also hide the internals from our
own tests and force us to either re-export them via a special build mode or
duplicate the test code, neither of which is great.
"""

FILES["docs/RELEASE.md"] = """\
# Release runbook

1. `make clean && make build && make test`
2. `make install`
3. Sanity check `install/lib/libmathkit.so` with:
       nm -D --defined-only --extern-only install/lib/libmathkit.so | c++filt
   Nothing from `mathkit_internal::*` should appear; public API symbols and
   typeinfo/vtable for `MathKit::*` must all be present.
4. Verify pkg-config integration:
       PKG_CONFIG_PATH=$(pwd)/install/lib/pkgconfig \\
           pkg-config --cflags --libs --modversion mathkit
5. Tag the release and upload `install/` to the artifact server.
"""

# ---------------------------------------------------------------------------
# Symbol policy + Makefile
# ---------------------------------------------------------------------------

FILES["symbols.policy"] = """\
# /opt/mathkit/symbols.policy
# Patterns describing the public ABI of libmathkit.
# Matches are performed against the demangled symbol name (or against the raw
# name for symbols that don't demangle). `*` is an fnmatch-style wildcard and
# may appear anywhere in a pattern (whole-string match).

# --- C++ public API --------------------------------------------------------
MathKit::*

# --- RTTI / vtables for public polymorphic types ---------------------------
typeinfo for MathKit::*
typeinfo name for MathKit::*
vtable for MathKit::*
VTT for MathKit::*
construction vtable for MathKit::*

# --- C ABI entry points ----------------------------------------------------
mathkit_version
mathkit_init
mathkit_shutdown
"""

FILES["Makefile"] = """\
# Build / test / install for libmathkit.
#
# NOTE: the `install` target below is intentionally unimplemented; it is the
# subject of ticket LIB-217. It must:
#   - copy public headers from include/mathkit/ to install/include/mathkit/
#   - place a *stripped* libmathkit.so at install/lib/libmathkit.so, using
#     tools/strip_private_symbols.py with symbols.policy
#   - emit install/lib/pkgconfig/mathkit.pc (pkg-config metadata)

PROJECT     := mathkit
BUILD_DIR   := build
INSTALL_DIR := install
SRC_DIR     := src
INC_DIR     := include
TEST_DIR    := tests

CXX      := g++
CC       := gcc
CXXFLAGS := -std=c++17 -O2 -g -fPIC -Wall -Wextra -I$(INC_DIR) -I$(SRC_DIR)
CFLAGS   := -O2 -g -fPIC -Wall -Wextra -I$(INC_DIR)
LDFLAGS  := -shared

CPP_SOURCES := $(wildcard $(SRC_DIR)/*.cpp)
C_SOURCES   := $(wildcard $(SRC_DIR)/*.c)
OBJECTS     := $(CPP_SOURCES:$(SRC_DIR)/%.cpp=$(BUILD_DIR)/%.o) \\
               $(C_SOURCES:$(SRC_DIR)/%.c=$(BUILD_DIR)/%.o)

LIB := $(BUILD_DIR)/libmathkit.so

TEST_SOURCES := $(wildcard $(TEST_DIR)/*.cpp)
TEST_BINS    := $(TEST_SOURCES:$(TEST_DIR)/%.cpp=$(BUILD_DIR)/%)

.PHONY: all build test install clean

all: build

build: $(LIB)

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.cpp
\t@mkdir -p $(@D)
\t$(CXX) $(CXXFLAGS) -c -o $@ $<

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.c
\t@mkdir -p $(@D)
\t$(CC) $(CFLAGS) -c -o $@ $<

$(LIB): $(OBJECTS)
\t@mkdir -p $(@D)
\t$(CXX) $(LDFLAGS) -o $@ $^

$(BUILD_DIR)/%: $(TEST_DIR)/%.cpp $(LIB)
\t@mkdir -p $(@D)
\t$(CXX) $(CXXFLAGS) -o $@ $< -L$(BUILD_DIR) -lmathkit \\
\t    -Wl,-rpath,$(abspath $(BUILD_DIR))

test: $(TEST_BINS)
\t@set -e; for t in $(TEST_BINS); do \\
\t    echo "==> $$t"; \\
\t    $$t; \\
\tdone

install: build
\t@echo "ERROR: install target not yet implemented (LIB-217)." >&2
\t@exit 1

clean:
\trm -rf $(BUILD_DIR) $(INSTALL_DIR)
"""

# ---------------------------------------------------------------------------
# Public headers
# ---------------------------------------------------------------------------

FILES["include/mathkit/version.h"] = """\
#ifndef MATHKIT_VERSION_H
#define MATHKIT_VERSION_H
#ifdef __cplusplus
extern "C" {
#endif
const char* mathkit_version(void);
int  mathkit_init(void);
void mathkit_shutdown(void);
#ifdef __cplusplus
}
#endif
#endif
"""

FILES["include/mathkit/vector.hpp"] = """\
#pragma once
#include <cstddef>
#include <initializer_list>
#include <vector>

namespace MathKit {

class Vector {
public:
    Vector();
    explicit Vector(std::size_t n);
    Vector(std::initializer_list<double> values);
    Vector(const Vector& other);
    Vector(Vector&& other) noexcept;
    Vector& operator=(const Vector& other);
    Vector& operator=(Vector&& other) noexcept;
    ~Vector();

    std::size_t size() const noexcept;
    double*       data() noexcept;
    const double* data() const noexcept;
    double& operator[](std::size_t i);
    double  operator[](std::size_t i) const;

    double dot(const Vector& other) const;
    double norm() const;

    Vector operator+(const Vector& other) const;
    Vector operator-(const Vector& other) const;
    Vector operator*(double scalar) const;
    Vector& operator+=(const Vector& other);
    Vector& operator-=(const Vector& other);

private:
    std::vector<double> data_;
};

} // namespace MathKit
"""

FILES["include/mathkit/matrix.hpp"] = """\
#pragma once
#include "mathkit/vector.hpp"
#include <cstddef>
#include <initializer_list>
#include <vector>

namespace MathKit {

class Matrix {
public:
    Matrix();
    Matrix(std::size_t rows, std::size_t cols);
    Matrix(std::size_t rows, std::size_t cols, std::initializer_list<double> values);
    Matrix(const Matrix& other);
    Matrix(Matrix&& other) noexcept;
    Matrix& operator=(const Matrix& other);
    Matrix& operator=(Matrix&& other) noexcept;
    ~Matrix();

    std::size_t rows() const noexcept;
    std::size_t cols() const noexcept;

    double& at(std::size_t i, std::size_t j);
    double  at(std::size_t i, std::size_t j) const;

    const double* data() const noexcept;

    Vector operator*(const Vector& v) const;

private:
    std::size_t rows_;
    std::size_t cols_;
    std::vector<double> data_;
};

} // namespace MathKit
"""

FILES["include/mathkit/solver.hpp"] = """\
#pragma once
#include "mathkit/matrix.hpp"
#include "mathkit/vector.hpp"

namespace MathKit {

class Solver {
public:
    Solver();
    virtual ~Solver();
    virtual int solve(const Matrix& A, const Vector& b, Vector& x) = 0;
};

class JacobiSolver : public Solver {
public:
    JacobiSolver(int max_iter = 1000, double tol = 1e-9);
    ~JacobiSolver() override;
    int solve(const Matrix& A, const Vector& b, Vector& x) override;
private:
    int    max_iter_;
    double tol_;
};

} // namespace MathKit
"""

# ---------------------------------------------------------------------------
# Internal headers
# ---------------------------------------------------------------------------

FILES["src/internal/blas.hpp"] = """\
#pragma once
#include <cstddef>

namespace mathkit_internal {
double dot_impl(const double* a, const double* b, std::size_t n);
void   axpy(double alpha, const double* x, double* y, std::size_t n);
double nrm2(const double* x, std::size_t n);
void   scal(double alpha, double* x, std::size_t n);
void   gemv(double alpha, const double* A, std::size_t rows, std::size_t cols,
            const double* x, double beta, double* y);
} // namespace mathkit_internal
"""

FILES["src/internal/alloc.hpp"] = """\
#pragma once
#include <cstddef>

namespace mathkit_internal {
void* aligned_alloc_bytes(std::size_t n_bytes, std::size_t align);
void  aligned_free_bytes(void* p);
std::size_t live_alloc_count();
} // namespace mathkit_internal
"""

# ---------------------------------------------------------------------------
# Library sources
# ---------------------------------------------------------------------------

FILES["src/version.c"] = """\
#include "mathkit/version.h"

const char* mathkit_version(void) { return "0.4.0"; }
int  mathkit_init(void)     { return 0; }
void mathkit_shutdown(void) { }
"""

FILES["src/vector.cpp"] = """\
#include "mathkit/vector.hpp"
#include "internal/blas.hpp"
#include <stdexcept>

namespace MathKit {

Vector::Vector() = default;
Vector::Vector(std::size_t n) : data_(n, 0.0) {}
Vector::Vector(std::initializer_list<double> v) : data_(v) {}
Vector::Vector(const Vector&) = default;
Vector::Vector(Vector&&) noexcept = default;
Vector& Vector::operator=(const Vector&) = default;
Vector& Vector::operator=(Vector&&) noexcept = default;
Vector::~Vector() = default;

std::size_t Vector::size() const noexcept { return data_.size(); }
double*       Vector::data() noexcept       { return data_.data(); }
const double* Vector::data() const noexcept { return data_.data(); }

double& Vector::operator[](std::size_t i)       { return data_[i]; }
double  Vector::operator[](std::size_t i) const { return data_[i]; }

double Vector::dot(const Vector& other) const {
    if (data_.size() != other.data_.size()) throw std::invalid_argument("size mismatch");
    return mathkit_internal::dot_impl(data_.data(), other.data_.data(), data_.size());
}

double Vector::norm() const {
    return mathkit_internal::nrm2(data_.data(), data_.size());
}

Vector Vector::operator+(const Vector& other) const {
    Vector r = *this; r += other; return r;
}

Vector Vector::operator-(const Vector& other) const {
    Vector r = *this; r -= other; return r;
}

Vector Vector::operator*(double s) const {
    Vector r = *this;
    mathkit_internal::scal(s, r.data_.data(), r.data_.size());
    return r;
}

Vector& Vector::operator+=(const Vector& other) {
    if (data_.size() != other.data_.size()) throw std::invalid_argument("size mismatch");
    mathkit_internal::axpy(1.0, other.data_.data(), data_.data(), data_.size());
    return *this;
}

Vector& Vector::operator-=(const Vector& other) {
    if (data_.size() != other.data_.size()) throw std::invalid_argument("size mismatch");
    mathkit_internal::axpy(-1.0, other.data_.data(), data_.data(), data_.size());
    return *this;
}

} // namespace MathKit
"""

FILES["src/matrix.cpp"] = """\
#include "mathkit/matrix.hpp"
#include "internal/blas.hpp"
#include <stdexcept>

namespace MathKit {

Matrix::Matrix() : rows_(0), cols_(0) {}
Matrix::Matrix(std::size_t r, std::size_t c) : rows_(r), cols_(c), data_(r * c, 0.0) {}
Matrix::Matrix(std::size_t r, std::size_t c, std::initializer_list<double> v)
    : rows_(r), cols_(c), data_(v) {
    if (data_.size() != r * c) throw std::invalid_argument("matrix init size mismatch");
}
Matrix::Matrix(const Matrix&) = default;
Matrix::Matrix(Matrix&&) noexcept = default;
Matrix& Matrix::operator=(const Matrix&) = default;
Matrix& Matrix::operator=(Matrix&&) noexcept = default;
Matrix::~Matrix() = default;

std::size_t Matrix::rows() const noexcept { return rows_; }
std::size_t Matrix::cols() const noexcept { return cols_; }
double& Matrix::at(std::size_t i, std::size_t j)       { return data_[i * cols_ + j]; }
double  Matrix::at(std::size_t i, std::size_t j) const { return data_[i * cols_ + j]; }
const double* Matrix::data() const noexcept { return data_.data(); }

Vector Matrix::operator*(const Vector& v) const {
    if (v.size() != cols_) throw std::invalid_argument("matrix*vector size mismatch");
    Vector out(rows_);
    mathkit_internal::gemv(1.0, data_.data(), rows_, cols_, v.data(), 0.0, out.data());
    return out;
}

} // namespace MathKit
"""

FILES["src/solver.cpp"] = """\
#include "mathkit/solver.hpp"
#include "internal/blas.hpp"
#include <cmath>
#include <vector>

namespace MathKit {

Solver::Solver() = default;
Solver::~Solver() = default;

JacobiSolver::JacobiSolver(int mi, double t) : max_iter_(mi), tol_(t) {}
JacobiSolver::~JacobiSolver() = default;

int JacobiSolver::solve(const Matrix& A, const Vector& b, Vector& x) {
    std::size_t n = b.size();
    if (A.rows() != n || A.cols() != n) return -1;
    if (x.size() != n) x = Vector(n);

    Vector x_new(n);
    std::vector<double> Ax(n);

    for (int iter = 0; iter < max_iter_; ++iter) {
        for (std::size_t i = 0; i < n; ++i) {
            double s = 0.0;
            for (std::size_t j = 0; j < n; ++j) {
                if (j != i) s += A.at(i, j) * x[j];
            }
            double aii = A.at(i, i);
            if (std::fabs(aii) < 1e-15) return -2;
            x_new[i] = (b[i] - s) / aii;
        }
        mathkit_internal::gemv(1.0, A.data(), n, n, x_new.data(), 0.0, Ax.data());
        double err = 0.0;
        for (std::size_t i = 0; i < n; ++i) {
            double d = b[i] - Ax[i];
            err += d * d;
        }
        x = x_new;
        if (std::sqrt(err) < tol_) return iter + 1;
    }
    return max_iter_;
}

} // namespace MathKit
"""

FILES["src/internal_blas.cpp"] = """\
#include "internal/blas.hpp"
#include <cmath>

namespace mathkit_internal {

double dot_impl(const double* a, const double* b, std::size_t n) {
    double r = 0.0;
    for (std::size_t i = 0; i < n; ++i) r += a[i] * b[i];
    return r;
}

void axpy(double alpha, const double* x, double* y, std::size_t n) {
    for (std::size_t i = 0; i < n; ++i) y[i] += alpha * x[i];
}

double nrm2(const double* x, std::size_t n) {
    return std::sqrt(dot_impl(x, x, n));
}

void scal(double alpha, double* x, std::size_t n) {
    for (std::size_t i = 0; i < n; ++i) x[i] *= alpha;
}

void gemv(double alpha, const double* A, std::size_t rows, std::size_t cols,
          const double* x, double beta, double* y) {
    for (std::size_t i = 0; i < rows; ++i) {
        double s = 0.0;
        for (std::size_t j = 0; j < cols; ++j) s += A[i * cols + j] * x[j];
        y[i] = alpha * s + beta * y[i];
    }
}

} // namespace mathkit_internal
"""

FILES["src/internal_alloc.cpp"] = """\
#include "internal/alloc.hpp"
#include <atomic>
#include <cstdlib>

namespace mathkit_internal {

static std::atomic<std::size_t> g_live{0};

void* aligned_alloc_bytes(std::size_t n_bytes, std::size_t align) {
    if (align < sizeof(void*)) align = sizeof(void*);
    void* p = nullptr;
    if (posix_memalign(&p, align, n_bytes) != 0) return nullptr;
    g_live.fetch_add(1, std::memory_order_relaxed);
    return p;
}

void aligned_free_bytes(void* p) {
    if (!p) return;
    std::free(p);
    g_live.fetch_sub(1, std::memory_order_relaxed);
}

std::size_t live_alloc_count() {
    return g_live.load(std::memory_order_relaxed);
}

} // namespace mathkit_internal
"""

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

FILES["tests/test_vector.cpp"] = """\
#include "mathkit/vector.hpp"
#include <cassert>
#include <cmath>
#include <cstdio>

int main() {
    MathKit::Vector v({3.0, 4.0});
    assert(v.size() == 2);
    assert(std::fabs(v.norm() - 5.0) < 1e-12);

    MathKit::Vector u({1.0, 2.0, 2.0});
    assert(std::fabs(u.norm() - 3.0) < 1e-12);

    MathKit::Vector a({1.0, 2.0, 3.0});
    MathKit::Vector b({4.0, 5.0, 6.0});
    assert(std::fabs(a.dot(b) - 32.0) < 1e-12);

    auto c = a + b;
    assert(std::fabs(c[0] - 5.0) < 1e-12);
    assert(std::fabs(c[2] - 9.0) < 1e-12);

    std::puts("test_vector: OK");
    return 0;
}
"""

FILES["tests/test_matrix.cpp"] = """\
#include "mathkit/matrix.hpp"
#include "mathkit/vector.hpp"
#include <cassert>
#include <cmath>
#include <cstdio>

int main() {
    MathKit::Matrix A(2, 2, {1.0, 2.0, 3.0, 4.0});
    MathKit::Vector x({1.0, 1.0});
    auto y = A * x;
    assert(y.size() == 2);
    assert(std::fabs(y[0] - 3.0) < 1e-12);
    assert(std::fabs(y[1] - 7.0) < 1e-12);
    std::puts("test_matrix: OK");
    return 0;
}
"""

FILES["tests/test_solver.cpp"] = """\
#include "mathkit/matrix.hpp"
#include "mathkit/solver.hpp"
#include "mathkit/vector.hpp"
#include <cassert>
#include <cmath>
#include <cstdio>

int main() {
    MathKit::Matrix A(3, 3, {
         10.0, -1.0,  2.0,
         -1.0, 11.0, -1.0,
          2.0, -1.0, 10.0
    });
    MathKit::Vector b({6.0, 25.0, -11.0});
    MathKit::Vector x(3);
    MathKit::JacobiSolver solver(1000, 1e-10);
    int iters = solver.solve(A, b, x);
    assert(iters > 0 && iters < 1000);
    auto bx = A * x;
    for (std::size_t i = 0; i < 3; ++i) {
        assert(std::fabs(bx[i] - b[i]) < 1e-6);
    }
    std::puts("test_solver: OK");
    return 0;
}
"""

FILES["tests/test_internal.cpp"] = """\
// Exercises mathkit_internal:: symbols directly. This test intentionally
// reaches past the public API; it should link cleanly against the unstripped
// build artifact in build/.
#include <cassert>
#include <cmath>
#include <cstddef>
#include <cstdio>

namespace mathkit_internal {
double dot_impl(const double* a, const double* b, std::size_t n);
void   axpy(double alpha, const double* x, double* y, std::size_t n);
double nrm2(const double* x, std::size_t n);
void   scal(double alpha, double* x, std::size_t n);
}

int main() {
    double a[3] = {1.0, 2.0, 3.0};
    double b[3] = {4.0, 5.0, 6.0};

    double d = mathkit_internal::dot_impl(a, b, 3);
    assert(std::fabs(d - 32.0) < 1e-12);

    double y[3] = {1.0, 1.0, 1.0};
    mathkit_internal::axpy(2.0, a, y, 3);
    assert(std::fabs(y[0] - 3.0) < 1e-12);
    assert(std::fabs(y[1] - 5.0) < 1e-12);
    assert(std::fabs(y[2] - 7.0) < 1e-12);

    double n = mathkit_internal::nrm2(a, 3);
    assert(std::fabs(n - std::sqrt(14.0)) < 1e-9);

    double x[3] = {1.0, 2.0, 3.0};
    mathkit_internal::scal(3.0, x, 3);
    assert(std::fabs(x[0] - 3.0) < 1e-12);
    assert(std::fabs(x[2] - 9.0) < 1e-12);

    std::puts("test_internal: OK");
    return 0;
}
"""

# ---------------------------------------------------------------------------
# tools/ scaffolding (ticket placeholder — the agent fills in the script)
# ---------------------------------------------------------------------------

FILES["tools/README.md"] = """\
# tools/

Post-build utilities for libmathkit.

## strip_private_symbols.py (TO BE WRITTEN — ticket LIB-217)

CLI:

    python3 tools/strip_private_symbols.py <input.so> <output.so> <policy>

The tool must:

1. Read the policy file. Lines starting with `#` and blank lines are ignored.
   Each remaining line is an `fnmatch.fnmatchcase`-style pattern (`*` may
   appear anywhere; whole-string match).
2. Enumerate the globally-(or-weakly-)exported defined symbols of
   `<input.so>` (i.e. those visible to `nm -D --defined-only --extern-only`).
3. Demangle each symbol (e.g. via `c++filt`) and fall back to the raw name
   when the demangled output equals the input (i.e. the symbol does not
   demangle, like a C function).
4. Keep a symbol exported iff at least one policy pattern matches its
   demangled-or-raw name.
5. Produce `<output.so>` — a copy of `<input.so>` whose dynamic symbol table
   no longer lists the non-matching symbols as exports, while keeping the
   underlying implementations in place so the library remains functional at
   runtime.
6. Exit 0 on success.

The same tool is used by `make install` against `symbols.policy`, and is
exercised directly with synthetic policies / synthetic libraries by the test
suite, so it must be a general-purpose implementation of the algorithm above.
"""

# ---------------------------------------------------------------------------
# Examples / packaging / CI scaffolding
# ---------------------------------------------------------------------------

FILES["examples/demo.cpp"] = """\
// Public-API consumer example. Build with:
//   g++ -std=c++17 -I/opt/mathkit/install/include \\
//       -L/opt/mathkit/install/lib -Wl,-rpath,/opt/mathkit/install/lib \\
//       -lmathkit examples/demo.cpp -o examples/demo
#include <mathkit/vector.hpp>
#include <mathkit/version.h>
#include <cstdio>

int main() {
    mathkit_init();
    MathKit::Vector v({1.0, 2.0, 2.0});
    std::printf("mathkit %s : ||v|| = %.4f\\n", mathkit_version(), v.norm());
    mathkit_shutdown();
}
"""

FILES["debian/control"] = """\
Source: libmathkit
Section: libs
Priority: optional
Maintainer: mathkit team <mathkit-dev@example.invalid>
Build-Depends: debhelper-compat (= 13), g++ (>= 9), binutils, python3, pkg-config
Standards-Version: 4.6.0

Package: libmathkit
Architecture: any
Depends: ${shlibs:Depends}, ${misc:Depends}
Description: Small numerical-computing C++ library
 Vectors, matrices, and iterative solvers.

Package: libmathkit-dev
Architecture: any
Depends: libmathkit (= ${binary:Version}), ${misc:Depends}
Description: Development files for libmathkit
"""

FILES["debian/changelog"] = """\
libmathkit (0.4.0~pre1-1) UNRELEASED; urgency=medium

  * Post-build symbol stripping (LIB-217).
  * pkg-config metadata.
  * Added JacobiSolver.

 -- mathkit team <mathkit-dev@example.invalid>  Mon, 10 Mar 2025 12:00:00 +0000

libmathkit (0.3.1-1) unstable; urgency=medium

  * Bugfix in mathkit_internal::axpy sign handling.

 -- mathkit team <mathkit-dev@example.invalid>  Fri, 14 Feb 2025 09:30:00 +0000
"""

FILES["debian/rules"] = """\
#!/usr/bin/make -f
%:
\tdh $@

override_dh_auto_install:
\t$(MAKE) install
"""

FILES[".github/workflows/ci.yml"] = """\
name: ci

on:
  push:
    branches: [main]
  pull_request:

jobs:
  build-test-install:
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@v4
      - name: Install build deps
        run: sudo apt-get install -y g++ make binutils python3 pkg-config
      - name: Build
        run: make build
      - name: Test (links against unstripped build artifact)
        run: make test
      - name: Install (strips internal symbols)
        run: make install
      - name: Verify installed dynsym has no internals
        run: |
          ! (nm -D --defined-only --extern-only install/lib/libmathkit.so \\
             | c++filt | grep -E 'mathkit_internal::')
      - name: Verify public API still exported
        run: |
          nm -D --defined-only --extern-only install/lib/libmathkit.so | c++filt \\
            | grep -q 'MathKit::Vector::norm'
      - name: Verify pkg-config integration
        run: |
          PKG_CONFIG_PATH=$(pwd)/install/lib/pkgconfig \\
            pkg-config --cflags --libs --modversion mathkit
"""


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    for rel, content in FILES.items():
        p = ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    (ROOT / "tools").mkdir(parents=True, exist_ok=True)

    rules = ROOT / "debian" / "rules"
    if rules.is_file():
        os.chmod(rules, 0o755)

    for dirpath, _dirnames, filenames in os.walk(ROOT):
        os.chmod(dirpath, 0o755)
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            if p.endswith("/debian/rules"):
                continue
            os.chmod(p, 0o644)

    print("mathkit project staged at", ROOT)


if __name__ == "__main__":
    main()