"""Bounded memory soak and dependency-free live charts for the Collector demo."""

import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.request import Request, urlopen


PAGE = '''<!doctype html><meta charset="utf-8"><title>Collector memory soak</title>
<style>body{font:16px system-ui;max-width:1100px;margin:30px auto;background:#101820;color:#e6edf3}
canvas{width:100%;height:220px;background:#17232e}p{line-height:1.6}code{color:#8dd8ce}</style>
<h1>Old vs fixed Collector — memory over time</h1>
<p>Old: orange · Fixed: teal. Every burst adds new operations; expiry gaps keep one operation active.
RSS may stay high after Go reclaims objects. Watch heap and retained combinations too.</p>
<p>BUDGET_DATA</p><p id="status">Waiting for samples</p>
<h2>RSS (MiB)</h2><canvas id="rss_bytes" width="1100" height="220"></canvas>
<h2>Go heap allocated (MiB)</h2><canvas id="heap_bytes" width="1100" height="220"></canvas>
<h2>CPU (% of one core)</h2><p>100% = one core. Each compared Collector has a 0.5-core quota (about 50%).</p>
<canvas id="cpu_percent" width="1100" height="220"></canvas>
<h2>Currently exported call combinations</h2><canvas id="combinations" width="1100" height="220"></canvas>
<p id="refusals"></p><p>This is a bounded diagnostic, not proof of production memory stability or backend ingestion.</p>
<script id="initial" type="application/json">INITIAL_DATA</script><script>
function draw(rows){
 if(!rows.length)return;const last=rows[rows.length-1];
 document.getElementById('status').textContent=`${last.elapsed_seconds.toFixed(1)}s · ${last.phase} · ${rows.length} samples`;
 document.getElementById('refusals').textContent=['old','fixed'].map(n=>`${n} refused attempts: ${last[n].refused_span_attempts ?? 'unavailable'}`).join(' | ');
 for(const field of ['rss_bytes','heap_bytes','cpu_percent','combinations']){
  const c=document.getElementById(field),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);
  const scale=field.endsWith('bytes')?1048576:1;
  const vals=rows.flatMap(r=>['old','fixed'].map(n=>r[n][field])).filter(v=>v!==null && v!==undefined);
  const ymax=Math.max(1,...vals)/scale*1.1,tmax=Math.max(1,last.elapsed_seconds);
  x.fillStyle='#aebdca';x.font='13px system-ui';x.fillText(ymax.toFixed(1),5,15);x.fillText('0',5,205);x.fillText(`${tmax.toFixed(0)} seconds`,950,218);
  for(const [name,color] of [['old','#ffac60'],['fixed','#67d8ca']]){
   x.beginPath();x.strokeStyle=color;x.lineWidth=2;let pen=false;
   for(const row of rows){const v=row[name][field];if(v===null || v===undefined){pen=false;continue;}
    const a=45+row.elapsed_seconds/tmax*1040,b=200-v/scale/ymax*180;
    if(pen)x.lineTo(a,b);else x.moveTo(a,b);pen=true;
   }x.stroke();
  }
 }
}
draw(JSON.parse(document.getElementById('initial').textContent));
if(location.protocol!=='file:')setInterval(()=>fetch('/samples.json',{cache:'no-store'}).then(r=>r.json()).then(draw).catch(()=>{}),2000);
</script>'''


def metric_value(text, prefix, total=False):
    names = {prefix, prefix + "_bytes", prefix + "_total"}
    values = [float(line.split()[-1]) for line in text.splitlines() if line.strip()
              and line.split("{", 1)[0].split()[0] in names]
    return sum(values) if values and total else (values[0] if values else None)


def cpu_usage(previous, current):
    """CPU-seconds delta / monotonic wall-seconds delta; 100% is one core."""
    if previous is None:
        return None
    values = [point.get(key) for point in (previous, current) for key in ("cpu_seconds", "cpu_sample_time")]
    if any(value is None for value in values):
        return None
    delta = current["cpu_seconds"] - previous["cpu_seconds"]
    elapsed = current["cpu_sample_time"] - previous["cpu_sample_time"]
    if delta < 0 or elapsed <= 0:
        return None
    return round(100 * delta / elapsed, 2)


def sample(urls, name):
    result = {"rss_bytes": None, "heap_bytes": None, "refused_span_attempts": None, "combinations": None,
              "cpu_seconds": None, "cpu_sample_time": None}
    try:
        with urlopen(urls[name] + "/metrics", timeout=3) as response:
            text = response.read().decode()
        result.update(cpu_seconds=metric_value(text, "otelcol_process_cpu_seconds"), cpu_sample_time=time.monotonic(),
                      rss_bytes=metric_value(text, "otelcol_process_memory_rss"),
                      heap_bytes=metric_value(text, "otelcol_process_runtime_heap_alloc_bytes"),
                      refused_span_attempts=metric_value(text, "otelcol_receiver_refused_spans", total=True))
        with urlopen(urls[name + "_spanmetrics"] + "/metrics", timeout=3) as response:
            # Only call counters are exposed; histograms remain aggregated inside
            # the connector but do not inflate the monitor's response payload.
            result["combinations"] = sum(1 for line in response if line.startswith(b"apm_calls_total{"))
    except OSError as exc:
        result["error"] = str(exc)
    return result


