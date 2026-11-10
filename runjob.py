"""Home, frame a G-code file (laser off), burn it, wait for completion, home again.

usage: python runjob.py jobs/file.gcode [max_s_allowed]
"""
import sys
import time

import server

path = sys.argv[1]
max_s = float(sys.argv[2]) if len(sys.argv) > 2 else 0

server.laser_connect()
try:
    st = server.laser_status()
    print("connected:", st["state"], "W", st["work_pos"][:2], flush=True)
    # The firmware reports MPos 0,0 after power-on without homing, so the
    # position is only trustworthy after $H. Home every time, like the vendor app.
    print("homing", flush=True)
    server.laser_home()
    server.laser_go_origin()
    r = server.laser_frame_gcode(path)
    print("framed (laser off):", r["framed"], flush=True)
    r = server.laser_run_gcode(path, confirm=True, max_s_allowed=max_s)
    print("started:", r["gcode"], flush=True)
    t0 = time.time()
    while server.g.job.state == "running":
        time.sleep(1)
        j = server.g.job
        print(f"  {time.time() - t0:5.1f}s  {j.acked}/{j.total}  {server.g.status.state} S{server.g.status.spindle:.0f}", flush=True)
    j = server.g.job
    print("job:", j.state, "errors:", j.errors, f"time {j.finished - j.started:.1f}s", flush=True)
    if j.state == "done":
        server.laser_home()  # park at home after every finished job
        print("homed after job", flush=True)
except BaseException as e:
    print("ERROR -> stopping:", e, flush=True)
    server.laser_stop()
    raise
finally:
    print("final:", server.laser_status()["state"], flush=True)
    server.laser_disconnect()
