import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BASE_DIR.parent

INCIDENT_DIR = Path(os.getenv("RESPONDER_INCIDENT_DIR", str(BASE_DIR / "incidents")))

PROMETHEUS_URL = os.getenv("RESPONDER_PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
LOKI_URL = os.getenv("RESPONDER_LOKI_URL", "http://localhost:3100").rstrip("/")
TEMPO_URL = os.getenv("RESPONDER_TEMPO_URL", "http://localhost:3200").rstrip("/")
DASHBOARD_URL = os.getenv("RESPONDER_DASHBOARD_URL", "http://localhost:3000")
ALLOWED_HOSTS = os.getenv("RESPONDER_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")

SERVICE_LABEL = os.getenv("RESPONDER_SERVICE_LABEL", "order-tracker")
ROUTE_LABEL = os.getenv("RESPONDER_ROUTE_LABEL", "http_route")
STATUS_LABEL = os.getenv("RESPONDER_STATUS_LABEL", "http_response_status_code")
METRIC_NAME = os.getenv("RESPONDER_METRIC_NAME", "http_server_request_count_total")
LOG_LOOKBACK_MINUTES = int(os.getenv("RESPONDER_LOG_LOOKBACK_MINUTES", "15"))
LOG_LIMIT = int(os.getenv("RESPONDER_LOG_LIMIT", "60"))
TRACE_LIMIT = int(os.getenv("RESPONDER_TRACE_LIMIT", "10"))
METRIC_STEP = os.getenv("RESPONDER_METRIC_STEP", "15s")
BACKEND_TIMEOUT = float(os.getenv("RESPONDER_BACKEND_TIMEOUT", "10"))

AGENT_COMMAND = os.getenv("RESPONDER_AGENT_COMMAND", "opencode")
AGENT_MODEL = os.getenv("RESPONDER_AGENT_MODEL", "").strip()
AGENT_DIR = os.getenv("RESPONDER_AGENT_DIR", str(REPO_DIR))
AGENT_TIMEOUT = int(os.getenv("RESPONDER_AGENT_TIMEOUT", "1800"))
AGENT_AUTO_APPROVE = os.getenv("RESPONDER_AGENT_AUTO_APPROVE", "1") == "1"
AGENT_ATTACH_BUNDLE = os.getenv("RESPONDER_AGENT_ATTACH_BUNDLE", "1") == "1"
AGENT_CONCURRENCY = int(os.getenv("RESPONDER_AGENT_CONCURRENCY", "1"))
DEDUP_WINDOW = int(os.getenv("RESPONDER_DEDUP_WINDOW", "300"))
MAX_INLINE_LOG_CHARS = int(os.getenv("RESPONDER_MAX_INLINE_LOG_CHARS", "8000"))

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "incident-responder")
SERVICE_VERSION = "0.1.0"