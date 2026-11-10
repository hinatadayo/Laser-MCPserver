"""Turn stencil objects placed in OrcaSlicer (saved as .3mf) into laser G-code.

OrcaSlicer is only used for placement: the stencil STL (a thin slab with
aperture holes) is arranged on a 450 x 310 mm bed matching the laser's work
area and saved as 3MF. This module reads every printable build item, applies
its transform, slices the mesh at mid-thickness and cuts the resulting loops:
apertures first (all holes get pass 1, then pass 2, ... so each spot cools),
optionally the outer outline last so the stencil comes free.

Coordinates: the slicer bed has 0,0 at the front-left with +Y going to the
back, while the laser homes to its back-left corner (machine 0,0) with +Y
coming towards the front. So machine X = bed X and machine Y = bed_h - bed Y,
and every move is written in machine coordinates (G53) so the result does not
depend on the work origin stored in the machine. What you see from above in
the slicer is what gets cut, seen from above. Always frame before burning.

usage: python stencil3mf.py plate.3mf out.gcode [--power 80] [--speed 3800]
                            [--passes 8] [--slot-below 0.45] [--kerf 0] [--outline] [--bed-h 310]
"""
import argparse
import math
import zipfile
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
P_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
IDENTITY = (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)


# ---- 3MF ------------------------------------------------------------------
def _tf(s):
    if not s:
        return IDENTITY
    v = tuple(float(x) for x in s.split())
    return v if len(v) == 12 else IDENTITY


def _mul(a, b):
    """Compose 3MF transforms: apply a, then b (row vectors, translation last)."""
    A = [[a[0], a[1], a[2]], [a[3], a[4], a[5]], [a[6], a[7], a[8]], [a[9], a[10], a[11]]]
    B = [[b[0], b[1], b[2]], [b[3], b[4], b[5]], [b[6], b[7], b[8]]]
    out = []
    for r in range(3):
        out += [sum(A[r][k] * B[k][c] for k in range(3)) for c in range(3)]
    out += [sum(A[3][k] * B[k][c] for k in range(3)) + b[9 + c] for c in range(3)]
    return tuple(out)


def _apply(t, p):
    x, y, z = p
    return (x * t[0] + y * t[3] + z * t[6] + t[9],
            x * t[1] + y * t[4] + z * t[7] + t[10],
            x * t[2] + y * t[5] + z * t[8] + t[11])


class _Model:
    def __init__(self, zf):
        self.zf = zf
        self.docs = {}

    def doc(self, path):
        path = path.lstrip("/")
        if path not in self.docs:
            self.docs[path] = ET.fromstring(self.zf.read(path))
        return self.docs[path]

    def objects(self, path):
        return {o.get("id"): o for o in self.doc(path).iterfind(".//m:resources/m:object", NS)}

    def triangles(self, path, objid, t, out):
        obj = self.objects(path)[objid]
        mesh = obj.find("m:mesh", NS)
        if mesh is not None:
            verts = [_apply(t, (float(v.get("x")), float(v.get("y")), float(v.get("z"))))
                     for v in mesh.iterfind("m:vertices/m:vertex", NS)]
            for tri in mesh.iterfind("m:triangles/m:triangle", NS):
                out.append((verts[int(tri.get("v1"))], verts[int(tri.get("v2"))], verts[int(tri.get("v3"))]))
        for comp in obj.iterfind("m:components/m:component", NS):
            sub = comp.get(f"{{{P_NS}}}path") or path
            self.triangles(sub, comp.get("objectid"), _mul(_tf(comp.get("transform")), t), out)


def read_3mf(path):
    """Return [(name, triangles)] for each printable build item, in bed coordinates."""
    with zipfile.ZipFile(path) as zf:
        m = _Model(zf)
        root = "3D/3dmodel.model"
        objs = m.objects(root)
        items = []
        for i, item in enumerate(m.doc(root).iterfind("m:build/m:item", NS)):
            if item.get("printable", "1") == "0":
                continue
            tris = []
            m.triangles(root, item.get("objectid"), _tf(item.get("transform")), tris)
            name = objs[item.get("objectid")].get("name") or f"item{i + 1}"
            items.append((name, tris))
        return items


def read_bed(path):
    """Printable area (xmin, ymin, xmax, ymax) from Orca/Bambu project settings, or None."""
    import json
    with zipfile.ZipFile(path) as zf:
        try:
            cfg = json.loads(zf.read("Metadata/project_settings.config"))
        except (KeyError, ValueError):
            return None
    pts = [tuple(float(v) for v in p.split("x")) for p in cfg.get("printable_area", [])]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


