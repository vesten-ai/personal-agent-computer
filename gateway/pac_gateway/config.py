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
class Config:
    data_dir: str = "/var/lib/pac"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8000
    admin_token_env: str = "PAC_ADMIN_TOKEN"
    egress_ca_file: str = "/var/lib/pac-egress/mitm/mitmproxy-ca-cert.pem"
    incus: dict = field(default_factory=dict)
    models: dict[str, ModelConfig] = field(default_factory=dict)
    default_model: str = ""
    max_steps: int = 60

    @property
    def admin_token(self) -> str:
        return os.environ.get(self.admin_token_env, "")


def load(path: str) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    models = {name: ModelConfig(name=name, **m) for name, m in raw.pop("models", {}).items()}
    cfg = Config(**{k: v for k, v in raw.items() if k in Config.__dataclass_fields__}, models=models)
    if not cfg.default_model and models:
        cfg.default_model = next(iter(models))
    return cfg
