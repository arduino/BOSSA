"""Emulated Arduino SAM-BA bootloader on a pseudo-terminal.

bossac talks to a serial port. On a real board that is the bootloader's USB
CDC-ACM port; here it is the slave end of a pty, and this module answers on
the master end the way the Arduino SAM-BA bootloaders do:

- UNO R4 WiFi: arduino/arduino-renesas-bootloader, src/bossa.c
- Nano 33 BLE: bcmi-labs/zephyr, samples/arduino_bootloader_nrf52/src/main.c

Both implement the same commands. Addresses in commands are relative to the
sketch area, which starts at `base` in flash:

  N#                 binary mode, reply "\\n\\r"
  V#                 version, reply with VERSION
  I#                 chip id, reply with CHIP_ID
  X<addr>#           chip erase, reply "X\\n\\r" (erases nothing)
  S<addr>,<len>#     receive <len> raw bytes into the RAM buffer at <addr>
  Y<addr>,0#         set the RAM buffer offset for the next copy, reply "Y\\n\\r"
  Y<addr>,<len>#     erase and program <len> bytes from the RAM buffer to
                     flash at base + <addr>, reply "Y\\n\\r"
  Z<addr>,<len>#     CRC16 of flash at base + <addr>, reply "Z%08X#\\n\\r"
  K#                 reset
  anything else      ignored, no reply (bossac sends W# to load its applet)
"""

import os
import select
import threading

VERSION = b"Arduino Bootloader (SAM-BA extended) 2.0 [Arduino:IKXYZ]\n\r"
CHIP_ID = b"nRF52840-QIAA\n\r"
COMMAND_SIZE = 64      # command_buffer[64] in both bootloaders
BUFFER_SIZE = 8192     # data_buffer[8192] in both bootloaders


def crc16(data, crc=0):
    """CRC16-CCITT (poly 0x1021, init 0), as serial_add_crc() in the bootloaders."""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def parse_args(command):
    """Hex arguments of a command: b"Y00001000,0#" -> [0x1000, 0]."""
    body = command[1:-1].decode("ascii", "replace")
    return [int(a, 16) for a in body.split(",")] if body else []


class SambaDevice:
    def __init__(self, base=0x10000, flash_size=0x100000, erase_block=4096,
                 corrupt_at=None, silent=False):
        self.base = base                  # sketch area start in flash
        self.erase_block = erase_block    # flash erase granularity
        self.memory = bytearray(b"\xff" * flash_size)
        self.buffer = bytearray(BUFFER_SIZE)
        self.copy_offset = 0
        self.corrupt_at = corrupt_at      # flash address that programs wrong
        self.silent = silent              # never answer (no bootloader running)
        self.events = []
        self.trace = []
        self.port = None
        self.master = self.slave = None
        self.thread = None
        self.stopping = threading.Event()

    def event(self, name):
        self.events.append(name)

    # Commands

    def command(self, command):
        """Handle one '#'-terminated command. Returns the reply, or None."""
        self.trace.append(command.decode("ascii", "replace"))
        kind = command[:1]
        args = parse_args(command) if kind in b"XSYZ" else []
        if kind == b"N":
            return b"\n\r"
        if kind == b"V":
            return VERSION
        if kind == b"I":
            return CHIP_ID
        if kind == b"X":
            self.event("chip-erase:0x%x" % args[0])
            return b"X\n\r"
        if kind == b"Y":
            addr, size = args
            if size == 0:
                self.copy_offset = addr
            else:
                self.program(self.base + addr, self.buffer[self.copy_offset:self.copy_offset + size])
            return b"Y\n\r"
        if kind == b"Z":
            addr, size = args
            start = self.base + addr
            return b"Z%08X#\n\r" % crc16(self.memory[start:start + size])
        if kind == b"K":
            self.event("reset")
            return None
        return None

    def program(self, addr, data):
        end = addr + len(data)
        if end > len(self.memory):
            self.event("write-beyond-flash:0x%x" % addr)
            return
        first = addr - addr % self.erase_block
        last = -(-end // self.erase_block) * self.erase_block
        self.memory[first:last] = b"\xff" * (last - first)
        self.memory[addr:end] = data
        if self.corrupt_at is not None and addr <= self.corrupt_at < end:
            self.memory[self.corrupt_at] ^= 0xFF
        self.event("write:0x%x" % addr)

    # Transport

    def attach(self):
        self.master, self.slave = os.openpty()
        self.port = os.ttyname(self.slave)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join()
        for fd in (self.master, self.slave):
            if fd is not None:
                os.close(fd)
        self.master = self.slave = None

    def serve(self):
        command = bytearray()
        receive = None    # [address, bytes left] while an S command reads data
        while not self.stopping.is_set():
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                continue
            try:
                data = os.read(self.master, 4096)
            except OSError:
                continue    # no slave open (Linux returns EIO)
            if self.silent:
                continue
            for byte in data:
                if receive:
                    address, left = receive
                    if address < BUFFER_SIZE:
                        self.buffer[address] = byte
                    else:
                        self.event("buffer-overflow")
                    receive = [address + 1, left - 1] if left > 1 else None
                    continue
                if len(command) < COMMAND_SIZE:
                    command.append(byte)
                if byte != ord("#"):
                    continue
                if command[:1] == b"S":
                    address, size = parse_args(command)
                    self.trace.append(command.decode("ascii", "replace"))
                    receive = [address, size] if size else None
                else:
                    reply = self.command(bytes(command))
                    if reply:
                        os.write(self.master, reply)
                command.clear()
