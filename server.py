"""MCP server for the Etcher Laser Pro (GRBL 1.2.1 over FTDI USB serial)."""

import os
import re
import time

from mcp.server.mcpserver import MCPServer

from grbl import Grbl, GrblError
import stencil3mf

BAUD = int(os.environ.get("ETCHER_BAUD", "921600"))  # measured on the real machine
PORT = os.environ.get("ETCHER_PORT") or None
DEFAULT_SEEK = 5500  # mm/min, SmartDIYsCreator default seek speed

INSTRUCTIONS = """\
Etcher Laser Pro (diode laser engraver, GRBL 1.2.1) control.
Safety rules for the assistant:
- laser_home, laser_jog, laser_frame and laser_frame_gcode MOVE the head (laser stays off).
  Tell the user before calling them.
- laser_run_gcode and laser_send with laser-on commands FIRE THE LASER. Only call them after the
  user explicitly confirmed in chat for that specific job, and pass confirm=true.
- laser_stop is always safe and should be used immediately if anything looks wrong.
Coordinates are millimetres in work coordinates (origin = the work origin set on the machine)."""

mcp = MCPServer("etcher-laser", instructions=INSTRUCTIONS)
g = Grbl()

LASER_ON_RE = re.compile(r"\b(M3|M4|M03|M04)\b", re.I)
S_RE = re.compile(r"\bS(\d+(?:\.\d+)?)", re.I)


def _ensure():
    if not g.connected:
        g.connect(PORT, BAUD)


def _no_job():
    if g.job.state == "running":
        raise GrblError("ジョブ実行中です。laser_stop で止めるか終了を待ってください。")


def _gcode_lines(path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.split(";", 1)[0]
            line = re.sub(r"\(.*?\)", "", line).strip()
            if line and not line.startswith("%"):
                out.append(line)
    return out


def _analyze(lines):
    """Bounding boxes, laser use, max S.
    bbox/burn_bbox: work-coordinate moves (G90 assumed unless G91 seen).
    machine_bbox/burn_machine_bbox: moves written with G53 (machine coordinates)."""
    absolute, x, y = True, 0.0, 0.0
    xs, ys, burn_xs, burn_ys = [], [], [], []
    mxs, mys, burn_mxs, burn_mys = [], [], [], []
    mprev = None
    max_s, laser_on, mode = 0.0, False, None
    for line in lines:
        u = line.upper()
        if "G91" in u:
            absolute = False
        if "G90" in u:
            absolute = True
        if LASER_ON_RE.search(u):
            laser_on = True
        if re.search(r"\bM0?5\b", u):
            laser_on = False
        m = re.search(r"\bG0?([0-3])\b", u)
        if m:
            mode = int(m.group(1))
        s = S_RE.search(u)
        if s:
            max_s = max(max_s, float(s.group(1)))
        mx = re.search(r"X(-?\d+(?:\.\d*)?)", u)
        my = re.search(r"Y(-?\d+(?:\.\d*)?)", u)
        if (mx or my) and re.search(r"\bG53\b", u):
            if not (mx and my):
                raise GrblError(f"G53の行はXとYを両方指定してください: {line}")
            px, py = float(mx.group(1)), float(my.group(1))
            mxs.append(px)
            mys.append(py)
            if laser_on and mode in (1, 2, 3):
                # the burnt segment starts at the previous point, so count both ends
                burn_mxs += [mprev[0], px] if mprev else [px]
                burn_mys += [mprev[1], py] if mprev else [py]
            mprev = (px, py)
        elif mx or my:
            nx = float(mx.group(1)) if mx else (x if absolute else 0.0)
            ny = float(my.group(1)) if my else (y if absolute else 0.0)
            px, py = x, y
            x, y = (nx, ny) if absolute else (x + nx, y + ny)
            xs.append(x)
            ys.append(y)
            if laser_on and mode in (1, 2, 3):
                burn_xs += [px, x]
                burn_ys += [py, y]
    bb = lambda a, b: [min(a), min(b), max(a), max(b)] if a else None
    return {"bbox": bb(xs, ys), "burn_bbox": bb(burn_xs, burn_ys),
            "machine_bbox": bb(mxs, mys), "burn_machine_bbox": bb(burn_mxs, burn_mys), "max_s": max_s,
            "uses_laser": bool(burn_xs or burn_mxs) or max_s > 0, "lines": len(lines)}


def _work_boxes(info):
    """All boxes of an analysed file in work coordinates (needs WCO from the machine)."""
    wco = g.status.wco
    to_work = lambda b: [b[0] - wco[0], b[1] - wco[1], b[2] - wco[0], b[3] - wco[1]] if b else None
    burn = [b for b in (info["burn_bbox"], to_work(info["burn_machine_bbox"])) if b]
    allb = [b for b in (info["bbox"], to_work(info["machine_bbox"])) if b]
    merge = lambda bs: [min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)] \
        if bs else None
    return merge(burn), merge(allb)


