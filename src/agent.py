"""AI Agent — core decision loop using the Claude API with tool use.

The agent:
  1. Receives an event (unhealthy pod / warning)
  2. Investigates using MCP tools (logs, events, deployment info)
  3. Diagnoses root cause via Claude
  4. Proposes a remediation
  5. Checks guardrails
  6. Executes or escalates
  7. Logs everything
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

try:
    import anthropic # type: ignore
except ImportError:
    anthropic = None # type: ignore

from .audit import AuditLogger
from .config import Config
from .guardrails import Guardrails
from .mcp_server import MCPToolHandler, TOOL_DEFINITIONS
from .mock_anthropic import MockAnthropicClient
from .mock_cluster import MockCluster

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a senior Site Reliability Engineer (SRE) operating an autonomous \
Kubernetes self-healing agent. Your job is to diagnose failing pods, \
determine the root cause, and take the least-disruptive corrective action.

## Investigation Protocol
1. When notified of an unhealthy pod, ALWAYS investigate first:
   - get_pod_status to see current state
   - get_pod_logs to read recent logs
   - get_events to check related cluster events
   - get_deployment_info to check for recent deployments
2. Only after investigation, determine the root cause.

## Remediation Playbook
- **CrashLoopBackOff + recent deployment** -> rollback_deployment
- **CrashLoopBackOff + NO recent deployment** -> restart_pod (if restart limit not hit)
- **OOMKilled** -> update_resource_limits (increase memory by ~50%, e.g. 256Mi -> 384Mi)
- **ImagePullBackOff + recent deployment** -> rollback_deployment (the new image tag is bad — roll back to the previous known-good revision)
- **ImagePullBackOff + NO recent deployment** -> alert_human (registry / auth issue, agent cannot fix)
- **Pending pods** -> alert_human (cluster capacity / scheduling issue — none of the agent's tools can create capacity)
- **>50% pods unhealthy in namespace** -> alert_human immediately (systemic failure)
- **High error rate across many pods** -> consider scale_deployment

## Rules
- Prefer the LEAST DISRUPTIVE fix.
- If unsure about the root cause or the right action, ESCALATE via alert_human.
- Never guess — investigate thoroughly first.
- Always use the tools available to gather data before deciding.

When you have completed investigation and decided on an action, state your \
diagnosis and invoke the appropriate remediation tool (restart_pod, \
rollback_deployment, update_resource_limits, scale_deployment) or \
escalation tool (alert_human).
"""

# Remediation tools that modify cluster state (vs read-only investigation tools)
_REMEDIATION_TOOLS = {
    "restart_pod",
    "scale_deployment",
    "rollback_deployment",
    "update_resource_limits",
}


def _load_skill(path: str | None) -> str:
    """Load the Kubernetes runbook skill file if present.

    Default search order: $SKILL_PATH -> ./skills/kubernetes/SKILL.md -> /app/skills/kubernetes/SKILL.md.
    Returns empty string if nothing found (agent falls back to the embedded prompt).
    """
    candidates = []
    if path:
        candidates.append(Path(path))
    here = Path(__file__).resolve().parent.parent
    candidates.extend([
        here / "skills" / "kubernetes" / "SKILL.md",
        Path("/app/skills/kubernetes/SKILL.md"),
    ])
    for p in candidates:
        try:
            if p.is_file():
                return p.read_text()
        except OSError:
            continue
    return ""


