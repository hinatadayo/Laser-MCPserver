"""KiCad paste-layer Gerber -> solder paste stencil (STL slab with apertures + SVG).

Supports what KiCad 8 writes for paste layers: %FS/%MO, apertures C, R, O and
the KiCad "RoundRect" macro, flashed with D03. Anything else (draws with D01,
G36 regions, other macros, clear polarity) raises an error instead of being
silently dropped, so a stencil can never quietly miss pads.

The slab covers the pads' bounding box plus --margin (default 8 mm). Output is
in Gerber coordinates (Y up), seen from the top of the board, so the STL lines
up with the board in OrcaSlicer and in stencil3mf.py.

usage: python gerber2stencil.py paste.gtp out_basename [--thickness 0.1]
                                [--margin 8] [--shrink 0] [--outline edge.gm1]
"""
import argparse
import re

from shapely import affinity
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

ROUNDRECT_MACRO = "RoundRect"


class GerberError(ValueError):
    pass


def _num(s, fmt):
    neg = s.startswith("-")
    s = s.lstrip("+-")
    v = int(s) / 10 ** fmt
    return -v if neg else v


def aperture_shape(kind, params, macros):
    p = [float(x) for x in params]
    if kind == "C":
        return Point(0, 0).buffer(p[0] / 2, quad_segs=8)
    if kind == "R":
        return box(-p[0] / 2, -p[1] / 2, p[0] / 2, p[1] / 2)
    if kind == "O":
        w, h = p[0], p[1]
        r = min(w, h) / 2
        return box(-w / 2 + r, -h / 2 + r, w / 2 - r, h / 2 - r).buffer(r, quad_segs=8) if w != h \
            else Point(0, 0).buffer(r, quad_segs=8)
    if kind == ROUNDRECT_MACRO and kind in macros:
        r = p[0]
        corners = [(p[1], p[2]), (p[3], p[4]), (p[5], p[6]), (p[7], p[8])]
        shape = Polygon(corners).buffer(r, quad_segs=4, join_style="round")
        if len(p) > 9 and p[9]:
            shape = affinity.rotate(shape, p[9], origin=(0, 0))
        return shape
    raise GerberError(f"未対応のアパーチャ形状です: {kind} {params}")


def parse_paste(path):
    text = open(path, encoding="ascii", errors="replace").read()
    fmt, unit_scale = 6, 1.0
    macros, apertures, pads = set(), {}, []
    cur, x, y = None, 0.0, 0.0
    for m in re.finditer(r"%([^%]*)%|([^%*]+)\*", text, re.S):
        if m.group(1) is not None:
            block = m.group(1).replace("\n", "").replace("\r", "")
            if block.startswith("FS"):
                fm = re.search(r"X(\d)(\d)", block)
                fmt = int(fm.group(2))
            elif block.startswith("MO"):
                unit_scale = 25.4 if "IN" in block else 1.0
            elif block.startswith("AM"):
                macros.add(block[2:].split("*", 1)[0])
            elif block.startswith("AD"):
                am = re.match(r"ADD(\d+)([A-Za-z_][\w.]*)(?:,(.*))?\*?$", block)
                if not am:
                    raise GerberError(f"アパーチャ定義を読めません: {block}")
                params = [v for v in (am.group(3) or "").rstrip("*").split("X") if v]
                apertures[int(am.group(1))] = aperture_shape(am.group(2), params, macros)
            elif block.startswith("LP") and "C" in block:
                raise GerberError("クリア極性(LPC)は未対応です")
            continue
        cmd = m.group(2).strip()
        if not cmd or cmd.startswith("G04"):
            continue
        if cmd in ("G36", "G37"):
            raise GerberError("塗りつぶし領域(G36/G37)は未対応です")
        dm = re.fullmatch(r"(?:G0?1)?D(\d+)", cmd)
        if dm and int(dm.group(1)) >= 10:
            cur = int(dm.group(1))
            continue
        cm = re.fullmatch(r"(?:X([+-]?\d+))?(?:Y([+-]?\d+))?(?:I[+-]?\d+)?(?:J[+-]?\d+)?D0?([123])", cmd)
        if cm:
            if cm.group(1):
                x = _num(cm.group(1), fmt) * unit_scale
            if cm.group(2):
                y = _num(cm.group(2), fmt) * unit_scale
            op = cm.group(3)
            if op == "3":
                if cur not in apertures:
                    raise GerberError(f"未定義のアパーチャ D{cur}")
                pads.append(affinity.translate(apertures[cur], x, y))
            elif op == "1":
                raise GerberError("線の描画(D01)は未対応です。パッド以外の要素がペースト層にあります")
            continue
        if cmd in ("G01", "G02", "G03", "G75", "M02", "G74"):
            continue
        raise GerberError(f"未対応のコマンドです: {cmd}")
    return pads


def board_outline(path):
    """Rough board outline from Edge_Cuts (line endpoints only) as a bbox."""
    xs, ys, fmt = [], [], 6
    for m in re.finditer(r"X([+-]?\d+)Y([+-]?\d+)", open(path, encoding="ascii", errors="replace").read()):
        xs.append(_num(m.group(1), fmt))
        ys.append(_num(m.group(2), fmt))
    return min(xs), min(ys), max(xs), max(ys)