def _check_inside(xmin, ymin, xmax, ymax):
    if not g.status.wco_known:
        raise GrblError("作業座標の基準点(WCO)をまだ受信していません。数秒待ってから再試行してください。")
    tr = g.travel()
    if not tr:
        return
    wco = g.status.wco
    mx0, my0 = xmin + wco[0], ymin + wco[1]
    mx1, my1 = xmax + wco[0], ymax + wco[1]
    # GRBL machine space here is 0..+travel after homing (observed MPos 0,0 at home).
    if mx0 < -0.01 or my0 < -0.01 or mx1 > tr[0] + 0.01 or my1 > tr[1] + 0.01:
        raise GrblError(f"範囲外です: 機械座標 X{mx0:.1f}..{mx1:.1f} Y{my0:.1f}..{my1:.1f} / 可動域 {tr[0]}x{tr[1]}mm")


def _jog_to(x, y, feed):
    g.command(f"$J=G90 G21 X{x:.3f} Y{y:.3f} F{feed:.0f}")
    g.wait_idle(120)


# ---- tools -----------------------------------------------------------------
@mcp.tool()
def laser_connect(port: str = "", baud: int = 0) -> dict:
    """Connect to the laser (auto-detects the FTDI COM port). SmartDIYsCreator must be closed."""
    if g.connected:
        g.disconnect()
        time.sleep(0.3)
    g.connect(port or PORT, baud or BAUD)
    return {"port": g.port, "baud": g.baud, "banner": g.banner, "status": g.status.as_dict(),
            "travel_mm": g.travel(), "laser_max_s": g.settings.get("30"), "laser_mode": g.settings.get("32")}


@mcp.tool()
def laser_disconnect() -> str:
    """Close the serial port so other software (SmartDIYsCreator) can use the machine."""
    g.disconnect()
    return "disconnected"


@mcp.tool()
def laser_status() -> dict:
    """Current machine state (Idle/Run/Hold/Alarm/Home...), positions, feed and laser power."""
    _ensure()
    time.sleep(0.3)
    d = g.status.as_dict()
    d["job"] = vars(g.job) | {"progress_pct": round(100 * g.job.acked / g.job.total, 1) if g.job.total else None}
    d["recent_messages"] = g.recent_lines(10)
    return d


@mcp.tool()
def laser_settings() -> dict:
    """Read GRBL $$ settings (e.g. $30 max S, $32 laser mode, $110/$111 max rate, $130/$131 travel)."""
    _ensure()
    _no_job()
    return g.read_settings()


@mcp.tool()
def laser_home() -> dict:
    """Run the homing cycle ($H). MOVES THE HEAD. Clears alarm state."""
    _ensure()
    _no_job()
    g.command("$H", timeout=90)
    g.wait_idle(30)
    return g.status.as_dict()


@mcp.tool()
def laser_unlock() -> dict:
    """Clear an alarm without homing ($X). Position may be untrusted afterwards."""
    _ensure()
    _no_job()
    g.command("$X", timeout=5)
    return g.status.as_dict()


