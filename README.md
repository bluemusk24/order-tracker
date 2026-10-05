# Order Tracker

A small order-tracking API used for an AI Dev Tools observability exercise. It ships with
a web page, an API, tests, a full OpenTelemetry stack (metrics, logs, traces), a
provisioned Grafana dashboard and alert rule, and an AI incident responder that receives
the alert, gathers evidence, and drives a headless coding agent to fix the fault.

The main user flow is creating an order and checking its status. Three sample orders are
created on first startup; one of them (`express-1002`) deliberately triggers a 500 so the
whole detect → respond → fix → verify loop can be exercised.

---

## Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Ports and services](#ports-and-services)
- [Observability](#observability)
- [Alerting](#alerting)
- [Incident responder](#incident-responder)
- [Reproducing the incident end to end](#reproducing-the-incident-end-to-end)
- [API reference](#api-reference)
- [Tests](#tests)
- [Configuration reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)

---

## Architecture

```
                    ┌──────────────┐
   browser  ───────▶│  app (8000)  │  FastAPI + SQLite
                    └──────┬───────┘
                           │ OTLP/HTTP (4318)
                           ▼
                    ┌──────────────────┐
                    │  otel-collector  │  fan-out
                    └──┬────┬─────┬────┘
          metrics ─────┘    │     └────── traces
                  ▼         │             ▼
          ┌──────────────┐  │      ┌──────────────┐
          │  prometheus  │  │      │    tempo     │
          │    (9090)    │  │      │   (3200)     │
          └──────┬───────┘  │      └──────┬───────┘
                 │          │ logs        │
                 │          ▼             │
                 │   ┌──────────────┐      │
                 │   │    loki      │      │
                 │   │   (3100)     │      │
                 │   └──────┬───────┘      │
                 └──────────┼──────────────┘
                            ▼
                     ┌──────────────┐        ┌──────────────────┐
                     │   grafana    │───────▶│ incident         │  POST /alerts
                     │   (3000)     │ webhook│ responder (8001) │
                     └──────────────┘        └────────┬─────────┘
                                                     │ opencode run (headless)
                                                     ▼
                                                code fix
```

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker + Compose v2 | `docker compose version` should work |
| Python 3.11+ and `uv` | Only needed to run tests locally |
| `opencode` CLI | Only needed for the incident responder |
| `curl` | Used throughout the verification steps |

On WSL2, if the Docker daemon is Windows-hosted, prefix commands with `docker.exe`, and
use `curl --noproxy '*'` when talking to published localhost ports (see
[Troubleshooting](#troubleshooting)).

## Quick start

```bash
docker compose up --build -d --wait
```

That starts six services. Once healthy:

| What | URL |
|---|---|
| Web app | <http://127.0.0.1:8000> |
| API | <http://127.0.0.1:8000/api/orders> |
| Health check | <http://127.0.0.1:8000/healthz> |
| Grafana | <http://127.0.0.1:3000> (`admin` / `admin`) |
| Prometheus | <http://127.0.0.1:9090> |
| Loki | <http://127.0.0.1:3100> |
| Tempo | <http://127.0.0.1:3200> |

Confirm data was seeded and telemetry is flowing:

```bash
curl -s http://127.0.0.1:8000/api/orders | python3 -m json.tool
curl -s http://127.0.0.1:8000/metrics | head        # Prometheus exposition endpoint
```

Stop everything with `docker compose down`. Add `-v` only if you also want to delete the
stored order data.

> The API source is **copied into the image**, not bind-mounted. After editing `app/`
> locally you must rebuild: `docker compose up --build -d --wait app`.

## Ports and services

| Service | Container port | Host port (override) |
|---|---|---|
| `app` | 8000 | `${ORDER_TRACKER_PORT:-8000}` |
| `otel-collector` | 4317, 4318 | `${OTLP_GRPC_PORT:-4317}`, `${OTLP_HTTP_PORT:-4318}` |
| `prometheus` | 9090 | `${PROMETHEUS_PORT:-9090}` |
| `loki` | 3100 | `${LOKI_PORT:-3100}` |
| `tempo` | 3200 | `${TEMPO_PORT:-3200}` |
| `grafana` | 3000 | `${GRAFANA_PORT:-3000}` |

All ports bind to `127.0.0.1` only. The incident responder (port 8001) runs on the host,
not in Compose.

## Observability

### Telemetry

`app/telemetry.py` wires three signals into the FastAPI app and is configured from
`OTEL_EXPORTER_OTLP_ENDPOINT`:

- **Metrics** — `RequestMetricsMiddleware` records `http_server_request_count_total` and
  `http_server_request_duration_seconds` per route template and status code. `healthz`
  is excluded.
- **Traces** — auto-instrumented via `FastAPIInstrumentor`, plus a manual `orders.lookup`
  span that records `order.id`, `order.found`, `order.status`, and `order.priority`.
- **Logs** — `LoggingInstrumentor` attaches trace and span IDs to every record, and order
  lookups emit a structured `order_lookup` event.

Because routes are labelled with the **route template** (`/api/orders/{order_id}`) rather
than the raw path, cardinality stays bounded no matter how many order IDs are requested.

### Dashboard

`Order Tracker API` (uid `order-tracker-api`) is provisioned automatically at startup —
no manual import. Seven panels:

1. Request rate by status (5m)
2. Server errors (5xx) (5m)
3. Client errors (4xx) by route (5m)
4. Total requests by status code
5. p95 request duration (5m)
6. Request logs (Loki)
7. Recent traces (Tempo)

Datasources are provisioned with fixed UIDs `prometheus`, `loki`, and `tempo`, so the
dashboard and alert rule bind without editing.

## Alerting

A Grafana Unified Alerting rule is provisioned from
`grafana/provisioning/alerting/order-tracker-5xx.yaml`:

```promql
sum by (http_route, http_response_status_code) (
  round(increase(http_server_request_count_total{
    otel_scope_name="order-tracker",
    http_response_status_code=~"5.."
  }[5m]))
)
```

- `for: 0s` — fires on the first evaluation that returns a value.
- `noDataState: NoData` — on a healthy, freshly restarted stack the series does not exist
  yet, so the rule reports **No data** rather than silently going normal. Grafana creates
  an active `DatasourceNoData` alert bound to `__alert_rule_uid__: order-tracker-5xx`.
- `execErrState: Error`.
- Annotations carry the affected endpoint, status code, response count in window, plus
  deep links to the dashboard, Loki, and Tempo.

Routing is configured in `responder-notifications.yaml`: a contact point named
`incident-responder` posts to `http://host.docker.internal:8001/alerts` with
`group_wait: 5s`, `group_interval: 10s`, and `repeat_interval: 2m`, matching alerts where
`service="order-tracker"`.

## Incident responder

See [`incident-response/README.md`](incident-response/README.md) for full detail.

```bash
cd incident-response
../.venv/bin/python -m uvicorn responder.main:app --host 0.0.0.0 --port 8001
```

It runs on the **host** rather than in Compose because it shells out to the `opencode`
CLI. Bind `0.0.0.0` so the Grafana container can reach it via `host.docker.internal:8001`.

Flow:

1. `POST /alerts` persists a minimal incident record and returns `202 Accepted`
   immediately, then attaches evidence (Prometheus series, Loki lines, Tempo traces).
2. A background task runs `opencode run "<prompt>" --format json --auto --dir <repo>
   --file=<bundle>`, streaming output to `incidents/<id>/agent.log`.
3. The responder decides `resolved` vs `escalated` and records the reason.

Two safeguards matter in practice:

- **Deduplication.** Grafana re-notifies every `repeat_interval`, so identical alerts
  inside `RESPONDER_DEDUP_WINDOW` are suppressed and point at the original
  `duplicate_of`. The incident is written to disk *before* evidence capture, otherwise
  concurrent webhooks each create their own incident during the ~30s capture window.
- **Single agent.** `RESPONDER_AGENT_CONCURRENCY` defaults to `1`; extra incidents queue.
  Concurrent agents exhausted the timeout and produced overlapping edits.

If the agent times out, partial output is kept: `agent.partial` is `true` and the
transcript plus any answer produced so far remain in `incident.json`.

## Reproducing the incident end to end

This is the full detect → respond → fix → verify loop.

### 1. Start the stack

```bash
docker compose up --build -d --wait
```

### 2. Start the responder

In a second terminal (see [Troubleshooting](#troubleshooting) for why tmux is handy):

```bash
cd incident-response
../.venv/bin/python -m uvicorn responder.main:app --host 0.0.0.0 --port 8001
```

Confirm Grafana can reach it:

```bash
docker compose exec grafana wget -qO- http://host.docker.internal:8001/healthz
```

### 3. Trigger the fault

```bash
curl -i http://127.0.0.1:8000/api/orders/express-1002
# HTTP/1.1 500 Internal Server Error
```

The app log shows the cause:

```
ValueError: day is out of range for month
```

### 4. Watch the alert fire

Within ~30s the rule evaluates, the contact point delivers the webhook, and the
responder records an incident. Poll it:

```bash
curl -s http://127.0.0.1:8001/incidents/latest | python3 -m json.tool | head -40
```

You should see exactly **one** incident, even though Grafana re-notifies on its repeat
interval — the rest are suppressed as duplicates. `status` moves `queued` →
`investigating` → `responded`. The agent run takes minutes.

### 5. Confirm the agent's change

The agent's edit lands in the working tree:

```bash
git diff app/main.py
```

The bug was in `app/main.py`:

```python
# before — day number pushed past the end of the month
estimated_at = placed_at.replace(day=placed_at.day + 2)

# after — real date arithmetic
estimated_at = placed_at + timedelta(days=2)
```

`express-1002` is seeded with `created_at` on the **last day of the previous month**, so
`day + 2` overflowed. The fix rolls over correctly across months, years, and leap years.

### 6. Rebuild, restart, verify

```bash
docker compose up --build -d --wait app
curl -i http://127.0.0.1:8000/api/orders/express-1002
```

Expected:

```
HTTP/1.1 200 OK

{"id":"express-1002","customer":"Sam","item":"Headphones","priority":"express",
 "status":"preparing","created_at":"2026-09-30T00:47:11+00:00",
 "estimated_delivery":"2026-10-02"}
```

The alert then returns to normal once the 5-minute window slides past the error.

## API reference

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Web page |
| `GET` | `/healthz` | Database health check |
| `GET` | `/api/orders` | List orders |
| `POST` | `/api/orders` | Create an order (`201`) |
| `GET` | `/api/orders/{id}` | Check an order |
| `PATCH` | `/api/orders/{id}` | Change an order status |
| `GET` | `/metrics` | Prometheus exposition endpoint |

Responder endpoints are documented in
[`incident-response/README.md`](incident-response/README.md).

The app uses SQLite to keep setup small. Run one app container at a time — the exercise
is about detecting and handling an incident, not scaling the database.

## Tests

```bash
# app tests
uv run --frozen pytest -q

# responder tests
cd incident-response && ../.venv/bin/python -m pytest -q
```

The responder suite mocks the agent and stubs the telemetry backends, so it never spawns
a real run and never makes a network call.

> **Slow imports on WSL.** If the virtualenv lives on a Windows mount (`/mnt/c/...`),
> expect the app suite to take ~15 minutes. `import fastapi` alone can take ~3 minutes
> and the OTLP HTTP exporter ~5, purely from filesystem latency — not a hang. Clone into
> the WSL filesystem (for example `~/src/order-tracker`) for a fast suite.

## Configuration reference

### App / Compose

| Variable | Default | Purpose |
|---|---|---|
| `ORDER_TRACKER_PORT` | `8000` | Host port for the app |
| `ORDER_TRACKER_SUBNET` | `10.215.24.0/24` | Compose network subnet |
| `ORDER_TRACKER_TAG` | `local` | Image tag |
| `GRAFANA_USER` / `GRAFANA_PASSWORD` | `admin` / `admin` | Grafana login |
| `ORDER_DB_PATH` | `/data/orders.db` | SQLite path inside the container |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4318` | OTLP target |
| `OTEL_METRIC_EXPORT_INTERVAL_MS` | `5000` | Metric push interval |
| `OTEL_TRACES_EXCLUDED_URLS` | `healthz` | Paths excluded from traces |
| `OTEL_METRICS_EXCLUDED_URLS` | `healthz` | Paths excluded from metrics |

Port overrides also exist for every observability service: `OTLP_GRPC_PORT`,
`OTLP_HTTP_PORT`, `PROMETHEUS_PORT`, `LOKI_PORT`, `TEMPO_PORT`, `GRAFANA_PORT`.

### Responder

See the full table in
[`incident-response/README.md`](incident-response/README.md). The most relevant:

| Variable | Default | Purpose |
|---|---|---|
| `RESPONDER_AGENT_COMMAND` | `opencode` | Coding assistant binary |
| `RESPONDER_AGENT_TIMEOUT` | `1800` | Seconds before the run is abandoned |
| `RESPONDER_AGENT_CONCURRENCY` | `1` | Simultaneous agent runs |
| `RESPONDER_DEDUP_WINDOW` | `300` | Seconds an identical alert is suppressed |
| `RESPONDER_PROMETHEUS_URL` | `http://localhost:9090` | Metrics source |
| `RESPONDER_LOKI_URL` | `http://localhost:3100` | Logs source |
| `RESPONDER_TEMPO_URL` | `http://localhost:3200` | Traces source |

## Troubleshooting

**`curl` to a published port hangs in WSL2.** WSL honours `HTTP_PROXY`/`HTTPS_PROXY` even
for localhost. Use `curl --noproxy '*' http://127.0.0.1:8000/...`.

**`docker.exe` fails with `UtilAcceptVsock: accept4 failed 110`.** WSL↔Windows interop
dropped. Retry — it usually recovers within a minute. If it does not, use the engine
socket directly from WSL instead of the CLI:

```bash
curl -s --unix-socket /var/run/docker.sock http://localhost/_ping
```

**Edits to `app/` have no effect.** The source is baked into the image; rebuild with
`docker compose up --build -d --wait app`.

**Background processes die between tool calls.** Long-running services should be started
under tmux:

```bash
tmux new-session -d -s responder -c "$PWD" \
  "../.venv/bin/python -m uvicorn responder.main:app --host 0.0.0.0 --port 8001 2>&1 | tee .run/responder.log"
```

**`opencode` exits with `File not found: <prompt>`.** The `--file` option is a yargs array,
so `--file <path> <prompt>` swallows the prompt as a second file. Use `--file=<path>` and
put the prompt first.

**The agent times out but may have already fixed the code.** Inspect the incident before
assuming failure:

```bash
python3 -m json.tool incident-response/incidents/<id>/incident.json | head -40
git diff
```

The agent is told not to restart the stack, so the fix may be staged but not live.

**Metrics look empty right after startup.** Counters are held in-process and reset on
restart. Wait for the next export interval (5s) and re-check.

**Grafana shows the alert as inactive but Prometheus shows no data.** Prometheus-compatible
status endpoints report `nodata` for the rule while the authoritative Alertmanager state
lives at `/api/alertmanager/grafana/api/v2/alerts`. Query that for the real state.

## Project layout

```
.
├── app/
│   ├── main.py            FastAPI app, endpoints, seeded data
│   └── telemetry.py       OTel metrics/logs/traces wiring
├── grafana/
│   ├── dashboards/        order-tracker-api.json (7 panels)
│   └── provisioning/      datasources, dashboards, folder, alert rule, contact point
├── otel-collector/config.yaml
├── prometheus/prometheus.yml
├── loki/loki.yaml
├── tempo/tempo.yaml
├── incident-response/     AI incident responder (runs on the host)
│   ├── responder/         main, incident, evidence, agent, responder, settings
│   └── tests/
├── static/index.html
├── tests/test_api.py
├── compose.yaml
└── Dockerfile
```

## License

Course exercise material.