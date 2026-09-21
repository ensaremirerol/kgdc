"""Paired comparison of kgdc vs the Thesis systems on the same vignettes, same scorer.

  python examples/chr/compare.py RUN_DIR [--exclude vignette_065,vignette_001,...]   # expects RUN_DIR/scores.txt (kgdc) and RUN_DIR/old_{a,b,d}.txt

Reports micro/macro P/R/F1 per system, paired per-document deltas, wins/ties/losses and a two-sided
sign test (exact binomial) of kgdc vs each system."""
import math, re, sys
from pathlib import Path
R = Path(sys.argv[1])
EXCLUDE = set(sys.argv[sys.argv.index("--exclude") + 1].split(",")) if "--exclude" in sys.argv else set()   # e.g. the development vignettes
def load(p):
    out = {}
    for l in Path(p).read_text().splitlines():
        m = re.match(r"(\S+)\s+gold=\s*(\d+) out=\s*(\d+) tp=\s*(\d+) P=([\d.]+) R=([\d.]+) F1=([\d.]+)", l)
        if m: out[m[1]] = (int(m[2]), int(m[3]), int(m[4]), float(m[5]), float(m[6]), float(m[7]))
    return out
systems = {"kgdc": load(R / "scores.txt")}
for s in "abd":
    if (R / f"old_{s}.txt").exists(): systems[f"thesis_{s}"] = load(R / f"old_{s}.txt")
common = sorted(d for d in systems["kgdc"] if d not in EXCLUDE)
if EXCLUDE: print(f"excluded {len(EXCLUDE & set(systems['kgdc']))} development documents: {', '.join(sorted(EXCLUDE))}")
for name, sc in systems.items():   # a document a system could not produce or that does not parse scores 0, it is not dropped
    for d in common:
        sc.setdefault(d, (systems["kgdc"][d][0], 0, 0, 0.0, 0.0, 0.0))
print(f"documents: {len(common)} (unscorable outputs count as F1 = 0: " + ", ".join(f"{n} {sum(1 for d in common if sc[d][1] == 0)}" for n, sc in systems.items()) + ")\n")
print(f"{'system':10s} {'microP':>7s} {'microR':>7s} {'microF1':>8s} {'macroP':>7s} {'macroR':>7s} {'macroF1':>8s} {'F1>=.9':>7s} {'F1<.5':>6s}")
for name, sc in systems.items():
    rows = [sc[d] for d in common]; G = sum(r[0] for r in rows); O = sum(r[1] for r in rows); TP = sum(r[2] for r in rows)
    P, Rc = TP / O if O else 0, TP / G; F = 2 * P * Rc / (P + Rc) if P + Rc else 0; n = len(rows)
    print(f"{name:10s} {P:7.3f} {Rc:7.3f} {F:8.3f} {sum(r[3] for r in rows)/n:7.3f} {sum(r[4] for r in rows)/n:7.3f} {sum(r[5] for r in rows)/n:8.3f} {sum(r[5]>=.9 for r in rows):7d} {sum(r[5]<.5 for r in rows):6d}")
def sign_test(w, l):
    n = w + l; k = min(w, l)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n) if n else 1.0
print()
for name, sc in systems.items():
    if name == "kgdc": continue
    d = [systems["kgdc"][x][5] - sc[x][5] for x in common]
    w, l, t = sum(x > 0.005 for x in d), sum(x < -0.005 for x in d), sum(abs(x) <= 0.005 for x in d)
    print(f"kgdc vs {name}: mean ΔF1 = {sum(d)/len(d):+.3f}, median = {sorted(d)[len(d)//2]:+.3f}, wins/ties/losses = {w}/{t}/{l}, sign test p = {sign_test(w, l):.2g}")
    worst = sorted(common, key=lambda x: systems["kgdc"][x][5] - sc[x][5])[:5]
    print("   kgdc loses most on:", ", ".join(f"{x[-3:]} ({systems['kgdc'][x][5]:.2f} vs {sc[x][5]:.2f})" for x in worst))
