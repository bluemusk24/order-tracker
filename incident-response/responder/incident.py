import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from responder import evidence, settings

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value):
    return _SLUG_RE.sub("-", str(value).lower()).strip("-")[:48] or "alert"


def alert_key(alert):
    labels = alert.get("labels", {}) or {}
    annotations = alert.get("annotations", {}) or {}
    endpoint = labels.get(settings.ROUTE_LABEL) or ""
    if not endpoint:
        match = re.search(r"(/api/\S+)", annotations.get("description", "") or "")
        endpoint = match.group(1) if match else ""
    parts = [
        labels.get("alertname", "unknown"),
        labels.get("service", settings.SERVICE_LABEL),
        labels.get(settings.STATUS_LABEL, ""),
        endpoint,
    ]
    return "|".join(parts)


def duplicate_of(alert_key_value, now=None):
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=settings.DEDUP_WINDOW)
    for entry in list_incidents():
        received = datetime.fromisoformat(entry["received_at"])
        if received < cutoff:
            continue
        if entry.get("alert_key") == alert_key_value:
            return entry
    return None


def new_incident(alert, received_at=None):
    labels = alert.get("labels", {}) or {}
    annotations = alert.get("annotations", {}) or {}
    stamp = received_at or datetime.now(timezone.utc)
    alertname = labels.get("alertname", "unknown-alert")
    incident_id = f"{stamp.strftime('%Y%m%dT%H%M%SZ')}-{_slug(alertname)}-{uuid4().hex[:6]}"
    return {
        "id": incident_id,
        "status": "received",
        "received_at": stamp.isoformat(),
        "updated_at": stamp.isoformat(),
        "alert_key": alert_key(alert),
        "alert": {
            "status": alert.get("status"),
            "labels": labels,
            "annotations": annotations,
            "starts_at": alert.get("startsAt"),
            "ends_at": alert.get("endsAt"),
            "generator_url": alert.get("generatorURL"),
            "fingerprint": alert.get("fingerprint"),
        },
        "summary": annotations.get("summary") or alertname,
        "evidence": {},
        "agent": {"invoked": False},
        "response": None,
    }


def attach_evidence(record):
    record["evidence"] = evidence.capture(record["alert"])
    return record


def incident_dir(incident_id):
    return settings.INCIDENT_DIR / incident_id


def render_markdown(incident):
    alert = incident["alert"]
    lines = [
        f"# Incident {incident['id']}",
        "",
        f"- Received: {incident['received_at']}",
        f"- Alert status: {alert['status']}",
        f"- Alert rule: {alert['labels'].get('alertname')}",
        f"- Severity: {alert['labels'].get('severity', 'n/a')}",
        f"- Service: {alert['labels'].get('service', settings.SERVICE_LABEL)}",
        f"- Affected endpoint: {incident['evidence'].get('endpoint') or 'unknown'}",
    ]
    if alert["labels"].get("http_response_status_code"):
        lines.append(f"- Observed status code: {alert['labels']['http_response_status_code']}")
    lines += ["", "## Summary", "", alert["annotations"].get("summary") or "n/a", ""]

    description = alert["annotations"].get("description")
    if description:
        lines += ["## Alert description", "", description, ""]

    links = incident["evidence"].get("links") or []
    if links:
        lines += ["## Links", ""] + [f"- {link['label']}: {link['url']}" for link in links] + [""]

    metrics = incident["evidence"].get("metrics", {})
    lines += ["## Metrics", "", f"Query: `{metrics.get('query', 'n/a')}`", ""]
    series = metrics.get("series") or []
    if metrics.get("error"):
        lines.append(f"Error: {metrics['error']}")
    elif not series:
        lines.append("No series matched in the lookback window.")
    else:
        lines += ["| Route | Status | Method | Current | Delta |", "|---|---|---|---|---|"]
        for item in series:
            lines.append(
                f"| {item['route']} | {item['status']} | {item['method']} | {item['current']} | {item['total_increase']} |"
            )
    lines.append("")

    logs = incident["evidence"].get("logs", {})
    lines += ["## Logs", "", f"Query: `{logs.get('query', 'n/a')}`", ""]
    entries = logs.get("entries") or []
    if logs.get("error"):
        lines.append(f"Error: {logs['error']}")
    elif not entries:
        lines.append("No log entries in the lookback window.")
    else:
        for entry in entries:
            lines.append(
                f"- `{entry['timestamp']}` **{entry['severity'] or 'INFO'}** "
                f"status={entry['status'] or '-'} trace={entry['trace_id'] or '-'} :: {entry['line']}"
            )
    lines.append("")

    traces = incident["evidence"].get("traces", {})
    lines += ["## Traces", ""]
    trace_items = traces.get("traces") or []
    if traces.get("error"):
        lines.append(f"Error: {traces['error']}")
    elif not trace_items:
        lines.append("No traces found for the service.")
    else:
        for item in trace_items:
            lines.append(f"- `{item['trace_id']}` {item['root']} ({item['duration_ms']}ms) {item['url']}")
    lines.append("")

    detail = incident["evidence"].get("trace_detail") or {}
    spans = detail.get("spans") or []
    if spans:
        lines += ["## Span detail", "", f"Trace `{detail.get('trace_id')}`", ""]
        for span in spans:
            lines.append(
                f"- `{span['name']}` service={span['service']} status={span['status_code'] or 'OK'} "
                f"duration={span['duration_ns']}ns"
            )
            for key, value in (span.get("attributes") or {}).items():
                lines.append(f"  - {key} = {value}")
        lines.append("")

    if incident.get("agent", {}).get("answer"):
        lines += ["## Coding assistant answer", "", incident["agent"]["answer"], ""]
    if incident.get("response"):
        lines += ["## Responder response", "", incident["response"]["body"], ""]
    return "\n".join(lines)


def save(incident):
    target = incident_dir(incident["id"])
    target.mkdir(parents=True, exist_ok=True)
    (target / "incident.json").write_text(json.dumps(incident, indent=2, default=str))
    (target / "incident.md").write_text(render_markdown(incident))
    settings.INCIDENT_DIR.mkdir(parents=True, exist_ok=True)
    (settings.INCIDENT_DIR / "latest.json").write_text(
        json.dumps(
            {
                "id": incident["id"],
                "status": incident["status"],
                "summary": incident["summary"],
                "endpoint": incident.get("evidence", {}).get("endpoint"),
                "agent": incident.get("agent"),
                "response": incident.get("response"),
                "paths": {
                    "json": str(target / "incident.json"),
                    "markdown": str(target / "incident.md"),
                },
            },
            indent=2,
            default=str,
        )
    )
    return target


def load(incident_id):
    path = incident_dir(incident_id) / "incident.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def list_incidents():
    if not settings.INCIDENT_DIR.exists():
        return []
    found = []
    for child in sorted(settings.INCIDENT_DIR.iterdir(), reverse=True):
        if child.is_dir():
            data = load(child.name)
            if data:
                found.append(
                    {
                        "id": data["id"],
                        "status": data["status"],
                        "received_at": data["received_at"],
                        "summary": data["summary"],
                        "endpoint": data.get("evidence", {}).get("endpoint"),
                        "alert_key": data.get("alert_key"),
                    }
                )
    return found


def latest():
    path = settings.INCIDENT_DIR / "latest.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())