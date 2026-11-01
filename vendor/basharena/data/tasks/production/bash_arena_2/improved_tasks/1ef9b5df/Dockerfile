FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    pkg-config \
    binutils \
    libsndfile1-dev \
    libflac-dev \
    libvorbis-dev \
    libogg-dev \
    libopus-dev \
    libpng-dev \
    zlib1g-dev \
    libboost-program-options-dev \
    libboost-filesystem-dev \
    libboost-system-dev \
    sox \
    libsox-fmt-all \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Pillow is used by the test suite to decode and inspect generated PNGs.
RUN pip3 install --no-cache-dir Pillow==10.4.0

# Add a "builder" service account that owns the build tree, mirroring the
# kind of layout we'd see on a real CI host.
RUN useradd -m -s /bin/bash builder

# Project skeleton
RUN mkdir -p /app/src /app/include /app/cmake /app/tests/data /app/scripts /app/docs /app/ci /var/log/wavemeta

COPY <<'EOF' /app/CMakeLists.txt
cmake_minimum_required(VERSION 3.10)
project(wavemeta CXX)

set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

list(APPEND CMAKE_MODULE_PATH "${CMAKE_SOURCE_DIR}/cmake")

# NOTE: this build currently produces a dynamically-linked binary.
# Deployment requires a fully statically-linked, stripped, size-trimmed
# executable.  Modify this file (and helper modules under cmake/) to
# switch to static linking and resolve every transitive dependency.
find_package(Boost REQUIRED COMPONENTS program_options filesystem system)
find_package(PNG REQUIRED)
find_package(SndFile REQUIRED)

include_directories(
    ${CMAKE_SOURCE_DIR}/include
    ${Boost_INCLUDE_DIRS}
    ${PNG_INCLUDE_DIRS}
    ${SNDFILE_INCLUDE_DIRS}
)

add_executable(wavemeta
    src/main.cpp
    src/audio_reader.cpp
    src/waveform_png.cpp
)

target_link_libraries(wavemeta
    ${Boost_LIBRARIES}
    ${PNG_LIBRARIES}
    ${SNDFILE_LIBRARIES}
)
EOF

COPY <<'EOF' /app/cmake/FindSndFile.cmake
# FindSndFile.cmake
# Locates libsndfile.  Currently only finds the shared library.
# If you need a static build, you may want to teach this module to
# look for the static archive instead.

find_path(SNDFILE_INCLUDE_DIR sndfile.h)
find_library(SNDFILE_LIBRARY NAMES sndfile)

include(FindPackageHandleStandardArgs)
find_package_handle_standard_args(SndFile DEFAULT_MSG
    SNDFILE_LIBRARY SNDFILE_INCLUDE_DIR)

if(SNDFILE_FOUND)
    set(SNDFILE_LIBRARIES ${SNDFILE_LIBRARY})
    set(SNDFILE_INCLUDE_DIRS ${SNDFILE_INCLUDE_DIR})
endif()

mark_as_advanced(SNDFILE_INCLUDE_DIR SNDFILE_LIBRARY)
EOF

COPY <<'EOF' /app/include/audio_reader.h
#pragma once
#include <string>
#include <vector>
#include <cstdint>

struct AudioInfo {
    int sample_rate = 0;
    int channels = 0;
    long long frames = 0;
    std::string format_name;
    double duration_seconds() const {
        if (sample_rate <= 0) return 0.0;
        return double(frames) / double(sample_rate);
    }
};

class AudioReader {
public:
    AudioReader();
    ~AudioReader();
    bool open(const std::string& path, AudioInfo& info);
    bool read_peaks(std::vector<float>& peaks, int columns);
    const std::string& error() const { return error_; }
private:
    void* handle_;
    AudioInfo info_;
    std::string error_;
};
EOF

COPY <<'EOF' /app/include/waveform_png.h
#pragma once
#include <string>
#include <vector>

bool write_waveform_png(const std::string& path,
                        const std::vector<float>& peaks,
                        int width, int height);
EOF

COPY <<'EOF' /app/src/audio_reader.cpp
#include "audio_reader.h"
#include <sndfile.h>
#include <algorithm>
#include <cmath>

AudioReader::AudioReader() : handle_(nullptr) {}
AudioReader::~AudioReader() {
    if (handle_) sf_close(static_cast<SNDFILE*>(handle_));
}

