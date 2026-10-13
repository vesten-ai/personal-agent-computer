"""Minimal JSON-over-HTTPS client (stdlib only). The adapters speak each API's wire format
directly instead of through SDKs, so the gateway controls exactly what is sent and replayed."""

import json
import time
import urllib.error
import urllib.request


class APIError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:2000]}")
        self.status = status
        self.body = body


def post_json(url: str, headers: dict, body: dict, timeout: float = 600, retries: int = 3) -> dict:
    data = json.dumps(body).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"content-type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504, 529) and attempt < retries:
                time.sleep(float(e.headers.get("retry-after") or 2 ** (attempt + 1)))
                continue
            raise APIError(e.code, text) from None
        except urllib.error.URLError:
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise
