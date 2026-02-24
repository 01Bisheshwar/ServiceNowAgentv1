#servicenow.py
import os
import time
import httpx


class ServiceNowClient:
    def __init__(self) -> None:
        self.instance = os.environ["SN_INSTANCE"].rstrip("/")
        self.client_id = os.getenv("SN_CLIENT_ID")
        self.client_secret = os.getenv("SN_CLIENT_SECRET")
        self.username = os.getenv("SN_USERNAME")
        self.password = os.getenv("SN_PASSWORD")

        self._token: str | None = None
        self._token_exp: float = 0.0

    def _using_oauth(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _auth_header(self) -> str:
        if self._using_oauth():
            token = await self._get_access_token()
            return f"Bearer {token}"

        if self.username and self.password:
            return "BASIC"

        raise RuntimeError(
            "ServiceNow auth not configured. "
            "Set SN_CLIENT_ID/SN_CLIENT_SECRET (preferred) or SN_USERNAME/SN_PASSWORD."
        )

    async def _get_access_token(self) -> str:
        now = time.time()
        if self._token and now < (self._token_exp - 60):
            return self._token

        token_url = f"{self.instance}/oauth_token.do"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(token_url, data=data)
            r.raise_for_status()
            j = r.json()

        self._token = j["access_token"]
        expires_in = int(j.get("expires_in", 3600))
        self._token_exp = now + expires_in
        return self._token
    
    async def table_get(self, table: str, sys_id: str, params: dict | None = None) -> dict:
        return await self._request("GET", f"/api/now/table/{table}/{sys_id}", params=params)

    async def table_query(self, table: str, query: str, params: dict | None = None) -> dict:
        params = params or {}
        params.setdefault("sysparm_query", query)
        params.setdefault("sysparm_limit", "25")
        return await self._request("GET", f"/api/now/table/{table}", params=params)

    async def table_delete(self, table: str, sys_id: str) -> dict:
        return await self._request("DELETE", f"/api/now/table/{table}/{sys_id}")

    async def table_create(self, table: str, payload: dict) -> dict:
        return await self._request("POST", f"/api/now/table/{table}", json=payload)

    async def table_update(self, table: str, sys_id: str, payload: dict) -> dict:
        return await self._request("PATCH", f"/api/now/table/{table}/{sys_id}", json=payload)

    async def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict:
        url = f"{self.instance}{path}"
        auth_header = await self._auth_header()

        async with httpx.AsyncClient(timeout=30) as client:
            if auth_header == "BASIC":
                r = await client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    auth=(self.username, self.password),
                    headers={"Accept": "application/json"},
                )
            else:
                r = await client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers={"Authorization": auth_header, "Accept": "application/json"},
                )

        r.raise_for_status()
        return r.json()

    async def rest_call(self, method: str, path: str, json: dict | None = None) -> dict:
        url = f"{self.instance}{path}"
        auth_header = await self._auth_header()

        async with httpx.AsyncClient(timeout=30) as client:
            if auth_header == "BASIC":
                r = await client.request(
                    method,
                    url,
                    json=json,
                    auth=(self.username, self.password),
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                )
            else:
                r = await client.request(
                    method,
                    url,
                    json=json,
                    headers={
                        "Authorization": auth_header,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                )

        r.raise_for_status()
        return r.json()
    
    async def abnormal_update(self, table: str, sys_id: str, fields: dict) -> dict:
        """
        Call Scripted REST API to update 'abnormal' fields using GlideRecord
        (equivalent to a background script), for a single record.
        Endpoint must be configured in ServiceNow.

        Expected JSON payload:
        {
            "table": "<table>",
            "sys_id": "<sys_id>",
            "fields": { "field_name": "value", ... }
        }
        """
        payload = {
            "table": table,
            "sys_id": sys_id,
            "fields": fields,
        }
        # Adjust the path if you name the API/resource differently:
        # /api/<namespace>/<api_id>/<resource_name>
        return await self.rest_call(
            "POST",
            "/api/pwcm2/agentservicenow/updateabnormalfields",
            json=payload,
        )
    
    async def complete_update_set(self, sys_id: str, force: bool = False) -> dict:
        """
        Calls the scripted REST resource to mark an update set complete (or attempt to).
        Payload:
        { "sys_id": "<sys_id>", "force": true|false }
        """
        if not sys_id or not isinstance(sys_id, str) or len(sys_id) != 32:
            raise ValueError("sys_id must be a 32-char string")
        payload = {"sys_id": sys_id, "force": bool(force)}
        return await self.rest_call("POST", "/api/pwcm2/agentservicenow/completeupdateset", json=payload)

    async def change_update_set(self, sys_id: str) -> dict:
        return await self.rest_call("POST", "/api/pwcm2/agentservicenow/changeUpdateSet", json={"sys_id": sys_id})

