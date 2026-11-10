"""Read-only check: connect, status, settings. Does not move the machine."""
import json
import sys

import server

info = server.laser_connect(sys.argv[1] if len(sys.argv) > 1 else "", int(sys.argv[2]) if len(sys.argv) > 2 else 0)
print(json.dumps(info, ensure_ascii=False, indent=1))
st = server.laser_status()
print(json.dumps({k: st[k] for k in ("state", "machine_pos", "work_pos", "work_offset")}, ensure_ascii=False))
print(json.dumps(server.g.settings, ensure_ascii=False))
server.laser_disconnect()
