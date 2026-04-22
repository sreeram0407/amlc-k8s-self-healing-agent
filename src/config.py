"""Configuration loader and typed config dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass
class AgentConfig:
    model: str = "claude-opus-4-7"
    max_tokens: int = 2048


@dataclass
class GuardrailsConfig:
    max_restarts_per_hour: int = 3
    max_replicas: int = 10
    rollback_window_minutes: int = 60
    max_memory_multiplier: float = 2.0
    cooldown_seconds: int = 60
    blast_radius_threshold: float = 0.5
    max_actions_per_hour: int = 10


@dataclass
class OpenClawConfig:
    channel: str = "#oncall-sre"
    webhook_url: str = ""
    paging_enabled: bool = False


@dataclass
class Config:
    agent: AgentConfig = field(default_factory=AgentConfig)
    guardrails: GuardrailsConfig = field(default_factory=GuardrailsConfig)
    openclaw: OpenClawConfig = field(default_factory=OpenClawConfig)


def _apply(dc: Any, data: dict[str, Any]) -> None:
    known = {f.name for f in fields(dc)}
    for k, v in data.items():
        if k in known:
            setattr(dc, k, v)


def _parse_yaml(text: str) -> dict[str, Any]:
    """Minimal YAML subset parser: top-level keys with nested key/value pairs.

    Supports the structure we actually use in config.yaml — no lists, no quoted
    multiline, no anchors. Falls through to PyYAML if available for anything fancy.
    """
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        pass

    result: dict[str, Any] = {}
    current_section: dict[str, Any] | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        line = raw.rstrip()
        if not raw.startswith((" ", "\t")):
            if line.endswith(":"):
                current_section = {}
                result[line[:-1].strip()] = current_section
            else:
                key, _, val = line.partition(":")
                result[key.strip()] = _coerce(_strip_inline_comment(val.strip()))
        else:
            if current_section is None:
                continue
            key, _, val = line.strip().partition(":")
            current_section[key.strip()] = _coerce(_strip_inline_comment(val.strip()))
    return result


def _strip_inline_comment(val: str) -> str:
    """Remove a trailing `# comment`, but not if # is inside quotes."""
    if val.startswith(('"', "'")):
        q = val[0]
        end = val.find(q, 1)
        if end != -1:
            return val[: end + 1]
        return val
    return val.split("#", 1)[0].rstrip()


def _coerce(val: str) -> Any:
    if val.lower() in ("true", "false"):
        return val.lower() == "true"
    if val.lower() in ("null", "none", "~"):
        return None
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        pass
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    return val


def load_config(path: str | Path) -> Config:
    """Load config from a YAML file. Missing file or keys fall back to defaults."""
    cfg = Config()
    p = Path(path)
    if not p.exists():
        return cfg

    data = _parse_yaml(p.read_text())
    if not isinstance(data, dict):
        return cfg

    if isinstance(data.get("agent"), dict):
        _apply(cfg.agent, data["agent"])
    if isinstance(data.get("guardrails"), dict):
        _apply(cfg.guardrails, data["guardrails"])
    if isinstance(data.get("openclaw"), dict):
        _apply(cfg.openclaw, data["openclaw"])

    return cfg
