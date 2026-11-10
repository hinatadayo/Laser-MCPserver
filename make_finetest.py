"""Fine-pitch aperture test: the real USB-C and CH552 (TSSOP-20) paste pads from a
Gerber, cut several times side by side with different kerf compensation.

Each variant gets 1, 2, 3... short tick marks above it (towards the back of the
machine) so the kerf can be read off the film. Output is in machine
coordinates (G53); x0/y0 is the top-left corner of the first variant.

usage: python make_finetest.py paste.gtp out.gcode --x0 113 --y0 45
                               [--kerfs 0,0.05,0.10] [--speed 3000] [--passes 10] [--power 100]
"""
import argparse
import math

from shapely import affinity
from shapely.ops import unary_union

from gerber2stencil import parse_paste

FINE_SIZES = {(0.30, 1.45), (0.60, 1.45), (0.36, 1.74)}  # USB-C, USB-C, TSSOP-20


def fine_pads(paste):
    out = []
    for p in parse_paste(paste):
        x0, y0, x1, y1 = p.bounds
        size = (round(min(x1 - x0, y1 - y0), 2), round(max(x1 - x0, y1 - y0), 2))
        if size in FINE_SIZES:
            out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paste")
    ap.add_argument("out")
    ap.add_argument("--x0", type=float, required=True, help="machine X of the top-left corner")
    ap.add_argument("--y0", type=float, required=True, help="machine Y of the top-left corner")
    ap.add_argument("--kerfs", default="0,0.05,0.10", help="outline mode: one variant per kerf")
    ap.add_argument("--mode", choices=["outline", "centerline"], default="outline",
                    help="centerline: one cut along the long axis of each pad (narrow slot, less heat)")
    ap.add_argument("--variants", default="", help="centerline mode: power%%:passes per variant, e.g. 100:13,80:16,60:21")
    ap.add_argument("--shorten", type=float, default=0.1, help="centerline: trim each end by this (beam radius)")
    ap.add_argument("--gap", type=float, default=4.0, help="mm between variants")
    ap.add_argument("--speed", type=float, default=3000)
    ap.add_argument("--passes", type=int, default=10)
    ap.add_argument("--power", type=float, default=100)
    ap.add_argument("--smax", type=float, default=1000)
    a = ap.parse_args()
    if a.mode == "centerline":
        variants = [tuple(float(x) for x in v.split(":")) for v in a.variants.split(",")]
    else:
        variants = [(a.power, a.passes)] * len(a.kerfs.split(","))
    kerfs = [float(k) for k in a.kerfs.split(",")] if a.mode == "outline" else [0.0] * len(variants)

    pads = fine_pads(a.paste)
    if len(pads) != 32:
        raise SystemExit(f"USB-C 12 + CH552 20 = 32 pads expected, found {len(pads)}")
    # Gerber is Y-up; machine Y points to the front, so mirror Y to keep the top view.
    pads = [affinity.scale(p, 1, -1, origin=(0, 0)) for p in pads]
    gx0, gy0, gx1, gy1 = unary_union(pads).bounds
    width = gx1 - gx0

    def centerline(p):
        r = p.minimum_rotated_rectangle
        c = list(r.exterior.coords)[:4]
        e1 = (c[1][0] - c[0][0], c[1][1] - c[0][1])
        e2 = (c[2][0] - c[1][0], c[2][1] - c[1][1])
        long_e = e1 if math.hypot(*e1) >= math.hypot(*e2) else e2
        length = math.hypot(*long_e)
        ux, uy = long_e[0] / length, long_e[1] / length
        half = max(length / 2 - a.shorten, 0.05)
        cx, cy = r.centroid.x, r.centroid.y
        return [(cx - ux * half, cy - uy * half), (cx + ux * half, cy + uy * half)]

    # every other pad first, then the rest, so neighbours are never cut back to back
    pads.sort(key=lambda p: (round(p.centroid.y, 1), p.centroid.x))
    pads = pads[0::2] + pads[1::2]

    groups, marks = [], []  # groups[i] = list of open/closed paths for variant i
    for i, k in enumerate(kerfs):
        ox = a.x0 + i * (width + a.gap) - gx0
        oy = a.y0 + 2.0 - gy0  # leave 2 mm above for the tick marks
        paths = []
        for p in pads:
            q = affinity.translate(p.buffer(-k / 2, join_style="mitre") if k else p, ox, oy)
            if a.mode == "centerline":
                paths.append(centerline(q))
            else:
                pts = list(q.exterior.coords)[:-1]
                paths.append(pts + pts[:1])
        groups.append(paths)
        for t in range(i + 1):
            mx = a.x0 + i * (width + a.gap) + t * 1.0
            marks.append([(mx, a.y0), (mx, a.y0 + 0.8)])

    g = [f"; fine-pitch test: USB-C + CH552 pads, mode {a.mode}, (1,2,3.. ticks = variant order)",
         f"; variants (power%, passes): {variants}  kerfs: {kerfs}  speed {a.speed}  MACHINE coordinates (G53)",
         "; pads cut every-other so neighbours cool; variants interleaved per pass",
         "G21", "G90", "M5", "S0"]
    for p in range(int(max(v[1] for v in variants))):
        g.append(f"; pass {p + 1}")
        for (pct, npass), paths in zip(variants, groups):
            if p >= npass:
                continue
            s = round(a.smax * pct / 100)
            for path in paths:
                g.append(f"G53 G0 X{path[0][0]:.3f} Y{path[0][1]:.3f}")
                g.append(f"M4 S{s}")
                for x, y in path[1:]:
                    g.append(f"G53 G1 X{x:.3f} Y{y:.3f} F{a.speed:.0f}")
                g.append("M5")
    s_mark = round(a.smax * variants[0][0] / 100)
    for (x0, y0), (x1, y1) in marks:  # ticks: 2 passes are enough to mark
        for _ in range(2):
            g += [f"G53 G0 X{x0:.3f} Y{y0:.3f}", f"M4 S{s_mark}", f"G53 G1 X{x1:.3f} Y{y1:.3f} F{a.speed:.0f}", "M5"]
    g.append("S0")
    with open(a.out, "w", encoding="ascii") as f:
        f.write("\n".join(g) + "\n")
    total_w = len(kerfs) * width + (len(kerfs) - 1) * a.gap
    print({"pads_per_variant": len(pads), "mode": a.mode, "variants": variants, "kerfs": kerfs,
           "machine_area": [a.x0, a.y0, round(a.x0 + total_w, 2), round(a.y0 + 2 + gy1 - gy0, 2)]})


if __name__ == "__main__":
    main()
