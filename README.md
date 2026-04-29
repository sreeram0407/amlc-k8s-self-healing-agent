# K8s Self-Healing Agent

An autonomous Kubernetes reliability agent. When a pod fails, the agent investigates with read-only tools, diagnoses the root cause via Claude (with tool use), and either auto-remediates or escalates to a human via Slack — every action gated by deterministic guardrails and recorded to an audit log.

Built for Columbia AMLC Spring 2026.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐     ┌──────────┐
│ K8s Cluster │ ──> │ Event Watcher│ ──> │ AI Agent    │ ──> │ MCP Tools    │ ──> │  Action  │
│ (real GKE)  │     │ (poller, 5m) │     │ (Claude)    │     │ (9 tools)    │     │ remediate│
└─────────────┘     └──────────────┘     └──────┬──────┘     └──────────────┘     │ or alert │
                                                │                                  └────┬─────┘
                                                v                                       │
                                         ┌─────────────┐                                v
                                         │  Guardrails │                          ┌──────────┐
                                         │ deterministic                          │  Slack   │
                                         └─────────────┘                          │  + Audit │
                                                                                  └──────────┘
```

The same agent code runs in two environments via dependency injection:
- **Local demo:** `MockCluster` (in-memory pods, failure injection) — no API key required, runs offline with `MockAnthropicClient`.
- **Production GKE:** `KubernetesCluster` (real K8s API), real Claude API, real Slack alerts and resolutions.

## Key Components

| Module | Purpose |
|---|---|
| `src/agent.py` | Core agentic loop with multi-turn tool use, model routing, guardrail gating |
| `src/mcp_server.py` | Tool definitions (9 tools) and dispatcher — Claude tool-use schema |
| `src/guardrails.py` | Deterministic safety checks (rate limits, blast radius, memory cap, cooldown) |
| `src/audit.py` | SQLite audit trail — every decision recorded with diagnosis and tokens |
| `src/mock_cluster.py` | In-memory cluster for offline demo |
| `src/k8s_cluster.py` | Real K8s adapter (drop-in replacement for `MockCluster`) |
| `src/openclaw_integration.py` | Local stdout integration (used by `demo/run_demo.py`) |
| `src/slack_integration.py` | Production Slack integration — both alerts and resolutions |
| `src/mock_anthropic.py` | Offline deterministic Claude stand-in for local demo |
| `poller.py` | CronJob entrypoint for production deployment |
| `skills/kubernetes/SKILL.md` | Runbook loaded into the system prompt at startup |

## Quick Start (Local Demo)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the offline demo (no API key needed — uses MockAnthropicClient)
python demo/run_demo.py

# 3. (Optional) Run with the real Claude API
export ANTHROPIC_API_KEY="sk-ant-..."
python demo/run_demo.py
```

## Demo Scenarios

`demo/run_demo.py` walks through four scenarios end-to-end:

1. **Simple Recovery** — `CrashLoopBackOff` with no recent deploy → agent restarts the pod.
2. **Rollback Bad Deploy** — Broken image was just pushed → agent detects the correlation and rolls back.
3. **Guardrail Escalation** — `OOMKilled` pod, agent tries to bump memory >2× original → guardrail blocks → escalate.
4. **Systemic Failure** — >50% pods unhealthy in a namespace → blast-radius guardrail triggers immediate escalation.

## Production Deployment (GKE)

The agent runs as a Kubernetes `CronJob` (`*/5 * * * *`) on GKE. Each tick polls for unhealthy pods and processes up to 5 events.

See [`DEPLOY.md`](DEPLOY.md) for the full step-by-step (Workload Identity, Secret Manager CSI, Artifact Registry, Cloud Build).

**High-level flow:**
1. Push GCP secrets (`anthropic-api-key`, `slack-bot-token`) to Secret Manager
2. Build and push the image: `gcloud builds submit --tag us-central1-docker.pkg.dev/<project>/healer/healer:latest`
3. Apply the manifests in `k8s/` in order (`01` → `06`)
4. Manually trigger or wait for the next 5-min cron tick

**Manifests in `k8s/`:**

| File | Purpose |
|---|---|
| `01-namespace-sa.yaml` | Namespace + ServiceAccount with WI annotation |
| `02-rbac.yaml` | ClusterRoleBinding to scoped ClusterRole (RBAC) |
| `03-secretproviderclass.yaml` | Secret Manager CSI driver config |
| `04-configmap-pvc.yaml` | Config + 1Gi PVC for SQLite audit log |
| `05-cronjob.yaml` | The CronJob itself (5-min schedule, Forbid concurrency) |
| `06-secret-sync-rbac.yaml` | Namespace Role for CSI to create the synced K8s Secret |
| `memory-hog.yaml` | Demo workload that reliably triggers `OOMKilled` |

