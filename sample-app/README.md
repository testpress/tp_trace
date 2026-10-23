# tp_trace sample Django application

This directory is a Django application used to exercise the SDK's integrations. To run it with local PostgreSQL, Redis, RabbitMQ, Celery worker, the Collector, Tempo, Prometheus, and Grafana, follow the [development setup](../dev/README.md).

The application contains examples for PostgreSQL, replicas, PgBouncer, Redis, RabbitMQ / Celery asynchronous tasks, outbound HTTP, Boto3/MinIO, errors, slow requests, and Django templates.

## Running Celery tasks

To trigger background Celery execution:

```bash
# Publish a successful task
curl -X POST http://localhost:8002/api/celery/publish/ \
  -H "Content-Type: application/json" \
  -d '{"value": 42, "fail": false}'

# Publish a failing task
curl -X POST http://localhost:8002/api/celery/publish/ \
  -H "Content-Type: application/json" \
  -d '{"value": 13, "fail": true}'
```

To run a standalone Celery worker locally:

```bash
celery -A api.celery_app:app worker --loglevel=info --concurrency=2
```

Use [the first-trace tutorial](../docs/tutorials/first-trace.md) for the SDK setup and [the integration reference](../docs/reference/integrations.md) to see what each example produces.

## Verify the Collector memory/cardinality fix locally

Run the bounded A/B regression from the repository root:

```bash
bash sample-app/generate_traffic.sh --collector-demo
```

It requires Docker Compose and the sample image created by the development
stack. If that image is missing, explicitly build it with:

```bash
bash sample-app/generate_traffic.sh --collector-demo --build
```

This mode starts its own Compose project with randomly assigned loopback ports.
It uses two copies of the sample app (`demo-v1` and `demo-v2`) and a fan-out
Collector that sends the **same spans** to Collector 0.96.0 and 0.153.0. It uses
in-memory SQLite settings and bounded, read-only core, catalog, checkout, and
search endpoints; no PostgreSQL,
Redis, external APIs, or existing development data are involved. The endpoint is
otherwise disabled unless `COLLECTOR_DEMO_ENABLED=true` is explicitly set.

The command exits nonzero if any of these checks fails:

- SERVER/CLIENT counters, histogram sums, counts, and buckets agree before expiry.
- INTERNAL combinations exist in old metrics and are absent from fixed metrics.
- Old retains inactive operations; fixed expires them while other operations in
  the **same resource** continue receiving traffic.
- Old retains the previous application version; fixed expires that resource.
- Old retained combinations grow across successive bursts; fixed returns to its
  active combinations between bursts. Repeating identical requests alone would
  only increase counters.
- Excess combinations produce an explicit overflow datapoint in the fixed path,
  with no more than the per-resource limit plus its overflow entry.
- Both trace sinks receive the same span IDs, including INTERNAL spans and SDK
  resource metadata.
- After all traffic stops, old continues exporting fresh cumulative snapshots;
  fixed stops re-exporting call points after expiry.

The default demo uses **6-second expiry, a limit of 16, a 1-second flush, and 64
bounded operations split across three bursts**. Catalog, checkout, and search
produce different route labels. Each burst introduces previously unused
operations; the gaps keep only the core operation active. The report records
retained combinations, RSS, and refused attempts after every burst. It finishes
in roughly one to two minutes. Production uses
5-minute expiry, a limit of 5,000, and a 15-second flush. The reduced fixtures
exercise the same aggregation/filtering behavior, rather than claiming to be a
byte-for-byte production configuration. See all bounded options with:

```bash
bash sample-app/generate_traffic.sh --collector-demo --help
```

The printed evidence directory contains the generated configs, exported metric
snapshots, trace JSON, container logs, internal memory/refusal measurements, and
`result.json`. Containers and their network are removed on completion or Ctrl+C;
the evidence directory remains. Existing development containers and volumes are
not reset.

This proves the **retention and cardinality mechanisms**, not a production OOM.
A small run may have zero refusals in both versions, and the newer Collector may
have a higher baseline RSS. Trace files prove Collector export, **not Tempo
storage**, and this fixture does not test Prometheus remote write or Grafana.
For the normal development stack, run `make smoke` separately to check storage
and dashboard provisioning; production still needs a sustained memory soak and
remote-write failure diagnosis.

### Watch memory over time

For a ten-minute observation, run:

```bash
bash sample-app/generate_traffic.sh --collector-demo --soak-seconds 600
```

Open the printed **Live memory chart** URL. The page updates every two seconds
with old/fixed RSS, Go heap allocation, CPU usage, exported call combinations, and refused
span attempts. The terminal prints the same measurements, and `memory.csv` /
`samples.json` are flushed throughout the run. After cleanup, `memory.html`
remains a standalone snapshot of the charts.

Each burst introduces 32 new operations across the three routes at at most
20 requests/second, followed by an expiry gap with one core operation still
active. The soak uses an explicit finite corpus of 16,383 non-core operations;
reaching that ceiling ends new-operation generation and is recorded in the
report. Duration is bounded to 30–3,600 seconds. To increase pressure within
those bounds:

```bash
bash sample-app/generate_traffic.sh --collector-demo \
  --soak-seconds 600 --soak-batch 128 --soak-rate 50 --sample-interval 2
```

Look for old retained combinations continuing to grow across bursts while fixed
combinations return near their active baseline during gaps. Heap allocation and
RSS may move differently because Go can reclaim objects without immediately
returning pages to the OS. The test records measurements rather than declaring
that RSS must fall, the old Collector must OOM, or a one-minute sample proves
production stability. Refusals and missing samples remain visible.

The long soak uses a calls-only Prometheus export for counting combinations;
histograms still aggregate inside both connectors. It avoids full file sinks
and bounds container logs, so repeated snapshots do not fill the disk. It does
not perform the short mode's span-by-span equivalence assertions. The two compared
Collectors use 512 MiB each; the gateway uses 256 MiB. Run the short mode separately for the
strict regression checks. No existing stack is reset.


### Reach memory pressure sooner

Use the same reduced budgets for both compared Collectors and introduce more
new operations per burst:

```bash
bash sample-app/generate_traffic.sh --collector-demo \
  --soak-seconds 300 --soak-batch 128 --soak-rate 50 \
  --collector-memory-mib 256 --heap-limit-mib 96 --heap-spike-mib 16
```

This sets a 256 MiB container limit and an 80 MiB soft / 96 MiB hard heap limit
for both versions. The gateway and applications keep their separate budgets.
The heap threshold must leave at least 64 MiB below the container limit for
runtime overhead. These are local diagnostic limits, not production sizing.

Watch whether old refused attempts rise while fixed keeps processing and
expiring combinations. The report explicitly records `old_only_refusals_observed`;
it stays false if the run never reaches that condition. A higher rate alone can
overload both Collectors. New distinct operations and lower equal memory budgets
make the retention difference easier to observe without requiring an OOM.


CPU is included in each soak log line, CSV, and chart as a percentage of one
core, calculated from CPU-seconds deltas over actual elapsed time. Each compared
Collector has a 0.5-core quota, so about 50% corresponds to its full quota. The
first sample, missing counters, and counter resets show an unknown value rather
than a fabricated zero.
