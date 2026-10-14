"""Personal Agent Computer gateway: the agent loop, model adapters and egress policy live here,
outside the agent's VM."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pac-gateway")
except PackageNotFoundError:  # running from a checkout
    __version__ = "0.1.0"
