#!/usr/bin/env python3
"""GitHub contribution graph -> Conway's Game of Life animated SVG.

stdlib only. Meant to run unchanged inside GitHub Actions.

  python glife.py fetch  --login USER --out contrib.json
  python glife.py stats  --data contrib.json
  python glife.py render --data contrib.json --out dist/

One loop is: hold the real graph, fade out any levels too thin to seed with,
run `gens` generations of Conway on what is left, cut back to the graph.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Optional
from datetime import date, timedelta

ROWS = 7

LEVELS = {
    "NONE": 0,
    "FIRST_QUARTILE": 1,
    "SECOND_QUARTILE": 2,
    "THIRD_QUARTILE": 3,
    "FOURTH_QUARTILE": 4,
}

PALETTES = {
    "light": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
    "dark": ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"],
}
LABEL_COLOR = {"light": "#57606a", "dark": "#8b949e"}

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

COLOR_MODES = ("hybrid", "density", "age", "gene")


def _bars() -> str:
    """block characters, or ASCII where the console cannot encode them"""
    blocks = "▁▂▃▄▅▆▇█"
    try:
        blocks.encode(sys.stdout.encoding or "utf-8")
        return blocks
    except (UnicodeEncodeError, LookupError, TypeError):
        return ".:-=+*#@"


BARS = _bars()


@dataclass
class Config:
    # rule. B3/S23 is Conway; see README for ones that survive a fuller board.
    birth: set = field(default_factory=lambda: {3})
    survive: set = field(default_factory=lambda: {2, 3})

    # wrap the edges. None tries both and keeps whichever lasts longer, which
    # is worth doing on `split` - there the wrap folds the two bands into each
    # other and dead edges run three times as long.
    torus: Optional[bool] = True

    # timeline. gens 0 means "as many as the board survives", see autotune().
    gens: int = 0
    hold: int = 5           # frames the real graph is held before Life starts
    fade: int = 2           # frames the dropped levels fade out over
    frame_ms: int = 150

    # only contribution levels >= this count as alive. 1 is the graph as it is;
    # raising it thins out a board too full to sustain Life. 0 picks whichever
    # threshold runs longest. Whatever is dropped fades out on screen first, so
    # nothing happens off camera.
    seed_level: int = 0

    # board shape: calendar 53x7, split two 27x7 bands, square 19x19
    layout: str = "calendar"

    # colour
    color: str = "hybrid"
    age_w: int = 1
    gene_w: int = 2
    dens_w: int = 1


def thin(seed, level):
    """keep only cells at or above `level`"""
    return [[v if v >= level else 0 for v in row] for row in seed]


# --------------------------------------------------------------------------
# board shape
# --------------------------------------------------------------------------

LAYOUTS = ("calendar", "split", "square")
SQUARE = 19          # 19x19 = 361 of the year's 371 slots


@dataclass
class Layout:
    """the board Life runs on, plus how to label it"""
    grid: list
    band: int                                   # rows per band
    months: list = field(default_factory=list)  # (col, row, text, side)
    weekdays: bool = True
    pad_l: int = 30


def build_layout(seed, starts, mode="calendar") -> Layout:
    rows, cols = len(seed), len(seed[0])

    def month_marks(xs, row, side):
        """first column of each month along a strip of week indices"""
        seen, out = set(), []
        for i, x in enumerate(xs):
            d = starts[x]
            if d.month not in seen and d.day <= 7 and i < len(xs) - 2:
                seen.add(d.month)
                out.append((i, row, MONTHS[d.month - 1], side))
        return out

    if mode == "calendar":
        return Layout(grid=[row[:] for row in seed], band=ROWS,
                      months=month_marks(range(cols), 0, "above"))

    if mode == "split":
        half = (cols + 1) // 2
        grid = [[0] * half for _ in range(ROWS * 2)]
        for x in range(cols):
            bx, band = (x, 0) if x < half else (x - half, 1)
            for y in range(ROWS):
                grid[band * ROWS + y][bx] = seed[y][x]
        # the bands are flush, so the lower one is labelled underneath
        return Layout(grid=grid, band=ROWS,
                      months=month_marks(range(half), 0, "above")
                      + month_marks(range(half, cols), ROWS * 2 - 1, "below"))

    # square: the most recent 361 days packed row-major. There is no calendar
    # structure left, so each month is named beside the row its 1st falls in.
    days = [(seed[y][x], starts[x] + timedelta(days=y))
            for x in range(cols) for y in range(ROWS)]
    days = days[-SQUARE * SQUARE:]
    grid = [[v for v, _ in days[i * SQUARE:(i + 1) * SQUARE]]
            for i in range(SQUARE)]
    months = [(0, i // SQUARE, MONTHS[d.month - 1], "left")
              for i, (_, d) in enumerate(days) if d.day == 1]
    return Layout(grid=grid, band=SQUARE, weekdays=False, months=months)


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

class GraphQLError(Exception):
    pass


QUERY = """query($login:String!){
  user(login:$login){
    contributionsCollection{
      contributionCalendar{
        totalContributions
        weeks{ firstDay contributionDays{ date contributionCount contributionLevel weekday } }
      }
    }
  }
}"""


def fetch_calendar(login: str, token: str) -> dict:
    body = json.dumps({"query": QUERY, "variables": {"login": login}}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "contribution-life",
        },
    )
    with urllib.request.urlopen(req) as r:
        payload = json.load(r)
    if "errors" in payload:
        raise GraphQLError("; ".join(e.get("message", "?")
                                     for e in payload["errors"]))
    return payload


def load_seed(payload: dict):
    """-> (level grid [7][cols], week start dates, total contributions)"""
    user = payload.get("data", {}).get("user")
    if not user:
        raise GraphQLError("no such user")
    cal = user["contributionsCollection"]["contributionCalendar"]
    weeks = cal["weeks"]
    grid = [[0] * len(weeks) for _ in range(ROWS)]
    starts = []
    for x, w in enumerate(weeks):
        starts.append(date.fromisoformat(w["firstDay"]))
        for d in w["contributionDays"]:
            grid[d["weekday"]][x] = LEVELS[d["contributionLevel"]]
    return grid, starts, cal["totalContributions"]


# --------------------------------------------------------------------------
# simulation
# --------------------------------------------------------------------------

class Sim:
    """Life-like automaton that also tracks, per cell, how long it has been
    alive (`age`) and the contribution level it descends from (`gene`)."""

    def __init__(self, seed, cfg: Config):
        self.cfg = cfg
        self.rows = len(seed)
        self.cols = len(seed[0])
        self.alive = [[v > 0 for v in row] for row in seed]
        self.gene = [[max(1, v) for v in row] for row in seed]
        self.age = [[0] * self.cols for _ in range(self.rows)]

    def _wrap(self, x, y):
        if self.cfg.torus:
            return x % self.cols, y % self.rows
        if 0 <= x < self.cols and 0 <= y < self.rows:
            return x, y
        return None

    def _neighbours(self, x, y):
        n, genes = 0, []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == dy == 0:
                    continue
                p = self._wrap(x + dx, y + dy)
                if p and self.alive[p[1]][p[0]]:
                    n += 1
                    genes.append(self.gene[p[1]][p[0]])
        return n, genes

    def _density(self, x, y, r=2):
        """live cells in the (2r+1)^2 block around it, self excluded"""
        n = 0
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dx == dy == 0:
                    continue
                p = self._wrap(x + dx, y + dy)
                if p and self.alive[p[1]][p[0]]:
                    n += 1
        return n

    @staticmethod
    def _inherit(genes) -> int:
        """majority vote of the parents, ties broken towards the darker gene"""
        c = Counter(genes)
        top = max(c.values())
        return max(g for g, k in c.items() if k == top)

    def step(self):
        cfg = self.cfg
        na = [[False] * self.cols for _ in range(self.rows)]
        ng = [row[:] for row in self.gene]
        nage = [[0] * self.cols for _ in range(self.rows)]

        for y in range(self.rows):
            for x in range(self.cols):
                n, genes = self._neighbours(x, y)
                if self.alive[y][x]:
                    if n in cfg.survive:
                        na[y][x] = True
                        nage[y][x] = self.age[y][x] + 1
                elif n in cfg.birth:
                    na[y][x] = True
                    ng[y][x] = self._inherit(genes)

        self.alive, self.gene, self.age = na, ng, nage

    def snapshot(self):
        """-> (alive grid, raw shade metric grid)"""
        c = self.cfg
        raw = [[0] * self.cols for _ in range(self.rows)]
        for y in range(self.rows):
            for x in range(self.cols):
                if not self.alive[y][x]:
                    continue
                g, a = self.gene[y][x] - 1, self.age[y][x]
                if c.color == "age":
                    v = a * c.age_w
                elif c.color == "gene":
                    v = g
                elif c.color == "density":
                    v = self._density(x, y)
                else:
                    v = a * c.age_w + g * c.gene_w + self._density(x, y) * c.dens_w
                raw[y][x] = v
        return [r[:] for r in self.alive], raw


def simulate(seed, cfg: Config):
    """life phase only -> (alive frames, raw metric frames)"""
    sim = Sim(seed, cfg)
    alive_f, raw_f = [], []
    for i in range(cfg.gens + 1):
        if i:
            sim.step()
        a, r = sim.snapshot()
        alive_f.append(a)
        raw_f.append(r)
    return alive_f, raw_f


# --------------------------------------------------------------------------
# colour
# --------------------------------------------------------------------------

def quantile_bins(values):
    """3 thresholds splitting values into 4 roughly equal buckets.

    Ties are common (age is mostly 0), so thresholds are de-duplicated and
    spread out rather than collapsing the palette onto one colour.
    """
    v = sorted(values)
    if not v:
        return [1, 2, 3]
    picks = []
    for q in (0.25, 0.50, 0.75):
        t = v[min(len(v) - 1, int(len(v) * q))]
        while picks and t <= picks[-1]:
            t = picks[-1] + 1
        picks.append(t)
    return picks


def colorize(alive_f, raw_f, life_seed, cfg: Config):
    nf, rows, cols = len(alive_f), len(alive_f[0]), len(alive_f[0][0])
    bins = quantile_bins([raw_f[i][y][x]
                          for i in range(1, nf)
                          for y in range(rows)
                          for x in range(cols)
                          if alive_f[i][y][x]])

    def level(v):
        return 1 + sum(1 for b in bins if v > b)

    # invariant: level 0 <=> dead. a coloured cell is always a live cell.
    frames = [[[level(raw_f[i][y][x]) if alive_f[i][y][x] else 0
                for x in range(cols)] for y in range(rows)]
              for i in range(nf)]

    # generation 0 shows the real contribution levels, not the simulation's
    frames[0] = [row[:] for row in life_seed]
    return frames, bins


def build_loop(seed, cfg: Config):
    """-> (all frames of one loop, quartile bins)

    intro: the real graph held, then every level below the threshold fades out
           together over `fade` frames. The board still carries their colour
           through that window; render_svg interpolates across it so they
           dissolve rather than blink out. See smooth_intervals().
    life:  generation 0 (what is left of the graph) onward
    """
    life_seed = thin(seed, cfg.seed_level)
    frames, bins = colorize(*simulate(life_seed, cfg), life_seed, cfg)

    held = cfg.hold + (cfg.fade if cfg.seed_level > 1 else 0)
    intro = [[row[:] for row in seed] for _ in range(held)]
    return intro + frames, bins


MAX_GENS = 100
GRACE = 12          # generations to keep running once the board starts repeating


def _trial(board, cfg: Config, torus: bool):
    """-> (generations worth animating, churn over them)

    The run ends when the board empties, or GRACE generations after the exact
    state first repeats. Repetition is the only "nothing new is happening"
    signal used, deliberately: population is not, because a sparse graph starts
    near zero and any measure of it would cut those boards off immediately no
    matter how much was still moving. A glider crossing a torus does not repeat
    until it has come all the way round, so it keeps running - which is the
    point.
    """
    total = len(board) * len(board[0])
    live = lambda g: sum(1 for r in g for v in r if v)
    sim = Sim(board, replace(cfg, torus=torus))
    prev = sim.snapshot()[0]
    if not live(prev):
        return 0, 0.0

    seen = {tuple(map(tuple, prev)): 0}
    good = changes = 0
    over = None
    for g in range(1, MAX_GENS + 1):
        sim.step()
        cur = sim.snapshot()[0]
        if not live(cur):
            break
        changes += sum(1 for y in range(len(cur)) for x in range(len(cur[0]))
                       if cur[y][x] != prev[y][x])
        prev = cur
        good = g
        key = tuple(map(tuple, cur))
        if over is None and key in seen:
            over = g
        seen[key] = g
        if over is not None and g >= over + GRACE:
            break
    return good, (changes / (good * total) if good else 0.0)


def autotune(seed, cfg: Config):
    """-> (seed level, generations, torus) that keep the board alive the longest

    Every combination is actually simulated. Longest run wins; ties go to the
    liveliest, then to the settings that distort the real graph least. Only the
    settings left on auto are searched - a fixed `torus` is measured as given,
    otherwise the generation count would be fitted to a board nobody renders.
    """
    edges = (False, True) if cfg.torus is None else (cfg.torus,)
    levels = (1, 2, 3, 4) if not cfg.seed_level else (cfg.seed_level,)
    best = None
    for torus in edges:
        for level in levels:
            good, churn = _trial(thin(seed, level), cfg, torus)
            if good and churn > 0 and (best is None or (good, churn) > best[0]):
                best = ((good, churn), level, good, torus)
    if best is None:                    # nothing survives; show the graph anyway
        return levels[0], 20, edges[-1]
    return best[1], best[2], best[3]


def resolve(seed, cfg: Config) -> Config:
    """fill in whichever of gens / seed_level / torus were left on auto"""
    if cfg.gens and cfg.seed_level and cfg.torus is not None:
        return cfg
    level, gens, torus = autotune(seed, cfg)
    cfg.seed_level = cfg.seed_level or level
    cfg.gens = cfg.gens or gens
    cfg.torus = torus if cfg.torus is None else cfg.torus
    return cfg


def smooth_intervals(cfg: Config):
    """[(start frame, end frame)] of the intro window that fades rather than cuts"""
    if cfg.seed_level <= 1:
        return []
    return [(cfg.hold, cfg.hold + cfg.fade)]


# --------------------------------------------------------------------------
# svg
# --------------------------------------------------------------------------

CELL, GAP = 11, 3
PITCH = CELL + GAP
PAD_T, PAD = 20, 5
RADIUS = 2


def render_svg(frames, layout: Layout, cfg: Config, theme: str, ns="k") -> str:
    """`ns` namespaces the generated CSS names, so two of these can be inlined
    into one document without their @keyframes colliding."""
    pal = PALETTES[theme]
    rows, cols, nf = len(frames[0]), len(frames[0][0]), len(frames)
    dur_ms = nf * cfg.frame_ms
    pad_l = layout.pad_l
    bands = (rows + layout.band - 1) // layout.band

    def px(x):
        return pad_l + x * PITCH

    def py(y):
        # bands sit flush against each other: they are adjacent on the torus,
        # so a visual gap would misreport where life can spread
        return PAD_T + y * PITCH

    w = px(cols) - GAP + PAD
    h = py(rows - 1) + CELL + PAD
    if any(side == "below" for *_, side in layout.months):
        h += 14

    # one @keyframes per distinct timeline, shared by every cell that has it
    timelines: dict[tuple, int] = {}
    statics, animated = [], []
    for y in range(rows):
        for x in range(cols):
            tl = tuple(frames[i][y][x] for i in range(nf))
            if len(set(tl)) == 1:
                statics.append((x, y, tl[0]))
            else:
                animated.append((x, y, timelines.setdefault(tl, len(timelines))))

    css = [
        f".{ns}c{{animation-duration:{dur_ms}ms;animation-iteration-count:infinite;"
        "animation-timing-function:step-end}",
        f".{ns}t{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,"
        f"Arial,sans-serif;font-size:10px;fill:{LABEL_COLOR[theme]}}}",
    ]
    smooth = [(s, e) for s, e in smooth_intervals(cfg) if e < nf]
    for tl, idx in timelines.items():
        css.append(f".{ns}a{idx}{{animation-name:{ns}f{idx}}}")
        # cells that actually change across a fade window get an extra stop at
        # its start, switched to linear so the colour glides to the next one
        lin = {s for s, e in smooth if tl[e] != tl[s]}
        back = {e for s, e in smooth if tl[e] != tl[s]}
        stops, prev = [], None
        for i, lv in enumerate(tl):
            if lv != prev or i in lin:   # otherwise a stop only where it changes
                tf = (";animation-timing-function:linear" if i in lin
                      else ";animation-timing-function:step-end" if i in back
                      else "")
                pct = f"{i * 100.0 / nf:.3f}".rstrip("0").rstrip(".")
                stops.append(f"{pct}%{{fill:{pal[lv]}{tf}}}")
                prev = lv
        css.append(f"@keyframes {ns}f{idx}{{{''.join(stops)}}}")

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="GitHub contribution graph running Conway&apos;s Game of Life">',
        "<style>" + "".join(css) + "</style>",
    ]

    for col, row, name, side in layout.months:
        if side == "left":
            x, y = 0, py(row) + CELL - 2
        elif side == "below":
            x, y = px(col), py(row) + CELL + 11
        else:
            x, y = px(col), py(row) - 6
        out.append(f'<text class="{ns}t" x="{x}" y="{y}">{name}</text>')
    if layout.weekdays:
        for band in range(bands):
            for y, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
                out.append(f'<text class="{ns}t" x="0" '
                           f'y="{py(band * layout.band + y) + CELL - 2}">{name}</text>')

    def rect(x, y, extra):
        return (f'<rect x="{px(x)}" y="{py(y)}" '
                f'width="{CELL}" height="{CELL}" rx="{RADIUS}" {extra}/>')

    for x, y, lv in statics:
        out.append(rect(x, y, f'fill="{pal[lv]}"'))
    for x, y, idx in animated:
        out.append(rect(x, y, f'class="{ns}c {ns}a{idx}" '
                              f'fill="{pal[frames[0][y][x]]}"'))

    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------

def stats_data(frames, bins, cfg: Config) -> dict:
    nf, rows, cols = len(frames), len(frames[0]), len(frames[0][0])
    total = rows * cols
    life = frames[nf - (cfg.gens + 1):]          # skip the intro
    pops = [sum(1 for r in f for v in r if v) for f in life]
    tot = Counter(v for f in life for r in f for v in r)
    live = max(1, sum(tot[k] for k in (1, 2, 3, 4)))
    churn = sum(1 for i in range(1, len(life)) for y in range(rows)
                for x in range(cols) if life[i][y][x] != life[i - 1][y][x])
    return {
        "frames": nf,
        "cells": total,
        "seed_level": cfg.seed_level,
        "edges": "torus" if cfg.torus else "dead",
        "loop_s": round(nf * cfg.frame_ms / 1000, 1),
        "bins": bins,
        "pops": pops,
        "pop_min": min(pops),
        "pop_max": max(pops),
        "pop_min_pct": min(pops) * 100 // total,
        "seed_pct": pops[0] * 100 // total,
        "graph_pct": sum(1 for r in frames[0] for v in r if v) * 100 // total,
        "palette": [tot[k] * 100 // live for k in (1, 2, 3, 4)],
        "churn_pct": churn * 100 // (max(1, len(life) - 1) * total),
    }


def print_stats(frames, bins, cfg: Config, note=""):
    d = stats_data(frames, bins, cfg)
    total, pops = d["cells"], d["pops"]
    life = frames[d["frames"] - len(pops):]
    lo, hi = d["pop_min"], d["pop_max"]
    span = max(1, hi - lo)
    bars = BARS
    print(f"frames={d['frames']} loop={d['loop_s']}s color={cfg.color} "
          f"gens={cfg.gens} seed=L{cfg.seed_level}+ "
          f"edges={'torus' if cfg.torus else 'dead'} bins={bins} {note}")
    print("  gen  pop   %    L1  L2  L3  L4")
    for i in range(0, len(pops), max(1, len(pops) // 14)):
        c = Counter(v for r in life[i] for v in r)
        b = bars[min(7, (pops[i] - lo) * 8 // span)]
        print(f"  {i:3d} {pops[i]:4d} {pops[i]*100//total:3d}%  "
              f"{c[1]:3d} {c[2]:3d} {c[3]:3d} {c[4]:3d}  {b * (1 + (pops[i]-lo)*24//span)}")
    print("  palette use  " + "  ".join(
        f"L{k} {p:2d}%" for k, p in zip((1, 2, 3, 4), d["palette"])))
    print(f"  population {lo}-{hi}   churn {d['churn_pct']}%/frame")


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def make_config(rule="B3/S23", **kw) -> Config:
    b, s = rule.upper().split("/")
    if not (b.startswith("B") and s.startswith("S")):
        raise ValueError(f"bad rule {rule!r}, expected something like B3/S23")
    return Config(birth={int(c) for c in b[1:]},
                  survive={int(c) for c in s[1:]}, **kw)


def build_frames(payload, cfg: Config):
    """-> (frames, quartile bins, layout, total contributions)"""
    seed, starts, total = load_seed(payload)
    layout = build_layout(seed, starts, cfg.layout)
    resolve(layout.grid, cfg)
    frames, bins = build_loop(layout.grid, cfg)
    return frames, bins, layout, total


def main(argv=None):
    ap = argparse.ArgumentParser(prog="glife")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch")
    f.add_argument("--login", required=True)
    f.add_argument("--out", default="contrib.json")
    f.add_argument("--token")

    d = Config()
    for name in ("stats", "render"):
        p = sub.add_parser(name)
        p.add_argument("--data", default="contrib.json")
        p.add_argument("--rule", default="B3/S23")
        p.add_argument("--gens", type=int, default=d.gens,
                       help="generations of Life per loop (0 = auto)")
        p.add_argument("--hold", type=int, default=d.hold)
        p.add_argument("--fade", type=int, default=d.fade,
                       help="frames each dropped contribution level lingers for")
        p.add_argument("--frame-ms", type=int, default=d.frame_ms)
        p.add_argument("--layout", choices=LAYOUTS, default=d.layout)
        p.add_argument("--color", choices=COLOR_MODES, default=d.color)
        p.add_argument("--age-w", type=int, default=d.age_w)
        p.add_argument("--gene-w", type=int, default=d.gene_w)
        p.add_argument("--dens-w", type=int, default=d.dens_w)
        p.add_argument("--seed-level", type=int, choices=[0, 1, 2, 3, 4],
                       default=d.seed_level,
                       help="minimum contribution level counted as alive "
                            "(0 = auto)")
        p.add_argument("--edges", choices=["torus", "dead", "auto"],
                       default="torus",
                       help="wrap the board edges into a torus, leave them "
                            "dead, or try both and keep the better")
        if name == "render":
            p.add_argument("--out", default="dist")
            p.add_argument("--name", default="contribution-life")

    a = ap.parse_args(argv)

    if a.cmd == "fetch":
        token = a.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            raise SystemExit("need --token or $GITHUB_TOKEN")
        try:
            payload = fetch_calendar(a.login, token)
        except GraphQLError as e:
            raise SystemExit(f"GraphQL: {e}")
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        seed, _, total = load_seed(payload)
        n, t = sum(1 for r in seed for v in r if v), 7 * len(seed[0])
        print(f"{a.out}: {total} contributions, seed density {n}/{t} = {n*100//t}%")
        return

    with open(a.data, encoding="utf-8") as fh:
        payload = json.load(fh)
    edges = {"auto": None, "torus": True, "dead": False}[a.edges]
    cfg = make_config(a.rule, torus=edges, gens=a.gens, hold=a.hold,
                      fade=a.fade, frame_ms=a.frame_ms, color=a.color, age_w=a.age_w,
                      gene_w=a.gene_w, dens_w=a.dens_w, seed_level=a.seed_level,
                      layout=a.layout)
    frames, bins, layout, _ = build_frames(payload, cfg)

    if a.cmd == "stats":
        print_stats(frames, bins, cfg, note=f"rule={a.rule} layout={a.layout}")
        return

    os.makedirs(a.out, exist_ok=True)
    for theme in ("light", "dark"):
        svg = render_svg(frames, layout, cfg, theme)
        path = os.path.join(a.out, f"{a.name}{'' if theme == 'light' else '-dark'}.svg")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(svg)
        print(f"{path}  {len(svg.encode())/1024:.1f} KB  {len(frames)} frames")


if __name__ == "__main__":
    sys.exit(main())
