import json

import pytest
from fastapi.testclient import TestClient

from responder import agent, evidence, incident, main, responder

BACKENDS = ["http://127.0.0.1:9090", "http://127.0.0.1:3100", "http://127.0.0.1:3200"]
_real_agent_run = agent.run


@pytest.fixture
def sample_alert():
    return {
        "status": "firing",
        "labels": {
            "alertname": "Order Tracker API - 5xx server errors",
            "alert_rule_uid": "order-tracker-5xx",
            "http_route": "/api/orders/{order_id}",
            "http_response_status_code": "500",
            "service": "order-tracker",
            "severity": "critical",
        },
        "annotations": {
            "summary": "5xx server errors on the Order Tracker API: 1 in the last 5m window",
            "description": (
                "Endpoint: /api/orders/{order_id} (status 500). "
                "Time window: last 5 minutes. "
                "Dashboard: http://localhost:3000/d/order-tracker-api/order-tracker-api?viewPanel=2"
            ),
        },
    }


@pytest.fixture(autouse=True)
def no_agent(monkeypatch, tmp_path):
    monkeypatch.setattr("responder.settings.INCIDENT_DIR", tmp_path / "incidents")
    monkeypatch.setattr(incident.settings, "INCIDENT_DIR", tmp_path / "incidents")
    monkeypatch.setattr(
        incident.evidence,
        "capture",
        lambda alert: {
            "endpoint": evidence.extract_route(alert.get("labels", {}), alert.get("annotations", {})),
            "logs": {"query": "stub", "entries": []},
            "traces": {"tags": ["stub"], "traces": []},
            "metrics": {"query": "stub", "series": []},
        },
    )
    monkeypatch.setattr(
        agent,
        "run",
        lambda record, bundle_path=None, log_path=None: {
            "invoked": True,
            "ok": True,
            "answer": "Root cause found.\nFixed the date overflow.\nAll good now.",
            "session_id": "ses_test",
            "duration_s": 1.2,
            "returncode": 0,
        },
    )


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client


def test_alerts_endpoint_accepts_grafana_payload(client, sample_alert):
    response = client.post("/alerts", json={"alerts": [sample_alert]})
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] == 1
    assert body["incidents"][0]["id"]
    assert body["incidents"][0]["bundle"].endswith("incident.md")


def test_alerts_endpoint_rejects_empty_payload(client):
    assert client.post("/alerts", json={"alerts": []}).status_code == 422
    assert client.post("/alerts", json={}).status_code == 422


def test_background_pipeline_runs_agent_and_responder(client, sample_alert):
    incident_id = client.post("/alerts", json={"alerts": [sample_alert]}).json()["incidents"][0]["id"]
    record = incident.load(incident_id)
    assert record["status"] == "responded"
    assert record["agent"]["invoked"] is True
    assert record["agent"]["status"] == "completed"
    assert record["response"]["verdict"] == "resolved"
    assert record["response"]["last_line"] == "All good now."


def test_bundle_records_endpoint_logs_and_traces(client, sample_alert):
    incident_id = client.post("/alerts", json={"alerts": [sample_alert]}).json()["incidents"][0]["id"]
    bundle = incident.incident_dir(incident_id)
    data = json.loads((bundle / "incident.json").read_text())
    assert data["evidence"]["endpoint"] == "/api/orders/{order_id}"
    assert "logs" in data["evidence"] and "traces" in data["evidence"]
    assert "metrics" in data["evidence"]
    markdown = (bundle / "incident.md").read_text()
    assert "## Metrics" in markdown
    assert "## Logs" in markdown
    assert "## Traces" in markdown
    assert "/api/orders/{order_id}" in markdown
    assert "## Coding assistant answer" in markdown
    assert "## Responder response" in markdown


def test_route_extracted_from_description_when_labels_lack_it(client):
    alert = {
        "status": "firing",
        "labels": {"alertname": "NoRoute"},
        "annotations": {
            "summary": "broken",
            "description": "Endpoint: /api/orders/abc (status 500). Dashboard: http://localhost:3000/x",
        },
    }
    incident_id = client.post("/alerts", json={"alerts": [alert]}).json()["incidents"][0]["id"]
    assert incident.load(incident_id)["evidence"]["endpoint"] == "/api/orders/abc"


def test_responder_escalates_when_agent_reports_cannot_fix(sample_alert, monkeypatch):
    monkeypatch.setattr(
        agent,
        "run",
        lambda record, bundle_path=None, log_path=None: {
            "invoked": True,
            "ok": True,
            "answer": "Root cause is a data corruption issue.\nThis must be escalated to the developers.",
            "session_id": "ses_test",
            "duration_s": 3.0,
        },
    )
    with TestClient(main.app) as client:
        incident_id = client.post("/alerts", json={"alerts": [sample_alert]}).json()["incidents"][0]["id"]
    record = incident.load(incident_id)
    assert record["response"]["verdict"] == "escalated"
    assert record["response"]["escalate"] is True


def test_responder_escalates_when_agent_fails(sample_alert, monkeypatch):
    monkeypatch.setattr(
        agent,
        "run",
        lambda record, bundle_path=None, log_path=None: {
            "invoked": True,
            "ok": False,
            "answer": "",
            "error": "coding assistant not found: opencode",
            "duration_s": 0.1,
        },
    )
    record = incident.new_incident(sample_alert)
    result = agent.run(record)
    assert responder.compose(record, result)["verdict"] == "escalated"


