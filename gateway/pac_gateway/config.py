"""Gateway configuration (TOML). Secrets are never in the file, only names of environment variables."""

import os
import tomllib
from dataclasses import dataclass, field


@dataclass
class ModelConfig:
    name: str
    api: str                  # "openai" (Responses) or "anthropic" (Messages)
    model: str
    key_env: str
    base_url: str | None = None
    tool_mode: str = "native"  # openai only: "native" computer tool or "function"
    effort: str = "medium"

    @property
    def api_key(self) -> str:
        key = os.environ.get(self.key_env, "")
        if not key:
            raise RuntimeError(f"model {self.name}: ${self.key_env} is not set")
        return key


@dataclass
class SecretConfig:
    """A credential the agent may use but never holds. Its value lives only in the egress proxy's
    environment (`value_env`); the agent's VM sees a per-agent placeholder in `vm_env`."""
    name: str
    value_env: str
    vm_env: str
    hosts: list[str]
    prefix: str = "pac-"


@dataclass
class Config:
    data_dir: str = "/var/lib/pac"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8000
    admin_token_env: str = "PAC_ADMIN_TOKEN"
    egress_ca_file: str = "/var/lib/pac-egress/mitm/mitmproxy-ca-cert.pem"
    egress_policy_file: str = "/etc/pac/egress-policy.json"
    egress_base_policy: str = "/etc/pac/egress-base.json"   # host allow/deny lists the gateway keeps
    secrets: dict[str, SecretConfig] = field(default_factory=dict)
    connectors: dict = field(default_factory=dict)
    incus: dict = field(default_factory=dict)
    models: dict[str, ModelConfig] = field(default_factory=dict)
    default_model: str = ""
    max_steps: int = 60
    # Idle agents sleep: their VM is paused (instant resume, memory kept), later stopped (no memory used;
    # a cold boot takes ~10 s). A new message wakes them. 0 turns a stage off.
    idle_pause_minutes: float = 10
    idle_stop_minutes: float = 120

    @property
    def admin_token(self) -> str:
        return os.environ.get(self.admin_token_env, "")


def load(path: str) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    models = {name: ModelConfig(name=name, **m) for name, m in raw.pop("models", {}).items()}
    secrets = {name: SecretConfig(name=name, **c) for name, c in raw.pop("secrets", {}).items()}
    cfg = Config(**{k: v for k, v in raw.items() if k in Config.__dataclass_fields__}, models=models,
                 secrets=secrets)
    if not cfg.default_model and models:
        cfg.default_model = next(iter(models))
    return cfg
