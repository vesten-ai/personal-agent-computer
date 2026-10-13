"""mitmproxy addon: the Personal Agent Computer egress policy.

Agent VMs hold placeholders, never real credentials. Every request leaving a VM passes
through this proxy, which:

  1. refuses destinations on private, loopback, link-local and other internal networks,
     so a VM can't reach the host, the gateway's own services or a cloud metadata endpoint;
  2. applies the host allow/deny lists;
  3. swaps each placeholder for its real secret, but only on requests to the hosts that
     secret is bound to (Host header and TLS SNI must both match). A placeholder headed
     anywhere else is blocked instead of forwarded;
  4. scrubs real secrets out of responses, in case an upstream echoes them back;
  5. writes one JSON line per request to the audit log.

Run:  mitmdump --mode transparent@8081 --mode regular@8080 --mode dns@53 \
        -s credential_swap.py --set pac_policy=/etc/pac/egress-policy.json

Policy file (JSON):
  {
    "default": "allow",                       # or "deny": then only allow_hosts get out
    "allow_hosts": ["api.openai.com", ".github.com"],
    "deny_hosts": [".internal"],
    "deny_networks": ["203.0.113.7/32"],      # added to the built-in internal ranges
    "audit_log": "/var/log/pac/egress.jsonl",
    "secrets": [
      {"name": "openai", "placeholder": "sk-pac-openai-placeholder", "env": "PAC_SECRET_OPENAI",
       "hosts": ["api.openai.com"], "clients": ["10.96.10.101"]}   # clients: optional
    ]
  }
Host patterns: "example.com" matches only that host; ".example.com" matches it and its subdomains.
Real secret values come from the gateway's environment (the "env" variable), never from the file.
"""


import ipaddress
import json
import os
import socket
import time
from dataclasses import dataclass, field

from mitmproxy import ctx, http
from mitmproxy.proxy import server_hooks

INTERNAL_NETWORKS = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4",
    "240.0.0.0/4", "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8",
]
# Bodies larger than this are forwarded untouched (and a placeholder inside them stays a placeholder).
MAX_SCAN_BYTES = 4 * 1024 * 1024
TEXT_TYPES = ("json", "x-www-form-urlencoded", "text/", "xml", "graphql")


