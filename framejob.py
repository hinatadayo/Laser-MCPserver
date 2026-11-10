"""Home, then frame the burn area of a G-code file with the laser OFF.

usage: python framejob.py jobs/file.gcode [loops]
"""
import sys

import server

path = sys.argv[1]
loops = int(sys.argv[2]) if len(sys.argv) > 2 else 2
server.laser_connect()
try:
    server.laser_home()
    st = server.g.status
    print("homed: M", st.mpos[:2], "WCO", st.wco[:2], flush=True)
    info = server._analyze(server._gcode_lines(path))
    burn, _ = server._work_boxes(info)
    print("burn area machine:", info["burn_machine_bbox"] or "-", " work:", [round(v, 3) for v in burn], flush=True)
    r = server.laser_frame(*burn, feed=3000, loops=loops)
    print("framed (laser off), loops:", loops, "final M", server.g.status.mpos[:2], flush=True)
finally:
    server.laser_disconnect()
