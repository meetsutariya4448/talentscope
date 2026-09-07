"""Median + variability across A/B trials, with equivalence checking."""
import json, pathlib, statistics as st, sys

rows = [json.loads(l) for l in pathlib.Path("evals/thread-ab/trials.jsonl").read_text().splitlines() if l.strip()]
if not rows:
    sys.exit("no trials")

def med(xs): return st.median(xs) if xs else None
def iqr(xs):
    if len(xs) < 4: return (min(xs), max(xs)) if xs else (None, None)
    q = st.quantiles(xs, n=4)
    return (q[0], q[2])

groups = {}
for r in rows:
    groups.setdefault(r["config"], []).append(r)

print(f"trials recorded: {len(rows)}  ({', '.join(f'{k}={len(v)}' for k,v in sorted(groups.items()))})")
commits = {r["commit"] for r in rows}
images = {r["image"] for r in rows}
print(f"commit(s): {sorted(commits)}   image(s): {sorted(images)}")

# tree_dirty is true for trials after the first only because the results file
# itself is untracked. What matters is whether any *tracked source* file
# changed mid-run — checked separately below.
import subprocess
tracked_diff = subprocess.run(["git", "diff", "--quiet", "HEAD"]).returncode
untracked = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout
stray = [l for l in untracked.splitlines() if l.strip() and "evals/thread-ab/" not in l]
print(f"tracked source modified during run: {'YES (!)' if tracked_diff else 'no'}")
print(f"untracked paths other than results: {stray or 'none'}")
print()

# --- equivalence: everything except OMP must be identical across all trials ---
fixed_keys = list(rows[0]["held_fixed"].keys())
print("EQUIVALENCE CHECK (all trials, both configs)")
mismatch = False
for k in fixed_keys:
    vals = {json.dumps(r["held_fixed"][k]) for r in rows}
    status = "OK  " if len(vals) == 1 else "DIFF"
    if len(vals) != 1: mismatch = True
    print(f"  {status} {k:26} {sorted(vals) if len(vals)>1 else list(vals)[0]}")
tv = {r["config"]: sorted({x["observed_api_torch_threads"] for x in g}) for r, g in [(r, groups[r["config"]]) for r in rows]}
print(f"  VARY observed_api_torch_threads  " + str({k: sorted({x['observed_api_torch_threads'] for x in v}) for k, v in sorted(groups.items())}))
print(f"  -> {'MISMATCH FOUND' if mismatch else 'only the thread setting differs'}")
print()

hdr = f"{'config':8} {'n':>2} {'rps med':>9} {'rps IQR':>17} {'p95 med':>10} {'p95 IQR':>19} {'embed/s med':>12} {'err rate':>9} {'2xx':>7}"
print(hdr); print("-" * len(hdr))
summary = {}
for cfg in sorted(groups):
    g = groups[cfg]
    rps = [r["k6"]["rps"] for r in g]
    p95 = [r["k6"]["p95_ms"] for r in g]
    emb = [r["ingestion"]["mean_per_s"] for r in g if r["ingestion"].get("mean_per_s") is not None]
    err = [r["application_requests"]["error_rate"] for r in g]
    ok  = sum(r["application_requests"]["successful_2xx"] for r in g)
    tot = sum(r["application_requests"]["total"] for r in g)
    lo_r, hi_r = iqr(rps); lo_p, hi_p = iqr(p95)
    summary[cfg] = {"rps": med(rps), "p95": med(p95), "emb": med(emb), "ok": ok, "tot": tot,
                    "rps_all": rps, "p95_all": p95, "emb_all": emb}
    print(f"{cfg:8} {len(g):>2} {med(rps):>9.1f} {lo_r:>7.1f}-{hi_r:<9.1f} {med(p95):>10.1f} {lo_p:>8.1f}-{hi_p:<10.1f} "
          f"{med(emb):>12.1f} {max(err):>9.4f} {ok:>4}/{tot}")

if "omp1" in summary and "omp10" in summary:
    a, b = summary["omp1"], summary["omp10"]
    print()
    print("RATIOS (median omp1 / median omp10)")
    print(f"  throughput      {a['rps']/b['rps']:.2f}x   ({a['rps']:.1f} vs {b['rps']:.1f} req/s)")
    print(f"  p95 latency     {b['p95']/a['p95']:.2f}x lower   ({a['p95']:.0f} vs {b['p95']:.0f} ms)")
    print(f"  ingestion       {a['emb']/b['emb']:.2f}x   ({a['emb']:.1f} vs {b['emb']:.1f} embeddings/s)")
    print()
    print("SEPARATION (do the two groups overlap?)")
    print(f"  rps  omp1 min={min(a['rps_all']):.1f}  omp10 max={max(b['rps_all']):.1f}  "
          f"-> {'disjoint' if min(a['rps_all']) > max(b['rps_all']) else 'OVERLAP'}")
    print(f"  p95  omp1 max={max(a['p95_all']):.0f}  omp10 min={min(b['p95_all']):.0f}  "
          f"-> {'disjoint' if max(a['p95_all']) < min(b['p95_all']) else 'OVERLAP'}")
    print()
    print("PER-TRIAL (alternating order)")
    for r in sorted(rows, key=lambda x: x["recorded_at"]):
        ar = r["application_requests"]
        print(f"  t{r['trial_index']} {r['config']:6} rps={r['k6']['rps']:6.1f} p95={r['k6']['p95_ms']:8.1f}ms "
              f"embed={r['ingestion'].get('mean_per_s'):6}/s  2xx={ar['successful_2xx']:>5}/{ar['total']:<5} "
              f"429={ar['rate_limited_429']} 503={ar['unavailable_503']} 5xx={ar['server_errors_5xx_other']}")
