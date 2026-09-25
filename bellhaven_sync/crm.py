"""Thin client for the CRM sandbox API."""
import json
import urllib.parse

from . import config
from .http import request


class CRM:
    def __init__(self, token=None, api_base=None):
        self.token = token or config.API_TOKEN
        self.api_base = api_base or config.API_BASE
        if not self.token:
            raise RuntimeError("BH_API_TOKEN is not set (env var or .env file)")

    def _call(self, method, path, params=None, body=None):
        url = self.api_base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        _, text = request(method, url, {"Authorization": f"Bearer {self.token}"}, body)
        return json.loads(text) if text else None

    def _all(self, path, **params):
        out, page = [], 1
        while True:
            r = self._call("GET", path, {**params, "page": page, "page_size": 100})
            out += r["data"]
            if not r["data"] or len(out) >= r["total"]:
                return out
            page += 1

    def accounts(self, **filters):
        return self._all("/accounts", **filters)

    def account(self, account_id):
        return self._call("GET", f"/accounts/{account_id}")

    def create_account(self, fields):
        return self._call("POST", "/accounts", body=fields)

    def update_account(self, account_id, fields):
        return self._call("PATCH", f"/accounts/{account_id}", body=fields)

    def contacts(self, **filters):
        return self._all("/contacts", **filters)

    def contact(self, contact_id):
        return self._call("GET", f"/contacts/{contact_id}")

    def update_contact(self, contact_id, fields):
        return self._call("PATCH", f"/contacts/{contact_id}", body=fields)
