import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from responder import agent, incident, responder, settings

logging.basicConfig(
    level=os.getenv("RESPONDER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("incident_responder")

app = FastAPI(title="Incident Responder", version=settings.SERVICE_VERSION)
agent_slots = threading.Semaphore(max(settings.AGENT_CONCURRENCY, 1))


class Alert(BaseModel):
    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str | None = None
    endsAt: str | None = None
    generatorURL: str | None = None
    fingerprint: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class WebhookPayload(BaseModel):
    alerts: list[Alert] = Field(default_factory=list)
    status: str | None = None
    receiver: str | None = None
    groupLabels: dict[str, str] = Field(default_factory=dict)
    externalURL: str | None = None
    message: str | None = None


def _investigate(incident_id: str):
    record = incident.load(incident_id)
    if record is None:
        logger.error("incident vanished before investigation", extra={"id": incident_id})
        return
    target = incident.incident_dir(incident_id)
    record["status"] = "investigating"
    record["agent"]["invoked"] = True
    record["agent"]["status"] = "queued"
    incident.save(record)
    with agent_slots:
        record = incident.load(incident_id) or record
        record["agent"]["status"] = "running"
        incident.save(record)
        logger.info("starting headless coding assistant", extra={"id": incident_id})
        result = agent.run(record, target / "incident.md", log_path=target / "agent.log")
    record["agent"].update(result)
    record["agent"]["status"] = "completed" if result.get("ok") else "failed"
    record["status"] = "responded"
    record["response"] = responder.compose(record, result)
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    incident.save(record)
    logger.info(
        "responder finished",
        extra={
            "id": incident_id,
            "verdict": record["response"]["verdict"],
            "partial": record["agent"].get("partial"),
        },
    )


@app.post("/alerts", status_code=202)
def receive_alerts(payload: WebhookPayload, background: BackgroundTasks):
    alerts = payload.alerts or []
    if not alerts:
        raise HTTPException(422, "no alerts in payload")
    created, suppressed = [], []
    for alert in alerts:
        key = incident.alert_key(alert.as_payload())
        duplicate = incident.duplicate_of(key)
        if duplicate is not None:
            logger.info(
                "suppressed duplicate delivery",
                extra={"alert_key": key, "duplicate_of": duplicate["id"]},
            )
            suppressed.append({"duplicate_of": duplicate["id"], "alert_key": key})
            continue
        record = incident.new_incident(alert.as_payload())
        incident.save(record)
        logger.info("incident recorded", extra={"id": record["id"], "alert_key": key})
        incident.attach_evidence(record)
        incident.save(record)
        logger.info(
            "evidence captured",
            extra={
                "id": record["id"],
                "alertname": alert.labels.get("alertname"),
                "endpoint": record["evidence"].get("endpoint"),
                "log_entries": len(record["evidence"].get("logs", {}).get("entries", [])),
                "traces": len(record["evidence"].get("traces", {}).get("traces", [])),
            },
        )
        background.add_task(_investigate, record["id"])
        created.append(
            {
                "id": record["id"],
                "summary": record["summary"],
                "endpoint": record["evidence"].get("endpoint"),
                "alert_key": key,
                "bundle": str(incident.incident_dir(record["id"]) / "incident.md"),
            }
        )
    return {
        "accepted": len(created),
        "suppressed_duplicates": len(suppressed),
        "incidents": created,
        "suppressed": suppressed,
    }


@app.get("/incidents")
def list_incidents():
    return {"incidents": incident.list_incidents()}


@app.get("/incidents/latest")
def latest_incident():
    data = incident.latest()
    if data is None:
        raise HTTPException(404, "no incidents yet")
    return data


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str, include_evidence: bool = False):
    record = incident.load(incident_id)
    if record is None:
        raise HTTPException(404, "incident not found")
    if not include_evidence:
        record = {key: value for key, value in record.items() if key != "evidence"}
    return record


@app.get("/healthz")
def health():
    return {
        "status": "ok",
        "incident_dir": str(settings.INCIDENT_DIR),
        "agent": settings.AGENT_COMMAND,
        "backends": {
            "prometheus": settings.PROMETHEUS_URL,
            "loki": settings.LOKI_URL,
            "tempo": settings.TEMPO_URL,
        },
    }