"""Check the automaton against known Game of Life patterns.

Everything here runs under B3/S23 (real Conway) on a dead-edged board unless
stated otherwise. Run: python test_rules.py
"""

from glife import Config, Sim

CONWAY = Config(birth={3}, survive={2, 3}, torus=False)


def board(rows, cols, cells):
    g = [[0] * cols for _ in range(rows)]
    for y, x in cells:
        g[y][x] = 1
    return g


def live(sim):
    return frozenset((y, x) for y in range(sim.rows) for x in range(sim.cols)
                     if sim.alive[y][x])


def run(seed, gens, cfg=CONWAY):
    sim = Sim(seed, cfg)
    hist = [live(sim)]
    for _ in range(gens):
        sim.step()
        hist.append(live(sim))
    return hist


def norm(cells):
    """translate a pattern so its bounding box starts at the origin"""
    if not cells:
        return frozenset()
    y0 = min(y for y, _ in cells)
    x0 = min(x for _, x in cells)
    return frozenset((y - y0, x - x0) for y, x in cells)


FAILS = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}{'   ' + detail if detail else ''}")
    if not cond:
        FAILS.append(name)


# -- still lifes ------------------------------------------------------------
print("still lifes (must not change)")
STILL = {
    "block": [(1, 1), (1, 2), (2, 1), (2, 2)],
    "beehive": [(1, 2), (1, 3), (2, 1), (2, 4), (3, 2), (3, 3)],
    "loaf": [(1, 2), (1, 3), (2, 1), (2, 4), (3, 2), (3, 4), (4, 3)],
    "boat": [(1, 1), (1, 2), (2, 1), (2, 3), (3, 2)],
    "tub": [(1, 2), (2, 1), (2, 3), (3, 2)],
}
for name, cells in STILL.items():
    h = run(board(8, 8, cells), 4)
    check(name, all(s == h[0] for s in h))

# -- oscillators ------------------------------------------------------------
print("\noscillators (period must be exact)")
OSC = {
    "blinker": ([(2, 1), (2, 2), (2, 3)], 2),
    "toad": ([(2, 2), (2, 3), (2, 4), (3, 1), (3, 2), (3, 3)], 2),
    "beacon": ([(1, 1), (1, 2), (2, 1), (3, 4), (4, 3), (4, 4)], 2),
    "pulsar": ([(y, x) for y, x in [
        (2, 4), (2, 5), (2, 6), (2, 10), (2, 11), (2, 12),
        (4, 2), (4, 7), (4, 9), (4, 14), (5, 2), (5, 7), (5, 9), (5, 14),
        (6, 2), (6, 7), (6, 9), (6, 14),
        (7, 4), (7, 5), (7, 6), (7, 10), (7, 11), (7, 12),
        (9, 4), (9, 5), (9, 6), (9, 10), (9, 11), (9, 12),
        (10, 2), (10, 7), (10, 9), (10, 14), (11, 2), (11, 7), (11, 9), (11, 14),
        (12, 2), (12, 7), (12, 9), (12, 14),
        (14, 4), (14, 5), (14, 6), (14, 10), (14, 11), (14, 12)]], 3),
}
for name, (cells, period) in OSC.items():
    n = 17 if name == "pulsar" else 9
    h = run(board(n, n, cells), 6)
    got = next((p for p in range(1, 6) if h[p] == h[0]), None)
    check(name, got == period and h[1] != h[0], f"period {got}, expected {period}")

# -- spaceship --------------------------------------------------------------
print("\nglider (must translate by (1,1) every 4 generations)")
glider = [(0, 1), (1, 2), (2, 0), (2, 1), (2, 2)]
h = run(board(20, 20, glider), 12)
check("shape preserved", all(norm(h[i]) == norm(h[0]) for i in (4, 8, 12)))
shifted = frozenset((y + 1, x + 1) for y, x in h[0])
check("displacement", h[4] == shifted,
      f"gen4 min corner {min(h[4])} vs expected {min(shifted)}")
check("distinct phases", len({h[0], h[1], h[2], h[3]}) == 4)

# -- simultaneity -----------------------------------------------------------
print("\nsimultaneous update (a sequential in-place update breaks these)")
h = run(board(9, 9, [(4, 2), (4, 3), (4, 4)]), 1)
check("blinker flips whole", h[1] == frozenset({(3, 3), (4, 3), (5, 3)}))


# -- differential test against an independent implementation ----------------
def ref_step(cells):
    """textbook sparse-set Life on an unbounded grid, structurally unrelated
    to Sim's grid scan, so the two are unlikely to share a bug"""
    n = {}
    for (y, x) in cells:
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy or dx:
                    n[(y + dy, x + dx)] = n.get((y + dy, x + dx), 0) + 1
    return frozenset(c for c, k in n.items()
                     if k == 3 or (k == 2 and c in cells))


def differential(name, cells, gens, size):
    off = size // 2 - 2
    placed = [(y + off, x + off) for y, x in cells]
    sim = Sim(board(size, size, placed), CONWAY)
    ref = frozenset(placed)
    for g in range(1, gens + 1):
        sim.step()
        ref = ref_step(ref)
        edge = any(y in (0, size - 1) or x in (0, size - 1) for y, x in ref)
        if edge:
            check(f"{name} stayed inside the board", False, f"escaped at gen {g}")
            return
        if live(sim) != ref:
            check(name, False, f"diverged at gen {g}: "
                               f"{len(live(sim))} vs {len(ref)} cells")
            return
    check(name, True, f"{gens} generations identical, {len(ref)} cells at the end")


print("\nchaotic patterns vs a reference implementation")
differential("r-pentomino", [(0, 1), (0, 2), (1, 0), (1, 1), (2, 1)], 150, 121)
differential("acorn", [(0, 1), (1, 3), (2, 0), (2, 1), (2, 4), (2, 5), (2, 6)],
             150, 121)
differential("diehard", [(0, 6), (1, 0), (1, 1), (2, 1), (2, 5), (2, 6), (2, 7)],
             130, 61)

# -- torus ------------------------------------------------------------------
print("\ntorus wrapping")
TOR = Config(birth={3}, survive={2, 3}, torus=True)
h = run(board(7, 7, [(3, 6), (3, 0), (3, 1)]), 2, TOR)   # blinker across the seam
check("blinker survives the seam", h[2] == h[0] and h[1] != h[0])
h = run(board(5, 5, [(0, 0), (0, 1), (1, 0), (1, 1)]), 3, TOR)
check("block stable on a small torus", all(s == h[0] for s in h))

# -- other rules ------------------------------------------------------------
print("\nrule parsing (non-Conway B/S)")
# under B34/S34 a lone block has 3 neighbours each -> survives
h = run(board(8, 8, [(1, 1), (1, 2), (2, 1), (2, 2)]), 1,
        Config(birth={3, 4}, survive={3, 4}, torus=False))
check("B34/S34 keeps the block", h[1] == h[0])
# under B3/S23 -> also survives; under S45 it must die
h = run(board(8, 8, [(1, 1), (1, 2), (2, 1), (2, 2)]), 1,
        Config(birth={3}, survive={4, 5}, torus=False))
check("B3/S45 kills the block", len(h[1]) == 0)

print("\n" + ("all passed" if not FAILS else f"FAILED: {', '.join(FAILS)}"))
raise SystemExit(1 if FAILS else 0)