# ---- slicing --------------------------------------------------------------
def _key(p):
    return (round(p[0], 5), round(p[1], 5))


def slice_loops(tris, z=None):
    zs = [v[2] for t in tris for v in t]
    zmin, zmax = min(zs), max(zs)
    if z is None:
        z = (zmin + zmax) / 2
    segs = []
    for tri in tris:
        pts = []
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            if (a[2] - z) * (b[2] - z) < 0:
                f = (z - a[2]) / (b[2] - a[2])
                pts.append((a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])))
        if len(pts) == 2 and _key(pts[0]) != _key(pts[1]):
            segs.append((_key(pts[0]), _key(pts[1])))
    adj = {}
    for a, b in segs:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    loops, used = [], set()
    for a, b in segs:
        if (a, b) in used or (b, a) in used:
            continue
        loop, prev, cur = [a], a, b
        used.add((a, b))
        while cur != a:
            loop.append(cur)
            nxt = [n for n in adj[cur] if (cur, n) not in used and (n, cur) not in used]
            if not nxt:
                break
            used.add((cur, nxt[0]))
            prev, cur = cur, nxt[0]
        if cur == a and len(loop) >= 3:
            loops.append(_simplify(loop))
    return loops, zmax - zmin


def _simplify(loop):
    """Drop collinear points (slicing triangulated faces yields many)."""
    out = []
    n = len(loop)
    for i in range(n):
        p0, p1, p2 = loop[i - 1], loop[i], loop[(i + 1) % n]
        cross = (p1[0] - p0[0]) * (p2[1] - p1[1]) - (p1[1] - p0[1]) * (p2[0] - p1[0])
        if abs(cross) > 1e-6:
            out.append(p1)
    return out or loop


def area(loop):
    return sum(loop[i - 1][0] * loop[i][1] - loop[i][0] * loop[i - 1][1] for i in range(len(loop))) / 2


def offset(loop, d):
    """Offset a simple polygon by d (positive = outward) using mitred corners."""
    if not d:
        return loop
    if area(loop) < 0:
        loop = loop[::-1]
    out, n = [], len(loop)
    for i in range(n):
        p0, p1, p2 = loop[i - 1], loop[i], loop[(i + 1) % n]
        e1 = (p1[0] - p0[0], p1[1] - p0[1])
        e2 = (p2[0] - p1[0], p2[1] - p1[1])
        l1, l2 = math.hypot(*e1), math.hypot(*e2)
        n1 = (e1[1] / l1, -e1[0] / l1)
        n2 = (e2[1] / l2, -e2[0] / l2)
        bis = (n1[0] + n2[0], n1[1] + n2[1])
        bl = math.hypot(*bis)
        if bl < 1e-9:
            out.append((p1[0] + n1[0] * d, p1[1] + n1[1] * d))
            continue
        cos_half = (n1[0] * bis[0] + n1[1] * bis[1]) / bl
        k = d / max(cos_half, 0.2)
        out.append((p1[0] + bis[0] / bl * k, p1[1] + bis[1] / bl * k))
    return out


# ---- G-code ---------------------------------------------------------------
def _slot_or_loop(loop, slot_below, shorten, crowded=False):
    """Narrow apertures (short side < slot_below) and apertures crowded by a
    neighbour become one centre-line cut along the long axis: tracing both edges
    of a 0.3 mm pad burnt away the 0.2 mm webs between USB-C pads. Others keep
    their outline."""
    from shapely.geometry import Polygon
    r = Polygon(loop).minimum_rotated_rectangle
    c = list(r.exterior.coords)[:4]
    e1 = (c[1][0] - c[0][0], c[1][1] - c[0][1])
    e2 = (c[2][0] - c[1][0], c[2][1] - c[1][1])
    l1, l2 = math.hypot(*e1), math.hypot(*e2)
    width, length, axis = (l2, l1, e1) if l1 >= l2 else (l1, l2, e2)
    if width >= slot_below and not crowded:
        return loop + loop[:1], "outline"
    ux, uy = axis[0] / length, axis[1] / length
    half = max(length / 2 - shorten, 0.05)
    cx, cy = r.centroid.x, r.centroid.y
    return [(cx - ux * half, cy - uy * half), (cx + ux * half, cy + uy * half)], "slot"


