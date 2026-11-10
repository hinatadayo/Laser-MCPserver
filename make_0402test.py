"""0402 aperture test: the real 0402 pad pairs from a paste Gerber, laid out in a
row, cut as outlines with several (pull-in, passes, speed) variants side by side.

Each variant gets 1, 2, 3... tick marks above it (towards the back). Output is in
machine coordinates (G53); x0/y0 is the top-left corner of the whole test.

usage: python make_0402test.py paste.gtp out.gcode --x0 65.66 --y0 124.63
         [--variants 0.10:3:1500,0.15:3:1500,0.10:4:2500]   (per side pull-in mm : passes : mm/min)
"""
import argparse

from shapely import affinity

from gerber2stencil import parse_paste

SIZES_0402 = {(0.54, 0.64), (0.56, 0.62)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paste")
    ap.add_argument("out")
    ap.add_argument("--x0", type=float, required=True)
    ap.add_argument("--y0", type=float, required=True)
    ap.add_argument("--variants", default="0.10:3:1500,0.15:3:1500,0.10:4:2500")
    ap.add_argument("--power", type=float, default=80)
    ap.add_argument("--smax", type=float, default=1000)
    ap.add_argument("--pitch", type=float, default=2.5, help="mm between parts in a row")
    ap.add_argument("--gap", type=float, default=4.0, help="mm between variants")
    a = ap.parse_args()
    variants = [tuple(float(x) for x in v.split(":")) for v in a.variants.split(",")]

    pads = []
    for p in parse_paste(a.paste):
        x0, y0, x1, y1 = p.bounds
        if (round(min(x1 - x0, y1 - y0), 2), round(max(x1 - x0, y1 - y0), 2)) in SIZES_0402:
            pads.append(affinity.scale(p, 1, -1, origin=(0, 0)))  # Gerber Y-up -> machine Y-down
    # pair each pad with its nearest neighbour = the two pads of one part
    pairs, used = [], set()
    for i, p in enumerate(pads):
        if i in used:
            continue
        j = min((k for k in range(len(pads)) if k != i and k not in used), key=lambda k: p.distance(pads[k]))
        used |= {i, j}
        pairs.append((p, pads[j]))
    if len(pairs) != 5:
        raise SystemExit(f"expected 5 parts (10 pads), found {len(pairs)} pairs")

    # normalise every part to the same orientation (pads side by side along X)
    parts = []
    for p, q in pairs:
        c = p.union(q)
        bx0, by0, bx1, by1 = c.bounds
        if by1 - by0 > bx1 - bx0:
            p = affinity.rotate(p, 90, origin=c.centroid)
            q = affinity.rotate(q, 90, origin=c.centroid)
            c = p.union(q)
        cx, cy = c.centroid.x, c.centroid.y
        parts.append([affinity.translate(s, -cx, -cy) for s in (p, q)])
    part_w = max(s.bounds[2] for pq in parts for s in pq) * 2
    part_h = max(s.bounds[3] for pq in parts for s in pq) * 2
    row_w = (len(parts) - 1) * a.pitch + part_w

    s = round(a.smax * a.power / 100)
    groups, marks = [], []
    for vi, (pull, passes, feed) in enumerate(variants):
        ox = a.x0 + vi * (row_w + a.gap) + part_w / 2
        oy = a.y0 + 2.0 + part_h / 2  # 2 mm above for tick marks
        loops = []
        for pi, pq in enumerate(parts):
            for pad in pq:
                q = affinity.translate(pad, ox + pi * a.pitch, oy).buffer(-pull, join_style="mitre")
                pts = list(q.exterior.coords)[:-1]
                loops.append(pts + pts[:1])
        groups.append((int(passes), feed, loops[0::2] + loops[1::2]))
        for t in range(vi + 1):
            mx = a.x0 + vi * (row_w + a.gap) + t * 1.0
            marks.append([(mx, a.y0), (mx, a.y0 + 0.8)])

    g = [f"; 0402 test: variants (pull-in per side mm, passes, mm/min) = {variants}, M4 S{s}",
         "; 1,2,3.. ticks above each group = variant order; MACHINE coordinates (G53)",
         "G21", "G90", "M5", "S0"]
    for p in range(max(v[0] for v in groups)):
        g.append(f"; pass {p + 1}")
        for passes, feed, loops in groups:
            if p >= passes:
                continue
            for lp in loops:
                g.append(f"G53 G0 X{lp[0][0]:.3f} Y{lp[0][1]:.3f}")
                g.append(f"M4 S{s}")
                for x, y in lp[1:]:
                    g.append(f"G53 G1 X{x:.3f} Y{y:.3f} F{feed:.0f}")
                g.append("M5")
    for (x0, y0), (x1, y1) in marks:
        for _ in range(2):
            g += [f"G53 G0 X{x0:.3f} Y{y0:.3f}", f"M4 S{s}", f"G53 G1 X{x1:.3f} Y{y1:.3f} F3800", "M5"]
    g.append("S0")
    with open(a.out, "w", encoding="ascii") as f:
        f.write("\n".join(g) + "\n")
    print({"parts_per_variant": len(parts), "variants": variants, "part_gap_mm":
           round(min(pq[0].distance(pq[1]) for pq in parts), 3),
           "machine_area": [a.x0, a.y0, round(a.x0 + len(variants) * row_w + (len(variants) - 1) * a.gap, 2),
                            round(a.y0 + 2 + part_h, 2)]})


if __name__ == "__main__":
    main()
