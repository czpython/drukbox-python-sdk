"""Async HTTP client for Drukbox.

The service is the canonical owner of host provisioning, Tailscale
auth-key minting and device discovery, exe.dev VM lifecycle, and host
teardown. This SDK is a thin wrapper around its HTTP API; everything
that needs to happen *inside* a provisioned VM (SSH, file transfer,
command execution) is the caller's responsibility — the SDK hands back
the host record with ``external_ssh_host`` / ``external_ssh_port`` /
``internal_ssh_host`` / ``known_hosts`` and stops there.

Usage::

    from drukbox_sdk import SandboxAPI

    sandbox = SandboxAPI(
        base_url="https://sandbox.internal.ts.net",
        token="...",
    )
    try:
        host = await sandbox.create_host(
            image="ghcr.io/.../sandbox:abc123",
            idempotency_key="agent-run-42",
        )
        # ... use host.external_ssh_host etc. with asyncssh ...
    finally:
        await sandbox.delete_host(host.id)
        await sandbox.aclose()

For env-backed config use :meth:`SandboxAPI.from_env`.
"""

import asyncio
import os
import uuid
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Self

import httpx

from .exceptions import (
    SandboxAuthError,
    SandboxConflictError,
    SandboxNotFoundError,
    SandboxProvisioningError,
    SandboxResponseError,
    SandboxUnavailableError,
)

# Explicit pool budget. The httpx defaults (100 / 20) are plenty for
# the SDK's traffic shape (a handful of provisioning calls per agent
# run), but keeping the values visible at the import site means a
# future bump in concurrency does not silently exhaust the pool.
_SANDBOX_HTTP_LIMITS = httpx.Limits(
    max_connections=20,
    max_keepalive_connections=5,
)


@dataclass(frozen=True)
class SandboxHost:
    """Snapshot of a provisioned host as returned by the service.

    The shape mirrors the Drukbox ``Host`` schema. ``external_ssh_host``
    is always populated by the VM provider; ``internal_ssh_host`` is
    populated only when the service runs with Tailscale enabled (MagicDNS
    name on the tailnet). The internal path is always reached on port 22
    by Tailscale convention, so there is no ``internal_ssh_port``.
    Callers pick whichever path they can reach and dial it themselves —
    this SDK doesn't speak SSH.
    """

    id: str
    name: str
    status: str
    provider: str
    image: str
    external_ssh_host: str
    external_ssh_port: int
    internal_ssh_host: str | None
    known_hosts: str
    tailscale_device_id: str | None
    last_error: str
    created_at: str
    updated_at: str
    activated_at: str | None
    expires_at: str | None


_SANDBOX_HOST_FIELDS = {field.name for field in fields(SandboxHost)}


def _parse_sandbox_host(data: dict[str, Any]) -> SandboxHost:
    """Build a :class:`SandboxHost` picking only known fields.

    Defensive against the service adding new fields — those flow
    through harmlessly without breaking the SDK on the unsuspecting
    caller's side.
    """

    return SandboxHost(**{key: data[key] for key in _SANDBOX_HOST_FIELDS})


