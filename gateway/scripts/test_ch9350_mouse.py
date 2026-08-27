#!/usr/bin/env python3
import time

TTY = "/dev/ttyS1"
SET_STATE3 = bytes.fromhex("57 AB 40 03")
PREFIX = bytes.fromhex("57 AB 04")
SCREEN_W = 1920
SCREEN_H = 1080


def px_to_abs_x(x: int) -> int:
    return max(0, min(1023, int(x * 1023 / (SCREEN_W - 1))))


def px_to_abs_y(y: int) -> int:
    return max(0, min(1023, int(y * 1023 / (SCREEN_H - 1))))


def send_raw(data: bytes) -> None:
    with open(TTY, "wb", buffering=0) as handle:
        handle.write(data)


def send(button: int, x: int, y: int, wheel: int = 0) -> None:
    ax = px_to_abs_x(x)
    ay = px_to_abs_y(y)
    report = bytes([0x01, button & 7, ax & 255, (ax >> 8) & 255, ay & 255, (ay >> 8) & 255, wheel & 255])
    send_raw(PREFIX + report)


send_raw(SET_STATE3)
time.sleep(0.2)
send(0, 300, 300)
time.sleep(0.1)
send(1, 300, 300)
time.sleep(0.08)
send(0, 300, 300)
