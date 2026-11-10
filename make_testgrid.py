"""Power x speed test grid for stencil cutting (GRBL laser mode, $32=1, $30=1000).

Each cell cuts a small square hole outline (like a solder-paste aperture) so the
result shows directly whether the film is cut through and how much the hole grows.
Rows = speed (top row slowest), columns = power. A notch cut above column 1 / row 1
marks the orientation. "Top" means towards the back of the machine (home is the
back-left corner and machine +Y points to the front).

usage: python make_testgrid.py out.gcode [--powers 30,50,70,90] [--speeds 200,400,800,1600]
                               [--size 1.5] [--pitch 6] [--passes 1] [--x0 5] [--y0 5]
"""
import argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--powers", default="30,50,70,90", help="percent of $30 (S max)")
    ap.add_argument("--speeds", default="200,400,800,1600", help="mm/min")
    ap.add_argument("--size", type=float, default=1.5, help="square hole size mm")
    ap.add_argument("--pitch", type=float, default=6.0)
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--passes-list", default="",
                    help="columns vary pass count instead of power, e.g. 2,4,6,8 (uses --power)")
    ap.add_argument("--power", type=float, default=100.0, help="percent, with --passes-list")
    ap.add_argument("--row-powers", default="",
                    help="rows vary power (percent) at fixed --speed instead of varying speed")
    ap.add_argument("--speed", type=float, default=600.0, help="mm/min, with --row-powers")
    ap.add_argument("--x0", type=float, default=5.0, help="work coords of first cell")
    ap.add_argument("--y0", type=float, default=5.0)
    ap.add_argument("--smax", type=float, default=1000.0)
    ap.add_argument("--travel", type=float, default=3000.0)
    a = ap.parse_args()
    speeds = [float(s) for s in a.speeds.split(",")]
    if a.passes_list:
        passes = [int(p) for p in a.passes_list.split(",")]
        powers = [a.power] * len(passes)
    else:
        powers = [float(p) for p in a.powers.split(",")]
        passes = [a.passes] * len(powers)

    g = [f"; test grid powers%={powers} speeds={speeds} size={a.size} passes={passes}",
         "; passes are interleaved: every cell gets pass 1, then pass 2, ... so each spot cools",
         "G21", "G90", "M5", "S0"]
    if a.row_powers:
        row_cfg = [(a.speed, float(p)) for p in a.row_powers.split(",")]
        g[0] = f"; test grid rows=power%{[p for _, p in row_cfg]} at F{a.speed:.0f}, cols=passes {passes}"
    else:
        row_cfg = [(s, None) for s in speeds]
    speeds = [s for s, _ in row_cfg]
    rows = len(row_cfg)
    for p in range(max(passes)):
        for r, (speed, row_pct) in enumerate(row_cfg):
            # machine +Y points to the front (home is back-left), so row 1 at the
            # smallest Y is physically the top row when looking at the machine
            y = a.y0 + r * a.pitch
            for c, pct in enumerate(powers):
                pct = row_pct if row_pct is not None else pct
                if p >= passes[c]:
                    continue
                x = a.x0 + c * a.pitch
                s = round(a.smax * pct / 100)
                g.append(f"; pass{p + 1} row{r + 1} F{speed:.0f} / col{c + 1} {pct:.0f}% S{s} x{passes[c]}")
                g.append(f"G0 X{x:.3f} Y{y:.3f}")
                g.append(f"M4 S{s}")
                for px, py in ((x + a.size, y), (x + a.size, y + a.size), (x, y + a.size), (x, y)):
                    g.append(f"G1 X{px:.3f} Y{py:.3f} F{speed:.0f}")
                g.append("M5")
    # orientation mark: short line above (towards the back of) the top-left cell
    top = a.y0 - 1.5
    mark_pct = row_cfg[-1][1] if row_cfg[-1][1] is not None else powers[len(powers) // 2]
    s_mark = round(a.smax * mark_pct / 100)
    g += [f"G0 X{a.x0:.3f} Y{top:.3f}", f"M4 S{s_mark}", f"G1 X{a.x0 + 3:.3f} Y{top:.3f} F{speeds[0]:.0f}", "M5",
          "S0", f"G0 X0 Y0 F{a.travel:.0f}"]
    with open(a.out, "w", encoding="ascii") as f:
        f.write("\n".join(g) + "\n")
    print(f"wrote {a.out}: {len(powers)}x{len(speeds)} cells")


if __name__ == "__main__":
    main()
