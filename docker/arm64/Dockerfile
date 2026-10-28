# syntax=docker/dockerfile:1.7-labs
# Native linux/arm64 build of the OpenROAD-flow-scripts image used by the PPA Optimizer.
# Pins the exact ORFS / OpenROAD / yosys / kepler-formal commits recorded in
# /OpenROAD-flow-scripts/pipeline-setup.txt of the amd64 openroad/orfs:latest image
# (pulled 2026-10-22, Jenkins build 1711), and follows ORFS's own docker/Dockerfile.{dev,builder}.
ARG fromImage=ubuntu:22.04

# ---------- stage 1: sources (pinned) ----------
FROM ${fromImage} AS src
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
ARG ORFS_SHA=0b56643596e8ffd41b929858107c36602e0644f5
ARG OPENROAD_SHA=bc739caf43047bfc2e225fe10dd968d672b617f8
ARG YOSYS_SHA=a5af9d690a43744bf6b2cc3dea2717c16b54621c
ARG KEPLER_SHA=f70c2e38592d704c8cccf68433b091e82c8118f0
WORKDIR /OpenROAD-flow-scripts
RUN set -e; \
    fetch() { git init -q "$1" && git -C "$1" remote add origin "$2" && git -C "$1" fetch -q --depth 1 origin "$3" && git -C "$1" checkout -q FETCH_HEAD; }; \
    fetch . https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts.git $ORFS_SHA; \
    rm -rf tools/OpenROAD tools/yosys tools/kepler-formal; \
    fetch tools/OpenROAD https://github.com/The-OpenROAD-Project/OpenROAD.git $OPENROAD_SHA; \
    fetch tools/yosys https://github.com/The-OpenROAD-Project/yosys.git $YOSYS_SHA; \
    fetch tools/kepler-formal https://github.com/keplertech/kepler-formal $KEPLER_SHA; \
    for d in tools/OpenROAD tools/yosys tools/kepler-formal; do \
        git -C $d submodule update --init --recursive --depth 1; done

# ---------- stage 2: dependencies (== Dockerfile.dev) ----------
FROM ${fromImage} AS dev
WORKDIR /tmp/installer/etc
COPY --from=src /OpenROAD-flow-scripts/etc/DependencyInstaller.sh /OpenROAD-flow-scripts/etc/requirements-common_lock.txt /OpenROAD-flow-scripts/etc/requirements-pip_lock.txt ./
COPY --from=src /OpenROAD-flow-scripts/tools/OpenROAD/etc/DependencyInstaller.sh /tmp/installer/tools/OpenROAD/etc/DependencyInstaller.sh
COPY --from=src /OpenROAD-flow-scripts/etc/setup_compiler_wrappers.sh /tmp/
ARG options="-ci -yosys-ver=v0.68"
ARG constantBuildDir="-constant-build-dir"
RUN sh /tmp/setup_compiler_wrappers.sh && rm /tmp/setup_compiler_wrappers.sh
ENV PATH="/usr/local/bin/wrapped-cc:$PATH"
RUN ./DependencyInstaller.sh -base $options $constantBuildDir -save-deps-prefixes=/etc/openroad_deps_prefixes.txt \
    && ./DependencyInstaller.sh -common $options $constantBuildDir -save-deps-prefixes=/etc/openroad_deps_prefixes.txt \
    && rm -rf /tmp/installer /tmp/* /var/tmp/* /var/lib/apt/lists/*
WORKDIR /OpenROAD-flow-scripts

# ---------- stage 2b: extra dependencies for OpenROAD's CMake build ----------
FROM dev AS dev2
COPY --from=src /OpenROAD-flow-scripts/tools/OpenROAD/etc/DependencyInstaller.sh /tmp/installer/etc/DependencyInstaller.sh
RUN cd /tmp/installer/etc && ./DependencyInstaller.sh -cmake -base -constant-build-dir -save-deps-prefixes=/etc/openroad_deps_prefixes.txt \
    && ./DependencyInstaller.sh -cmake -common -constant-build-dir -threads=${numThreads:-4} -save-deps-prefixes=/etc/openroad_deps_prefixes.txt \
    && rm -rf /tmp/installer /tmp/* /var/tmp/* /var/lib/apt/lists/*
WORKDIR /OpenROAD-flow-scripts

# ---------- stage 3: build yosys(+slang), OpenROAD, kepler-formal (== Dockerfile.builder) ----------
FROM dev2 AS build
COPY --from=src /OpenROAD-flow-scripts /OpenROAD-flow-scripts
ARG numThreads=4
RUN echo "" > tools/yosys/abc/.gitcommit || true
# OpenROAD's default Bazel build compiles Qt 6.9.1 from source and fails on linux/arm64
# (qsimd.cpp), and its pinned sv-lang archive checksum no longer matches GitHub's regenerated
# tarball. The deprecated-but-supported CMake build (system Qt5, slang from the submodule) works.
# Step A configures + compiles with CMake; the deprecated CMake tree has a unit-test target that
# fails to compile at this OpenROAD revision (src/rsz/test/cpp/TestResizer.cc) and -no-tests breaks
# configuration (src/stt/test/cpp), so tolerate the failure here and build/install only the
# openroad target in step B.
RUN ./build_openroad.sh --no_init --local --openroad_only --threads ${numThreads} --openroad-args "-cmake-build" || echo "step A: ignoring test-target failure"
RUN cd tools/OpenROAD/build && make -j ${numThreads} openroad && cmake --install . && cd /OpenROAD-flow-scripts && test -x tools/install/OpenROAD/bin/openroad
# Step C: yosys (+slang) and kepler-formal
RUN ./build_openroad.sh --no_init --local --skip_openroad --threads ${numThreads}
RUN test -x tools/install/OpenROAD/bin/openroad && test -x tools/install/yosys/bin/yosys

# ---------- stage 4: runtime image ----------
FROM dev2
COPY --from=src /OpenROAD-flow-scripts/flow /OpenROAD-flow-scripts/flow
COPY --from=src /OpenROAD-flow-scripts/docs /OpenROAD-flow-scripts/docs
COPY --from=src /OpenROAD-flow-scripts/tools/AutoTuner /OpenROAD-flow-scripts/tools/AutoTuner
COPY --from=src /OpenROAD-flow-scripts/build_openroad.sh /OpenROAD-flow-scripts/dev_env.sh /OpenROAD-flow-scripts/env.sh /OpenROAD-flow-scripts/
COPY --from=build /OpenROAD-flow-scripts/tools/install /OpenROAD-flow-scripts/tools/install
# KLayout was built from source into /usr/local (ORFS looks it up with `command -v klayout`)
RUN ln -sf /usr/local/klayout /usr/local/bin/klayout