def _is_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def host_matches(host: str, patterns: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for p in patterns:
        p = p.lower().rstrip(".")
        if p.startswith("."):
            if host == p[1:] or host.endswith(p):
                return True
        elif host == p:
            return True
    return False


@dataclass
class Secret:
    name: str
    placeholder: str
    value: str
    hosts: list[str]
    clients: list[str] = field(default_factory=list)

    def usable_by(self, client_ip: str) -> bool:
        return not self.clients or client_ip in self.clients


@dataclass
class Policy:
    default: str = "allow"
    allow_hosts: list[str] = field(default_factory=list)
    deny_hosts: list[str] = field(default_factory=list)
    deny_networks: list[ipaddress._BaseNetwork] = field(default_factory=list)
    secrets: list[Secret] = field(default_factory=list)
    audit_log: str | None = None

    @classmethod
    def load(cls, path: str) -> "Policy":
        with open(path) as f:
            raw = json.load(f)
        secrets = []
        for s in raw.get("secrets", []):
            value = os.environ.get(s["env"], "")
            if not value:
                ctx.log.warn(f"pac: secret {s['name']}: ${s['env']} is empty, its placeholder will be blocked")
            if len(s["placeholder"]) < 12:
                raise ValueError(f"pac: secret {s['name']}: placeholder too short to be unambiguous")
            secrets.append(Secret(s["name"], s["placeholder"], value, s["hosts"], s.get("clients", [])))
        nets = [ipaddress.ip_network(n) for n in INTERNAL_NETWORKS + raw.get("deny_networks", [])]
        return cls(raw.get("default", "allow"), raw.get("allow_hosts", []), raw.get("deny_hosts", []),
                   nets, secrets, raw.get("audit_log"))

    def ip_denied(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            return True
        if getattr(addr, "ipv4_mapped", None):
            addr = addr.ipv4_mapped
        return any(addr in n for n in self.deny_networks)

    def host_denied(self, host: str) -> str | None:
        if host_matches(host, self.deny_hosts):
            return "host on deny list"
        if self.default == "deny" and not host_matches(host, self.allow_hosts):
            return "host not on allow list"
        return None


class CredentialSwap:
    def __init__(self) -> None:
        self.policy = Policy()
        self.mtime = 0.0

    def load(self, loader) -> None:
        loader.add_option("pac_policy", str, "/etc/pac/egress-policy.json", "Path to the PAC egress policy")

    def configure(self, updated) -> None:
        if "pac_policy" in updated:
            self._load()

    def _load(self) -> None:
        path = ctx.options.pac_policy
        self.mtime = os.stat(path).st_mtime
        self.policy = Policy.load(path)
        ctx.log.info(f"pac: policy loaded, {len(self.policy.secrets)} secret(s)")

    def _maybe_reload(self) -> None:
        # The gateway rewrites the policy whenever agents come and go; pick it up without a restart.
        try:
            if os.stat(ctx.options.pac_policy).st_mtime != self.mtime:
                self._load()
        except (OSError, ValueError) as e:
            ctx.log.warn(f"pac: policy reload failed, keeping the previous one: {e}")

    # -- network layer: never connect to internal addresses --------------------------------
    def server_connect(self, data: server_hooks.ServerConnectionHookData) -> None:
        self._maybe_reload()
        if data.server.address is None:
            return
        host, port = data.server.address
        try:
            ips = {ai[4][0] for ai in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)}
        except OSError:
            return  # let mitmproxy report the resolution failure itself
        bad = sorted(ip for ip in ips if self.policy.ip_denied(ip))
        if bad:
            data.server.error = f"pac: destination {host} resolves to internal address {bad[0]}"
            return
        # Pin the address we checked, so a second lookup can't be rebound to an internal one.
        # TLS still verifies against the original name (server.sni).
        if data.server.sni is None and not _is_ip(host):
            data.server.sni = host
        data.server.address = (sorted(ips)[0], port)

    # -- HTTP layer ------------------------------------------------------------------------
    def http_connect(self, flow: http.HTTPFlow) -> None:
        # Explicit-proxy CONNECT: refuse deny-listed hosts before opening the tunnel.
        reason = self.policy.host_denied(flow.request.host)
        if reason:
            self._block(flow, reason)

    def request(self, flow: http.HTTPFlow) -> None:
        self._maybe_reload()
        req = flow.request
        host = (req.host_header or req.pretty_host or "").split(":")[0].lower()
        sni = (flow.client_conn.sni or "").lower()
        client_ip = flow.client_conn.peername[0] if flow.client_conn.peername else ""
        flow.metadata["pac_swapped"] = []

        reason = self.policy.host_denied(host) or (sni and self.policy.host_denied(sni))
        if reason:
            return self._block(flow, reason)

        for s in self.policy.secrets:
            if not self._contains(req, s.placeholder):
                continue
            # Both names must agree: a matching Host header behind someone else's SNI is domain fronting.
            if not host_matches(host, s.hosts) or (sni and not host_matches(sni, s.hosts)):
                return self._block(flow, f"placeholder for {s.name} sent to {host}")
            # Only over verified TLS: in transparent mode a plain-HTTP request goes to whatever
            # address the VM dialled, whatever its Host header says.
            if req.scheme != "https":
                return self._block(flow, f"placeholder for {s.name} sent without TLS")
            if not s.usable_by(client_ip):
                return self._block(flow, f"{client_ip} may not use {s.name}")
            if not s.value:
                return self._block(flow, f"secret {s.name} is not configured on the gateway")
            self._replace(req, s.placeholder, s.value)
            flow.metadata["pac_swapped"].append(s.name)

    def response(self, flow: http.HTTPFlow) -> None:
        resp = flow.response
        if resp is not None and not flow.metadata.get("pac_blocked"):
            client_ip = flow.client_conn.peername[0] if flow.client_conn.peername else ""
            # Several agents can share one real secret. Put back this client's own placeholder, and never
            # hand one agent another agent's placeholder: anything else is redacted outright.
            ordered = sorted((s for s in self.policy.secrets if s.value), key=lambda s: not s.usable_by(client_ip))
            for s in ordered:
                if self._contains(resp, s.value):
                    mask = s.placeholder if s.usable_by(client_ip) else "[redacted by pac]"
                    self._replace(resp, s.value, mask)
                    flow.metadata.setdefault("pac_scrubbed", []).append(s.name)
        self._audit(flow)

    def error(self, flow: http.HTTPFlow) -> None:
        self._audit(flow)

    # -- helpers ---------------------------------------------------------------------------
    @staticmethod
    def _scannable(msg: http.Message) -> bool:
        if not msg.raw_content or len(msg.raw_content) > MAX_SCAN_BYTES:
            return False
        ctype = msg.headers.get("content-type", "").lower()
        return not ctype or any(t in ctype for t in TEXT_TYPES)

    def _contains(self, msg: http.Message, needle: str) -> bool:
        if any(needle in v for v in msg.headers.values()):
            return True
        if isinstance(msg, http.Request) and needle in msg.url:
            return True
        if self._scannable(msg):
            body = msg.get_text(strict=False) or ""
            return needle in body
        return False

    def _replace(self, msg: http.Message, old: str, new: str) -> None:
        for k, v in list(msg.headers.items(multi=True)):
            if old in v:
                msg.headers.set_all(k, [x.replace(old, new) for x in msg.headers.get_all(k)])
        if isinstance(msg, http.Request) and old in msg.url:
            msg.url = msg.url.replace(old, new)
        if self._scannable(msg):
            body = msg.get_text(strict=False) or ""
            if old in body:
                msg.set_text(body.replace(old, new))

    def _block(self, flow: http.HTTPFlow, reason: str) -> None:
        flow.metadata["pac_blocked"] = reason
        flow.response = http.Response.make(
            403, json.dumps({"error": "blocked by Personal Agent Computer egress policy", "reason": reason}),
            {"content-type": "application/json"},
        )
        ctx.log.info(f"pac: blocked {flow.request.method} {flow.request.pretty_host}: {reason}")

    def _audit(self, flow: http.HTTPFlow) -> None:
        if not self.policy.audit_log:
            return
        req = flow.request
        rec = {
            "ts": round(time.time(), 3),
            "client": flow.client_conn.peername[0] if flow.client_conn.peername else None,
            "method": req.method,
            "host": req.pretty_host,
            "path": req.path.split("?")[0],
            "status": flow.response.status_code if flow.response else None,
            "swapped": flow.metadata.get("pac_swapped", []),
            "scrubbed": flow.metadata.get("pac_scrubbed", []),
            "blocked": flow.metadata.get("pac_blocked"),
            "error": str(flow.error) if flow.error else None,
        }
        try:
            os.makedirs(os.path.dirname(self.policy.audit_log), exist_ok=True)
            with open(self.policy.audit_log, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:
            ctx.log.warn(f"pac: audit log write failed: {e}")


addons = [CredentialSwap()]
