"""Mock Kubernetes cluster simulator.

Simulates pods, deployments, services, and events entirely in-memory.
Supports failure injection and realistic log generation.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


class PodStatus(str, Enum):
    RUNNING = "Running"
    CRASH_LOOP_BACK_OFF = "CrashLoopBackOff"
    OOM_KILLED = "OOMKilled"
    IMAGE_PULL_BACK_OFF = "ImagePullBackOff"
    ERROR = "Error"
    PENDING = "Pending"


# ---------------------------------------------------------------------------
# Realistic log templates per failure type
# ---------------------------------------------------------------------------

_RUNNING_LOGS: list[str] = [
    "INFO  {ts} — Health check passed (HTTP 200)",
    "INFO  {ts} — Request processed in 42ms (GET /api/v1/status)",
    "INFO  {ts} — Connection pool: 8/20 active",
    "DEBUG {ts} — Cache hit ratio: 94.2%",
    "INFO  {ts} — Metrics flushed to collector",
]

_CRASH_LOOP_LOGS: list[str] = [
    "ERROR {ts} — NullPointerException at com.app.service.PaymentHandler.process(PaymentHandler.java:142)",
    "ERROR {ts} — Unhandled exception in main loop: IndexError: list index out of range",
    "FATAL {ts} — Application startup failed: cannot bind to port 8080 (address already in use)",
    "ERROR {ts} — Panic: runtime error: invalid memory address or nil pointer dereference",
    "ERROR {ts} — Traceback (most recent call last): ... KeyError: 'DATABASE_URL'",
    "INFO  {ts} — Container restarting (exit code 1)",
]

_OOM_LOGS: list[str] = [
    "WARN  {ts} — Memory usage 490Mi / 512Mi (95.7%)",
    "WARN  {ts} — GC overhead limit exceeded — heap usage 498Mi",
    "ERROR {ts} — Container killed due to OOM. Memory usage: 510Mi, limit: 512Mi",
    "INFO  {ts} — Killed process 1 (app) total-vm:524288kB, anon-rss:523264kB",
    "INFO  {ts} — Container restarting after OOMKilled",
]

_IMAGE_PULL_LOGS: list[str] = [
    "WARN  {ts} — Failed to pull image 'myapp:v2.3.1-bad': manifest unknown",
    "WARN  {ts} — Error: ErrImagePull — rpc error: code = NotFound",
    "WARN  {ts} — Back-off pulling image 'myapp:v2.3.1-bad'",
    "INFO  {ts} — Normal pulling image 'myapp:v2.3.1-bad' ...",
    "WARN  {ts} — Failed to pull image 'myapp:v2.3.1-bad': unauthorized",
]

_ERROR_LOGS: list[str] = [
    "ERROR {ts} — Connection refused: upstream service 'db-primary' at 10.0.2.15:5432",
    "ERROR {ts} — TLS handshake timeout after 30s",
    "ERROR {ts} — HTTP 503 from dependency 'auth-service'",
    "ERROR {ts} — Circuit breaker OPEN for 'payment-gateway' (failure rate 72%)",
    "WARN  {ts} — Retries exhausted (3/3) for request to /api/v1/process",
]

_PENDING_LOGS: list[str] = [
    "WARN  {ts} — 0/3 nodes are available: insufficient cpu",
    "WARN  {ts} — Pod scheduled but waiting for resources",
    "INFO  {ts} — Waiting for node affinity match",
]

_LOG_MAP: dict[PodStatus, list[str]] = {
    PodStatus.RUNNING: _RUNNING_LOGS,
    PodStatus.CRASH_LOOP_BACK_OFF: _CRASH_LOOP_LOGS,
    PodStatus.OOM_KILLED: _OOM_LOGS,
    PodStatus.IMAGE_PULL_BACK_OFF: _IMAGE_PULL_LOGS,
    PodStatus.ERROR: _ERROR_LOGS,
    PodStatus.PENDING: _PENDING_LOGS,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts() -> str:
    return _now().isoformat()


def _generate_logs(status: PodStatus, count: int = 15) -> list[str]:
    """Generate realistic log lines for a given pod status."""
    templates = _LOG_MAP.get(status, _RUNNING_LOGS)
    logs: list[str] = []
    base = _now() - timedelta(minutes=count)
    for i in range(count):
        ts = (base + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tpl = random.choice(templates)
        logs.append(tpl.format(ts=ts))
    return logs


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Pod:
    name: str
    namespace: str = "default"
    status: PodStatus = PodStatus.RUNNING
    restart_count: int = 0
    memory_usage: str = "128Mi"
    memory_limit: str = "256Mi"
    cpu_usage: str = "100m"
    cpu_limit: str = "250m"
    image: str = "myapp:v1.0.0"
    created_at: str = field(default_factory=_ts)
    logs: list[str] = field(default_factory=list)
    deployment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "status": self.status.value,
            "restart_count": self.restart_count,
            "memory_usage": self.memory_usage,
            "memory_limit": self.memory_limit,
            "cpu_usage": self.cpu_usage,
            "cpu_limit": self.cpu_limit,
            "image": self.image,
            "created_at": self.created_at,
            "deployment": self.deployment,
        }


@dataclass
class Deployment:
    name: str
    namespace: str = "default"
    replicas: int = 2
    current_replicas: int = 2
    image: str = "myapp:v1.0.0"
    revision_history: list[dict[str, Any]] = field(default_factory=list)
    last_deployed: str = field(default_factory=_ts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "replicas": self.replicas,
            "current_replicas": self.current_replicas,
            "image": self.image,
            "revision_history": self.revision_history,
            "last_deployed": self.last_deployed,
        }


@dataclass
class Event:
    timestamp: str
    event_type: str  # Normal / Warning
    reason: str
    message: str
    involved_object: str
    namespace: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "type": self.event_type,
            "reason": self.reason,
            "message": self.message,
            "involved_object": self.involved_object,
            "namespace": self.namespace,
        }


# ---------------------------------------------------------------------------
# Mock cluster
# ---------------------------------------------------------------------------


class MockCluster:
    """In-memory Kubernetes cluster simulator."""

    def __init__(self) -> None:
        self.pods: dict[str, Pod] = {}
        self.deployments: dict[str, Deployment] = {}
        self.events: list[Event] = []
        self._tick_count: int = 0
        self._initialize()

    # -- bootstrap --------------------------------------------------------

    def _initialize(self) -> None:
        """Seed the cluster with a realistic set of workloads."""
        prod_services = [
            ("api-server", "myapp/api-server:v1.2.0"),
            ("payment-service", "myapp/payment-service:v3.1.0"),
            ("user-service", "myapp/user-service:v2.0.4"),
            ("data-processor", "myapp/data-processor:v1.8.2"),
            ("notification-service", "myapp/notification-service:v1.1.0"),
        ]
        staging_services = [
            ("api-server-staging", "myapp/api-server:v1.3.0-rc1"),
            ("worker-staging", "myapp/worker:v0.9.0-beta"),
        ]

        for svc_name, image in prod_services:
            dep = Deployment(
                name=svc_name,
                namespace="production",
                replicas=2,
                current_replicas=2,
                image=image,
                revision_history=[{"revision": 1, "image": image, "timestamp": _ts()}],
            )
            self.deployments[f"production/{svc_name}"] = dep
            for i in range(dep.replicas):
                pod = Pod(
                    name=f"{svc_name}-{uuid.uuid4().hex[:6]}",
                    namespace="production",
                    image=image,
                    deployment=svc_name,
                    logs=_generate_logs(PodStatus.RUNNING),
                )
                self.pods[f"production/{pod.name}"] = pod

        for svc_name, image in staging_services:
            dep = Deployment(
                name=svc_name,
                namespace="staging",
                replicas=1,
                current_replicas=1,
                image=image,
                revision_history=[{"revision": 1, "image": image, "timestamp": _ts()}],
            )
            self.deployments[f"staging/{svc_name}"] = dep
            pod = Pod(
                name=f"{svc_name}-{uuid.uuid4().hex[:6]}",
                namespace="staging",
                image=image,
                deployment=svc_name,
                logs=_generate_logs(PodStatus.RUNNING),
            )
            self.pods[f"staging/{pod.name}"] = pod

        self._add_event("Normal", "ClusterReady", "All pods initialised and healthy", "cluster", "production")

    # -- helpers ----------------------------------------------------------

    def _add_event(
        self, event_type: str, reason: str, message: str, involved_object: str, namespace: str = "default"
    ) -> None:
        self.events.append(
            Event(
                timestamp=_ts(),
                event_type=event_type,
                reason=reason,
                message=message,
                involved_object=involved_object,
                namespace=namespace,
            )
        )

    def _key(self, name: str, namespace: str) -> str:
        return f"{namespace}/{name}"

    def _find_pod(self, pod_name: str, namespace: str = "default") -> Pod | None:
        key = self._key(pod_name, namespace)
        if key in self.pods:
            return self.pods[key]
        # Try to match by pod name prefix across namespaces
        for k, p in self.pods.items():
            if p.name == pod_name and (namespace == "default" or p.namespace == namespace):
                return p
        return None

    def _find_deployment(self, deployment_name: str) -> Deployment | None:
        for k, d in self.deployments.items():
            if d.name == deployment_name:
                return d
        return None

    # -- public API -------------------------------------------------------

    def get_all_pods(self, namespace: str | None = None) -> list[dict[str, Any]]:
        pods = self.pods.values()
        if namespace:
            pods = [p for p in pods if p.namespace == namespace]
        return [p.to_dict() for p in pods]

    def get_pod(self, pod_name: str, namespace: str = "default") -> dict[str, Any] | None:
        pod = self._find_pod(pod_name, namespace)
        if pod:
            return pod.to_dict()
        return None

    def get_pod_logs(self, pod_name: str, namespace: str = "default", lines: int = 50) -> list[str]:
        pod = self._find_pod(pod_name, namespace)
        if not pod:
            return [f"Error: pod '{pod_name}' not found in namespace '{namespace}'"]
        if not pod.logs:
            pod.logs = _generate_logs(pod.status, count=lines)
        return pod.logs[-lines:]

    def get_events(self, namespace: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        evts = self.events
        if namespace:
            evts = [e for e in evts if e.namespace == namespace]
        return [e.to_dict() for e in evts[-limit:]]

    def get_deployment(self, deployment_name: str) -> dict[str, Any] | None:
        dep = self._find_deployment(deployment_name)
        if dep:
            return dep.to_dict()
        return None

    def restart_pod(self, pod_name: str, namespace: str = "default") -> str:
        pod = self._find_pod(pod_name, namespace)
        if not pod:
            return f"Error: pod '{pod_name}' not found in namespace '{namespace}'"
        old_status = pod.status
        pod.status = PodStatus.RUNNING
        pod.restart_count += 1
        pod.logs = _generate_logs(PodStatus.RUNNING)
        self._add_event(
            "Normal",
            "PodRestarted",
            f"Pod {pod_name} restarted (was {old_status.value})",
            pod_name,
            pod.namespace,
        )
        return f"Pod '{pod_name}' restarted successfully. Previous status: {old_status.value}"

    def scale_deployment(self, deployment_name: str, replicas: int) -> str:
        dep = self._find_deployment(deployment_name)
        if not dep:
            return f"Error: deployment '{deployment_name}' not found"

        old_count = dep.current_replicas
        dep.replicas = replicas

        # Remove or add pods to match
        dep_pods = [p for p in self.pods.values() if p.deployment == deployment_name]
        current_count = len(dep_pods)

        if replicas > current_count:
            for _ in range(replicas - current_count):
                pod = Pod(
                    name=f"{deployment_name}-{uuid.uuid4().hex[:6]}",
                    namespace=dep.namespace,
                    image=dep.image,
                    deployment=deployment_name,
                    logs=_generate_logs(PodStatus.RUNNING),
                )
                self.pods[self._key(pod.name, dep.namespace)] = pod
        elif replicas < current_count:
            to_remove = current_count - replicas
            for pod in dep_pods[:to_remove]:
                del self.pods[self._key(pod.name, pod.namespace)]

        dep.current_replicas = replicas
        self._add_event(
            "Normal",
            "ScaledDeployment",
            f"Deployment {deployment_name} scaled from {old_count} to {replicas}",
            deployment_name,
            dep.namespace,
        )
        return f"Deployment '{deployment_name}' scaled from {old_count} to {replicas}"

    def rollback_deployment(self, deployment_name: str, revision: int | None = None) -> str:
        dep = self._find_deployment(deployment_name)
        if not dep:
            return f"Error: deployment '{deployment_name}' not found"
        if not dep.revision_history:
            return f"Error: no revision history for deployment '{deployment_name}'"

        if revision is not None:
            target = next((r for r in dep.revision_history if r["revision"] == revision), None)
        else:
            # Rollback to previous revision (second-to-last if multiple, else first)
            target = dep.revision_history[-2] if len(dep.revision_history) > 1 else dep.revision_history[-1]

        if not target:
            return f"Error: revision {revision} not found"

        old_image = dep.image
        dep.image = target["image"]
        dep.last_deployed = _ts()
        dep.revision_history.append(
            {"revision": len(dep.revision_history) + 1, "image": target["image"], "timestamp": _ts()}
        )

        # Restart all pods in this deployment with new image
        for pod in self.pods.values():
            if pod.deployment == deployment_name:
                pod.image = target["image"]
                pod.status = PodStatus.RUNNING
                pod.restart_count += 1
                pod.logs = _generate_logs(PodStatus.RUNNING)

        self._add_event(
            "Normal",
            "RolledBack",
            f"Deployment {deployment_name} rolled back from {old_image} to {target['image']}",
            deployment_name,
            dep.namespace,
        )
        return f"Deployment '{deployment_name}' rolled back to image {target['image']}"

    def update_resource_limits(
        self, pod_name: str, memory: str | None = None, cpu: str | None = None
    ) -> str:
        pod = self._find_pod(pod_name)
        if not pod:
            # Try across all namespaces
            for p in self.pods.values():
                if p.name == pod_name:
                    pod = p
                    break
        if not pod:
            return f"Error: pod '{pod_name}' not found"

        changes: list[str] = []
        if memory:
            old = pod.memory_limit
            pod.memory_limit = memory
            changes.append(f"memory {old} → {memory}")
        if cpu:
            old = pod.cpu_limit
            pod.cpu_limit = cpu
            changes.append(f"cpu {old} → {cpu}")

        if not changes:
            return "No changes specified"

        # If pod was OOMKilled and memory was increased, recover it
        if pod.status == PodStatus.OOM_KILLED and memory:
            pod.status = PodStatus.RUNNING
            pod.logs = _generate_logs(PodStatus.RUNNING)

        desc = ", ".join(changes)
        self._add_event(
            "Normal",
            "ResourcesUpdated",
            f"Pod {pod_name} resource limits updated: {desc}",
            pod_name,
            pod.namespace,
        )
        return f"Pod '{pod_name}' resource limits updated: {desc}"

    # -- failure injection ------------------------------------------------

    def inject_failure(self, pod_name: str, failure_type: str, namespace: str | None = None) -> str:
        """Inject a failure into a specific pod."""
        pod: Pod | None = None
        if namespace:
            pod = self._find_pod(pod_name, namespace)
        else:
            for p in self.pods.values():
                if p.name == pod_name or (p.deployment and p.deployment == pod_name):
                    pod = p
                    break
            # Also try finding by deployment name and inject into all pods
            if pod is None:
                for p in self.pods.values():
                    if p.deployment == pod_name:
                        pod = p
                        break

        if not pod:
            return f"Error: pod '{pod_name}' not found"

        try:
            status = PodStatus(failure_type)
        except ValueError:
            return f"Error: unknown failure type '{failure_type}'"

        pod.status = status
        pod.logs = _generate_logs(status)

        event_msgs = {
            PodStatus.CRASH_LOOP_BACK_OFF: f"Back-off restarting failed container in pod {pod.name}",
            PodStatus.OOM_KILLED: f"Container in pod {pod.name} was OOMKilled (limit: {pod.memory_limit})",
            PodStatus.IMAGE_PULL_BACK_OFF: f"Failed to pull image '{pod.image}' for pod {pod.name}",
            PodStatus.ERROR: f"Container in pod {pod.name} exited with error",
            PodStatus.PENDING: f"Pod {pod.name} is pending — insufficient resources",
        }

        self._add_event(
            "Warning",
            status.value,
            event_msgs.get(status, f"Pod {pod.name} entered {status.value}"),
            pod.name,
            pod.namespace,
        )
        return f"Injected {failure_type} into pod '{pod.name}' (namespace: {pod.namespace})"

    def inject_failure_on_deployment(self, deployment_name: str, failure_type: str) -> str:
        """Inject a failure into ALL pods of a deployment."""
        dep = self._find_deployment(deployment_name)
        if not dep:
            return f"Error: deployment '{deployment_name}' not found"

        results: list[str] = []
        for pod in list(self.pods.values()):
            if pod.deployment == deployment_name:
                results.append(self.inject_failure(pod.name, failure_type, pod.namespace))
        return "; ".join(results) if results else "No pods found for deployment"

    def simulate_deploy(self, deployment_name: str, new_image: str) -> str:
        """Simulate a new deployment (push a new revision)."""
        dep = self._find_deployment(deployment_name)
        if not dep:
            return f"Error: deployment '{deployment_name}' not found"

        old_image = dep.image
        dep.image = new_image
        dep.last_deployed = _ts()
        dep.revision_history.append(
            {"revision": len(dep.revision_history) + 1, "image": new_image, "timestamp": _ts()}
        )

        for pod in self.pods.values():
            if pod.deployment == deployment_name:
                pod.image = new_image

        self._add_event(
            "Normal",
            "DeploymentUpdated",
            f"Deployment {deployment_name} updated: {old_image} → {new_image}",
            deployment_name,
            dep.namespace,
        )
        return f"Deployed {new_image} to {deployment_name} (was {old_image})"

    def random_failure(self) -> str:
        """Inject a random failure into a random pod."""
        healthy = [p for p in self.pods.values() if p.status == PodStatus.RUNNING]
        if not healthy:
            return "No healthy pods to inject failure into"
        pod = random.choice(healthy)
        failure = random.choice(
            [PodStatus.CRASH_LOOP_BACK_OFF, PodStatus.OOM_KILLED, PodStatus.ERROR]
        )
        return self.inject_failure(pod.name, failure.value, pod.namespace)

    def tick(self) -> list[dict[str, Any]]:
        """Advance simulation time. Returns any new events generated."""
        self._tick_count += 1
        new_events: list[dict[str, Any]] = []
        # Small chance of random event each tick
        if random.random() < 0.1:
            msg = self.random_failure()
            if not msg.startswith("No healthy"):
                new_events.append({"tick": self._tick_count, "action": msg})
        return new_events

    def get_unhealthy_pods(self) -> list[dict[str, Any]]:
        """Return all pods not in Running state."""
        return [p.to_dict() for p in self.pods.values() if p.status != PodStatus.RUNNING]

    def get_namespace_health(self, namespace: str) -> dict[str, Any]:
        """Get health summary for a namespace."""
        ns_pods = [p for p in self.pods.values() if p.namespace == namespace]
        total = len(ns_pods)
        healthy = sum(1 for p in ns_pods if p.status == PodStatus.RUNNING)
        return {
            "namespace": namespace,
            "total_pods": total,
            "healthy_pods": healthy,
            "unhealthy_pods": total - healthy,
            "health_ratio": healthy / total if total > 0 else 1.0,
        }
