"""Motion check with the laser OFF: home -> work origin -> 20 mm frame -> stop."""
import time

import server


def show(tag, st):
    print(f"{time.strftime('%H:%M:%S')} {tag}: {st['state']} M{st['machine_pos'][:2]} W{st['work_pos'][:2]}", flush=True)


server.laser_connect()
try:
    show("connected", server.laser_status())
    t = time.time()
    show(f"home", server.laser_home())
    print(f"  homing took {time.time() - t:.1f}s")
    show("origin", server.laser_go_origin())
    t = time.time()
    r = server.laser_frame(0, 0, 20, 20, feed=3000)
    show(f"frame {r['framed']}", r["status"])
    print(f"  frame took {time.time() - t:.1f}s")
    r = server.laser_stop()
    print("stop banner:", r["banner"], "job:", r["job"])
    time.sleep(1)
    show("after stop", server.laser_status())
    print("recent:", server.g.recent_lines(8))
finally:
    server.laser_disconnect()
