#!/usr/bin/env python3
"""Temporary ILI9488 hardware test: initialize the panel and show solid colors."""

import argparse
import fcntl
import os
import struct
import time
from pathlib import Path


SPI_IOC_WR_MODE = 0x40016B01
SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04


class Gpio:
    def __init__(self, number: int) -> None:
        self.path = Path(f"/sys/class/gpio/gpio{number}")
        if not self.path.exists():
            Path("/sys/class/gpio/export").write_text(str(number), encoding="ascii")
            deadline = time.monotonic() + 1.0
            while not self.path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
        (self.path / "direction").write_text("out", encoding="ascii")

    def set(self, value: bool) -> None:
        (self.path / "value").write_text("1" if value else "0", encoding="ascii")


class Spi:
    def __init__(self, path: str, speed: int) -> None:
        self.fd = os.open(path, os.O_RDWR)
        fcntl.ioctl(self.fd, SPI_IOC_WR_MODE, struct.pack("B", 0))
        fcntl.ioctl(self.fd, SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))
        fcntl.ioctl(self.fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", speed))

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            count = os.write(self.fd, view[:4096])
            view = view[count:]


class Ili9488:
    def __init__(self, spi_path: str, dc: int, reset: int, cs: int, speed: int) -> None:
        self.spi = Spi(spi_path, speed)
        self.dc = Gpio(dc)
        self.reset = Gpio(reset) if reset >= 0 else None
        self.cs = Gpio(cs) if cs >= 0 else None
        if self.cs is not None:
            self.cs.set(True)

    def command(self, value: int, data: bytes = b"") -> None:
        if self.cs is not None:
            self.cs.set(False)
        try:
            self.dc.set(False)
            self.spi.write(bytes((value,)))
            if data:
                self.dc.set(True)
                self.spi.write(data)
        finally:
            if self.cs is not None:
                self.cs.set(True)

    def data(self, payload: bytes) -> None:
        if self.cs is not None:
            self.cs.set(False)
        try:
            self.dc.set(True)
            self.spi.write(payload)
        finally:
            if self.cs is not None:
                self.cs.set(True)

    def init(self) -> None:
        if self.reset is not None:
            self.reset.set(True)
            time.sleep(0.05)
            self.reset.set(False)
            time.sleep(0.10)
            self.reset.set(True)
            time.sleep(0.15)

        self.command(0x01)
        time.sleep(0.12)
        self.command(0x11)
        time.sleep(0.12)
        self.command(0xE0, bytes((0x00, 0x03, 0x09, 0x08, 0x16, 0x0A, 0x3F, 0x78, 0x4C, 0x09, 0x0A, 0x08, 0x16, 0x1A, 0x0F)))
        self.command(0xE1, bytes((0x00, 0x16, 0x19, 0x03, 0x0F, 0x05, 0x32, 0x45, 0x46, 0x04, 0x0E, 0x0D, 0x35, 0x37, 0x0F)))
        self.command(0xC0, bytes((0x17, 0x15)))
        self.command(0xC1, bytes((0x41,)))
        self.command(0xC5, bytes((0x00, 0x12, 0x80)))
        self.command(0xB0, bytes((0x00,)))
        self.command(0xB1, bytes((0xA0,)))
        self.command(0xB4, bytes((0x02,)))
        self.command(0xB6, bytes((0x02, 0x02)))
        self.command(0x36, bytes((0xE8,)))
        self.command(0x3A, bytes((0x66,)))
        self.command(0xE9, bytes((0x00,)))
        self.command(0xF7, bytes((0xA9, 0x51, 0x2C, 0x82)))
        self.command(0x20)
        self.command(0x29)
        time.sleep(0.05)

    def window(self) -> None:
        self.command(0x2A, struct.pack(">HH", 0, 479))
        self.command(0x2B, struct.pack(">HH", 0, 319))
        self.command(0x2C)

    def color(self, r: int, g: int, b: int) -> None:
        self.window()
        self.data(bytes((r, g, b)) * (480 * 320))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spidev", default="/dev/spidev3.0")
    parser.add_argument("--dc", type=int, default=133)
    parser.add_argument("--reset", type=int, default=-1)
    parser.add_argument("--cs", type=int, default=134)
    parser.add_argument("--speed", type=int, default=4000000)
    parser.add_argument("--hold", type=float, default=2.0)
    args = parser.parse_args()

    panel = Ili9488(args.spidev, args.dc, args.reset, args.cs, args.speed)
    panel.init()
    for color in ((0xFC, 0x00, 0x00), (0x00, 0xFC, 0x00), (0x00, 0x00, 0xFC), (0xFC, 0xFC, 0xFC)):
        panel.color(*color)
        print("displayed", color, flush=True)
        time.sleep(args.hold)


if __name__ == "__main__":
    main()
