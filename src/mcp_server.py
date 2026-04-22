"""MCP Server — exposes Kubernetes tools for the AI agent.

Can be run as a standalone MCP server or used programmatically
to build a tool list for the Claude API.
"""

from __future__ import annotations

import json
from typing import Any

from .mock_cluster import MockCluster

# ---------------------------------------------------------------------------
# Tool definitions (Claude API tool_use format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_cluster_status",
        "description": "Get an overview of all pods and deployments in the cluster.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_pod_status",
        "description": "Get detailed status of a specific pod including status, restarts, resource usage, and age.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pod_name": {"type": "string", "description": "Name of the pod"},
                "namespace": {"type": "string", "description": "Kubernetes namespace", "default": "default"},
            },
            "required": ["pod_name"],
        },
    },
    {
        "name": "get_pod_logs",
        "description": "Get recent log lines from a pod. Useful for diagnosing crashes, errors, and OOM events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pod_name": {"type": "string", "description": "Name of the pod"},
                "namespace": {"type": "string", "description": "Kubernetes namespace", "default": "default"},
                "lines": {"type": "integer", "description": "Number of log lines to retrieve", "default": 50},
            },
            "required": ["pod_name"],
        },
    },
    {
        "name": "get_events",
        "description": "Get recent cluster events (warnings, errors, normal events).",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Filter by namespace (optional)"},
                "limit": {"type": "integer", "description": "Max events to return", "default": 20},
            },
            "required": [],
        },
    },
    {
        "name": "get_deployment_info",
        "description": "Get deployment details including replica count, current image, and full revision history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "deployment_name": {"type": "string", "description": "Name of the deployment"},
            },
            "required": ["deployment_name"],
        },
    },
    {
        "name": "restart_pod",
        "description": "Restart (delete and recreate) a pod. Use for CrashLoopBackOff when no recent deployment caused it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pod_name": {"type": "string", "description": "Name of the pod to restart"},
                "namespace": {"type": "string", "description": "Kubernetes namespace", "default": "default"},
            },
            "required": ["pod_name"],
        },
    },
    {
        "name": "scale_deployment",
        "description": "Change the replica count of a deployment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "deployment_name": {"type": "string", "description": "Name of the deployment"},
                "replicas": {"type": "integer", "description": "Desired replica count"},
            },
            "required": ["deployment_name", "replicas"],
        },
    },
    {
        "name": "rollback_deployment",
        "description": "Rollback a deployment to a previous revision. Use when a recent deployment caused pod failures.",
        "input_schema": {
            "type": "object",
            "properties": {
                "deployment_name": {"type": "string", "description": "Name of the deployment"},
                "revision": {"type": "integer", "description": "Target revision number (optional, defaults to previous)"},
            },
            "required": ["deployment_name"],
        },
    },
    {
        "name": "update_resource_limits",
        "description": "Update memory and/or CPU limits for a pod. Use for OOMKilled pods.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pod_name": {"type": "string", "description": "Name of the pod"},
                "memory": {"type": "string", "description": "New memory limit (e.g. '512Mi', '1Gi')"},
                "cpu": {"type": "string", "description": "New CPU limit (e.g. '500m', '1')"},
            },
            "required": ["pod_name"],
        },
    },
    {
        "name": "alert_human",
        "description": "Escalate an issue to a human operator via alerting channel. Use when the issue cannot be auto-remediated or guardrails block the action.",
        "input_schema": {
            "type": "object",
            "properties": {
                "severity": {
                    "type": "string",
                    "description": "Alert severity",
                    "enum": ["critical", "warning", "info"],
                },
                "summary": {"type": "string", "description": "Short summary of the issue"},
                "details": {"type": "string", "description": "Full diagnostic details"},
                "recommended_action": {"type": "string", "description": "What the agent would do if allowed"},
            },
            "required": ["severity", "summary", "details", "recommended_action"],
        },
    },
]


class MCPToolHandler:
    """Dispatches tool calls from the Claude API to the mock cluster."""

    def __init__(self, cluster: MockCluster, alert_callback: Any = None) -> None:
        self.cluster = cluster
        self.alert_callback = alert_callback

    def handle(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Execute a tool call and return a JSON string result."""
        handler_map = {
            "get_cluster_status": self._get_cluster_status,
            "get_pod_status": self._get_pod_status,
            "get_pod_logs": self._get_pod_logs,
            "get_events": self._get_events,
            "get_deployment_info": self._get_deployment_info,
            "restart_pod": self._restart_pod,
            "scale_deployment": self._scale_deployment,
            "rollback_deployment": self._rollback_deployment,
            "update_resource_limits": self._update_resource_limits,
            "alert_human": self._alert_human,
        }
        handler = handler_map.get(tool_name)
        if not handler:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        result = handler(tool_input)
        return json.dumps(result, default=str)

    def _get_cluster_status(self, _: dict[str, Any]) -> dict[str, Any]:
        pods = self.cluster.get_all_pods()
        deployments = {k: d.to_dict() for k, d in self.cluster.deployments.items()}
        return {"pods": pods, "deployments": deployments}

    def _get_pod_status(self, inp: dict[str, Any]) -> Any:
        return self.cluster.get_pod(inp["pod_name"], inp.get("namespace", "default"))

    def _get_pod_logs(self, inp: dict[str, Any]) -> dict[str, Any]:
        logs = self.cluster.get_pod_logs(
            inp["pod_name"], inp.get("namespace", "default"), inp.get("lines", 50)
        )
        return {"pod_name": inp["pod_name"], "logs": logs}

    def _get_events(self, inp: dict[str, Any]) -> dict[str, Any]:
        events = self.cluster.get_events(inp.get("namespace"), inp.get("limit", 20))
        return {"events": events}

    def _get_deployment_info(self, inp: dict[str, Any]) -> Any:
        return self.cluster.get_deployment(inp["deployment_name"])

    def _restart_pod(self, inp: dict[str, Any]) -> dict[str, str]:
        result = self.cluster.restart_pod(inp["pod_name"], inp.get("namespace", "default"))
        return {"result": result}

    def _scale_deployment(self, inp: dict[str, Any]) -> dict[str, str]:
        result = self.cluster.scale_deployment(inp["deployment_name"], inp["replicas"])
        return {"result": result}

    def _rollback_deployment(self, inp: dict[str, Any]) -> dict[str, str]:
        result = self.cluster.rollback_deployment(inp["deployment_name"], inp.get("revision"))
        return {"result": result}

    def _update_resource_limits(self, inp: dict[str, Any]) -> dict[str, str]:
        result = self.cluster.update_resource_limits(
            inp["pod_name"], inp.get("memory"), inp.get("cpu")
        )
        return {"result": result}

    def _alert_human(self, inp: dict[str, Any]) -> dict[str, str]:
        if self.alert_callback:
            self.alert_callback(inp)
        return {
            "result": f"Alert sent — severity: {inp['severity']}, summary: {inp['summary']}"
        }