def run_soak(directory, urls, args):
    page = PAGE.replace("BUDGET_DATA", "Equal container budgets: {} MiB; heap soft: {} MiB; hard: {} MiB.".format(
        args.collector_memory_mib, args.heap_limit_mib - args.heap_spike_mib, args.heap_limit_mib))
    rows = []
    phase = ["starting"]
    stop = threading.Event()
    started = time.monotonic()
    csv_path = directory / "memory.csv"
    fields = ["elapsed_seconds", "phase"] + [name + "_" + field for name in ("old", "fixed")
              for field in ("rss_bytes", "heap_bytes", "cpu_percent", "combinations", "refused_span_attempts")]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/samples.json"):
                self.send_error(404)
                return
            path = directory / ("memory.html" if self.path == "/" else "samples.json")
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8" if self.path == "/" else "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    (directory / "samples.json").write_text("[]")
    (directory / "memory.html").write_text(page.replace("INITIAL_DATA", "[]"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("Live memory chart: http://127.0.0.1:{}/".format(server.server_port), flush=True)
    print("CSV history: {}".format(csv_path), flush=True)

    def monitor():
        previous = {}
        with csv_path.open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            while not stop.is_set():
                row = {"elapsed_seconds": round(time.monotonic() - started, 2), "phase": phase[0]}
                for name in ("old", "fixed"):
                    row[name] = sample(urls, name)
                    row[name]["cpu_percent"] = cpu_usage(previous.get(name), row[name])
                    if row[name]["cpu_seconds"] is not None:
                        previous[name] = row[name]
                rows.append(row)
                flat = {"elapsed_seconds": row["elapsed_seconds"], "phase": row["phase"]}
                flat.update({name + "_" + field: row[name][field] for name in ("old", "fixed")
                             for field in ("rss_bytes", "heap_bytes", "cpu_percent", "combinations", "refused_span_attempts")})
                writer.writerow(flat)
                output.flush()
                temporary = directory / "samples.tmp"
                temporary.write_text(json.dumps(rows))
                temporary.replace(directory / "samples.json")
                def show(name):
                    r = row[name]
                    return "{} RSS={} MiB heap={} MiB CPU={}% combinations={} refused={}".format(
                        name, round(r["rss_bytes"] / 1048576, 1) if r["rss_bytes"] is not None else "?",
                        round(r["heap_bytes"] / 1048576, 1) if r["heap_bytes"] is not None else "?",
                        r["cpu_percent"] if r["cpu_percent"] is not None else "?",
                        r["combinations"], r["refused_span_attempts"])
                print("{:6.1f}s {} | {} | {}".format(row["elapsed_seconds"], phase[0], show("old"), show("fixed")), flush=True)
                stop.wait(args.sample_interval)

    errors = []
    def guarded_monitor():
        try:
            monitor()
        except Exception as exc:
            errors.append(str(exc))
    worker = threading.Thread(target=guarded_monitor, daemon=True)
    worker.start()
    deadline = started + args.soak_seconds
    operation = 1
    requests = 0
    seen_operations = set()
    bursts = 0

    def hit(op, workload=""):
        nonlocal requests
        suffix = workload + "/" if workload else ""
        with urlopen(Request(urls["v2"] + "/api/collector-demo/{}{}/".format(suffix, op)), timeout=10) as response:
            result = json.load(response)
            if result["operation"] != op or result.get("trace_id") in (None, "0" * 32):
                raise AssertionError("Sample workload tracing is not active")
        requests += 1
        if op:
            seen_operations.add(op)

    try:
        while time.monotonic() < deadline and operation < 16384:
            bursts += 1
            phase[0] = "burst {}".format(bursts)
            for _ in range(args.soak_batch):
                for workload in ("catalog", "checkout", "search"):
                    if time.monotonic() >= deadline:
                        break
                    before = time.monotonic()
                    hit(operation, workload)
                    time.sleep(max(0, 1 / args.soak_rate - (time.monotonic() - before)))
                operation += 1
                if time.monotonic() >= deadline or operation >= 16384:
                    break
            phase[0] = "expiry gap {}".format(bursts)
            gap = min(deadline, time.monotonic() + args.expiration + 5)
            while time.monotonic() < gap:
                hit(0)
                time.sleep(0.5)
        phase[0] = "final silence"
        stop.wait(args.expiration + 5)
    finally:
        stop.set()
        worker.join(timeout=15)
        server.shutdown()
        server.server_close()
        (directory / "memory.html").write_text(page.replace("INITIAL_DATA", json.dumps(rows)))
    if errors or worker.is_alive():
        raise AssertionError("Memory monitor failed: {}".format(errors))
    if not all(any((row[name]["combinations"] or 0) > 0 for row in rows) for name in ("old", "fixed")):
        raise AssertionError("No span-metric data observed on both Collectors")
    if not rows or not any(row["old"]["rss_bytes"] is not None and row["fixed"]["rss_bytes"] is not None for row in rows):
        raise AssertionError("No comparable Collector memory samples were captured")
    refusals = {}
    for name in ("old", "fixed"):
        observed = [row[name]["refused_span_attempts"] for row in rows if row[name]["refused_span_attempts"] is not None]
        refusals[name] = max(observed) if observed else None
    old_only = refusals["old"] is not None and refusals["old"] > 0 and refusals["fixed"] == 0
    print("Refusal attempts observed: old={}, fixed={}; old-only refusal={}".format(
        refusals["old"], refusals["fixed"], old_only), flush=True)
    return {"mode": "memory_soak", "refused_span_attempts": refusals, "old_only_refusals_observed": old_only, "requested_seconds": args.soak_seconds, "sample_count": len(rows),
            "bursts": bursts, "requests": requests, "distinct_operations": len(seen_operations),
            "corpus_exhausted": operation >= 16384, "memory_soak_verified": False,
            "note": "Measurements captured; inspect trends. This does not automatically prove production memory stability.",
            "charts": str(directory / "memory.html"), "csv": str(csv_path)}
