"""Four demo scenarios that exercise the agent end-to-end.

Each function takes a MockCluster, mutates it to set up the scenario, and
returns a dict with an `event` the agent will handle.
"""

from __future__ import annotations

from typing import Any

from src.mock_cluster import MockCluster, PodStatus


def _first_pod(cluster: MockCluster, deployment: str) -> Any:
    for p in cluster.pods.values():
        if p.deployment == deployment:
            return p
    return None


def scenario_simple_recovery(cluster: MockCluster) -> dict[str, Any]:
    """CrashLoopBackOff on a pod with no recent deploy → restart."""
    pod = _first_pod(cluster, "notification-service")
    if pod is None:
        return {"error": "notification-service pod not found"}

    cluster.inject_failure(pod.name, "CrashLoopBackOff", pod.namespace)
    return {
        "name": "Scenario 1: Simple Recovery",
        "description": (
            "A notification-service pod has entered CrashLoopBackOff. No recent "
            "deploy. The agent should investigate and restart the pod."
        ),
        "event": {
            "pod_name": pod.name,
            "namespace": pod.namespace,
            "reason": "CrashLoopBackOff",
            "status": "CrashLoopBackOff",
            "message": f"Back-off restarting failed container in pod {pod.name}",
        },
    }


def scenario_rollback_bad_deploy(cluster: MockCluster) -> dict[str, Any]:
    """A bad image was deployed — agent should roll back."""
    deployment = "payment-service"
    result = cluster.simulate_deploy(deployment, "myapp/payment-service:v3.2.0-broken")
    if result.startswith("Error"):
        return {"error": result}

    cluster.inject_failure_on_deployment(deployment, "CrashLoopBackOff")
    pod = _first_pod(cluster, deployment)
    if pod is None:
        return {"error": "payment-service pod not found"}

    return {
        "name": "Scenario 2: Rollback Bad Deploy",
        "description": (
            "payment-service was just deployed with a broken image "
            "(v3.2.0-broken). All pods are crashing. The agent should detect the "
            "correlation and rollback to the previous revision."
        ),
        "event": {
            "pod_name": pod.name,
            "namespace": pod.namespace,
            "reason": "CrashLoopBackOff",
            "status": "CrashLoopBackOff",
            "message": (
                f"Pod {pod.name} crashed after recent deployment of "
                "payment-service:v3.2.0-broken"
            ),
        },
    }


def scenario_guardrail_escalation(cluster: MockCluster) -> dict[str, Any]:
    """OOMKilled pod: agent tries to raise memory past 2x, guardrail blocks, escalates."""
    pod = _first_pod(cluster, "data-processor")
    if pod is None:
        return {"error": "data-processor pod not found"}

    pod.memory_limit = "256Mi"
    cluster.inject_failure(pod.name, "OOMKilled", pod.namespace)
    return {
        "name": "Scenario 3: Guardrail Escalation",
        "description": (
            "A data-processor pod was OOMKilled. The agent will try to increase "
            "memory aggressively (beyond 2x original), which hits the memory "
            "guardrail. It should then escalate to a human."
        ),
        "event": {
            "pod_name": pod.name,
            "namespace": pod.namespace,
            "reason": "OOMKilled",
            "status": "OOMKilled",
            "message": f"Container in {pod.name} killed (memory limit 256Mi exceeded)",
        },
    }


def scenario_systemic_failure(cluster: MockCluster) -> dict[str, Any]:
    """>50% of production pods unhealthy → blast-radius guardrail triggers escalation."""
    prod_pods = [p for p in cluster.pods.values() if p.namespace == "production"]
    target_unhealthy = max(1, int(len(prod_pods) * 0.6))
    healthy = [p for p in prod_pods if p.status == PodStatus.RUNNING]
    for pod in healthy[:target_unhealthy]:
        cluster.inject_failure(pod.name, "Error", pod.namespace)

    victim = healthy[0] if healthy else prod_pods[0]
    return {
        "name": "Scenario 4: Systemic Failure",
        "description": (
            "More than 50% of production pods are unhealthy. The blast-radius "
            "guardrail should stop any remediation attempt and immediately "
            "escalate to a human."
        ),
        "event": {
            "pod_name": victim.name,
            "namespace": victim.namespace,
            "reason": "Error",
            "status": "Error",
            "message": (
                f"Pod {victim.name} failed — multiple services affected cluster-wide"
            ),
        },
    }
