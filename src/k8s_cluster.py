"""Real Kubernetes cluster adapter.

Implements the same interface as MockCluster, backed by the real Kubernetes
API via the official Python client. Drop-in replacement: all existing agent /
guardrails / mcp_server code works unchanged.

Designed to run inside a pod with a ServiceAccount that has RBAC to:
  - pods:            get, list, watch, delete
  - pods/log:        get, list, watch
  - events:          get, list, watch
  - deployments:     get, list, watch, patch, update
  - replicasets:     get, list, watch
  - configmaps:      get, list, patch
  - nodes:           get, list, watch
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException


# ---------------------------------------------------------------------------
# Lightweight pod wrapper so `.name` / `.to_dict()` / `.deployment` work
# the same way MockCluster.pods.values() does (guardrails.py touches these).
# ---------------------------------------------------------------------------

class _PodView:
    """Read-only pod view with the subset of fields MockCluster exposes."""

    def __init__(self, pod_dict: dict[str, Any]) -> None:
        self._d = pod_dict
        self.name = pod_dict["name"]
        self.namespace = pod_dict["namespace"]
        self.deployment = pod_dict.get("deployment")
        self.status = pod_dict["status"]
        self.memory_limit = pod_dict.get("memory_limit", "256Mi")

    def to_dict(self) -> dict[str, Any]:
        return dict(self._d)


# ---------------------------------------------------------------------------
# Status mapping — map real K8s pod phase/conditions to MockCluster statuses
# ---------------------------------------------------------------------------

def _derive_status(pod: Any) -> str:
    """Map a V1Pod to a MockCluster-style status string."""
    phase = pod.status.phase or "Unknown"

    if phase == "Pending":
        # Check for ImagePullBackOff / ErrImagePull in container statuses
        for cs in (pod.status.container_statuses or []):
            waiting = getattr(cs.state, "waiting", None)
            if waiting and waiting.reason in ("ImagePullBackOff", "ErrImagePull"):
                return "ImagePullBackOff"
        return "Pending"

    if phase == "Running":
        # Check if any container is crashing
        for cs in (pod.status.container_statuses or []):
            waiting = getattr(cs.state, "waiting", None)
            if waiting and waiting.reason == "CrashLoopBackOff":
                return "CrashLoopBackOff"
            terminated = getattr(cs.last_state, "terminated", None) if cs.last_state else None
            if terminated and terminated.reason == "OOMKilled":
                return "OOMKilled"
        return "Running"

    if phase == "Failed":
        # Check termination reason
        for cs in (pod.status.container_statuses or []):
            terminated = getattr(cs.state, "terminated", None)
            if terminated and terminated.reason == "OOMKilled":
                return "OOMKilled"
        return "Error"

    return phase  # Succeeded, Unknown, etc.


def _deployment_from_owner(pod: Any) -> str | None:
    """Infer deployment name from pod owner references (via ReplicaSet)."""
    refs = pod.metadata.owner_references or []
    for ref in refs:
        if ref.kind == "ReplicaSet":
            # ReplicaSet name format: <deployment>-<hash>
            name = ref.name
            # Strip trailing hash (last segment after final dash)
            parts = name.rsplit("-", 1)
            return parts[0] if len(parts) == 2 else name
    return None


def _pod_to_dict(pod: Any) -> dict[str, Any]:
    """Convert a V1Pod into the MockCluster-style dict agent code expects."""
    spec = pod.spec
    container = spec.containers[0] if spec.containers else None
    resources = getattr(container, "resources", None) if container else None
    limits = (resources.limits if resources and resources.limits else {}) or {}
    requests = (resources.requests if resources and resources.requests else {}) or {}

    restart_count = 0
    for cs in (pod.status.container_statuses or []):
        restart_count += cs.restart_count or 0

    return {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "status": _derive_status(pod),
        "restart_count": restart_count,
        "memory_usage": requests.get("memory", ""),  # Actual usage requires metrics-server
        "memory_limit": limits.get("memory", "256Mi"),
        "cpu_usage": requests.get("cpu", ""),
        "cpu_limit": limits.get("cpu", "250m"),
        "image": container.image if container else "",
        "created_at": pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else "",
        "deployment": _deployment_from_owner(pod),
    }


# ---------------------------------------------------------------------------
# KubernetesCluster — the adapter
# ---------------------------------------------------------------------------

class KubernetesCluster:
    """Real-K8s backed cluster. Matches MockCluster's public surface."""

    def __init__(self, watch_namespaces: list[str] | None = None) -> None:
        # Auto-detect: in-cluster when running as a pod, kubeconfig when local.
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()

        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        # If None, scan all namespaces. Otherwise restrict to these.
        self.watch_namespaces = watch_namespaces

    # ------------------------------------------------------------------
    # Private dict-like views so `cluster.pods.values()` and
    # `cluster.deployments` still work (called from guardrails.py)
    # ------------------------------------------------------------------

    @property
    def pods(self) -> dict[str, _PodView]:
        """Dict of {namespace/name: _PodView} across watched namespaces."""
        out: dict[str, _PodView] = {}
        for ns in self._namespaces():
            try:
                pods = self.core.list_namespaced_pod(ns).items
            except ApiException:
                continue
            for p in pods:
                key = f"{p.metadata.namespace}/{p.metadata.name}"
                out[key] = _PodView(_pod_to_dict(p))
        return out

    @property
    def deployments(self) -> dict[str, Any]:
        """Dict of {name: object-with-.to_dict()} matching MockCluster shape."""
        class _DepView:
            def __init__(self, d: dict[str, Any]) -> None:
                self._d = d
                self.name = d["name"]
            def to_dict(self) -> dict[str, Any]:
                return dict(self._d)

        out: dict[str, Any] = {}
        for ns in self._namespaces():
            try:
                deps = self.apps.list_namespaced_deployment(ns).items
            except ApiException:
                continue
            for d in deps:
                out[d.metadata.name] = _DepView(self._deployment_to_dict(d))
        return out

    def _namespaces(self) -> list[str]:
        if self.watch_namespaces:
            return self.watch_namespaces
        try:
            return [ns.metadata.name for ns in self.core.list_namespace().items
                    if ns.metadata.name not in ("kube-system", "kube-public", "kube-node-lease",
                                                 "gke-managed-system", "gmp-system", "gmp-public")]
        except ApiException:
            return ["default"]

    # ------------------------------------------------------------------
    # MockCluster-compatible public API
    # ------------------------------------------------------------------

    def get_all_pods(self) -> list[dict[str, Any]]:
        return [v.to_dict() for v in self.pods.values()]

    def get_pod(self, pod_name: str, namespace: str = "default") -> dict[str, Any] | None:
        try:
            p = self.core.read_namespaced_pod(pod_name, namespace)
            return _pod_to_dict(p)
        except ApiException:
            # Try other namespaces as a fallback (MockCluster does this)
            for ns in self._namespaces():
                if ns == namespace:
                    continue
                try:
                    p = self.core.read_namespaced_pod(pod_name, ns)
                    return _pod_to_dict(p)
                except ApiException:
                    continue
            return None

    def get_pod_logs(self, pod_name: str, namespace: str = "default", lines: int = 50) -> list[str]:
        try:
            raw = self.core.read_namespaced_pod_log(
                pod_name, namespace, tail_lines=lines, timestamps=True
            )
            return [line for line in (raw or "").split("\n") if line]
        except ApiException as e:
            # Pod might be in a state where logs aren't available yet
            return [f"[log unavailable: {e.reason}]"]

    def get_events(self, namespace: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        namespaces = [namespace] if namespace else self._namespaces()
        for ns in namespaces:
            try:
                evs = self.core.list_namespaced_event(ns).items
            except ApiException:
                continue
            for e in evs:
                events.append({
                    "timestamp": (e.last_timestamp or e.event_time or e.metadata.creation_timestamp or "").isoformat()
                        if e.last_timestamp or e.event_time or e.metadata.creation_timestamp else "",
                    "type": e.type or "",
                    "reason": e.reason or "",
                    "message": e.message or "",
                    "involved_object": e.involved_object.name if e.involved_object else "",
                    "namespace": e.metadata.namespace,
                })
        # Sort newest-first and truncate
        events.sort(key=lambda x: x["timestamp"], reverse=True)
        return events[:limit]

    def get_deployment(self, deployment_name: str) -> dict[str, Any] | None:
        for ns in self._namespaces():
            try:
                d = self.apps.read_namespaced_deployment(deployment_name, ns)
                return self._deployment_to_dict(d)
            except ApiException:
                continue
        return None

    def _deployment_to_dict(self, d: Any) -> dict[str, Any]:
        """Real deployment → MockCluster-shaped dict with revision_history."""
        container = d.spec.template.spec.containers[0] if d.spec.template.spec.containers else None
        image = container.image if container else ""

        # Build revision history from ReplicaSets owned by this deployment
        revision_history: list[dict[str, Any]] = []
        try:
            rs_list = self.apps.list_namespaced_replica_set(d.metadata.namespace).items
            # Filter to ReplicaSets owned by this deployment
            owned = [rs for rs in rs_list
                     if any(r.kind == "Deployment" and r.name == d.metadata.name
                            for r in (rs.metadata.owner_references or []))]
            # Sort by revision annotation
            owned.sort(key=lambda rs: int(
                (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0")
            ))
            for rs in owned:
                rev = int((rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0"))
                rs_container = rs.spec.template.spec.containers[0] if rs.spec.template.spec.containers else None
                revision_history.append({
                    "revision": rev,
                    "image": rs_container.image if rs_container else "",
                    "timestamp": rs.metadata.creation_timestamp.isoformat() if rs.metadata.creation_timestamp else "",
                })
        except ApiException:
            pass

        last_deployed = ""
        if revision_history:
            last_deployed = revision_history[-1]["timestamp"]

        return {
            "name": d.metadata.name,
            "namespace": d.metadata.namespace,
            "replicas": d.spec.replicas or 0,
            "current_replicas": d.status.ready_replicas or 0,
            "image": image,
            "revision_history": revision_history,
            "last_deployed": last_deployed,
        }

    # ------------------------------------------------------------------
    # Remediation actions (state-changing) — called via guardrails
    # ------------------------------------------------------------------

    def restart_pod(self, pod_name: str, namespace: str = "default") -> str:
        """Delete the pod — the controller will recreate it."""
        try:
            self.core.delete_namespaced_pod(pod_name, namespace)
            return f"Pod '{pod_name}' deleted (will be recreated by controller)"
        except ApiException as e:
            return f"Error: {e.reason} (status {e.status})"

    def scale_deployment(self, deployment_name: str, replicas: int) -> str:
        for ns in self._namespaces():
            try:
                self.apps.patch_namespaced_deployment_scale(
                    deployment_name, ns,
                    body={"spec": {"replicas": replicas}},
                )
                return f"Deployment '{deployment_name}' scaled to {replicas}"
            except ApiException as e:
                if e.status == 404:
                    continue
                return f"Error: {e.reason} (status {e.status})"
        return f"Error: deployment '{deployment_name}' not found"

    def rollback_deployment(self, deployment_name: str, revision: int | None = None) -> str:
        """Roll back by patching the deployment's pod template to a previous RS's spec."""
        for ns in self._namespaces():
            try:
                dep = self.apps.read_namespaced_deployment(deployment_name, ns)
            except ApiException as e:
                if e.status == 404:
                    continue
                return f"Error: {e.reason}"

            # Find previous revision
            try:
                rs_list = self.apps.list_namespaced_replica_set(ns).items
            except ApiException as e:
                return f"Error reading ReplicaSets: {e.reason}"

            owned = [rs for rs in rs_list
                     if any(r.kind == "Deployment" and r.name == deployment_name
                            for r in (rs.metadata.owner_references or []))]
            owned.sort(key=lambda rs: int(
                (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0")
            ))

            if len(owned) < 2 and revision is None:
                return f"Error: no previous revision available for '{deployment_name}'"

            if revision is not None:
                target = next((rs for rs in owned
                               if int((rs.metadata.annotations or {}).get(
                                   "deployment.kubernetes.io/revision", "0")) == revision), None)
            else:
                target = owned[-2]  # second-to-last = previous

            if not target:
                return f"Error: revision {revision} not found for '{deployment_name}'"

            # Patch deployment's pod template with target's template
            patch = {"spec": {"template": target.spec.template.to_dict()}}
            try:
                self.apps.patch_namespaced_deployment(deployment_name, ns, body=patch)
                target_rev = (target.metadata.annotations or {}).get(
                    "deployment.kubernetes.io/revision", "?")
                return f"Deployment '{deployment_name}' rolled back to revision {target_rev}"
            except ApiException as e:
                return f"Error: {e.reason} (status {e.status})"

        return f"Error: deployment '{deployment_name}' not found"

    def update_resource_limits(
        self, pod_name: str, memory: str | None = None, cpu: str | None = None
    ) -> str:
        """Update via the deployment's pod template (real K8s can't patch pod resources in-place)."""
        # Find the pod's owning deployment
        pod_dict = self.get_pod(pod_name)
        if not pod_dict:
            return f"Error: pod '{pod_name}' not found"
        deployment = pod_dict.get("deployment")
        namespace = pod_dict["namespace"]
        if not deployment:
            return f"Error: pod '{pod_name}' has no owning deployment"

        try:
            dep = self.apps.read_namespaced_deployment(deployment, namespace)
        except ApiException as e:
            return f"Error reading deployment: {e.reason}"

        # Build patch
        container = dep.spec.template.spec.containers[0]
        resources = container.resources or client.V1ResourceRequirements(limits={}, requests={})
        new_limits = dict(resources.limits or {})
        if memory:
            new_limits["memory"] = memory
        if cpu:
            new_limits["cpu"] = cpu

        patch = {"spec": {"template": {"spec": {"containers": [
            {"name": container.name, "resources": {"limits": new_limits}}
        ]}}}}

        try:
            self.apps.patch_namespaced_deployment(deployment, namespace, body=patch)
            parts = []
            if memory:
                parts.append(f"memory → {memory}")
            if cpu:
                parts.append(f"cpu → {cpu}")
            return f"Deployment '{deployment}' patched: {', '.join(parts)} (triggers pod recreation)"
        except ApiException as e:
            return f"Error: {e.reason} (status {e.status})"

    # ------------------------------------------------------------------
    # Read-only helpers
    # ------------------------------------------------------------------

    def get_namespace_health(self, namespace: str) -> dict[str, Any]:
        try:
            pods = self.core.list_namespaced_pod(namespace).items
        except ApiException:
            return {"namespace": namespace, "total_pods": 0, "healthy_pods": 0,
                    "unhealthy_pods": 0, "health_ratio": 1.0}
        total = len(pods)
        healthy = sum(1 for p in pods if _derive_status(p) == "Running")
        return {
            "namespace": namespace,
            "total_pods": total,
            "healthy_pods": healthy,
            "unhealthy_pods": total - healthy,
            "health_ratio": healthy / total if total > 0 else 1.0,
        }

    def get_unhealthy_pods(self) -> list[dict[str, Any]]:
        """Used by the poller — returns pods that need attention."""
        out: list[dict[str, Any]] = []
        for ns in self._namespaces():
            try:
                pods = self.core.list_namespaced_pod(ns).items
            except ApiException:
                continue
            for p in pods:
                status = _derive_status(p)
                if status not in ("Running", "Succeeded"):
                    out.append(_pod_to_dict(p))
        return out
