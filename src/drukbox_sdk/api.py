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
from dataclasses import dataclass
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
    SandboxValidationError,
)

# Explicit pool budget. The httpx defaults (100 / 20) are plenty for
# the SDK's traffic shape (a handful of provisioning calls per agent
# run), but keeping the values visible at the import site means a
# future bump in concurrency does not silently exhaust the pool.
_SANDBOX_HTTP_LIMITS = httpx.Limits(
    max_connections=20,
    max_keepalive_connections=5,
)


class _Unset:
    """Sentinel distinguishing an omitted argument from an explicit ``None``.

    ``create_host`` mirrors the service's tri-state ``expires_at``: an
    omitted field means "default lease", an explicit ``null`` means
    "permanent". A plain ``datetime | None`` default can't tell those two
    apart, so the omitted case uses this sentinel.
    """

    def __repr__(self) -> str:
        return "UNSET"


_UNSET = _Unset()


@dataclass(frozen=True)
class SandboxHost:
    """Snapshot of a provisioned host as returned by the service.

    The shape mirrors the Drukbox ``Host`` schema. Either reachable
    address may be empty/None depending on the provider and deployment:
    ``external_ssh_host`` carries the provider's public path (always
    populated by exe.dev; empty for an AWS host with Tailscale on);
    ``internal_ssh_host`` carries the tailnet MagicDNS form, populated
    only when the service runs with Tailscale enabled. The internal
    path is always reached on port 22 by Tailscale convention, so
    there is no ``internal_ssh_port``. Callers pick whichever path
    they can reach and dial it themselves — this SDK doesn't speak SSH.

    ``private_key`` carries per-VM SSH private key material returned
    exactly once at create time, when the provider mints fresh material
    per instance (AWS with Tailscale off). Providers that use a
    different SSH auth model (exe.dev's edge proxy, Tailscale ACLs
    via tailscaled-SSH) return None here. A later ``get_host`` call
    always returns None — the key is not persisted server-side.
    """

    id: str
    name: str
    status: str
    provider: str
    image: str
    instance_type: str | None
    disk_gb: int | None
    external_ssh_host: str
    external_ssh_port: int
    ssh_username: str
    internal_ssh_host: str | None
    known_hosts: str
    tailscale_device_id: str | None
    private_key: str | None
    last_error: str
    created_at: str
    updated_at: str
    activated_at: str | None
    expires_at: str | None


@dataclass(frozen=True)
class DoctorCheck:
    """One dependency probe. ``hint`` is a remediation slug, set only on failures."""

    name: str
    status: str
    detail: str | None
    latency_ms: int | None
    hint: str | None


@dataclass(frozen=True)
class DoctorReport:
    """Health snapshot. The HTTP status is always 200 — branch on ``ok``."""

    ok: bool
    active_provider: str
    tailscale_enabled: bool
    checks: list[DoctorCheck]


@dataclass(frozen=True)
class HTTPProxy:
    """An account-bound HTTP proxy as returned by the service.

    ``status`` echoes the lifecycle step the call reached
    (``"created"``). Proxies are account resources, not host state:
    deleting a host they front does not remove them.
    """

    name: str
    status: str


