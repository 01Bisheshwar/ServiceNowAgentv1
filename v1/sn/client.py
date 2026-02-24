import os
import time
import httpx
from typing import Optional, Dict, Any

class ServiceNowClient:
    def __init__(self, instance: str, access_token: Optional[str] = None, token_type: str = "Bearer"):
        self.instance = instance.rstrip("/")
        self.access_token = access_token
        self.token_type = token_type
        self._client = httpx.AsyncClient(timeout=45)

        # fallback basic auth (optional)
        self.username = os.getenv("SN_USERNAME")
        self.password = os.getenv("SN_PASSWORD")

    @classmethod
    def from_session(cls, session: dict) -> "ServiceNowClient":
        instance = os.environ["SN_INSTANCE"]
        token = session.get("sn_access_token")
        token_type = session.get("sn_token_type", "Bearer")
        return cls(instance=instance, access_token=token, token_type=token_type)

    async def close(self):
        await self._client.aclose()

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if self.access_token:
            h["Authorization"] = f"{self.token_type} {self.access_token}"
        return h

    async def request(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> Dict[str, Any]:
        url = f"{self.instance}{path}"
        if self.access_token:
            r = await self._client.request(method, url, params=params, json=json, headers=self._headers())
        else:
            # basic fallback
            if not (self.username and self.password):
                raise RuntimeError("No OAuth token in session and no basic auth configured.")
            r = await self._client.request(
                method, url, params=params, json=json,
                auth=(self.username, self.password),
                headers={"Accept": "application/json"},
            )

        r.raise_for_status()
        return r.json()

    # ---------- Table API ----------
    async def table_get(self, table: str, sys_id: str, params: Optional[dict] = None) -> dict:
        return await self.request("GET", f"/api/now/table/{table}/{sys_id}", params=params)

    async def table_query(self, table: str, query: str, params: Optional[dict] = None) -> dict:
        params = params or {}
        params.setdefault("sysparm_query", query)
        return await self.request("GET", f"/api/now/table/{table}", params=params)

    async def table_create(self, table: str, payload: dict) -> dict:
        return await self.request("POST", f"/api/now/table/{table}", json=payload)

    async def table_update(self, table: str, sys_id: str, payload: dict) -> dict:
        return await self.request("PATCH", f"/api/now/table/{table}/{sys_id}", json=payload)

    async def table_delete(self, table: str, sys_id: str) -> dict:
        # SN returns empty body sometimes; but many instances return JSON
        return await self.request("DELETE", f"/api/now/table/{table}/{sys_id}")

    # ---------- UI helpers ----------
    async def ui_me(self) -> dict:
        return await self.request("GET", "/api/now/ui/me")

        # ---------- Custom Scripted REST APIs ----------
    async def rest_call(self, method: str, path: str, json: dict | None = None, params: dict | None = None) -> Dict[str, Any]:
        # wrapper, same as request but keeps naming consistent with your other file
        return await self.request(method, path, params=params, json=json)

    async def abnormal_update(self, table: str, sys_id: str, fields: dict) -> dict:
        payload = {"table": table, "sys_id": sys_id, "fields": fields}
        return await self.rest_call(
            "POST",
            "/api/pwcm2/agentservicenow/updateabnormalfields",
            json=payload,
        )

    async def complete_update_set(self, sys_id: str, force: bool = False) -> dict:
        if not sys_id or not isinstance(sys_id, str) or len(sys_id) != 32:
            raise ValueError("sys_id must be a 32-char string")
        payload = {"sys_id": sys_id, "force": bool(force)}
        return await self.rest_call(
            "POST",
            "/api/pwcm2/agentservicenow/completeupdateset",
            json=payload,
        )

    async def change_update_set(self, sys_id: str) -> dict:
        if not sys_id or not isinstance(sys_id, str) or len(sys_id) != 32:
            raise ValueError("sys_id must be a 32-char string")
        payload = {"sys_id": sys_id}
        return await self.rest_call(
            "POST",
            "/api/pwcm2/agentservicenow/changeUpdateSet",
            json=payload,
        )

    
