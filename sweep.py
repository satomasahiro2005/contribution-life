"""Compare Life-like rules and board shapes on the real contribution data.

  python sweep.py [gens]
"""
import json
import statistics
import sys

from glife import BARS, LAYOUTS, Sim, build_layout, load_seed, make_config

GENS = int(sys.argv[1]) if len(sys.argv) > 1 else 120

RULES = ["B3/S23", "B36/S23", "B34/S34", "B36/S125", "B368/S238",
         "B35/S236", "B3/S1234", "B378/S235678"]

seed, mask, starts, _ = load_seed(json.load(open("contrib.json", encoding="utf-8")))
print(f"{GENS} generations\n")

for mode in LAYOUTS:
    grid = build_layout(seed, mask, starts, mode).grid
    total = len(grid) * len(grid[0])
    print(f"--- {mode}  {len(grid[0])}x{len(grid)}  "
          f"seed {sum(1 for r in grid for v in r if v) * 100 // total}% ---")
    for rule in RULES:
        sim = Sim(grid, make_config(rule))
        prev, pops, churn = sim.snapshot()[0], [], 0
        for _ in range(GENS):
            sim.step()
            cur = sim.snapshot()[0]
            pops.append(sum(1 for r in cur for v in r if v))
            churn += sum(1 for y in range(len(grid))
                         for x in range(len(grid[0])) if cur[y][x] != prev[y][x])
            prev = cur
        pct = [p * 100 // total for p in pops]
        tail = pct[GENS // 2:]
        spark = "".join(BARS[min(7, p * 8 // 45)] for p in pct[::4])
        flag = "  <- dies" if statistics.mean(tail) < 8 else ""
        print(f"  {rule:<12} med={statistics.median(pct):2.0f}% "
              f"tail={statistics.mean(tail):4.1f}% "
              f"churn={churn * 100 // (GENS * total):2d}%  {spark}{flag}")
    print()
