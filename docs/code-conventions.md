# Code conventions

## General

- Python 3.8 compatibility is required.
- Follow the existing module structure and nearby code style.
- Prefer simple functions over unnecessary abstractions.
- Avoid unrelated cleanup while implementing changes.
- Keep published SDK code under `src/tp_trace/` independent of sample-app,
  Docker, Grafana, and optional integration packages at import time.

## Typing

- Type new public APIs.
- Preserve the typing style of existing internal code.
- Do not introduce syntax or runtime APIs unavailable in Python 3.8 without an
  explicit compatibility path.

## Logging

- Use module loggers under the package namespace; do not configure the host
  application's root logger.
- Instrumentation failures must not break the host application.
- Avoid logging sensitive request/application data, tokens, or credentials.

## Instrumentation

- Keep hot-path overhead minimal; avoid network calls in instrumentation paths.
- Execute application work exactly once and preserve its arguments, return value,
  and original exception, including when telemetry setup or enrichment fails.
- Use existing `safety.py`, tracing, and integration lifecycle helpers.
- Keep patching/unpatching reversible and safe to repeat; missing optional
  dependencies must not prevent SDK import or startup.
- Balance context attachment and detachment. Do not leak request data across
  requests, threads, or async tasks through shared mutable state.
- Bound retries, queues, and shutdown waits; respect provider/resource ownership.
- Never use unbounded values as metric attributes.
- Prefer OpenTelemetry semantic conventions where applicable. Treat existing
  metric names, labels, span attributes, and units as compatibility interfaces;
  assess consumers before changing them.
- Avoid depending on private third-party APIs. If unavoidable, isolate the usage
  and test the supported integration paths.
- Preserve SQL/URL sanitization and configurable masking. Redis command/endpoint
  exposure is a documented default; masking remains an explicit option.
- Distinguish SDK metrics from metrics derived from sampled traces; their counts
  are not automatically interchangeable.

## Configuration

- Existing environment variables are backwards-compatible interfaces, including
  supported `TP_TRACE_*` and `OTEL_*` aliases.
- New configuration must have safe defaults.
- Preserve documented precedence for existing fields. Precedence is field-specific;
  inspect `SDKConfig.from_env_and_kwargs()` rather than assuming a universal order.
- Resolve new fields through the established configuration layer. Integrations
  consume resolved values instead of adding competing environment interpretations.

## Tests

- Behavioral changes require tests; bug fixes should include regression tests at
  the actual failure seam.
- Test externally observable behavior rather than implementation details.
- Wrapper tests should check application call count, results/exceptions, and
  relevant telemetry failure paths.
- Prefer in-memory exporters/readers and existing reset fixtures for SDK tests.
- Keep live probes in `tests/e2e/` and run them explicitly; ordinary test collection
  must not start Docker or generate application traffic.
- Never change tests solely to make an incorrect implementation pass.

## Operational artifacts

- Keep `grafana/` as the dashboard source of truth. Check datasource UIDs, metric
  units/labels, absent-label filters, and rate windows against export cadence.
- Dashboard defaults are one hour with a 30-second refresh unless intentionally changed.
- Batch span counts are not byte limits; assess representative payload sizes
  against Tempo receive limits and collector memory/queue budgets.
- Keep local networking and anonymous Grafana access confined to the intentional
  dev workflow. `setup/` is a separate deployment boundary.
- Do not commit generated distributions, caches, real environment files,
  credentials, or reference checkouts. Environment templates use `.example`.
