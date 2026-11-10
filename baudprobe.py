"""Find the baud rate: send only '?' (status query, no motion) and look for a GRBL reply."""
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM12"
for baud in (115200, 250000, 230400, 500000, 921600, 1000000, 57600, 38400, 19200, 9600):
    with serial.Serial(port, baud, timeout=0.1) as s:
        time.sleep(0.3)
        s.reset_input_buffer()
        got = b""
        t0 = time.time()
        while time.time() - t0 < 2.5:
            s.write(b"?")
            time.sleep(0.25)
            got += s.read(512)
        print(baud, repr(got[:120]), flush=True)
        if b"<" in got and b"|" in got:
            print("FOUND", baud)
            break