@mcp.tool()
def laser_jog(x: float, y: float, relative: bool = False, feed: float = DEFAULT_SEEK) -> dict:
    """Move the head with the laser OFF (GRBL jog). Absolute work coordinates unless relative=true."""
    _ensure()
    _no_job()
    if relative:
        wx, wy, _ = g.status.wpos
        tx, ty = wx + x, wy + y
    else:
        tx, ty = x, y
    _check_inside(tx, ty, tx, ty)
    _jog_to(tx, ty, feed)
    return g.status.as_dict()


@mcp.tool()
def laser_go_origin(feed: float = DEFAULT_SEEK) -> dict:
    """Move to work origin X0 Y0 with the laser off (what the vendor app does after homing)."""
    return laser_jog(0, 0, False, feed)


@mcp.tool()
def laser_frame(x_min: float, y_min: float, x_max: float, y_max: float,
                feed: float = 3000, loops: int = 1) -> dict:
    """Trace a rectangle (work coords, mm) with the laser OFF to check placement. MOVES THE HEAD."""
    _ensure()
    _no_job()
    if x_max <= x_min or y_max <= y_min:
        raise GrblError("x_max > x_min, y_max > y_min にしてください")
    _check_inside(x_min, y_min, x_max, y_max)
    for _ in range(max(1, min(loops, 5))):
        for px, py in ((x_min, y_min), (x_max, y_min), (x_max, y_max), (x_min, y_max), (x_min, y_min)):
            _jog_to(px, py, feed)
    return {"framed": [x_min, y_min, x_max, y_max], "status": g.status.as_dict()}


@mcp.tool()
def laser_analyze_gcode(path: str) -> dict:
    """Inspect a G-code file without touching the machine: bounding boxes, max S, laser use."""
    info = _analyze(_gcode_lines(path))
    tr = g.travel() if g.connected else None
    info["machine_travel_mm"] = tr
    return info


@mcp.tool()
def laser_stencil_from_3mf(src_3mf: str, out_gcode: str, power: float = 80, speed: float = 3800,
                           passes: int = 8, kerf: float = 0, outline: bool = False,
                           slot_below: float = 0.45, outline_speed: float = 2200,
                           outline_passes: int = 4, outline_kerf: float = 0.25) -> dict:
    """Make laser G-code from stencil STL(s) placed in OrcaSlicer / Bambu Studio and saved as 3MF
    (no machine access). Set the slicer bed to 450x310 mm. The slicer's front-left 0,0 maps to the
    laser's back-left home: machine X = bed X, machine Y = 310 - bed Y; output uses G53 machine coords.
    Defaults are the Kapton 0.1 mm condition from focused test cuts: 80% / 3800 mm/min / 8 interleaved
    passes; apertures narrower than slot_below mm (USB-C, TSSOP) are cut as one centre line, wider ones
    as outlines. kerf shrinks outlined apertures by kerf/2; outline=true also cuts the stencil outline."""
    text, info = stencil3mf.build_gcode(src_3mf, power, speed, passes, kerf, outline,
                                        bed_h=(g.travel() or (450, 310))[1] if g.connected else 310.0,
                                        slot_below=slot_below, outline_speed=outline_speed,
                                        outline_passes=outline_passes, outline_kerf=outline_kerf)
    with open(out_gcode, "w", encoding="ascii") as f:
        f.write(text)
    info["gcode"] = _analyze(_gcode_lines(out_gcode))
    info["out"] = out_gcode
    return info