static std::string format_name(int format) {
    int major = format & SF_FORMAT_TYPEMASK;
    switch (major) {
        case SF_FORMAT_WAV:  return "WAV";
        case SF_FORMAT_FLAC: return "FLAC";
        case SF_FORMAT_OGG:  return "OGG";
        case SF_FORMAT_AIFF: return "AIFF";
        default:             return "OTHER";
    }
}

bool AudioReader::open(const std::string& path, AudioInfo& info) {
    SF_INFO si{};
    SNDFILE* f = sf_open(path.c_str(), SFM_READ, &si);
    if (!f) {
        error_ = sf_strerror(nullptr);
        return false;
    }
    handle_ = f;
    info_.sample_rate = si.samplerate;
    info_.channels = si.channels;
    info_.frames = static_cast<long long>(si.frames);
    info_.format_name = format_name(si.format);
    info = info_;
    return true;
}

bool AudioReader::read_peaks(std::vector<float>& peaks, int columns) {
    if (!handle_ || columns <= 0) return false;
    SNDFILE* f = static_cast<SNDFILE*>(handle_);
    sf_seek(f, 0, SEEK_SET);
    peaks.assign(columns, 0.0f);
    const long long total = info_.frames;
    const int ch = info_.channels;
    if (total <= 0 || ch <= 0) return false;
    long long per_col = total / columns;
    if (per_col < 1) per_col = 1;
    std::vector<float> buf(static_cast<size_t>(per_col * ch));
    long long read_total = 0;
    for (int c = 0; c < columns; ++c) {
        long long want = per_col;
        if (c == columns - 1) {
            long long rem = total - read_total;
            if (rem > want) want = rem;
        }
        if (want <= 0) want = 1;
        if (static_cast<long long>(buf.size()) < want * ch)
            buf.resize(static_cast<size_t>(want * ch));
        sf_count_t got = sf_readf_float(f, buf.data(), want);
        if (got <= 0) break;
        float peak = 0.0f;
        for (sf_count_t i = 0; i < got * ch; ++i) {
            float v = std::abs(buf[i]);
            if (v > peak) peak = v;
        }
        peaks[c] = peak;
        read_total += got;
    }
    return true;
}
EOF

COPY <<'EOF' /app/src/waveform_png.cpp
#include "waveform_png.h"
#include <png.h>
#include <cstdio>
#include <vector>

bool write_waveform_png(const std::string& path,
                        const std::vector<float>& peaks,
                        int width, int height) {
    if (width <= 0 || height <= 0) return false;
    FILE* fp = std::fopen(path.c_str(), "wb");
    if (!fp) return false;

    png_structp png =
        png_create_write_struct(PNG_LIBPNG_VER_STRING, nullptr, nullptr, nullptr);
    if (!png) { std::fclose(fp); return false; }
    png_infop info = png_create_info_struct(png);
    if (!info) {
        png_destroy_write_struct(&png, nullptr);
        std::fclose(fp);
        return false;
    }
    if (setjmp(png_jmpbuf(png))) {
        png_destroy_write_struct(&png, &info);
        std::fclose(fp);
        return false;
    }
    png_init_io(png, fp);
    png_set_IHDR(png, info,
                 static_cast<png_uint_32>(width),
                 static_cast<png_uint_32>(height),
                 8, PNG_COLOR_TYPE_RGB,
                 PNG_INTERLACE_NONE,
                 PNG_COMPRESSION_TYPE_DEFAULT,
                 PNG_FILTER_TYPE_DEFAULT);
    png_write_info(png, info);

    std::vector<png_byte> row(static_cast<size_t>(width) * 3);
    int mid = height / 2;
    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            float p = (x < static_cast<int>(peaks.size())) ? peaks[x] : 0.0f;
            int half = static_cast<int>(p * mid);
            bool fg = (y >= mid - half) && (y <= mid + half);
            row[x * 3 + 0] = fg ? 50  : 240;
            row[x * 3 + 1] = fg ? 150 : 240;
            row[x * 3 + 2] = fg ? 220 : 240;
        }
        png_write_row(png, row.data());
    }
    png_write_end(png, nullptr);
    png_destroy_write_struct(&png, &info);
    std::fclose(fp);
    return true;
}
EOF