def test_list_and_latest_endpoints(client, sample_alert):
    client.post("/alerts", json={"alerts": [sample_alert]})
    assert len(client.get("/incidents").json()["incidents"]) == 1
    latest = client.get("/incidents/latest")
    assert latest.status_code == 200
    assert latest.json()["status"] == "responded"
    assert client.get("/incidents/does-not-exist").status_code == 404


def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert set(body["backends"]) == {"prometheus", "loki", "tempo"}

def test_duplicate_delivery_is_suppressed(client, sample_alert):
    first = client.post("/alerts", json={"alerts": [sample_alert]}).json()
    assert first["accepted"] == 1
    second = client.post("/alerts", json={"alerts": [sample_alert]}).json()
    assert second["accepted"] == 0
    assert second["suppressed_duplicates"] == 1
    assert second["suppressed"][0]["duplicate_of"] == first["incidents"][0]["id"]
    assert len(client.get("/incidents").json()["incidents"]) == 1


def test_distinct_alerts_are_not_deduplicated(client, sample_alert):
    other = dict(sample_alert, labels=dict(sample_alert["labels"], http_response_status_code="503"))
    assert client.post("/alerts", json={"alerts": [sample_alert]}).json()["accepted"] == 1
    assert client.post("/alerts", json={"alerts": [other]}).json()["accepted"] == 1


def test_alert_key_is_stable_across_repeated_counts(sample_alert):
    first = incident.alert_key(sample_alert)
    second = incident.alert_key(dict(sample_alert, annotations={"summary": "5xx: 7 in the last 5m"}))
    assert first == second


def _fake_agent(tmp_path, name, body):
    script = tmp_path / name
    script.write_text("#!/usr/bin/env python3\n" + body)
    script.chmod(0o755)
    return str(script)


def test_timeout_preserves_partial_answer(tmp_path, monkeypatch, sample_alert):
    script = _fake_agent(
        tmp_path,
        "emit_then_hang.py",
        "import json, time\n"
        "print(json.dumps({'type': 'text', 'sessionID': 'ses_x', "
        "'part': {'type': 'text', 'text': 'Found the bug.\\nPatched the date math.\\nShould be fixed.'}}), flush=True)\n"
        "time.sleep(120)\n",
    )
    monkeypatch.setattr("responder.settings.AGENT_COMMAND", script)
    monkeypatch.setattr("responder.settings.AGENT_TIMEOUT", 4)
    record = incident.new_incident(sample_alert)
    log = tmp_path / "agent.log"
    result = _real_agent_run(record, None, log_path=log)
    assert result["ok"] is False
    assert "timed out" in result["error"]
    assert result["partial"] is True
    assert result["answer"].startswith("Found the bug.")
    assert result["session_id"] == "ses_x"
    assert log.exists() and "Found the bug." in log.read_text()


def test_timeout_with_no_output_is_not_marked_partial(tmp_path, monkeypatch, sample_alert):
    script = _fake_agent(tmp_path, "hang.py", "import time\ntime.sleep(120)\n")
    monkeypatch.setattr("responder.settings.AGENT_COMMAND", script)
    monkeypatch.setattr("responder.settings.AGENT_TIMEOUT", 3)
    record = incident.new_incident(sample_alert)
    result = _real_agent_run(record, None, log_path=tmp_path / "agent.log")
    assert result["ok"] is False
    assert result["partial"] is False
    assert result["answer"] == ""
    assert "timed out" in result["error"]


def test_missing_agent_command_is_reported(tmp_path, monkeypatch, sample_alert):
    monkeypatch.setattr("responder.settings.AGENT_COMMAND", str(tmp_path / "nope"))
    record = incident.new_incident(sample_alert)
    result = _real_agent_run(record, None)
    assert result["ok"] is False
    assert "not found" in result["error"]
    assert result["answer"] == ""


def test_duplicate_suppressed_while_incident_still_received(client, sample_alert):
    first = client.post("/alerts", json={"alerts": [sample_alert]}).json()
    incident_id = first["incidents"][0]["id"]
    record = incident.load(incident_id)
    record["status"] = "received"
    incident.save(record)
    second = client.post("/alerts", json={"alerts": [sample_alert]}).json()
    assert second["accepted"] == 0
    assert second["suppressed"][0]["duplicate_of"] == incident_id


def test_duplicate_allowed_once_window_expires(client, sample_alert, monkeypatch):
    import responder.settings as s
    monkeypatch.setattr(s, "DEDUP_WINDOW", 0)
    assert client.post("/alerts", json={"alerts": [sample_alert]}).json()["accepted"] == 1
    assert client.post("/alerts", json={"alerts": [sample_alert]}).json()["accepted"] == 1


def test_incident_is_durable_before_evidence_capture(client, sample_alert):
    first = client.post("/alerts", json={"alerts": [sample_alert]}).json()
    incident_id = first["incidents"][0]["id"]
    second = client.post("/alerts", json={"alerts": [sample_alert]}).json()
    assert second["accepted"] == 0
    assert second["suppressed"][0]["duplicate_of"] == incident_id


def test_evidence_attached_after_record_created(client, sample_alert):
    client.post("/alerts", json={"alerts": [sample_alert]})
    record = client.get("/incidents/latest").json()
    assert record["id"]
    stored = incident.load(record["id"])
    assert stored["evidence"]["endpoint"] == "/api/orders/{order_id}"
