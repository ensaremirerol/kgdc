"""Aggregate a run_all.sh scores.txt: python examples/chr/summary.py RUN_DIR"""
import re, sys
from pathlib import Path
R = Path(sys.argv[1]); rows = []
for l in (R / "scores.txt").read_text().splitlines():
    m = re.match(r"(\S+)\s+gold=\s*(\d+) out=\s*(\d+) tp=\s*(\d+) P=([\d.]+) R=([\d.]+) F1=([\d.]+)", l)
    if m: rows.append((m[1], int(m[2]), int(m[3]), int(m[4]), float(m[5]), float(m[6]), float(m[7])))
n = len(rows); G = sum(r[1] for r in rows); O = sum(r[2] for r in rows); TP = sum(r[3] for r in rows)
P, Rc = TP / O, TP / G; f1s = sorted(r[6] for r in rows)
q = lambda x: f1s[min(n - 1, int(x * n))]
print(f"documents scored: {n} | gold triples {G} | output triples {O}")
print(f"micro  P={P:.3f} R={Rc:.3f} F1={2*P*Rc/(P+Rc):.3f}")
print(f"macro  P={sum(r[4] for r in rows)/n:.3f} R={sum(r[5] for r in rows)/n:.3f} F1={sum(r[6] for r in rows)/n:.3f}")
print(f"F1 quartiles  min={f1s[0]:.3f} q1={q(.25):.3f} median={q(.5):.3f} q3={q(.75):.3f} max={f1s[-1]:.3f}  | F1>=0.9: {sum(f>=.9 for f in f1s)}  <0.7: {sum(f<.7 for f in f1s)}")
for lo, hi in ((0, 50), (50, 150), (150, 10**9)):
    b = [r for r in rows if lo <= r[1] < hi]
    if b: print(f"  gold size {lo:>3}-{hi if hi < 10**9 else '':<4} n={len(b):3d}  macro F1={sum(r[6] for r in b)/len(b):.3f}")
worst = sorted(rows, key=lambda r: r[6])[:8]
print("worst:", ", ".join(f"{r[0]}={r[6]:.2f}" for r in worst))
failed = [p.stem for p in R.glob("vignette_*.log") if "exit=0" not in p.read_text() and "exit=1" not in p.read_text() or not (R / (p.stem + ".ttl")).exists()]
print("no output:", failed or "none")
