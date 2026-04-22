# K8s Self-Healing Agent

An autonomous Kubernetes self-healing agent that monitors cluster events, diagnoses failures using the Claude API, attempts auto-remediation via MCP tools, and escalates to humans when it can't fix things itself. Every decision is audit-logged.

## Architecture

```
K8s Cluster (Mock)
       │
       ▼
  Event Watcher ──► AI Agent (Claude API) ──► MCP Tools ──► Auto-fix
                         │                                     OR
                         │                               Escalate (OpenClaw)
                         ▼
                    Audit Log (SQLite)
```

**Key components:**

| Module | Purpose |
|--------|---------|
| `mock_cluster.py` | In-memory K8s cluster with pods, deployments, events, and failure injection |
| `mcp_server.py` | Tool definitions and handler — bridges Claude tool calls to the cluster |
| `agent.py` | Core agentic loop: investigate → diagnose → remediate or escalate |
| `guardrails.py` | Safety checks (rate limits, blast radius, memory caps) before every action |
| `openclaw_integration.py` | Alert formatting and human-in-the-loop escalation |
| `audit.py` | SQLite audit trail of every agent decision |

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your Anthropic API key
export ANTHROPIC_API_KEY="sk-ant-..."

# 3. Run the interactive demo
python demo/run_demo.py
```

## Demo Scenarios

The demo walks through four scenarios:

1. **Simple Recovery** — A pod enters CrashLoopBackOff with no recent deploy. Agent restarts it.
2. **Rollback Bad Deploy** — A broken image is deployed. Agent detects the correlation and rolls back.
3. **Guardrail Escalation** — OOMKilled pod hits the 2× memory guardrail. Agent escalates to a human.
4. **Systemic Failure** — 60% of production pods fail. Blast-radius guardrail triggers immediate escalation.

## Running Tests

```bash
# All offline tests (no API key required)
python tests/test_mock_cluster.py
python tests/test_guardrails.py
python tests/test_agent.py
```

## Configuration

Edit `config.yaml` to tune guardrail thresholds, model selection, and OpenClaw settings.

## Guardrail Rules

| Rule | Default | Description |
|------|---------|-------------|
| max_restarts_per_hour | 3 | Per-pod restart limit |
| max_replicas | 10 | Upper bound on scaling |
| rollback_window_minutes | 60 | Only rollback recent deploys |
| max_memory_multiplier | 2.0 | Cap memory increase at 2× original |
| cooldown_seconds | 60 | Wait between actions on same pod |
| blast_radius_threshold | 0.5 | Escalate if >50% pods unhealthy |
| max_actions_per_hour | 10 | Global automated action limit |

## Project Structure

```
k8s-self-healing-agent/
├── config.yaml
├── requirements.txt
├── src/
│   ├── agent.py
│   ├── audit.py
│   ├── config.py
│   ├── guardrails.py
│   ├── mock_cluster.py
│   ├── mcp_server.py
│   └── openclaw_integration.py
├── demo/
│   ├── run_demo.py
│   └── scenarios.py
└── tests/
    ├── test_agent.py
    ├── test_guardrails.py
    └── test_mock_cluster.py
```
