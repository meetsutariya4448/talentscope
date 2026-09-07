"""Record one A/B trial as a JSON line. Reads config from env, k6 summary and
the api's own status counters."""
import json, os, pathlib, re, datetime

OUT = pathlib.Path("evals/thread-ab/trials.jsonl")
OUT.parent.mkdir(parents=True, exist_ok=True)

LABEL_RE = re.compile(r'^http_requests_total\{(.+?)\}\s+([0-9.eE+]+)$')

def parse(path):
    out = {}
    p = pathlib.Path(path)
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        m = LABEL_RE.match(line.strip())
        if not m:
            continue
        labels = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        out[(labels.get("method"), labels.get("path"), labels.get("status"))] = float(m.group(2))
    return out

before, after = parse("/tmp/ts_before.txt"), parse("/tmp/ts_after.txt")

# Application paths only: exclude the probe and scrape endpoints, which are
# infrastructure traffic, not requests the load test made.
EXCLUDE = {"/ready", "/health", "/metrics"}
by_status = {}
for key, val in after.items():
    method, path, status = key
    if path in EXCLUDE:
        continue
    delta = val - before.get(key, 0.0)
    if delta > 0:
        by_status[status] = by_status.get(status, 0.0) + delta

total = sum(by_status.values())
ok = by_status.get("200", 0.0)
rate_limited = by_status.get("429", 0.0)
unavailable = by_status.get("503", 0.0)
server_err = sum(v for s, v in by_status.items() if s.startswith("5") and s != "503")
client_err = sum(v for s, v in by_status.items() if s.startswith("4") and s != "429")

k6 = {}
try:
    m = json.loads(pathlib.Path(os.environ["SUMMARY"]).read_text())["metrics"]
    d = m.get("http_req_duration", {})
    k6 = {
        "http_reqs": m.get("http_reqs", {}).get("count"),
        "rps": m.get("http_reqs", {}).get("rate"),
        "p50_ms": d.get("med"), "p95_ms": d.get("p(95)"),
        "p90_ms": d.get("p(90)"), "max_ms": d.get("max"), "avg_ms": d.get("avg"),
        "failed_rate": m.get("http_req_failed", {}).get("value"),
        "vus_max": m.get("vus_max", {}).get("value"),
        "checks_succeeded": m.get("checks_succeeded", {}).get("value"),
    }
except Exception as exc:
    k6 = {"error": str(exc)}

# Ingestion throughput over the sampled window.
emb = {"mean_per_s": None, "peak_per_s": None, "delta": None, "window_s": None}
try:
    rows = [l.split() for l in pathlib.Path(os.environ["SAMP"]).read_text().splitlines() if l.strip()]
    rows = [(int(a), int(b)) for a, b in rows]
    if len(rows) >= 2:
        delta = rows[-1][1] - rows[0][1]
        secs = (rows[-1][0] - rows[0][0]) or 1
        # Two bracketing samples only, so mean over the window — no peak.
        emb = {"mean_per_s": round(delta / secs, 2), "peak_per_s": None,
               "delta": delta, "window_s": secs, "samples": len(rows)}
except Exception as exc:
    emb["error"] = str(exc)

k6_wall = int(os.environ.get("K6_END", 0)) - int(os.environ.get("K6_START", 0))

# k6's http_reqs.rate and avg are count/wall and mean — both are destroyed by a
# single pathological sample. Percentiles are order statistics and survive it.
# A max far beyond any plausible request time is a host-level stall (observed:
# a 623s http_req_duration inside an iteration whose own max was 1.7s), so it is
# flagged rather than silently averaged in.
STALL_THRESHOLD_MS = 60_000
max_ms = (k6 or {}).get("max_ms") or 0
stall = max_ms > STALL_THRESHOLD_MS

row = {
    "run_id": os.environ["RUN_ID"],
    "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "trial_index": int(os.environ["IDX"]),
    "config": os.environ["LABEL"].split("-")[0],
    "omp_num_threads": int(os.environ["OMP"]),
    "commit": os.environ["COMMIT"],
    "tree_dirty": os.environ["DIRTY"] == "true",
    "image": os.environ["IMG"],
    "held_fixed": {
        "api_nano_cpus": int(os.environ["API_NANO"]),
        "api_memory_bytes": int(os.environ["API_MEM"]),
        "worker_nano_cpus": int(os.environ["W_NANO"]),
        "worker_memory_bytes": int(os.environ["W_MEM"]),
        "worker_celery_processes": int(os.environ["W_PROCS"] or 0),
        "worker_concurrency": 2,
        "embed_rate_limit": "",
        "vus": int(os.environ["VUS"]),
        "duration": os.environ["DURATION"],
        "ingestion_backlog": int(os.environ["BACKLOG"]),
        "dataset_postings": int(os.environ["DATASET"] or 0),
    },
    "observed_api_torch_threads": int(os.environ["API_TORCH"] or 0),
    "k6": k6,
    "requests_by_status": {k: int(v) for k, v in sorted(by_status.items())},
    "application_requests": {
        "total": int(total),
        "successful_2xx": int(ok),
        "rate_limited_429": int(rate_limited),
        "unavailable_503": int(unavailable),
        "server_errors_5xx_other": int(server_err),
        "client_errors_4xx_other": int(client_err),
        "error_rate": round((total - ok) / total, 5) if total else None,
    },
    "ingestion": emb,
    "timing": {
        "k6_wall_seconds": k6_wall,
        "k6_configured_duration": os.environ["DURATION"],
        "k6_exit": int(os.environ.get("K6_EXIT", 0)),
        "host_stall_detected": stall,
        "stall_threshold_ms": STALL_THRESHOLD_MS,
    },
    # Server-side throughput over the true measured window. Preferred over
    # k6's http_reqs.rate, which a stall corrupts.
    "throughput_2xx_per_s": round(ok / k6_wall, 2) if k6_wall > 0 else None,
    "k6_summary_file": os.environ["SUMMARY"],
}
with OUT.open("a") as fh:
    fh.write(json.dumps(row) + "\n")
print(f"  recorded {row['run_id']}: tput={row['throughput_2xx_per_s']}/s p95={k6.get('p95_ms')} "
      f"ok={int(ok)}/{int(total)} embed={emb.get('mean_per_s')}/s torch={row['observed_api_torch_threads']} "
      f"wall={k6_wall}s stall={stall}")
