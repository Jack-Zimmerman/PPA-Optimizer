# Native linux/arm64 ORFS image (`ppa-orfs:arm64`)

Replaces the amd64-only `openroad/orfs:latest` (Rosetta) with a native aarch64 build for the Colima VM.

## Build
```
docker/arm64/build.sh [threads]     # default 5; ~16 GB VM: do not exceed 5. Log: docker/arm64/build.log
```
= `docker build --platform linux/arm64 --build-arg numThreads=5 -t ppa-orfs:arm64 docker/arm64`
(classic builder; needs network access to GitHub, Ubuntu apt, klayout/boost/ortools sources).
Cold build: several hours in the Colima VM (about 1.5 h dependencies incl. KLayout/Qt/or-tools from source,
about 1 h OpenROAD, about 1.5 h yosys+kepler-formal with LTO, all slowed by other jobs on the VM).
Image size: 6.22 GB on disk (1.53 GB content). `build.full.log` is the log of the last full build.

## Revisions (identical to the amd64 image, taken from its /OpenROAD-flow-scripts/pipeline-setup.txt)
- ORFS `0b56643596e8ffd41b929858107c36602e0644f5` (master, 2026-10-22)
- OpenROAD `bc739caf43047bfc2e225fe10dd968d672b617f8`, OpenSTA `846862b`, yosys fork `a5af9d690a43744bf6b2cc3dea2717c16b54621c` (v0.68), yosys-slang inside OpenROAD third-party, kepler-formal `f70c2e3`
- Ubuntu 22.04, same ORFS DependencyInstaller scripts as the amd64 image.

## Differences from the amd64 build (they matter)
- OpenROAD is built with the deprecated **CMake** path (gcc 11, Release), not Bazel/clang: Bazel compiles Qt 6.9.1 from source and fails on arm64, and the pinned slang tarball checksum no longer matches GitHub. CMake support is slated for removal upstream on 2026-11-01 (only matters for rebuilding).
- The CMake test target `TestResizer` does not compile at this revision; the Dockerfile tolerates that and builds/installs only the `openroad` target.
- KLayout 0.30.12 is built from source (no aarch64 .deb); `/usr/local/bin/klayout` symlinks to it.
- yosys: `read_slang` is built in (no plugin file); `yosys -m slang` fails, plain `read_slang` (as `lint()` uses) works.
- The "strip ABI-tag from libQt5Core" x86 workaround is omitted.

## Use
```
docker run --rm --platform linux/arm64 --cpus 1 -v <run_dir>:/work ppa-orfs:arm64 bash -lc "cd /OpenROAD-flow-scripts/flow && make DESIGN_CONFIG=/work/config.mk"
docker run --rm --platform linux/arm64 ppa-orfs:arm64 /OpenROAD-flow-scripts/tools/install/yosys/bin/yosys -p "read_slang --top <top> <files>; hierarchy -check -top <top>"
```
`<run_dir>` must live under a path shared into the Colima VM (under $HOME works; /private/tmp does not).

## Validation (see the task report): metrics match the amd64 runs (area identical, slack within 0.01 ns); runtime gain is modest (~1.1-1.7x).
