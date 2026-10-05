import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from responder import settings

logger = logging.getLogger("incident_responder.evidence")

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
_ROUTE_RE = re.compile(r"(?:GET|POST|PATCH|PUT|DELETE)?\s*(/api/[^\s\"'<>)\]]+)")


def now():
    return datetime.now(timezone.utc)


def _allowed(url):
    host = httpx.URL(url).host
    return host in settings.ALLOWED_HOSTS


def _get(url, params=None):
    if not _allowed(url):
        return {"error": f"refused non-allowlisted host for {url}"}
    try:
        response = httpx.get(url, params=params, timeout=settings.BACKEND_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.warning("backend query failed", extra={"url": url, "error": str(exc)})
        return {"error": f"{type(exc).__name__}: {exc}"}


def extract_route(labels, annotations):
    route = labels.get(settings.ROUTE_LABEL)
    if route:
        return route
    for key in ("description", "summary"):
        text = annotations.get(key) or ""
        match = _ROUTE_RE.search(text)
        if match:
            return match.group(1)
    return None


def extract_links(annotations):
    links = []
    for key in ("runbook_url", "dashboard_url"):
        value = annotations.get(key)
        if value:
            links.append({"label": key, "url": value})
    for url in _URL_RE.findall(annotations.get("description", "")):
        if any(host in url for host in ("localhost", "127.0.0.1")):
            links.append({"label": "referenced", "url": url.rstrip(".,")})
    seen, unique = set(), []
    for link in links:
        if link["url"] not in seen:
            seen.add(link["url"])
            unique.append(link)
    return unique


def fetch_logs(route=None, trace_id=None):
    window = timedelta(minutes=settings.LOG_LOOKBACK_MINUTES)
    end, start = now(), now() - window
    selector = f'{{service_name="{settings.SERVICE_LABEL}"'
    if route:
        selector += f', {settings.ROUTE_LABEL}="{route}"'
    selector += "}"
    payload = _get(
        f"{settings.LOKI_URL}/loki/api/v1/query_range",
        params={
            "query": selector,
            "start": int(start.timestamp() * 1e9),
            "end": int(end.timestamp() * 1e9),
            "limit": settings.LOG_LIMIT,
            "direction": "backward",
        },
    )
    if "error" in payload:
        return {"query": selector, "error": payload["error"], "entries": []}
    entries = []
    for stream in payload.get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        for ts, line in stream.get("values", []):
            entries.append(
                {
                    "timestamp": datetime.fromtimestamp(int(ts) / 1e9, timezone.utc).isoformat(),
                    "line": line,
                    "severity": labels.get("severity_text"),
                    "route": labels.get(settings.ROUTE_LABEL),
                    "status": labels.get(settings.STATUS_LABEL),
                    "trace_id": labels.get("trace_id"),
                }
            )
    entries.sort(key=lambda item: item["timestamp"], reverse=True)
    if trace_id:
        matching = [item for item in entries if item["trace_id"] == trace_id]
        if matching:
            return {"query": selector, "filtered_by_trace": trace_id, "entries": matching}
    return {"query": selector, "entries": entries}


def fetch_traces(route=None, limit=None):
    tags = [f"service.name={settings.SERVICE_LABEL}"]
    if route:
        tags.append(f'http.route="{route}"')
    payload = _get(
        f"{settings.TEMPO_URL}/api/search",
        params={"tags": " ".join(tags), "limit": limit or settings.TRACE_LIMIT},
    )
    if isinstance(payload, dict) and "error" in payload:
        return {"error": payload["error"], "traces": []}
    traces = []
    for item in payload if isinstance(payload, list) else []:
        traces.append(
            {
                "trace_id": item.get("traceID"),
                "root": item.get("rootTraceName"),
                "duration_ms": item.get("durationMs"),
                "start": item.get("startTimeUnixNano"),
                "url": f"{settings.TEMPO_URL}/trace/{item.get('traceID')}",
            }
        )
    return {"tags": tags, "traces": traces}


def fetch_trace_detail(trace_id):
    if not trace_id or not _allowed(settings.TEMPO_URL):
        return {"trace_id": trace_id, "spans": [], "error": "no trace id"}
    payload = _get(f"{settings.TEMPO_URL}/api/traces/{trace_id}")
    if "error" in payload:
        return {"trace_id": trace_id, "spans": [], "error": payload["error"]}
    spans = []
    for batch in payload.get("batches", []):
        scope = (batch.get("resource") or {}).get("attributes", [])
        service = next((a["value"].get("stringValue") for a in scope if a.get("key") == "service.name"), None)
        for span in batch.get("scopeSpans", []):
            for item in span.get("spans", []):
                spans.append(
                    {
                        "name": item.get("name"),
                        "service": service,
                        "span_id": item.get("spanId"),
                        "parent_span_id": item.get("parentSpanId"),
                        "duration_ns": int(item.get("endTimeUnixNano", 0)) - int(item.get("startTimeUnixNano", 0)),
                        "status_code": item.get("status", {}).get("code"),
                        "status_message": item.get("status", {}).get("message"),
                        "attributes": {
                            a["key"]: a["value"].get("stringValue") or a["value"].get("intValue")
                            for a in item.get("attributes", [])
                        },
                    }
                )
    return {"trace_id": trace_id, "spans": spans}


def fetch_metrics(route=None):
    selector = f'{settings.METRIC_NAME}{{otel_scope_name="{settings.SERVICE_LABEL}"'
    if route:
        selector += f',{settings.ROUTE_LABEL}="{route}"'
    selector += "}"
    end, start = now(), now() - timedelta(minutes=settings.LOG_LOOKBACK_MINUTES)
    payload = _get(
        f"{settings.PROMETHEUS_URL}/api/v1/query_range",
        params={
            "query": selector,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "step": settings.METRIC_STEP,
        },
    )
    if "error" in payload:
        return {"query": selector, "error": payload["error"], "series": []}
    series = []
    for item in payload.get("data", {}).get("result", []):
        values = item.get("values", [])
        series.append(
            {
                "status": item["metric"].get(settings.STATUS_LABEL),
                "route": item["metric"].get(settings.ROUTE_LABEL),
                "method": item["metric"].get("http_request_method"),
                "current": values[-1][1] if values else None,
                "total_increase": _sum_values(values),
                "points": len(values),
            }
        )
    return {"query": selector, "series": series}


def _sum_values(values):
    try:
        numbers = [float(value) for _, value in values]
    except (TypeError, ValueError):
        return None
    if not numbers:
        return None
    first, last = numbers[0], numbers[-1]
    return round(last - first, 4) if last >= first else 0.0


def capture(alert):
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    route = extract_route(labels, annotations)
    trace_id = annotations.get("trace_id") or labels.get("trace_id")
    return {
        "captured_at": now().isoformat(),
        "lookback_minutes": settings.LOG_LOOKBACK_MINUTES,
        "endpoint": route,
        "trace_id": trace_id,
        "links": extract_links(annotations),
        "logs": fetch_logs(route),
        "traces": fetch_traces(route),
        "trace_detail": fetch_trace_detail(trace_id) if trace_id else {"spans": []},
        "metrics": fetch_metrics(route),
    }