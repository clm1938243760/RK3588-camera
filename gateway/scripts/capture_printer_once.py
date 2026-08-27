#!/usr/bin/env python3
import os
import time
from datetime import datetime
from pathlib import Path

DEVICE = os.environ.get("PRINTER_DEVICE", "/dev/g_printer0")
OUT_DIR = Path(os.environ.get("PRINT_OUT_DIR", "/var/lib/rk3588-gateway/print_jobs"))
IDLE_SECONDS = float(os.environ.get("PRINT_IDLE_SECONDS", "2"))
CHUNK = 65536

OUT_DIR.mkdir(parents=True, exist_ok=True)
path = OUT_DIR / f"manual_print_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.prn"

print(f"waiting for data from {DEVICE}")
fd = os.open(DEVICE, os.O_RDONLY)
total = 0
last = None

with path.open("wb") as handle:
    while True:
        data = os.read(fd, CHUNK)
        now = time.monotonic()
        if data:
            if total == 0:
                print(f"print data started -> {path}")
            handle.write(data)
            handle.flush()
            total += len(data)
            last = now
            continue
        if total and last is not None and now - last >= IDLE_SECONDS:
            break
        time.sleep(0.05)

os.close(fd)
print(f"saved {total} bytes to {path}")
