# AGENTS.md

## Project

tp_trace is a production Python APM library built on OpenTelemetry.
Changes can affect host application performance and telemetry volume.
Python 3.8 compatibility is required; the Django integration supports Django 4.2
or later. Check `pyproject.toml` for the declared dependency contracts.

## Working rules

- Read relevant code, tests, and documented contracts before modifying anything.
- Make the smallest change necessary; do not perform unrelated refactors or cleanup.
- Preserve backwards compatibility unless explicitly requested.
- Do not add dependencies without justification.
- Follow [docs/code-conventions.md](docs/code-conventions.md).
- Follow existing repository conventions. If an existing pattern conflicts with
  correctness, safety, or maintainability, explain the problem and make the
  smallest justified improvement.
- Preserve existing user changes and persistent development data.
- Version and changelog updates belong to explicit release work.

## Releases

The release workflow is manual via GitHub Actions (`workflow_dispatch`):

- Choose `bump_type`: `patch`, `minor`, or `major`.
- The workflow reads the current version from `pyproject.toml`, computes the
  bumped version (major/minor/patch), updates it with `sed` in
  `pyproject.toml` and `src/tp_trace/version.py`, commits the bump, creates an
  annotated git tag, and pushes both.
- It then builds with `uv build` and publishes to PyPI with `uv publish`.
- A `dry_run` option skips tag creation and publishing.
- Changelog edits are human-maintained; the workflow does not touch
  `CHANGELOG.md`.
- Tag and push happen **after** a successful build and publish, so a
  publish failure does not leave a published version tag behind.

Pre-release tagging convention (manual, not handled by the workflow):
commits on an in-progress release track may carry a pre-release tag (e.g.
`vX.Y.Z-rc.1`). The release workflow only cuts final `vX.Y.Z` tags; it does
not parse or advance `-rc` suffix strings. On release, delete the old
pre-release tag and let the workflow publish the final version.

## Telemetry safety

Telemetry failures must never prevent application execution, execute application
code twice, alter its return value, or suppress or replace its original exception.
Read `src/tp_trace/safety.py` before changing instrumentation wrappers.

For changes to metrics, spans, attributes, sampling, exporters, histograms, or
Collector configuration:

- Evaluate cardinality and CPU, memory, and network impact.
- Avoid unbounded metric attributes.
- Check affected Grafana dashboards and other telemetry consumers.
- Preserve existing telemetry semantics unless intentionally changing them.
- Treat existing metric names, labels, span attributes, and units as compatibility
  interfaces; assess downstream consumers before changing them.

## Repository map

- `src/tp_trace/`: published SDK and optional integrations.
- `tests/test_*.py`: SDK tests; `tests/conftest.py` provides shared setup.
- `tests/e2e/`: explicit live-stack probes, outside ordinary pytest discovery.
- `sample-app/`: instrumented Django application and traffic generator.
- `dev/`: local observability stack; root Compose delegates here.
- `grafana/`: canonical dashboards and datasource provisioning.
- `setup/`: separate deployment configuration. Preserve its purpose; assess shared
  changes without copying intentional local exposure into deployment settings.
- `.agents/skills/`: repository skills for commit conventions, branch creation, debugging,
  and OpenTelemetry workflows. Use applicable skills without
  treating their guidance as permission for unrelated external actions.

## Validation

Before completing a change:

- Run relevant tests and configured lint/type checks (`make lint`).
- For instrumentation changes, test against the development environment and inspect
  emitted telemetry when applicable.
- Report unavailable checks and verification gaps.
- Never change tests solely to make an incorrect implementation pass.

Useful commands:

| Scope | Command |
|---|---|
| Lint & Typecheck | `make lint` |
| Pre-commit hooks | `make pre-commit` |
| Focused SDK tests | `make test ARGS='tests/test_relevant.py'` |
| Full SDK suite | `make test` |
| Packaging | `make build` |
| Local Compose | `make check-config` |
| Deployment Compose, when affected | `docker compose -f setup/docker-compose.yml config --quiet` |
| Live pipeline | `make smoke` |
| Oversized export regression | `python3 tests/e2e/check_large_batch.py` |
| Traffic script syntax | `bash -n sample-app/generate_traffic.sh` |

`make up` applies Compose changes using existing images; `make rebuild` builds
explicitly. `make restart SERVICE=otel-collector` reloads the collector's mounted
configuration. Do not start infrastructure or generate traffic merely for a
documentation change. A running container or HTTP 200 alone does not prove that
telemetry reaches storage or that dashboard queries return useful data.

## Completion

Report what changed, tests performed, compatibility impact, telemetry/performance
impact, and remaining risks. Keep the report proportional to the change and
state when an impact is absent or unverified.