def aperture_paths(holes, slot_below=0.45, shorten=0.1, tight_gap=0.3, outline_kerf=0.0):
    """Cut paths (machine coords) for aperture loops already in machine coords.
    Returns ([(path, kind)], counts); outlined apertures are pulled in by outline_kerf/2."""
    from shapely.geometry import Polygon
    polys = [Polygon(lp) for lp in holes]
    paths, kinds = [], {"slot": 0, "outline": 0}
    for i, lp in enumerate(holes):
        crowded = any(j != i and polys[i].distance(q) < tight_gap for j, q in enumerate(polys)
                      if polys[i].envelope.distance(q.envelope) < tight_gap)
        p, kind = _slot_or_loop(lp, slot_below, shorten, crowded)
        if kind == "outline" and outline_kerf:
            shrunk = polys[i].buffer(-outline_kerf / 2, join_style="mitre")
            if not shrunk.is_empty and shrunk.geom_type == "Polygon":
                pts = list(shrunk.exterior.coords)[:-1]
                p = pts + pts[:1]
        paths.append((p, kind))
        kinds[kind] += 1
    order = _order([p for p, _ in paths])
    lookup = {id(p): k for p, k in paths}
    paths = [(p, lookup[id(p)]) for p in order]
    paths = paths[0::2] + paths[1::2]  # neighbours in the tour are never cut back to back
    return paths, kinds


def build_gcode(path, power=80.0, speed=3800.0, passes=8, kerf=0.0, outline=False, smax=1000.0,
                bed_h=310.0, slot_below=0.45, shorten=0.1, tight_gap=0.3,
                outline_speed=2200.0, outline_passes=4, outline_kerf=0.25):
    items = read_3mf(path)
    if not items:
        raise ValueError("3MFに印刷対象のオブジェクトがありません")
    # Whatever corner the slicer bed origin is at, its back-left corner is the
    # laser's home: machine X = x - bed xmin, machine Y = bed ymax - y.
    bed = read_bed(path) or (0.0, 0.0, 450.0, bed_h)
    bx0, by1 = bed[0], bed[3]
    to_machine = lambda lp: [(x - bx0, by1 - y) for x, y in lp]
    holes, outlines, summary = [], [], []
    for name, tris in items:
        loops, thick = slice_loops(tris)
        if not loops:
            continue
        loops.sort(key=lambda lp: abs(area(lp)), reverse=True)
        outer, inner = loops[0], loops[1:]
        # apertures shrink by half the kerf so the burnt hole lands on size
        holes += [to_machine(offset(lp, -kerf / 2)) for lp in inner]
        outlines.append(to_machine(offset(outer, kerf / 2)))
        m = outlines[-1]
        summary.append({"name": name, "apertures": len(inner), "thickness_mm": round(thick, 3),
                        "machine_bbox": [round(min(p[0] for p in m), 3), round(min(p[1] for p in m), 3),
                                         round(max(p[0] for p in m), 3), round(max(p[1] for p in m), 3)]})
    paths, kinds = aperture_paths(holes, slot_below, shorten, tight_gap, outline_kerf)
    header = [f"; stencil from {path}",
              f"; slicer bed {bed}: machine X = x - {bx0}, machine Y = {by1} - y"]
    text = emit(paths, outlines if outline else [], power, speed, passes, smax, header,
                slot_below, tight_gap, kinds, kerf, outline_speed, outline_passes, outline_kerf)
    return text, {"objects": summary, "bed": bed, "apertures": len(paths), "slots": kinds["slot"],
                  "outlined": kinds["outline"], "S": round(smax * power / 100), "coords": "machine",
                  "speed": speed, "passes": passes, "kerf": kerf, "outline": outline,
                  "outline_speed": outline_speed, "outline_passes": outline_passes, "outline_kerf": outline_kerf}


