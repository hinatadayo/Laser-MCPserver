"""Raw serial diagnostics (no motion): listen, '?', XON, newline, soft reset."""
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM12"
baud = int(sys.argv[2]) if len(sys.argv) > 2 else 921600


def listen(s, sec):
    t0, got = time.time(), b""
    while time.time() - t0 < sec:
        got += s.read(512)
    return got


with serial.Serial(port, baud, timeout=0.1) as s:
    print("DSR/CTS/CD:", s.dsr, s.cts, s.cd)
    print("idle listen :", repr(listen(s, 2.0)[:200]), flush=True)
    for name, data in (("?", b"?"), ("XON+?", b"\x11?"), ("newline", b"\r\n"), ("$I", b"$I\n"),
                       ("soft reset", b"\x18"), ("? after reset", b"?")):
        s.write(data)
        print(f"{name:14}:", repr(listen(s, 1.5)[:200]), flush=True)
