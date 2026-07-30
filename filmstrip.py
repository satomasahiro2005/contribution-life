"""ASCII dump of selected frames, for eyeballing a rule, colour or layout.

  python filmstrip.py [rule] [color] [gens] [layout]
"""
import json
import sys

from glife import build_layout, load_seed, make_config, build_loop

RULE = sys.argv[1] if len(sys.argv) > 1 else "B3/S23"
COLOR = sys.argv[2] if len(sys.argv) > 2 else "hybrid"
GENS = int(sys.argv[3]) if len(sys.argv) > 3 else 35
LAYOUT = sys.argv[4] if len(sys.argv) > 4 else "calendar"

CHARS = ".-+*#"   # level 0..4, dark to bright

seed, starts, _ = load_seed(json.load(open("contrib.json", encoding="utf-8")))
cfg = make_config(RULE, gens=GENS, color=COLOR, layout=LAYOUT)
frames, bins = build_loop(build_layout(seed, starts, LAYOUT).grid, cfg)
first_life = len(frames) - (GENS + 1)

print(f"{RULE}  color={COLOR}  layout={LAYOUT}  {len(frames)} frames  bins={bins}")
print(f"legend: {' '.join(f'{c}=L{i}' for i, c in enumerate(CHARS))}\n")

for i in range(0, len(frames), max(1, len(frames) // 8)):
    pop = sum(1 for r in frames[i] for v in r if v)
    tag = ("  <- intro (real graph)" if i < first_life
           else "  <- generation 0" if i == first_life else "")
    print(f"frame {i:3d}  pop {pop:3d}{tag}")
    for row in frames[i]:
        print("  " + "".join(CHARS[v] for v in row))
    print()
