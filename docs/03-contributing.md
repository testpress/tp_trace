# Contributing

## Prepare a Change

Read [code conventions](code-conventions.md), [`AGENTS.md`](../AGENTS.md), and the code and tests for the affected behavior. Keep SDK changes compatible with Python 3.8 and preserve optional integration behavior when the target library is absent. Use a separate branch or worktree and preserve existing development data.

## Locate the Relevant Code

| Area | Location |
|---|---|
| SDK initialization and public helpers | `src/tp_trace/__init__.py` |
| Configuration resolution | `src/tp_trace/config.py` |
| Integration lifecycle and wrappers | `src/tp_trace/integrations/` |
| Telemetry failure isolation | `src/tp_trace/safety.py`, `exporter.py` |
| Native metrics and dependency aggregation | `src/tp_trace/metrics.py`, `red.py` |
| Dashboards and datasource provisioning | `grafana/` |
| Local stack and sample application | `dev/` |
| Deployment automation | `ansible/`, `setup/` |
| SDK tests and explicit live probes | `tests/`, `tests/e2e/` |

## Validate a Change

Use the root [local setup instructions](../README.md) to prepare the environment. Run `make lint` and the tests for the affected behavior, then the full SDK suite when changes cross integration boundaries. Live probes are explicit commands and must not become part of ordinary pytest collection.

For wrappers, verify application execution occurs once and preserves results and exceptions when telemetry preparation or export fails. For metric changes, assess series growth, units, label contracts, and affected dashboard queries. For configuration moves, validate both local and deployment Compose files and all build paths.

## Submit a Change

Describe the resulting behavior, compatibility impact, telemetry/performance impact, checks run, and verification gaps. Keep version and changelog updates for explicit release work. Do not commit credentials, generated packages, caches, or real environment files.
