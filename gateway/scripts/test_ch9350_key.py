#!/usr/bin/env python3
import time

TTY = "/dev/ttyS1"
PREFIX = bytes.fromhex("57 AB 01")


def send(report: bytes) -> None:
    with open(TTY, "wb", buffering=0) as handle:
        handle.write(PREFIX + report)


send(bytes([0, 0, 0x04, 0, 0, 0, 0, 0]))
time.sleep(0.05)
send(bytes(8))
