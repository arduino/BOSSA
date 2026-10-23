#!/bin/bash
# Runs inside ghcr.io/arduino/crossbuild; builds all targets in parallel into dist/
set -euo pipefail

: "${VERSION:?VERSION must be set}"
LINUX_LDFLAGS="-static-libstdc++ -static-libgcc"

# Keep the committed applet source so arm-none-eabi-as is not needed
touch src/WordCopyArm.cpp
mkdir -p dist

# build <name> <OS> <toolchain prefix> <c++ compiler> <exe suffix> <ldflags>
build() {
	local name=$1 os=$2 prefix=$3 cxx=$4 exe=$5 ldflags=$6
	make -j"$(nproc)" bossac Q= OS="$os" CXX="$cxx" LDFLAGS="$ldflags" VERSION="$VERSION" \
		OBJDIR="obj-$name" BINDIR="bin-$name" >"build-$name.log" 2>&1
	"$prefix-strip" "bin-$name/bossac$exe"
	mkdir -p "stage-$name/bin"
	cp "bin-$name/bossac$exe" "stage-$name/bin/"
	tar -czf "dist/bossac-$VERSION-$name.tar.gz" -C "stage-$name" "bin/bossac$exe"
}

declare -A pids
build linux64 Linux x86_64-ubuntu16.04-linux-gnu x86_64-ubuntu16.04-linux-gnu-g++ "" "$LINUX_LDFLAGS" & pids[linux64]=$!
build linux32 Linux i686-ubuntu16.04-linux-gnu i686-ubuntu16.04-linux-gnu-g++ "" "$LINUX_LDFLAGS" & pids[linux32]=$!
build linuxarm Linux arm-linux-gnueabihf arm-linux-gnueabihf-g++ "" "$LINUX_LDFLAGS" & pids[linuxarm]=$!
build linuxaarch64 Linux aarch64-linux-gnu aarch64-linux-gnu-g++ "" "$LINUX_LDFLAGS" & pids[linuxaarch64]=$!
build windows MINGW32 i686-w64-mingw32 i686-w64-mingw32-g++ .exe "" & pids[windows]=$!
build windows64 MINGW32 x86_64-w64-mingw32 x86_64-w64-mingw32-g++ .exe "" & pids[windows64]=$!
build osx Darwin x86_64-apple-darwin13 o64-clang++ "" "" & pids[osx]=$!

failed=0
for name in "${!pids[@]}"; do
	if ! wait "${pids[$name]}"; then
		echo "::error::bossac build for $name failed"
		cat "build-$name.log"
		failed=1
	fi
done
exit $failed