class Agent:
    """Claude-powered K8s self-healing agent."""

    def __init__(
        self,
        config: Config,
        cluster: MockCluster,
        audit: AuditLogger,
        guardrails: Guardrails,
        openclaw: Any,
    ) -> None:
        self.config = config
        self.cluster = cluster
        self.audit = audit
        self.guardrails = guardrails
        self.openclaw = openclaw
        self.client = self._build_client()
        self.tool_handler = MCPToolHandler(
            cluster,
            alert_callback=lambda inp: openclaw.format_alert_for_tool(inp),
        )
        # Compose the effective system prompt: embedded framing + (optional) SKILL.md runbook
        skill = _load_skill(os.environ.get("SKILL_PATH"))
        self.system_prompt = (
            SYSTEM_PROMPT + "\n\n---\n\n" + skill if skill else SYSTEM_PROMPT
        )
        if skill:
            print(f" Loaded Kubernetes skill ({len(skill)} chars)")

    @staticmethod
    def _build_client() -> Any:
        """Return a real anthropic client when ANTHROPIC_API_KEY is set,
        else a local mock so the demo runs offline."""
        use_mock = os.environ.get("DEMO_MOCK") == "1" or not os.environ.get(
            "ANTHROPIC_API_KEY"
        )
        if use_mock or anthropic is None:
            print(" Using offline mock Claude client "
                  "(set ANTHROPIC_API_KEY to call the real API)")
            return MockAnthropicClient()
        return anthropic.Anthropic()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process a cluster event end-to-end. Returns the audit entry."""
        event_id = str(uuid.uuid4())[:8]
        pod_name = event.get("pod_name", event.get("involved_object", ""))
        namespace = event.get("namespace", "default")

        print(f"\n Agent received event: {event.get('reason', 'unknown')} on {pod_name}")

        # Build initial user message describing the alert
        user_msg = self._build_alert_message(event)

        # Run the agentic tool-use loop
        diagnosis, action_taken, action_params, reasoning, tokens, models_used = self._run_agent_loop(
            user_msg, pod_name, namespace, event_id
        )

        # Build audit entry
        audit_entry: dict[str, Any] = {
            "event_id": event_id,
            "pod_name": pod_name,
            "namespace": namespace,
            "event_type": event.get("reason", ""),
            "diagnosis": diagnosis,
            "action_taken": action_taken,
            "action_params": action_params,
            "guardrail_check": "passed",
            "outcome": "success",
            "llm_reasoning": reasoning,
            "tokens_used": tokens,
            "models_used": models_used,
        }
        self.audit.log(audit_entry)

        # Notify channel of successful auto-remediation (mirror of alert_human)
        if action_taken in _REMEDIATION_TOOLS:
            try:
                self.openclaw.post_resolution(
                    pod_name=pod_name,
                    namespace=namespace,
                    action_taken=action_taken,
                    action_params=action_params,
                    diagnosis=diagnosis,
                )
            except AttributeError:
                # Older integration without post_resolution — ignore silently
                pass
            except Exception as e:
                print(f" [warn] post_resolution failed: {e}")

        return audit_entry

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_alert_message(self, event: dict[str, Any]) -> str:
        pod_name = event.get("pod_name", event.get("involved_object", "unknown"))
        return (
            f"ALERT: Pod '{pod_name}' in namespace '{event.get('namespace', 'default')}' "
            f"has entered state '{event.get('status', event.get('reason', 'Unknown'))}'.\n"
            f"Event message: {event.get('message', 'N/A')}\n\n"
            f"Please investigate this pod, determine the root cause, and take "
            f"appropriate action."
        )

    def _run_agent_loop(
        self,
        user_message: str,
        pod_name: str,
        namespace: str,
        event_id: str,
    ) -> tuple[str, str, dict[str, Any], str, int, str]:
        """
        Run the Claude tool-use loop until the agent reaches a final answer
        or has executed a remediation / escalation.

        Returns (diagnosis, action_taken, action_params, reasoning, total_tokens, models_used).
        """
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
        reasoning_parts: list[str] = []
        total_tokens = 0
        action_taken = "none"
        action_params: dict[str, Any] = {}
        diagnosis = ""
        max_turns = 10 # safety limit

        # Model routing: triage_model (Haiku) while investigating.
        # Switch to incident_model (Sonnet) once remediation / escalation is
        # being proposed — where we want the best judgment + best-written alert.
        incident_phase = False
        models_used: list[str] = []

        for turn in range(max_turns):
            model_for_turn = (
                self.config.agent.incident_model
                if incident_phase
                else self.config.agent.model
            )
            models_used.append(model_for_turn)
            response = self.client.messages.create(
                model=model_for_turn,
                max_tokens=self.config.agent.max_tokens,
                system=self.system_prompt,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )

            total_tokens += (response.usage.input_tokens + response.usage.output_tokens)

            # Collect text blocks as reasoning
            for block in response.content:
                if block.type == "text" and block.text:
                    reasoning_parts.append(block.text)
                    print(f" Agent: {block.text[:200]}{'…' if len(block.text) > 200 else ''}")

            # If model stopped without tool use, we're done
            if response.stop_reason == "end_turn":
                diagnosis = "\n".join(reasoning_parts)
                break

            # Process tool calls
            if response.stop_reason == "tool_use":
                tool_results: list[dict[str, Any]] = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue

                    tool_name = block.name
                    tool_input = block.input
                    print(f" Tool call: {tool_name}({json.dumps(tool_input, default=str)[:120]})")

                    # --- Guardrail gate for remediation tools ---
                    if tool_name in _REMEDIATION_TOOLS:
                        allowed, reason = self.guardrails.check(tool_name, tool_input)
                        if not allowed:
                            print(f" Guardrail BLOCKED: {reason}")
                            # Instead of executing, inform Claude it was blocked
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps({
                                    "error": "GUARDRAIL_BLOCKED",
                                    "reason": reason,
                                    "instruction": (
                                        "This action was blocked by a safety guardrail. "
                                        "Please escalate to a human operator via alert_human."
                                    ),
                                }),
                            })
                            action_taken = f"blocked:{tool_name}"
                            action_params = tool_input
                            # A blocked action means we're in incident territory —
                            # the next turn should reason about escalation with
                            # the stronger model.
                            incident_phase = True
                            # Log the guardrail block
                            self.audit.log({
                                "event_id": event_id,
                                "pod_name": pod_name,
                                "namespace": namespace,
                                "action_taken": tool_name,
                                "action_params": tool_input,
                                "guardrail_check": reason,
                                "outcome": "guardrail_blocked",
                            })
                            continue

                    # Execute the tool
                    result_str = self.tool_handler.handle(tool_name, tool_input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    })

                    # Record remediation actions
                    if tool_name in _REMEDIATION_TOOLS:
                        self.guardrails.record_action(tool_name, tool_input)
                        action_taken = tool_name
                        action_params = tool_input
                        incident_phase = True
                        print(f" [ok] Action executed: {tool_name}")

                    if tool_name == "alert_human":
                        action_taken = "escalated"
                        action_params = tool_input
                        incident_phase = True
                        print(f" Escalated to human: {tool_input.get('summary', '')}")

                # Feed tool results back to Claude
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

        diagnosis = diagnosis or "\n".join(reasoning_parts) or "Agent completed without explicit diagnosis."
        # Deduplicate models_used while preserving order
        seen: set[str] = set()
        unique_models = [m for m in models_used if not (m in seen or seen.add(m))]
        models_label = ",".join(unique_models) if unique_models else "unknown"
        return diagnosis, action_taken, action_params, "\n".join(reasoning_parts), total_tokens, models_label