def extrude(slab, thickness):
    """Extrude a polygon with holes into a closed mesh.

    trimesh's earcut path derives the side walls from the cap triangulation,
    and with apertures 0.2 mm apart (USB-C) it bridged neighbouring holes.
    Here the caps come from a constrained Delaunay triangulation and the walls
    are built straight from the polygon rings, so every aperture keeps its own wall.
    """
    import shapely
    import trimesh
    from shapely.geometry.polygon import orient

    slab = orient(slab, 1.0)  # exterior CCW, holes CW: solid always on the left
    verts, faces = [], []

    def vid(x, y, z):
        verts.append((x, y, z))
        return len(verts) - 1

    for tri in shapely.constrained_delaunay_triangles(slab).geoms:
        t = orient(tri, 1.0)
        (ax, ay), (bx, by), (cx, cy) = list(t.exterior.coords)[:3]
        faces.append((vid(ax, ay, thickness), vid(bx, by, thickness), vid(cx, cy, thickness)))
        faces.append((vid(ax, ay, 0), vid(cx, cy, 0), vid(bx, by, 0)))
    for ring in [slab.exterior, *slab.interiors]:
        pts = list(ring.coords)[:-1]
        for i, (px, py) in enumerate(pts):
            qx, qy = pts[(i + 1) % len(pts)]
            p0, q0 = vid(px, py, 0), vid(qx, qy, 0)
            q1, p1 = vid(qx, qy, thickness), vid(px, py, thickness)
            faces += [(p0, q0, q1), (p0, q1, p1)]
    return trimesh.Trimesh(vertices=verts, faces=faces, process=True)


def count_slice_loops(mesh):
    """Independent check: number of closed loops at mid-thickness."""
    from stencil3mf import slice_loops
    loops, _ = slice_loops([tuple(map(tuple, t)) for t in mesh.triangles])
    return len(loops)


def build(paste, out, thickness=0.1, margin=8.0, shrink=0.0, outline=None):
    pads = parse_paste(paste)
    if not pads:
        raise GerberError("パッドがありません")
    holes = unary_union([p.buffer(-shrink, join_style="mitre") if shrink else p for p in pads])
    hx0, hy0, hx1, hy1 = holes.bounds
    if outline:
        sx0, sy0, sx1, sy1 = board_outline(outline)
    else:
        sx0, sy0, sx1, sy1 = hx0 - margin, hy0 - margin, hx1 + margin, hy1 + margin
    slab = box(sx0, sy0, sx1, sy1).difference(holes)
    mesh = extrude(slab, thickness)
    geoms = list(holes.geoms) if hasattr(holes, "geoms") else [holes]
    loops = count_slice_loops(mesh)
    if loops != len(geoms) + 1 or not mesh.is_watertight:
        raise GerberError(f"STLの検証に失敗: 穴 {len(geoms)} 個に対し断面ループ {loops} 個 / watertight={mesh.is_watertight}")
    mesh.export(out + ".stl")
    w, h = sx1 - sx0, sy1 - sy0
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.3f}mm" height="{h:.3f}mm" '
           f'viewBox="{sx0:.4f} {-sy1:.4f} {w:.4f} {h:.4f}">',
           f'<rect x="{sx0:.4f}" y="{-sy1:.4f}" width="{w:.4f}" height="{h:.4f}" fill="none" stroke="red" stroke-width="0.05"/>']
    for g in geoms:
        pts = " ".join(f"{px:.4f},{-py:.4f}" for px, py in g.exterior.coords)
        svg.append(f'<polygon points="{pts}" fill="none" stroke="black" stroke-width="0.02"/>')
    svg.append("</svg>")
    open(out + ".svg", "w", encoding="ascii").write("\n".join(svg))
    sizes = {}
    for p in pads:
        bx0, by0, bx1, by1 = p.bounds
        k = f"{min(bx1 - bx0, by1 - by0):.2f}x{max(bx1 - bx0, by1 - by0):.2f}"
        sizes[k] = sizes.get(k, 0) + 1
    return {"pads": len(pads), "apertures_after_merge": len(geoms), "pad_sizes_mm": sizes,
            "slab_mm": [round(sx0, 3), round(sy0, 3), round(sx1, 3), round(sy1, 3)],
            "smallest_gap_mm": _min_gap(geoms), "watertight": bool(mesh.is_watertight),
            "slice_loops_check": loops,
            "stl": out + ".stl", "svg": out + ".svg"}


def _min_gap(geoms):
    best = None
    for i, a in enumerate(geoms):
        for b in geoms[i + 1:]:
            if a.envelope.distance(b.envelope) < 2:
                d = a.distance(b)
                best = d if best is None else min(best, d)
    return round(best, 3) if best is not None else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paste")
    ap.add_argument("out")
    ap.add_argument("--thickness", type=float, default=0.1)
    ap.add_argument("--margin", type=float, default=8.0)
    ap.add_argument("--shrink", type=float, default=0.0, help="shrink each aperture by this many mm per side")
    ap.add_argument("--outline", help="Edge_Cuts gerber: use the board bbox as slab instead of pads+margin")
    a = ap.parse_args()
    print(build(a.paste, a.out, a.thickness, a.margin, a.shrink, a.outline))


if __name__ == "__main__":
    main()
