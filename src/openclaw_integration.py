"""OpenClaw human-in-the-loop escalation.

Formats agent alerts for a human operator and optionally posts them to the
OpenClaw webhook running in GKE. If OpenClaw is not configured, it falls back
to stdout so local demos stay offline.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .config import OpenClawConfig


_SEV_ICON = {"critical": "[CRIT]", "warning": "[WARN]", "info": "[INFO]"}


class OpenClawIntegration:
    def __init__(self, config: OpenClawConfig) -> None:
        self.config = config
        self.alerts: list[dict[str, Any]] = []
        self.webhook_url = os.environ.get("OPENCLAW_WEBHOOK_URL", config.webhook_url).strip()
        self.token = os.environ.get(config.hooks_token_env, "").strip()
        self.enabled = _env_bool("OPENCLAW_ENABLED", config.enabled)
        self.timeout_seconds = int(os.environ.get("OPENCLAW_TIMEOUT_SECONDS", config.timeout_seconds))
        if self.enabled and not self.webhook_url:
            print(" OPENCLAW_WEBHOOK_URL not set — OpenClaw alerts will print to stdout")
        if self.enabled and not self.token:
            print(f" {config.hooks_token_env} not set — OpenClaw alerts will print to stdout")

    def format_alert_for_tool(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Handle an alert_human tool call. Returns the formatted alert."""
        alert = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": inp.get("severity", "info"),
            "summary": inp.get("summary", ""),
            "details": inp.get("details", ""),
            "recommended_action": inp.get("recommended_action", ""),
            "channel": self.config.channel,
        }
        self.alerts.append(alert)
        if self.enabled and self.webhook_url and self.token:
            self._post(alert)
        else:
            self._emit(alert)
        return alert

    def _post(self, alert: dict[str, Any]) -> None:
        payload = self._payload(alert)
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "k8s-self-healer/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                response_body = resp.read(4096).decode("utf-8", errors="replace")
            print(f" OpenClaw incident posted: {response_body[:240]}")
        except urllib.error.HTTPError as exc:
            error_body = exc.read(2048).decode("utf-8", errors="replace")
            print(f" [fail] OpenClaw webhook failed: HTTP {exc.code} {error_body}")
            self._emit(alert)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f" [fail] OpenClaw webhook unavailable: {exc}")
            self._emit(alert)

    def _payload(self, alert: dict[str, Any]) -> dict[str, Any]:
        severity = alert["severity"].upper()
        message = (
            "You are the on-call copilot for k8s-healer. "
            "Explain the Kubernetes incident, recommend safe human next steps, "
            "and do not execute kubectl or mutate the cluster.\n\n"
            f"Severity: {severity}\n"
            f"Summary: {alert['summary']}\n"
            f"Details:\n{_redact(alert['details'])}\n\n"
            f"Recommended human action:\n{_redact(alert['recommended_action'])}\n\n"
            f"Timestamp: {alert['timestamp']}"
        )
        return {
            "name": f"k8s-healer-{alert['severity']}",
            "agentId": self.config.agent_id,
            "wakeMode": "now",
            "deliver": bool(self.config.deliver or self.config.paging_enabled),
            "message": message,
            "metadata": {
                "source": "k8s-self-healer",
                "severity": alert["severity"],
                "summary": alert["summary"],
                "channel": alert["channel"],
                "timestamp": alert["timestamp"],
            },
        }

    def _emit(self, alert: dict[str, Any]) -> None:
        icon = _SEV_ICON.get(alert["severity"], "•")
        bar = "─" * 58
        print(f"\n   ┌{bar}┐")
        print(f"   │ {icon}  OpenClaw alert -> {self.config.channel:<30s}│")
        print(f"   ├{bar}┤")
        print(f"   │ severity : {alert['severity']:<45s}│")
        print(f"   │ summary  : {_trunc(alert['summary'], 45):<45s}│")
        print(f"   │ details  : {_trunc(alert['details'], 45):<45s}│")
        print(f"   │ action   : {_trunc(alert['recommended_action'], 45):<45s}│")
        print(f"   └{bar}┘")

    def post_resolution(self, pod_name: str, namespace: str,
                        action_taken: str, action_params: dict,
                        diagnosis: str = "") -> dict[str, Any]:
        """Pretty-print resolution for the local demo. Matches SlackIntegration's signature."""
        params_str = ", ".join(f"{k}={v}" for k, v in (action_params or {}).items()
                               if k not in ("pod_name", "namespace"))
        bar = "─" * 58
        print(f"\n   ┌{bar}┐")
        print(f"   │ [FIXED]  Auto-remediated -> {self.config.channel:<27s}│")
        print(f"   ├{bar}┤")
        print(f"   │ pod      : {pod_name:<45s}│")
        print(f"   │ ns       : {namespace:<45s}│")
        print(f"   │ action   : {action_taken:<45s}│")
        print(f"   │ params   : {_trunc(params_str, 45):<45s}│")
        print(f"   └{bar}┘")
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pod_name": pod_name,
            "namespace": namespace,
            "action_taken": action_taken,
            "action_params": action_params,
        }


def _trunc(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _redact(text: str) -> str:
    redacted_lines = []
    for line in (text or "").splitlines():
        lowered = line.lower()
        if any(key in lowered for key in ("token", "secret", "password", "api_key", "apikey")):
            redacted_lines.append("[redacted sensitive line]")
        else:
            redacted_lines.append(line)
    return "\n".join(redacted_lines)
