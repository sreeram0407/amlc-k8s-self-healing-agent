"""Offline mock of the Anthropic client.

Implements just enough of the `client.messages.create(...)` surface that
`src.agent.Agent` uses, and drives a deterministic investigate → decide loop
that roughly follows the system-prompt playbook. Activated automatically when
`ANTHROPIC_API_KEY` is not set, so the demo runs end-to-end without a key.
"""

from __future__ import annotations

import json
import re
from typing import Any


class _Block:
    def __init__(self, type: str, **kwargs: Any) -> None:
        self.type = type
        for k, v in kwargs.items():
            setattr(self, k, v)


class _Usage:
    def __init__(self, input_tokens: int = 80, output_tokens: int = 40) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Response:
    def __init__(self, content: list[_Block], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


_EVENT_TYPES = ("CrashLoopBackOff", "OOMKilled", "ImagePullBackOff", "Error", "Pending")

_REMEDIATION = {
    "restart_pod", "scale_deployment", "rollback_deployment", "update_resource_limits",
}


def _parse_alert(text: str) -> tuple[str, str, str]:
    pod = re.search(r"Pod '([^']+)'", text)
    ns = re.search(r"namespace '([^']+)'", text)
    state = re.search(r"state '([^']+)'", text)
    pod_name = pod.group(1) if pod else ""
    namespace = ns.group(1) if ns else "default"
    event_type = state.group(1) if state else ""
    if event_type not in _EVENT_TYPES:
        for e in _EVENT_TYPES:
            if e in text:
                event_type = e
                break
    return pod_name, namespace, event_type


def _collect_tool_uses(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            t = getattr(b, "type", None) or (b.get("type") if isinstance(b, dict) else None)
            if t != "tool_use":
                continue
            out.append({
                "id": getattr(b, "id", None) or (b.get("id") if isinstance(b, dict) else None),
                "name": getattr(b, "name", None) or (b.get("name") if isinstance(b, dict) else None),
                "input": getattr(b, "input", None) or (b.get("input", {}) if isinstance(b, dict) else {}),
            })
    return out


def _collect_tool_results(messages: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            raw = item.get("content", "")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                parsed = raw
            out[item.get("tool_use_id", "")] = parsed
    return out


class _MessagesAPI:
    def __init__(self) -> None:
        self._id = 0

    def _next_id(self) -> str:
        self._id += 1
        return f"mock_tu_{self._id:04d}"

    def _tu(self, name: str, input_: dict[str, Any], narrative: str) -> _Response:
        return _Response(
            content=[
                _Block("text", text=narrative),
                _Block("tool_use", id=self._next_id(), name=name, input=input_),
            ],
            stop_reason="tool_use",
        )

    def _end(self, text: str) -> _Response:
        return _Response(content=[_Block("text", text=text)], stop_reason="end_turn")

    def create(self, **kwargs: Any) -> _Response:
        messages: list[dict] = kwargs["messages"]
        first_text = messages[0].get("content", "") if messages else ""
        pod_name, namespace, event_type = _parse_alert(first_text)

        tool_uses = _collect_tool_uses(messages)
        results_by_id = _collect_tool_results(messages)

        called = [t["name"] for t in tool_uses]
        results_by_name: dict[str, Any] = {}
        for tu in tool_uses:
            r = results_by_id.get(tu["id"])
            if r is not None:
                results_by_name[tu["name"]] = r

        # Did the last tool get blocked by a guardrail?
        last_blocked = False
        last_name = ""
        if tool_uses:
            last_name = tool_uses[-1]["name"]
            r = results_by_id.get(tool_uses[-1]["id"])
            if isinstance(r, dict) and r.get("error") == "GUARDRAIL_BLOCKED":
                last_blocked = True

        pod_info = results_by_name.get("get_pod_status")
        deployment = pod_info.get("deployment") if isinstance(pod_info, dict) else None

        # 1. Handle guardrail block → escalate
        if last_blocked and "alert_human" not in called:
            return self._tu(
                "alert_human",
                {
                    "severity": "critical",
                    "summary": f"Auto-remediation blocked for {pod_name}",
                    "details": (
                        f"Attempted {last_name} on {pod_name} ({event_type}) but a "
                        "safety guardrail prevented it. Escalating to human operator."
                    ),
                    "recommended_action": (
                        f"Operator review required for {pod_name} in {namespace}."
                    ),
                },
                narrative=f"{last_name} was blocked by a guardrail — escalating.",
            )

        # 2. Investigate
        if "get_pod_status" not in called:
            return self._tu(
                "get_pod_status",
                {"pod_name": pod_name, "namespace": namespace},
                f"Investigating {pod_name} — checking pod status first.",
            )
        if "get_pod_logs" not in called:
            return self._tu(
                "get_pod_logs",
                {"pod_name": pod_name, "namespace": namespace, "lines": 15},
                "Reading recent logs for error patterns.",
            )
        if deployment and "get_deployment_info" not in called:
            return self._tu(
                "get_deployment_info",
                {"deployment_name": deployment},
                f"Checking deployment '{deployment}' for a recent rollout.",
            )

        # 3. If we've already taken a remediation or escalated, end.
        if any(n in _REMEDIATION or n == "alert_human" for n in called):
            return self._end(
                f"Diagnosis: {pod_name} in '{namespace}' entered {event_type}. "
                f"Final action: {called[-1]}. Done."
            )

        # 4. Decide based on event type
        if event_type == "OOMKilled":
            return self._tu(
                "update_resource_limits",
                {"pod_name": pod_name, "memory": "1024Mi"},
                f"Logs confirm OOM. Raising memory limit on {pod_name} to 1024Mi.",
            )

        if event_type == "CrashLoopBackOff":
            dep_info = results_by_name.get("get_deployment_info") or {}
            history = dep_info.get("revision_history", []) if isinstance(dep_info, dict) else []
            if deployment and len(history) > 1:
                return self._tu(
                    "rollback_deployment",
                    {"deployment_name": deployment},
                    f"Crash correlates with recent deploy of {deployment} — rolling back.",
                )
            return self._tu(
                "restart_pod",
                {"pod_name": pod_name, "namespace": namespace},
                "No recent deploy on this pod — restarting to clear the crash loop.",
            )

        if event_type == "Error":
            return self._tu(
                "restart_pod",
                {"pod_name": pod_name, "namespace": namespace},
                f"Pod in Error state. Attempting a restart on {pod_name}.",
            )

        # Fallback → escalate
        return self._tu(
            "alert_human",
            {
                "severity": "warning",
                "summary": f"Cannot auto-remediate {event_type}",
                "details": (
                    f"Pod {pod_name} in {namespace} is in {event_type}; no playbook entry."
                ),
                "recommended_action": "Manual review and remediation",
            },
            f"No automated playbook for {event_type} — escalating.",
        )


class MockAnthropicClient:
    """Drop-in for `anthropic.Anthropic` that needs no API key."""

    def __init__(self) -> None:
        self.messages = _MessagesAPI()
