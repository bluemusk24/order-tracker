# Incident Responder

Receives Grafana alerts on `POST /alerts` (port 8001), captures the evidence needed to
understand the incident, drives a coding assistant in headless mode, and then produces
the on-call responder's answer.

## Flow

```
Grafana alert ──POST /alerts──▶ create incident
                                 │
                                 ├─ capture evidence (Prometheus + Loki + Tempo)
                                 ├─ save incident.json / incident.md / latest.json
                                 │
                                 ├─ run `opencode run` headless, bundle attached
                                 │    └─ parse JSON event stream → answer text
                                 │
                                 └─ responder decides resolved vs escalated
                                      └─ POST 202 returns immediately, work runs in background
```

`POST /alerts` returns `202 Accepted` as soon as the bundle is on disk. The agent run
takes minutes, so poll `GET /incidents/{id}` until `status` becomes `responded`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/alerts` | Grafana webhook. Body `{"alerts":[{status,labels,annotations}]}` |
| `GET` | `/incidents` | List recorded incidents |
| `GET` | `/incidents/latest` | Most recent incident, including the agent answer and responder response |
| `GET` | `/incidents/{id}` | One incident; add `?include_evidence=true` for full telemetry |
| `GET` | `/healthz` | Liveness plus configured backends |

## Running

The service runs on the host rather than in Compose because it shells out to the
`opencode` CLI, which is installed on the host only.

```bash
cd incident-response
../.venv/bin/python -m uvicorn responder.main:app --host 0.0.0.0 --port 8001
```

Bind to `0.0.0.0` so Grafana can reach it. From a container the host is
`host.docker.internal:8001`.

## Configuration

All optional; defaults target a local Compose stack.

| Variable | Default | Purpose |
|---|---|---|
| `RESPONDER_INCIDENT_DIR` | `./incidents` | Where bundles are written |
| `RESPONDER_PROMETHEUS_URL` | `http://localhost:9090` | Metrics source |
| `RESPONDER_LOKI_URL` | `http://localhost:3100` | Logs source |
| `RESPONDER_TEMPO_URL` | `http://localhost:3200` | Traces source |
| `RESPONDER_SERVICE_LABEL` | `order-tracker` | Service name used in queries |
| `RESPONDER_LOG_LOOKBACK_MINUTES` | `15` | How far back to pull evidence |
| `RESPONDER_AGENT_COMMAND` | `opencode` | Coding assistant binary |
| `RESPONDER_AGENT_MODEL` | *(empty)* | Optional `provider/model` override |
| `RESPONDER_AGENT_DIR` | repo root | Working directory for the agent |
| `RESPONDER_AGENT_TIMEOUT` | `1800` | Seconds before the run is abandoned |
| `RESPONDER_AGENT_AUTO_APPROVE` | `1` | Pass `--auto` for unattended runs |
| `RESPONDER_AGENT_ATTACH_BUNDLE` | `1` | Attach `incident.md` to the agent prompt |
| `RESPONDER_AGENT_CONCURRENCY` | `1` | Simultaneous agent runs; extras queue |
| `RESPONDER_DEDUP_WINDOW` | `300` | Seconds an identical alert is suppressed |
| `RESPONDER_LOG_LIMIT` | `60` | Max Loki lines captured |
| `RESPONDER_TRACE_LIMIT` | `10` | Max Tempo traces captured |
| `RESPONDER_BACKEND_TIMEOUT` | `10` | Per-request timeout for the backends |

## Incident bundle

`incidents/<id>/incident.json` holds the alert plus everything captured; `incident.md`
is the same thing rendered for a human or the agent. `incidents/latest.json` is a
convenience pointer at the most recent run.

Captured per alert: affected endpoint (from `http_route`, falling back to the
description text), Prometheus series for the route, Loki log lines for the route, the
Tempo search results, and full span detail when the alert carries a `trace_id`.

## Agent invocation

```
opencode run "<prompt>" --format json --auto --dir <repo> --file=<bundle>
```

The prompt must come first and `--file` must use the `=` form. `--file` is a yargs
array option, so `--file <path> <prompt>` silently consumes the prompt as a second
file and opencode exits with `File not found: <prompt>`. The answer is read from the
`{"type":"text","part":{"text":...}}` events in the JSON stream.

## Responder decision

`resolved` when the assistant returns an answer it did not flag as needing a
developer; `escalated` when it says the issue is out of scope, when it fails, or when
it returns nothing. The decision, the reason, and the assistant's final line are all
recorded on the incident.

## Tests

```bash
cd incident-response && ../.venv/bin/python -m pytest -q
```

The agent is mocked and the telemetry backends are stubbed, so the suite never spawns a
real run and never makes a network call.

## Duplicate alerts

Grafana re-notifies on its `repeat_interval`, so the same incident can arrive several
times. Alerts are keyed on `service` + `alertname` + `http_route` + `status_code`; a match
inside `RESPONDER_DEDUP_WINDOW` is not re-investigated and reports `duplicate_of`.

The incident record is written to disk **before** evidence capture. Capture takes roughly
30s against a live stack, and persisting afterwards let concurrent webhooks each create
their own incident.

## Concurrency

`RESPONDER_AGENT_CONCURRENCY` defaults to `1`. Four simultaneous agents each hit the
timeout and produced overlapping, partially-written edits. With a limit of one, extra
incidents stay `queued` and run in order.

## Partial runs

The agent is streamed to `incidents/<id>/agent.log` as it runs, and the last text answer is
persisted incrementally. If the run is abandoned at `RESPONDER_AGENT_TIMEOUT`,
`agent.partial` is `true` and whatever the agent had produced is retained — it may already
have applied a fix. Always check `git diff` before assuming the run failed outright.