@mcp.tool()
def laser_stencil_from_gerber(paste_gerber: str, out_gcode: str, top_left_x: float = -1, top_left_y: float = -1,
                              power: float = 80, speed: float = 3800, passes: int = 8,
                              slot_below: float = 0.45, outline_speed: float = 2200,
                              outline_passes: int = 4, outline_kerf: float = 0.25) -> dict:
    """Make stencil G-code straight from a KiCad paste Gerber (no slicer). The top-left corner of the burn
    area goes to (top_left_x, top_left_y) in MACHINE coordinates; leave both at -1 to use the current head
    position (jog the head to the spot first). Seen from above, the result matches the board's top view."""
    if top_left_x < 0 or top_left_y < 0:
        _ensure()
        time.sleep(0.3)
        top_left_x, top_left_y = g.status.mpos[0], g.status.mpos[1]
    text, info = stencil3mf.build_from_gerber(paste_gerber, (top_left_x, top_left_y), power, speed, passes,
                                              slot_below=slot_below, outline_speed=outline_speed,
                                              outline_passes=outline_passes, outline_kerf=outline_kerf)
    with open(out_gcode, "w", encoding="ascii") as f:
        f.write(text)
    info["gcode"] = _analyze(_gcode_lines(out_gcode))
    info["out"] = out_gcode
    return info


@mcp.tool()
def laser_frame_gcode(path: str, feed: float = 3000) -> dict:
    """Frame the area a G-code file will burn (laser OFF). MOVES THE HEAD."""
    _ensure()
    info = _analyze(_gcode_lines(path))
    burn, allb = _work_boxes(info)
    bb = burn or allb
    if not bb:
        raise GrblError("G-codeにXY移動がありません")
    return laser_frame(*bb, feed=feed) | {"gcode": info}


@mcp.tool()
def laser_run_gcode(path: str, confirm: bool = False, max_s_allowed: float = 0) -> dict:
    """Stream a G-code file to the laser. FIRES THE LASER.
    Only call after the user explicitly approved this exact job in chat; pass confirm=true.
    max_s_allowed: optional cap; refuses the job if any S value exceeds it."""
    if not confirm:
        raise GrblError("レーザー照射を伴います。ユーザーの明示的な承認を得てから confirm=true で呼んでください。")
    _ensure()
    _no_job()
    if g.status.state == "Alarm":
        raise GrblError("アラーム状態です。laser_home で原点復帰してください。")
    lines = _gcode_lines(path)
    info = _analyze(lines)
    if max_s_allowed and info["max_s"] > max_s_allowed:
        raise GrblError(f"S最大値 {info['max_s']} が上限 {max_s_allowed} を超えています")
    _, bb = _work_boxes(info)
    if bb:
        _check_inside(*bb)
    g.start_job(path, lines)
    return {"started": path, "gcode": info}


@mcp.tool()
def laser_pause() -> dict:
    """Feed hold ('!'). The laser turns off while held. Resume with laser_resume."""
    _ensure()
    g.realtime(b"!")
    time.sleep(0.4)
    return g.status.as_dict()


@mcp.tool()
def laser_resume() -> dict:
    """Resume after feed hold ('~')."""
    _ensure()
    g.realtime(b"~")
    time.sleep(0.4)
    return g.status.as_dict()


@mcp.tool()
def laser_stop() -> dict:
    """EMERGENCY-style stop: feed hold then soft reset (same as the vendor app's stop). Always safe."""
    _ensure()
    g.soft_reset()
    time.sleep(0.5)
    return {"banner": g.banner, "status": g.status.as_dict(), "job": g.job.state}


@mcp.tool()
def laser_send(command: str, confirm: bool = False) -> dict:
    """Send one raw GRBL line (e.g. '$I', 'G92 X0 Y0', '$$'). Lines that turn the laser on (M3/M4)
    require the user's explicit approval and confirm=true."""
    _ensure()
    _no_job()
    if LASER_ON_RE.search(command) and not confirm:
        raise GrblError("レーザーONを含むコマンドです。ユーザーの承認を得て confirm=true で呼んでください。")
    before = len(g._log)
    g.command(command, timeout=30)
    time.sleep(0.2)
    return {"reply": [line for _, line in g._log[before:]], "status": g.status.as_dict()}


if __name__ == "__main__":
    mcp.run("stdio")
