import logging
import re

from responder import settings

logger = logging.getLogger("incident_responder.responder")

_ESCALATE_RE = re.compile(
    r"\b(escalat\w*|developer (?:must|needs to|should)|not something (?:i|we) can|cannot be (?:fixed|safely)|"
    r"needs? (?:a )?(?:developer|human)|manual (?:intervention|fix)|out of scope)\b",
    re.IGNORECASE,
)
_RESOLVE_RE = re.compile(
    r"\b(fixed|resolved|reproduced|corrected|no (?:incident|problem|action)|nothing to fix|test (?:alert|notification)|"
    r"benign|false positive|no-op)\b",
    re.IGNORECASE,
)


def last_line(text):
    for line in reversed((text or "").splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def decide(incident, agent_result):
    answer = agent_result.get("answer") or ""
    tail = last_line(answer)
    if not agent_result.get("ok"):
        verdict = "escalated"
        reason = agent_result.get("error") or "the coding assistant did not return an answer"
    elif _ESCALATE_RE.search(answer):
        verdict = "escalated"
        reason = "the coding assistant reported it cannot safely fix this itself"
    elif not answer:
        verdict = "escalated"
        reason = "empty answer from the coding assistant"
    else:
        verdict = "resolved"
        reason = "the coding assistant completed its run"
    return {"verdict": verdict, "reason": reason, "last_line": tail, "resolved_pattern": bool(_RESOLVE_RE.search(answer))}


def compose(incident, agent_result):
    evidence = incident.get("evidence", {}) or {}
    endpoint = evidence.get("endpoint") or "unknown"
    decision = decide(incident, agent_result)
    answer = agent_result.get("answer") or ""
    lines = [
        f"Incident {incident['id']} for {incident['alert']['labels'].get('alertname', 'unknown-alert')}",
        f"Status: {incident['alert'].get('status')}",
        f"Affected endpoint: {endpoint}",
        "",
        "Coding assistant ran headlessly"
        + (f" in {agent_result.get('duration_s')}s" if agent_result.get("duration_s") else "")
        + f" (session {agent_result.get('session_id') or 'n/a'}).",
        "",
        "Answer:",
        answer if answer else "(no answer returned)",
        "",
        f"Responder decision: {decision['verdict'].upper()} because {decision['reason']}.",
    ]
    if evidence.get("links"):
        lines.append("")
        lines.append("Links:")
        lines += [f"- {link['label']}: {link['url']}" for link in evidence["links"]]
    if incident.get("evidence", {}).get("traces", {}).get("traces"):
        trace_id = incident["evidence"]["traces"]["traces"][0]["trace_id"]
        lines.append(f"- first trace: {settings.TEMPO_URL}/trace/{trace_id}")
    lines.append("")
    lines.append(decision["last_line"])
    return {
        "verdict": decision["verdict"],
        "reason": decision["reason"],
        "escalate": decision["verdict"] == "escalated",
        "last_line": decision["last_line"],
        "body": "\n".join(lines),
        "agent_ok": bool(agent_result.get("ok")),
        "agent_session_id": agent_result.get("session_id"),
        "agent_duration_s": agent_result.get("duration_s"),
    }