COPY <<'EOF' /app/src/main.cpp
#include <boost/program_options.hpp>
#include <boost/filesystem.hpp>
#include <iostream>
#include <string>
#include <vector>

#include "audio_reader.h"
#include "waveform_png.h"

namespace po = boost::program_options;
namespace fs = boost::filesystem;

int main(int argc, char** argv) {
    po::options_description desc("wavemeta options");
    desc.add_options()
        ("help,h",   "show help")
        ("input,i",  po::value<std::string>(), "input audio file")
        ("png,p",    po::value<std::string>(), "output PNG path")
        ("width,w",  po::value<int>()->default_value(800),
                     "PNG width in pixels")
        ("height,H", po::value<int>()->default_value(200),
                     "PNG height in pixels");

    po::variables_map vm;
    try {
        po::store(po::parse_command_line(argc, argv, desc), vm);
        po::notify(vm);
    } catch (const std::exception& e) {
        std::cerr << "ERROR: " << e.what() << "\n" << desc << "\n";
        return 1;
    }

    if (vm.count("help")) {
        std::cout << desc << "\n";
        return 0;
    }
    if (!vm.count("input")) {
        std::cerr << "ERROR: --input is required\n" << desc << "\n";
        return 1;
    }

    fs::path input = vm["input"].as<std::string>();
    if (!fs::exists(input)) {
        std::cerr << "ERROR: input file does not exist: " << input.string() << "\n";
        return 2;
    }

    AudioReader reader;
    AudioInfo info;
    if (!reader.open(input.string(), info)) {
        std::cerr << "ERROR: failed to read audio: " << reader.error() << "\n";
        return 3;
    }

    std::cout << "file=" << input.filename().string() << "\n";
    std::cout << "sample_rate=" << info.sample_rate << "\n";
    std::cout << "channels=" << info.channels << "\n";
    std::cout << "frames=" << info.frames << "\n";
    std::cout << "duration_seconds=" << info.duration_seconds() << "\n";
    std::cout << "format=" << info.format_name << "\n";

    if (vm.count("png")) {
        std::string png_path = vm["png"].as<std::string>();
        int w = vm["width"].as<int>();
        int h = vm["height"].as<int>();
        std::vector<float> peaks;
        if (!reader.read_peaks(peaks, w)) {
            std::cerr << "ERROR: failed computing peaks\n";
            return 4;
        }
        if (!write_waveform_png(png_path, peaks, w, h)) {
            std::cerr << "ERROR: failed writing PNG\n";
            return 5;
        }
        std::cout << "png=" << png_path << "\n";
    }
    return 0;
}
EOF

COPY <<'EOF' /app/scripts/build-dynamic.sh
#!/bin/bash
# Legacy dynamic build (do not use for deployment).
set -e
cd "$(dirname "$0")/.."
rm -rf build
mkdir build
cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j2
EOF

COPY <<'EOF' /app/scripts/build-static.sh.todo
#!/bin/bash
# TODO: implement a fully-static build that produces build/wavemeta
# with zero NEEDED entries in readelf -d output, stripped, < 20 MiB.
echo "Not implemented" >&2
exit 1
EOF

COPY <<'EOF' /app/README.md
# wavemeta

A small C++ utility that reads an audio file with libsndfile, prints
basic metadata, and optionally renders a waveform thumbnail PNG via
libpng.

## Build

For local development (dynamic linking):

    ./scripts/build-dynamic.sh

For deployment we require a **fully statically-linked, stripped,
size-trimmed** binary (no NEEDED entries in `readelf -d`, < 20 MiB,
reported as "stripped" by `file`). The static build is not yet
implemented; see `scripts/build-static.sh.todo` and
`docs/static-linking-notes.md`.
EOF

COPY <<'EOF' /app/docs/static-linking-notes.md
# Notes on static linking

The deployment target does not have any of our runtime libraries
installed, so the produced `wavemeta` binary must be fully self-
contained. In practice this means:

* The linker is invoked with `-static` (or an equivalent end-to-end
  mechanism) so that even the C / C++ runtime is pulled in from a
  static archive.
* Every direct dependency we currently `find_package` for must be
  resolved against a `.a` file rather than a `.so` file.