## MCP Tools

The agent has 9 tools split into read (unrestricted) and write (guardrail-gated):

**Read — investigation:**
- `get_cluster_status` — overview of pods + deployments
- `get_pod_status` — state, restarts, resources, age
- `get_pod_logs` — recent log lines
- `get_events` — namespace events
- `get_deployment_info` — replica count, image, revision history

**Write — remediation (gated):**
- `restart_pod` — delete pod (controller recreates)
- `scale_deployment` — adjust replica count
- `rollback_deployment` — revert to previous ReplicaSet revision
- `update_resource_limits` — patch deployment template (memory/CPU)

**Escalation (always allowed):**
- `alert_human` — post to Slack with severity, diagnosis, and recommended action

## Guardrails

Deterministic checks run **outside** the LLM, in plain Python. The model proposes; deterministic code disposes. When a guardrail blocks, the block is returned to the agent as a `tool_result` so it can adapt within-turn — typically by escalating instead.

| Rule | Default | Description |
|---|---|---|
| `max_restarts_per_hour` | 3 | Per-pod restart limit |
| `max_replicas` | 10 | Upper bound on `scale_deployment` |
| `rollback_window_minutes` | 60 | Only rollback recent deploys |
| `max_memory_multiplier` | 2.0 | Cap memory increase at 2× **original** (prevents 256→512→1024 climb) |
| `cooldown_seconds` | 60 | Wait between actions on the same pod |
| `blast_radius_threshold` | 0.5 | Refuse remediation if >50% pods unhealthy in a namespace |
| `max_actions_per_hour` | 10 | Global rate limit across all pods |

## Configuration

`config.yaml` controls models, guardrail thresholds, and the Slack channel:

```yaml
agent:
  model: claude-haiku-4-5-20251001    # Triage model (cheap, fast)
  incident_model: claude-sonnet-4-6   # Incident model (used for remediation/escalation)
  max_tokens: 2048

guardrails:
  max_restarts_per_hour: 3
  ...

openclaw:
  channel: "#k8s-alerts"
```

**Two-tier model routing:** Haiku 4.5 handles the cheap initial triage tools (status, logs, events). Once the agent decides to remediate or escalate, it switches to Sonnet 4.6 for the more consequential reasoning. This keeps cost low without sacrificing decision quality on the actions that matter.

## Slack Integration

The agent posts **two** message types to `#k8s-alerts`:

1. **Alert** — when escalation is required (`alert_human` is called). Block-kit formatted with severity, what failed, why auto-fix didn't apply, recommended action.
2. **Resolution** — when auto-remediation succeeds. Confirms which pod, which action was taken, and the agent's diagnosis.

Both fall back gracefully to stdout if `SLACK_BOT_TOKEN` is unset (so local dev still works).

## Audit Log

Every decision lands in SQLite (`/data/audit.db` in the cluster, `:memory:` in the demo):

```
event_id | pod_name | namespace | event_type | diagnosis | action_taken |
action_params | guardrail_check | outcome | llm_reasoning | tokens_used | models_used
```

`models_used` records which Claude models were called for that event (for cost tracking and observability).

## Project Structure

```
.
├── README.md
├── DEPLOY.md
├── Dockerfile
├── config.yaml
├── poller.py
├── requirements.txt
├── demo/
│   ├── run_demo.py
│   └── scenarios.py
├── k8s/
│   ├── 01-namespace-sa.yaml
│   ├── 02-rbac.yaml
│   ├── 03-secretproviderclass.yaml
│   ├── 04-configmap-pvc.yaml
│   ├── 05-cronjob.yaml
│   ├── 06-secret-sync-rbac.yaml
│   └── memory-hog.yaml
├── skills/
│   └── kubernetes/
│       └── SKILL.md
└── src/
    ├── agent.py
    ├── audit.py
    ├── config.py
    ├── guardrails.py
    ├── k8s_cluster.py
    ├── mcp_server.py
    ├── mock_anthropic.py
    ├── mock_cluster.py
    ├── openclaw_integration.py
    └── slack_integration.py
```

## Team

Spring 2026 AMLC project, four students:
- **S1** — GCP infrastructure: project, GKE cluster, IAM, RBAC, Secret Manager
- **S2** — Agent code, K8s cluster adapter, Slack integration, Dockerfile, CronJob deploy
- **S3** — SKILL runbook, model routing logic, escalation templates
- **S4** — Demo workloads, integration testing, end-to-end harness
