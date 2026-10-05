import json
import logging
import shlex
import subprocess
import threading
import time
from pathlib import Path

from responder import settings

logger = logging.getLogger("incident_responder.agent")

_COMMAND_LABEL = "<prompt>"


def _format_logs(incident):
    logs = incident.get("evidence", {}).get("logs", {}) or {}
    entries = logs.get("entries") or []
    if logs.get("error"):
        return f"Loki unavailable: {logs['error']}"
    if not entries:
        return "No log entries in the lookback window."
    rendered = [
        f"{entry['timestamp']} {entry['severity'] or 'INFO'} "
        f"status={entry['status'] or '-'} trace={entry['trace_id'] or '-'} {entry['line']}"
        for entry in entries
    ]
    text = "\n".join(rendered)
    if len(text) > settings.MAX_INLINE_LOG_CHARS:
        text = text[-settings.MAX_INLINE_LOG_CHARS:]
    return text


def _format_metrics(incident):
    metrics = incident.get("evidence", {}).get("metrics", {}) or {}
    series = metrics.get("series") or []
    if metrics.get("error"):
        return f"Prometheus unavailable: {metrics['error']}"
    if not series:
        return f"No series matched `{metrics.get('query')}` in the lookback window."
    rows = [f"query: {metrics.get('query')}"]
    for item in series:
        rows.append(
            f"route={item['route']} status={item['status']} method={item['method']} "
            f"current={item['current']} delta={item['total_increase']}"
        )
    return "\n".join(rows)


def _format_traces(incident):
    evidence = incident.get("evidence", {})
    traces = evidence.get("traces", {}) or {}
    items = traces.get("traces") or []
    rows = []
    if traces.get("error"):
        rows.append(f"Tempo unavailable: {traces['error']}")
    elif not items:
        rows.append("No traces found.")
    else:
        for item in items:
            rows.append(
                f"trace={item['trace_id']} root={item['root']} duration_ms={item['duration_ms']} {item['url']}"
            )
    for span in (evidence.get("trace_detail") or {}).get("spans") or []:
        rows.append(
            f"  span={span['name']} service={span['service']} status={span['status_code'] or 'OK'} "
            f"attrs={json.dumps(span.get('attributes', {}))}"
        )
    return "\n".join(rows) or "No trace information."


def build_prompt(incident):
    alert = incident["alert"]
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    endpoint = incident.get("evidence", {}).get("endpoint") or "unknown"
    return f"""You are the on-call coding assistant for the `order-tracker` service.

A Grafana alert just fired. Investigate it using the evidence below and the repository at
{settings.AGENT_DIR}. The service source is in `app/`, the stack config in `compose.yaml`,
and telemetry is already wired up (metrics, logs, traces).

Rules:
- Diagnose the actual cause using the evidence. Do not guess.
- Fix the problem if a safe, local, reversible code change resolves it.
- Do not run destructive or irreversible commands, and do not commit or push.
- Do NOT restart containers or otherwise disturb the running stack; the operator does that.
- If the cause is not something you can safely fix, say so clearly instead.

## Alert
- rule: {labels.get("alertname", "unknown")}
- status: {alert.get("status")}
- severity: {labels.get("severity", "n/a")}
- service: {labels.get("service", settings.SERVICE_LABEL)}
- affected endpoint: {endpoint}
- labels: {json.dumps(labels)}
- summary: {incident.get("summary")}

## Alert description
{annotations.get("description", "n/a")}

## Metrics from Prometheus
{_format_metrics(incident)}

## Logs from Loki
{_format_logs(incident)}

## Traces from Tempo
{_format_traces(incident)}

## What to return
1. What happened, in one or two sentences.
2. The likely root cause, with the evidence that supports it.
3. What you changed, file by file, or that you changed nothing.
4. Whether a developer must be involved, and why.

Keep it brief. End your answer with a single concluding line."""


def build_command(prompt, bundle_path):
    command = [settings.AGENT_COMMAND, "run", prompt, "--format", "json"]
    if settings.AGENT_AUTO_APPROVE:
        command.append("--auto")
    if settings.AGENT_MODEL:
        command += ["--model", settings.AGENT_MODEL]
    command += ["--dir", settings.AGENT_DIR]
    if settings.AGENT_ATTACH_BUNDLE and bundle_path and Path(bundle_path).exists():
        command.append(f"--file={bundle_path}")
    return command


def _consume_event(event, state):
    state["session_id"] = event.get("sessionID") or state["session_id"]
    part = event.get("part") or {}
    if event.get("type") == "text" and part.get("text"):
        state["answer"].append(part["text"])
    if event.get("type") == "step_finish":
        state["tokens"] = (part.get("tokens") or {}).get("total", state["tokens"])
        state["cost"] = event.get("cost", state["cost"])


def run(incident, bundle_path=None, log_path=None):
    prompt = build_prompt(incident)
    command = build_command(prompt, bundle_path)
    label = shlex.join([settings.AGENT_COMMAND, "run", _COMMAND_LABEL])
    started = time.time()
    state = {"answer": [], "session_id": None, "tokens": None, "cost": None}
    stderr_lines = []
    logger.info("invoking coding assistant", extra={"id": incident["id"], "cwd": settings.AGENT_DIR})

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=settings.AGENT_DIR,
        )
    except FileNotFoundError:
        return {
            "invoked": True,
            "ok": False,
            "command": label,
            "error": f"coding assistant not found: {settings.AGENT_COMMAND}",
            "answer": "",
            "partial": False,
            "duration_s": round(time.time() - started, 2),
        }

    log_file = open(log_path, "w", encoding="utf-8") if log_path else None

    def read_stdout():
        for line in process.stdout:
            if log_file:
                log_file.write(line)
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                _consume_event(json.loads(line), state)
            except json.JSONDecodeError:
                continue

    def read_stderr():
        for line in process.stderr:
            stderr_lines.append(line)

    readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=settings.AGENT_TIMEOUT)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass

    for reader in readers:
        reader.join(timeout=15)
    if log_file:
        log_file.close()

    answer = "\n".join(state["answer"]).strip()
    result = {
        "invoked": True,
        "ok": process.returncode == 0 and bool(answer) and not timed_out,
        "command": label,
        "returncode": process.returncode,
        "answer": answer,
        "partial": bool(answer) and (timed_out or process.returncode != 0),
        "session_id": state["session_id"],
        "tokens": state["tokens"],
        "cost": state["cost"],
        "stderr": "".join(stderr_lines)[-4000:],
        "duration_s": round(time.time() - started, 2),
    }
    if timed_out:
        result["error"] = (
            f"timed out after {settings.AGENT_TIMEOUT}s"
            + (" (partial output kept)" if answer else " (no output captured)")
        )
    return result