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
    """CrashLoopBackOff on a pod with no recent deploy -> restart."""
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


def scenario_imagepull_rollback(cluster: MockCluster) -> dict[str, Any]:
    """ImagePullBackOff after deploying a non-existent image tag -> rollback.

    Catalog: S03 (Deployment Rollout Stuck) — corner case #1:
    'New image tag doesn't exist in registry -> ImagePullBackOff on all new replicas.'
    """
    deployment = "api-server"
    bad_image = "myapp/api-server:v9.9.9-nonexistent"
    result = cluster.simulate_deploy(deployment, bad_image)
    if result.startswith("Error"):
        return {"error": result}

    cluster.inject_failure_on_deployment(deployment, "ImagePullBackOff")
    pod = _first_pod(cluster, deployment)
    if pod is None:
        return {"error": "api-server pod not found"}

    return {
        "name": "Scenario 5: ImagePull Rollback",
        "corner_case": (
            "S03#1 — Image tag doesn't exist in registry; ImagePullBackOff on all new "
            "replicas right after a deploy."
        ),
        "naive_baseline": (
            "A naive watchdog would restart the pods in a loop — useless, because the "
            "image itself is unpullable."
        ),
        "description": (
            f"api-server was just deployed with a bad image tag ({bad_image}). "
            "All replicas are stuck in ImagePullBackOff. The agent should distinguish "
            "this from a crash, correlate with the recent deploy, and rollback."
        ),
        "event": {
            "pod_name": pod.name,
            "namespace": pod.namespace,
            "reason": "ImagePullBackOff",
            "status": "ImagePullBackOff",
            "message": (
                f"Pod {pod.name} cannot pull image {bad_image} after recent deploy of "
                "api-server"
            ),
        },
    }


def scenario_pending_capacity_escalation(cluster: MockCluster) -> dict[str, Any]:
    """Pending pod due to insufficient cluster capacity -> escalate.

    Catalog: S09 (Resource Quota Exhaustion) corner case #4 / S02 capacity:
    pod cannot schedule, no pod-level fault — none of the agent's tools can fix it.
    """
    pod = _first_pod(cluster, "user-service")
    if pod is None:
        return {"error": "user-service pod not found"}

    cluster.inject_failure(pod.name, "Pending", pod.namespace)
    cluster._add_event(
        "Warning",
        "FailedScheduling",
        f"0/3 nodes are available: insufficient cpu. Pod {pod.name} cannot be scheduled.",
        pod.name,
        pod.namespace,
    )

    return {
        "name": "Scenario 6: Pending Pod — Capacity Escalation",
        "corner_case": (
            "S09#4 / S02 — Pending pod with FailedScheduling 'insufficient cpu'. "
            "Looks like a pod problem; is actually a cluster capacity problem."
        ),
        "naive_baseline": (
            "A naive watchdog would call restart_pod — a no-op, since the pod was "
            "never scheduled in the first place."
        ),
        "description": (
            "A user-service pod is stuck Pending because the cluster has no spare "
            "CPU. The agent has no tool that can create capacity, so it should "
            "recognize its own limits and escalate to a human cleanly."
        ),
        "event": {
            "pod_name": pod.name,
            "namespace": pod.namespace,
            "reason": "FailedScheduling",
            "status": "Pending",
            "message": (
                f"Pod {pod.name} pending — 0/3 nodes available: insufficient cpu"
            ),
        },
    }


def scenario_systemic_failure(cluster: MockCluster) -> dict[str, Any]:
    """>50% of production pods unhealthy -> blast-radius guardrail triggers escalation."""
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
