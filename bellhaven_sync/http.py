"""Tiny stdlib HTTP helper with retries (keeps the project dependency-free)."""
import json
import time
import urllib.error
import urllib.request


class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def request(method, url, headers=None, body=None, retries=3, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(headers or {})
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            # Retry only transient server errors, and only for idempotent reads.
            if e.code >= 500 and method == "GET" and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise HttpError(e.code, text) from None
        except (urllib.error.URLError, TimeoutError):
            if method == "GET" and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise
