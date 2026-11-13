"""Run bossac against an emulated Arduino SAM-BA bootloader on a pty.

Usage:
  run.py --bossac PATH [--tests NAME[,NAME...]] [--verbose] [--trace]
  run.py --list

Each scenario starts a fresh emulated bootloader, runs bossac once, then
checks the exit code, the output and the device's flash and events. Exits
with status 1 if any scenario fails.
"""

import argparse
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from samba import SambaDevice  # noqa: E402

COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_RESET = "\033[0m"

rng = random.Random(0xB055)
SKETCH = rng.randbytes(150 * 1024)
SMALL = rng.randbytes(5000)        # not a multiple of the 4096-byte page
LOADER = rng.randbytes(64 * 1024)
TOO_LARGE = rng.randbytes(1024 * 1024 + 1)

# SambaDevice arguments per board:
#   base         flash address that bootloader command addresses are relative to
#   flash_size   chip flash size
#   erase_block  smallest erasable unit; a write erases every block it touches
NANO33BLE = {"base": 0x10000, "flash_size": 0x100000, "erase_block": 4096}
UNO_R4_WIFI = {"base": 0x4000, "flash_size": 0x40000, "erase_block": 2048}


# Checks. Each is (label, function of the result).

def exits(code):
    return ("exit code %d" % code, lambda r: r["exit"] == code)


def output_has(text):
    return ("output has '%s'" % text, lambda r: text in r["output"])


def event(name):
    return ("device saw %s" % name, lambda r: name in r["events"])


def no_event(prefix):
    return ("device saw no %s" % prefix,
            lambda r: not any(e.startswith(prefix) for e in r["events"]))


def memory_at(addr, data):
    return ("flash at 0x%x matches the file" % addr,
            lambda r: r["memory"][addr:addr + len(data)] == data)


# bossac command lines, as the Arduino cores' platform.txt build them.

def renesas():
    return ["--port={port}", "-U", "-e", "-w", "{fw}", "-R"]


def mbed():
    return ["--port={port}", "-U", "-i", "-e", "-w", "{fw}", "-R"]


def zephyr_sketch(address):
    return ["--port={port}", "-U", "-i", "-e", "-w", "{fw}", "--offset", address, "-R"]


def zephyr_loader(address):
    return ["--port={port}", "-U", "-i", "-e", "-w", "{fw}", "--offset", address]


class Scenario(NamedTuple):
    name: str
    device: dict
    firmware: bytes
    args: list
    checks: list


SCENARIOS = [
    # Arduino Renesas core (tools.bossac.upload.pattern), UNO R4 WiFi.
    Scenario("arduino-renesas-uno-r4-wifi", UNO_R4_WIFI, SKETCH, renesas(),
             [exits(0), memory_at(0x4000, SKETCH), event("chip-erase:0x0"),
              event("write:0x4000"), event("reset")]),

    # Arduino mbed cores (tools.bossac.upload.pattern), Nano 33 BLE, Edge
    # Control, Stella.
    Scenario("arduino-mbed-nano33ble", NANO33BLE, SKETCH, mbed(),
             [exits(0), memory_at(0x10000, SKETCH), output_has("Device       : nRF52840-QIAA"),
              event("write:0x10000"), event("reset")]),

    # Arduino Zephyr core (tools.bossac.sketch.cmd / loader.cmd), Nano 33 BLE.
    # upload.address and bootloader.address from boards.txt, relative to the
    # bootloader's 0x10000.
    Scenario("arduino-zephyr-nano33ble-sketch", NANO33BLE, SKETCH, zephyr_sketch("0x60000"),
             [exits(0), memory_at(0x70000, SKETCH), event("chip-erase:0x60000"),
              event("write:0x70000"), event("reset")]),
    Scenario("arduino-zephyr-nano33ble-loader", NANO33BLE, LOADER, zephyr_loader("0x0000"),
             [exits(0), memory_at(0x10000, LOADER), event("write:0x10000"), no_event("reset")]),

    # Generic behavior.
    Scenario("partial-page", NANO33BLE, SMALL, ["--port={port}", "-U", "-w", "{fw}"],
             [exits(0), memory_at(0x10000, SMALL), event("write:0x11000")]),
    Scenario("verify", NANO33BLE, SKETCH, ["--port={port}", "-U", "-w", "-v", "{fw}"],
             [exits(0), output_has("Verify successful")]),
    # On a CRC mismatch bossac reads the page back with R, which the Arduino
    # bootloaders do not implement, so it fails on the read instead of
    # reporting the byte errors.
    Scenario("verify-mismatch", dict(NANO33BLE, corrupt_at=0x10000 + 5000), SKETCH,
             ["--port={port}", "-U", "-w", "-v", "{fw}"],
             [exits(1), output_has("SAM-BA operation failed")]),

    # Failures an IDE upload can run into.
    Scenario("no-bootloader", dict(NANO33BLE, silent=True), SKETCH, mbed(),
             [exits(1), output_has("No device found on"), no_event("write:")]),
    Scenario("file-too-large", NANO33BLE, TOO_LARGE, mbed(),
             [exits(1), output_has("File operation exceeds flash size"),
              no_event("write:"), no_event("reset")]),
    Scenario("offset-not-page-aligned", NANO33BLE, SKETCH, zephyr_sketch("0x100"),
             [exits(1), output_has("Flash offset is invalid"), no_event("write:")]),
]


def run_scenario(bossac, scenario):
    device = SambaDevice(**scenario.device)
    device.attach()
    with tempfile.TemporaryDirectory() as tmp:
        fw = Path(tmp) / "sketch.bin"
        fw.write_bytes(scenario.firmware)
        argv = [bossac] + [a.format(fw=fw, port=device.port) for a in scenario.args]
        try:
            proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, errors="replace", timeout=60)
            output, code = proc.stdout, proc.returncode
        except subprocess.TimeoutExpired as e:
            output, code = "TIMEOUT\n" + (e.stdout or ""), -1
        device.stop()

    result = {
        "argv": " ".join(argv[1:]).replace(tmp + "/", "").replace(device.port, "PORT"),
        "exit": code,
        "output": output.replace(tmp + "/", ""),
        "events": list(device.events),
        "trace": list(device.trace),
        "memory": bytes(device.memory),
    }
    result["failed"] = [label for label, fn in scenario.checks if not fn(result)]
    return result


def print_result(name, result, time_ms):
    s = "Test (%s)" % name
    padding = "." * max(60 - len(s), 3)
    if result["failed"]:
        status = COLOR_RED + "FAILED" + COLOR_RESET
    else:
        status = COLOR_GREEN + "PASSED" + COLOR_RESET
    print(s + padding + status + " (%dms)" % time_ms, flush=True)


def print_details(result, trace):
    for label in result["failed"]:
        print(COLOR_RED + "  failed: %s" % label + COLOR_RESET)
    print("  $ bossac %s" % result["argv"])
    print("  exit %d, events: %s" % (result["exit"], ", ".join(result["events"])))
    for line in result["output"].replace("\r", "\n").splitlines():
        if line.strip():
            print("  | %s" % line)
    if trace:
        for line in result["trace"]:
            print("  > %s" % line)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bossac")
    parser.add_argument("--tests", help="comma-separated scenario names")
    parser.add_argument("--list", action="store_true", help="list scenarios")
    parser.add_argument("--verbose", action="store_true",
                        help="print bossac's command line, output and device events")
    parser.add_argument("--trace", action="store_true",
                        help="print every command the device received")
    args = parser.parse_args()

    if args.list:
        for s in SCENARIOS:
            print(s.name)
        return 0
    if not args.bossac:
        parser.error("--bossac is required")

    bossac = str(Path(args.bossac).resolve())
    selected = set(args.tests.split(",")) if args.tests else None
    version = subprocess.run([bossac, "--version"], capture_output=True,
                             text=True).stdout.splitlines()[0]
    print("Testing %s (%s)\n" % (bossac, version))

    passed = failed = 0
    total_start = time.monotonic()
    for scenario in SCENARIOS:
        if selected and scenario.name not in selected:
            continue
        start = time.monotonic()
        result = run_scenario(bossac, scenario)
        print_result(scenario.name, result, (time.monotonic() - start) * 1000)
        if result["failed"]:
            failed += 1
        else:
            passed += 1
        if args.verbose or args.trace or result["failed"]:
            print_details(result, args.trace)

    total_ms = (time.monotonic() - total_start) * 1000
    stats = "Total: %d | Passed: %d | Failed: %d | Time: %dms" % (
        passed + failed, passed, failed, total_ms)
    separator = "=" * len(stats)
    print("\n" + separator)
    print("Total: %d | " % (passed + failed) +
          COLOR_GREEN + "Passed: %d" % passed + COLOR_RESET + " | " +
          COLOR_RED + "Failed: %d" % failed + COLOR_RESET + " | " +
          "Time: %dms" % total_ms)
    print(separator)
    if failed:
        print(COLOR_RED + "Some tests FAILED." + COLOR_RESET)
    else:
        print(COLOR_GREEN + "All tests PASSED." + COLOR_RESET)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