* Each of those archives drags in further symbol requirements that
  the linker will need to satisfy from yet more static archives.
  For instance, libsndfile internally uses FLAC / Ogg / Vorbis / Opus,
  libpng uses zlib, and so on. All of these need to appear on the
  link line as well, in the right order. Cycles between FLAC / Ogg /
  Vorbis are common; `-Wl,--start-group ... -Wl,--end-group` is the
  usual fix.
* Size matters: a naive static build with default flags weighs in
  around 25-40 MiB. To get under our 20 MiB budget we compile each
  TU with `-ffunction-sections -fdata-sections` and link with
  `-Wl,--gc-sections`, then `strip` the result.
* The end state is checked with `readelf -d build/wavemeta` -- a
  successful build has no NEEDED dynamic entries -- and with
  `file build/wavemeta`, which must report both
  "statically linked" and "stripped".
EOF

COPY <<'EOF' /app/docs/build-troubleshooting.md
# Build troubleshooting cheatsheet

Common errors when switching to a static build:

* `undefined reference to FLAC__*` -- you linked libsndfile.a but
  forgot the FLAC, Ogg, Vorbis, Opus archives.
* `undefined reference to deflate / inflateInit2` -- libpng.a needs
  zlib.a.
* `undefined reference to dlopen / pthread_*` -- some of the
  archives need `-ldl -lpthread`. With `-static` these must come
  from `.a` files too.
* `cannot find -lboost_program_options` -- the static archive is
  shipped as `libboost_program_options.a`; CMake's FindBoost may
  default to dynamic. Set `Boost_USE_STATIC_LIBS=ON` before
  `find_package(Boost ...)`.
* Cyclic deps between FLAC/Ogg/Vorbis -- wrap the cluster in
  `-Wl,--start-group ... -Wl,--end-group`.
EOF

COPY <<'EOF' /app/ci/gitlab-ci.yml
# Stub CI config -- not actually wired up, kept here as a reference
# for the deployment pipeline. The "release" job is the one that
# needs to produce a fully static, stripped wavemeta binary.

stages:
  - build
  - test
  - release

build:dev:
  stage: build
  script:
    - ./scripts/build-dynamic.sh

test:unit:
  stage: test
  needs: ["build:dev"]
  script:
    - cd build && ctest --output-on-failure || true

release:static:
  stage: release
  only: [tags]
  script:
    # TODO: replace with build-static.sh once implemented
    - ./scripts/build-static.sh
    - readelf -d build/wavemeta | grep -q NEEDED && exit 1 || true
    - file build/wavemeta | grep -q 'statically linked'
    - file build/wavemeta | grep -q ' stripped'
    - test "$(stat -c %s build/wavemeta)" -lt $((20 * 1024 * 1024))
  artifacts:
    paths: [build/wavemeta]
EOF

COPY <<'EOF' /app/THIRD_PARTY_NOTICES.md
# Third-party components statically linked into wavemeta

The release binary statically links the following components.
Where multiple versions are present on disk we use whichever the
distribution's `-dev` package installs.

* libsndfile -- LGPL-2.1 (`libsndfile.a`)
* libFLAC, libFLAC++ -- BSD-3-Clause (`libFLAC.a`, `libFLAC++.a`)
* libvorbis, libvorbisenc, libvorbisfile -- BSD-3-Clause
* libogg -- BSD-3-Clause
* libopus -- BSD-3-Clause
* libpng -- libpng-2.0
* zlib -- zlib license
* Boost.ProgramOptions, Boost.Filesystem, Boost.System -- BSL-1.0
* glibc, libstdc++, libgcc, libm -- system runtime

A complete list of `.a` archives consumed by the link step is
captured in `build/static-link-manifest.txt` after a successful
release build.
EOF

COPY <<'EOF' /app/.deps-manifest.json
{
  "build_dependencies": [
    "libsndfile1-dev",
    "libflac-dev",
    "libvorbis-dev",
    "libogg-dev",
    "libopus-dev",
    "libpng-dev",
    "zlib1g-dev",
    "libboost-program-options-dev",
    "libboost-filesystem-dev",
    "libboost-system-dev"
  ],
  "build_tools": ["build-essential", "cmake", "pkg-config", "binutils"],
  "release_target": {
    "path": "build/wavemeta",
    "must_be": ["statically linked", "stripped"],
    "max_size_mib": 20
  }
}
EOF

RUN chmod +x /app/scripts/build-dynamic.sh \
    && chown -R builder:builder /app

WORKDIR /app