class SandboxAPI:
    """Async client for Drukbox.

    Construct one per process (or one per consuming subsystem) and
    reuse it. The internal ``httpx.AsyncClient`` is loop-aware: if the
    client gets used from a different event loop than the one it was
    created on (e.g. an ASGI request handler vs. a CLI command vs. a
    cron job all using the same SDK instance via module-level state),
    it transparently rebinds to the running loop on next use. This is
    a known foot-gun with long-lived ``httpx.AsyncClient`` instances;
    we handle it here so callers don't have to.

    Always call :meth:`aclose` during graceful shutdown.
    """

    def __init__(self, *, base_url: str, token: str, timeout: float = 300.0) -> None:
        # The default covers the worst-case server-side budget for inline
        # `POST /hosts`: Tailscale device discovery (~180s) + ssh-keyscan
        # retries (~30s) + provider + network jitter. A tighter timeout that
        # fires while the server is still provisioning leaves an orphan VM
        # on the provider side until the janitor reaps it.
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        # The bound event loop is recorded the first time a client is
        # created. httpx clients are tied to the loop they were
        # instantiated on; reusing one across loops raises at request
        # time. Track the loop here so cross-loop reuse rebinds
        # instead of crashing.
        self._client_loop: asyncio.AbstractEventLoop | None = None
        # Concurrent first-callers can both observe ``self._client is
        # None`` and race to create two clients, leaking one. Serialize
        # the initialization path with a lazy-allocated lock.
        self._client_lock: asyncio.Lock | None = None

    @classmethod
    def from_env(cls, *, prefix: str = "SANDBOX_") -> Self:
        """Build from ``{prefix}SERVICE_URL`` / ``{prefix}SERVICE_TOKEN``
        / ``{prefix}SERVICE_TIMEOUT`` env vars.

        Convenience for callers that don't want to thread settings
        through their own config layer. The constructor stays the
        canonical entry point — this just reads the env once.
        """

        url = os.environ[f"{prefix}SERVICE_URL"]
        token = os.environ[f"{prefix}SERVICE_TOKEN"]
        timeout = float(os.environ.get(f"{prefix}SERVICE_TIMEOUT", "300"))
        return cls(base_url=url, token=token, timeout=timeout)

    # ------------------------------------------------------------------
    # Host lifecycle
    # ------------------------------------------------------------------

    async def create_host(
        self,
        *,
        env: dict[str, str] | None = None,
        expires_at: datetime | None = None,
        idempotency_key: str | None = None,
        image: str | None = None,
    ) -> SandboxHost:
        """Provision a new host.

        The service provisions inline; this call blocks until the host
        is ``active`` (~10-30s typical, up to a few minutes in the worst
        case). Raises :class:`SandboxProvisioningError` if the service
        reaches its own provisioning failure (502); raises
        :class:`SandboxResponseError` on transport failure or unexpected
        status.

        ``idempotency_key`` is sent as the service's ``Idempotency-Key``
        header. Useful for retry safety on flaky networks — a retry with
        the same key after a successful provision returns the original
        host instead of creating a duplicate.
        """

        payload: dict[str, Any] = {}
        if env is not None:
            payload["env"] = env
        if expires_at is not None:
            payload["expires_at"] = expires_at.isoformat()
        if image is not None:
            payload["image"] = image

        headers: dict[str, str] = {}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        data = await self._request("POST", "/hosts", json=payload, headers=headers)
        assert isinstance(data, dict)
        return _parse_sandbox_host(data)

    async def get_host(self, host_id: uuid.UUID | str) -> SandboxHost:
        data = await self._request("GET", f"/hosts/{host_id}")
        assert isinstance(data, dict)
        return _parse_sandbox_host(data)

    async def attach(self, host_id: uuid.UUID | str) -> SandboxHost:
        """Alias for :meth:`get_host` that reads better at call sites
        coming back to a host they provisioned in an earlier process.

        Same wire call; the rename only exists so a restart-resumption
        path doesn't have to start with ``get_host`` (which would read
        like "go discover a host" when the intent is "reattach to one
        I already know about").
        """

        return await self.get_host(host_id)

    async def list_hosts(self) -> list[SandboxHost]:
        data = await self._request("GET", "/hosts")
        assert isinstance(data, list)
        return [_parse_sandbox_host(item) for item in data]

    async def delete_host(self, host_id: uuid.UUID | str) -> None:
        await self._request("DELETE", f"/hosts/{host_id}")

    async def aclose(self) -> None:
        if self._client is None:
            return
        await self._client.aclose()
        self._client = None
        self._client_loop = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        client = await self._get_client()
        request_headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if headers is not None:
            request_headers.update(headers)

        try:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                json=json,
                headers=request_headers,
                timeout=self.timeout,
            )
        except httpx.RequestError as exc:
            raise SandboxUnavailableError(f"Sandbox service transport failed: {exc}") from exc

        if response.status_code == 204:
            return {}

        try:
            json_response = response.json()
        except ValueError as exc:
            raise SandboxResponseError("Sandbox service returned non-JSON output") from exc

        if response.status_code in {401, 403}:
            raise SandboxAuthError(json_response.get("detail", "auth failed"))

        if response.status_code == 404:
            raise SandboxNotFoundError(json_response.get("detail", "not found"))

        if response.status_code == 409:
            raise SandboxConflictError(json_response.get("detail", "conflict"))

        if response.status_code == 502:
            raise SandboxProvisioningError(json_response.get("detail", "provisioning failed"))

        if response.status_code == 503:
            raise SandboxUnavailableError(json_response.get("detail", "service unavailable"))

        if response.status_code >= 400:
            raise SandboxResponseError(json_response.get("detail", "error"))
        return json_response

    async def _get_client(self) -> httpx.AsyncClient:
        running_loop = asyncio.get_running_loop()
        # Fast-path: client exists and is bound to the current loop.
        if self._client is not None and self._client_loop is running_loop:
            return self._client
        # Slow-path: either no client yet, or the client is bound to a
        # stale loop (e.g. fixture teardown). Serialize through the
        # lock.
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        async with self._client_lock:
            # Re-check under the lock; another coroutine may have raced
            # ahead.
            if self._client is not None and self._client_loop is running_loop:
                return self._client
            if self._client is not None:
                # Stale-loop client: closing on its own loop is unsafe,
                # so drop the reference and rely on GC. httpx will emit
                # an "unclosed client" warning, which is the correct
                # signal that the process-level lifecycle hook
                # (e.g. ASGI lifespan) didn't run on the previous loop.
                self._client = None
                self._client_loop = None
            self._client = httpx.AsyncClient(limits=_SANDBOX_HTTP_LIMITS)
            self._client_loop = running_loop
            return self._client