def build_from_gerber(paste, top_left, power=80.0, speed=3800.0, passes=8, smax=1000.0,
                      slot_below=0.45, shorten=0.1, tight_gap=0.3,
                      outline_speed=2200.0, outline_passes=4, outline_kerf=0.25):
    """Stencil G-code straight from a paste Gerber, placed so the top-left corner of
    the burn area lands on top_left (machine coords, e.g. where the head was jogged to)."""
    from gerber2stencil import parse_paste
    pads = parse_paste(paste)
    # Gerber is Y-up seen from the top; machine Y points to the front: mirror Y.
    holes = [[(x, -y) for x, y in list(p.exterior.coords)[:-1]] for p in pads]
    paths, kinds = aperture_paths(holes, slot_below, shorten, tight_gap, outline_kerf)
    xs = [x for pth, _ in paths for x, _ in pth]
    ys = [y for pth, _ in paths for _, y in pth]
    dx, dy = top_left[0] - min(xs), top_left[1] - min(ys)
    paths = [([(x + dx, y + dy) for x, y in pth], kind) for pth, kind in paths]
    header = [f"; stencil from {paste}", f"; burn area top-left at machine X{top_left[0]:.3f} Y{top_left[1]:.3f}"]
    text = emit(paths, [], power, speed, passes, smax, header, slot_below, tight_gap, kinds, 0.0,
                outline_speed, outline_passes, outline_kerf)
    return text, {"apertures": len(paths), "slots": kinds["slot"], "outlined": kinds["outline"],
                  "machine_burn_bbox": [round(min(xs) + dx, 3), round(min(ys) + dy, 3),
                                        round(max(xs) + dx, 3), round(max(ys) + dy, 3)],
                  "S": round(smax * power / 100), "speed": speed, "passes": passes,
                  "outline_speed": outline_speed, "outline_passes": outline_passes, "outline_kerf": outline_kerf}


def emit(paths, outlines, power, speed, passes, smax, header, slot_below, tight_gap, kinds, kerf,
         outline_speed=1500.0, outline_passes=4, outline_kerf=0.0):
    """paths: [(points, "slot"|"outline")]. Slots run at speed x passes. Outlined apertures run
    slower and fewer times: tiny 0402 outlines never reach 3800 mm/min between corners, so M4
    scaled the power below the cutting threshold (not through); M3 at constant power instead
    burnt big holes at the corners. M4 at an achievable feed keeps full power on the straights
    and still backs off in the corners."""
    s = round(smax * power / 100)
    g = header + [
        f"; power {power}% (S{s}), M4 dynamic; slots {speed} mm/min x{passes}, "
        f"outlines {outline_speed} mm/min x{outline_passes} pulled in {outline_kerf / 2} mm; kerf {kerf}",
        f"; apertures narrower than {slot_below} mm or within {tight_gap} mm of a neighbour cut as one "
        f"centre line ({kinds['slot']}), others as outline ({kinds['outline']})",
        "; MACHINE coordinates (G53); passes interleaved over all apertures so each spot cools",
        "G21", "G90", "M5", "S0"]

    def cut(pts, feed):
        g.append(f"G53 G0 X{pts[0][0]:.3f} Y{pts[0][1]:.3f}")
        g.append(f"M4 S{s}")
        for x, y in pts[1:]:
            g.append(f"G53 G1 X{x:.3f} Y{y:.3f} F{feed:.0f}")
        g.append("M5")

    for p in range(max(passes, outline_passes if kinds["outline"] else 0)):
        g.append(f"; pass {p + 1}")
        for pts, kind in paths:
            if kind == "slot" and p < passes:
                cut(pts, speed)
            elif kind == "outline" and p < outline_passes:
                cut(pts, outline_speed)
    for p in range(passes if outlines else 0):
        g.append(f"; stencil outline pass {p + 1}/{passes}")
        for lp in outlines:
            cut(lp + lp[:1], outline_speed)
    g += ["S0"]
    return "\n".join(g) + "\n"


def _order(loops):
    """Nearest-neighbour ordering to keep travel short."""
    rest, out, cur = list(loops), [], (0.0, 0.0)
    while rest:
        i = min(range(len(rest)), key=lambda k: math.dist(cur, rest[k][0]))
        out.append(rest.pop(i))
        cur = out[-1][0]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--power", type=float, default=80.0)
    ap.add_argument("--speed", type=float, default=3800.0)
    ap.add_argument("--passes", type=int, default=8)
    ap.add_argument("--slot-below", type=float, default=0.45,
                    help="apertures narrower than this (mm) are cut as a single centre line")
    ap.add_argument("--kerf", type=float, default=0.0, help="mm; apertures shrink by kerf/2")
    ap.add_argument("--outline", action="store_true", help="also cut the stencil outline (last)")
    ap.add_argument("--bed-h", type=float, default=310.0, help="slicer bed depth = machine Y travel")
    a = ap.parse_args()
    text, info = build_gcode(a.src, a.power, a.speed, a.passes, a.kerf, a.outline, bed_h=a.bed_h,
                             slot_below=a.slot_below)
    with open(a.out, "w", encoding="ascii") as f:
        f.write(text)
    print(info)


if __name__ == "__main__":
    main()