@dataclass(frozen=True)
class HTTPProxyAttachment:
    """The result of pointing an HTTP proxy at a host's backing VM.

    ``host_id`` is the string UUID of the attached host; ``status``
    echoes the step reached (``"attached"``).
    """

    name: str
    host_id: str
    status: str


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
        disk_gb: int | None = None,
        env: dict[str, str] | None = None,
        expires_at: datetime | None | _Unset = _UNSET,
        idempotency_key: str | None = None,
        image: str | None = None,
        instance_type: str | None = None,
        provider: str | None = None,
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

        ``provider`` pins which VM provider serves the request; omit it to
        use the service default. An unknown provider raises
        :class:`SandboxResponseError` (the service rejects it with 400).

        ``instance_type`` (provider-native size, e.g. ``t3.xlarge`` /
        ``cx33``) and ``disk_gb`` (root disk size) pin the VM shape; omit
        either to use the provider's configured default. A size the active
        provider can't serve raises :class:`SandboxResponseError` (400).

        Lease (mirrors the wire contract's tri-state ``expires_at``): omit
        it for the service's default lease, pass a ``datetime`` for an
        explicit expiry, or pass ``None`` for a never-reaped (permanent)
        host — exactly as an omitted field vs. an explicit ``null`` behave
        on the API.
        """

        payload: dict[str, Any] = {}
        if disk_gb is not None:
            payload["disk_gb"] = disk_gb
        if env is not None:
            payload["env"] = env
        # Sentinel default lets us tell "caller omitted expires_at" (default
        # lease) from "caller passed None" (explicit null → permanent host).
        if not isinstance(expires_at, _Unset):
            payload["expires_at"] = None if expires_at is None else expires_at.isoformat()
        if image is not None:
            payload["image"] = image
        if instance_type is not None:
            payload["instance_type"] = instance_type
        if provider is not None:
            payload["provider"] = provider

        headers: dict[str, str] = {}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        data = await self._request("POST", "/hosts", json=payload, headers=headers)
        assert isinstance(data, dict)
        return SandboxHost(**data)

    async def get_host(self, host_id: uuid.UUID | str) -> SandboxHost:
        data = await self._request("GET", f"/hosts/{host_id}")
        assert isinstance(data, dict)
        return SandboxHost(**data)

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
        return [SandboxHost(**item) for item in data]

    async def renew_host(
        self,
        host_id: uuid.UUID | str,
        *,
        expires_at: datetime | None = None,
    ) -> SandboxHost:
        """Extend a host's lease and return the updated record.

        Omit ``expires_at`` to extend by the service's default TTL from
        now; pass one to set an explicit future expiry. Renewal never
        makes a host permanent — that is a create-time choice.

        Raises :class:`SandboxNotFoundError` for an unknown host and
        :class:`SandboxConflictError` when the host is in a non-renewable
        state (still provisioning, errored, or an unclaimed pool host).
        """

        payload: dict[str, Any] = {}
        if expires_at is not None:
            payload["expires_at"] = expires_at.isoformat()
        data = await self._request("POST", f"/hosts/{host_id}/renew", json=payload)
        assert isinstance(data, dict)
        return SandboxHost(**data)

    async def delete_host(self, host_id: uuid.UUID | str) -> None:
        await self._request("DELETE", f"/hosts/{host_id}")

    async def doctor(self) -> DoctorReport:
        """Fetch ``GET /doctor`` — read-only dependency health.

        Always responds 200; inspect :attr:`DoctorReport.ok`, not the HTTP
        status. Auth failures raise :class:`SandboxAuthError`.
        """

        data = await self._request("GET", "/doctor")
        assert isinstance(data, dict)
        return DoctorReport(
            ok=data["ok"],
            active_provider=data["active_provider"],
            tailscale_enabled=data["tailscale_enabled"],
            checks=[DoctorCheck(**check) for check in data["checks"]],
        )

    # ------------------------------------------------------------------
    # HTTP proxies
    # ------------------------------------------------------------------

    async def create_http_proxy(
        self,
        *,
        name: str,
        target: str,
        headers: dict[str, str],
    ) -> HTTPProxy:
        """Create an account-bound HTTP proxy in front of ``target``.

        ``target`` must be an origin-only URL (scheme + host, no path,
        query, fragment, or credentials); ``headers`` must carry at least
        one entry. The service rejects a malformed target or empty headers
        with :class:`SandboxValidationError`; a name that already exists
        raises :class:`SandboxConflictError`.
        """

        payload: dict[str, Any] = {"name": name, "target": target, "headers": headers}
        data = await self._request("POST", "/http-proxies", json=payload)
        assert isinstance(data, dict)
        return HTTPProxy(**data)

    async def delete_http_proxy(self, name: str) -> None:
        """Delete an HTTP proxy by name. Unknown name raises
        :class:`SandboxNotFoundError`."""

        await self._request("DELETE", f"/http-proxies/{name}")

    async def attach_http_proxy(
        self,
        name: str,
        host_id: uuid.UUID | str,
    ) -> HTTPProxyAttachment:
        """Point proxy ``name`` at ``host_id``'s backing VM.

        The host must have a backing VM (``bootstrapping`` or ``active``);
        otherwise the service raises :class:`SandboxConflictError`. An
        unknown host or proxy raises :class:`SandboxNotFoundError`.
        """

        data = await self._request("POST", f"/http-proxies/{name}/hosts/{host_id}")
        assert isinstance(data, dict)
        return HTTPProxyAttachment(**data)

    async def detach_http_proxy(self, name: str, host_id: uuid.UUID | str) -> None:
        """Detach proxy ``name`` from ``host_id``. Unknown host or proxy
        raises :class:`SandboxNotFoundError`."""

        await self._request("DELETE", f"/http-proxies/{name}/hosts/{host_id}")

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

        if response.status_code == 422:
            # FastAPI's 422 detail is a list of error dicts; join the messages
            # so it reads as a line, not a raw list repr.
            detail = json_response.get("detail", "validation error")
            if isinstance(detail, list):
                detail = "; ".join(item.get("msg", "") for item in detail) or "validation error"
            raise SandboxValidationError(detail)

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
