"""SDK-only tests against a fake httpx server via respx.

The SDK's contract is "speak HTTP to the sandbox service correctly,
parse the responses into typed records, raise typed errors on
non-success codes." No SSH, no provisioning, no real network. respx
gives us a controllable transport layer so every test stays in-process.
"""

# Tests legitimately read the SDK's internal _client / _client_loop to
# verify lifecycle + loop-binding behaviour. Suppressing here rather
# than promoting them to public API or adding test-only accessors.
# pyright: reportPrivateUsage=false

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx

from drukbox_sdk import (
    DoctorReport,
    SandboxAPI,
    SandboxAuthError,
    SandboxConflictError,
    SandboxHost,
    SandboxNotFoundError,
    SandboxProvisioningError,
    SandboxResponseError,
    SandboxUnavailableError,
)

BASE_URL = "https://sandbox.test"


def _doctor_payload(**overrides: Any) -> dict[str, Any]:
    """One canonical /doctor payload used across tests; overrides per case."""

    payload: dict[str, Any] = {
        "ok": True,
        "active_provider": "aws",
        "tailscale_enabled": True,
        "checks": [
            {
                "name": "db",
                "status": "ok",
                "detail": "select 1 -> 1",
                "latency_ms": 2,
                "hint": None,
            },
            {
                "name": "provider",
                "status": "ok",
                "detail": "account=111122223333 arn=arn:aws:iam::111122223333:user/drukbox",
                "latency_ms": 142,
                "hint": None,
            },
            {
                "name": "tailscale",
                "status": "ok",
                "detail": "tailnet=example.ts.net devices=3",
                "latency_ms": 88,
                "hint": None,
            },
        ],
    }
    payload.update(overrides)
    return payload


def _host_payload(**overrides: Any) -> dict[str, Any]:
    """One canonical host payload used across tests; overrides per case."""

    payload: dict[str, Any] = {
        "id": str(uuid4()),
        "name": "host-abc",
        "status": "provisioning",
        "provider": "exe",
        "image": "ghcr.io/drukbox/sandbox:test",
        "external_ssh_host": "203.0.113.42",
        "external_ssh_port": 22,
        "ssh_username": "exedev",
        "internal_ssh_host": "host-abc.example.ts.net",
        "known_hosts": "ssh-ed25519 AAAA...\n",
        "tailscale_device_id": None,
        "private_key": None,
        "last_error": "",
        "created_at": "2026-05-28T12:00:00+00:00",
        "updated_at": "2026-05-28T12:00:00+00:00",
        "activated_at": None,
        "expires_at": None,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
async def api() -> AsyncGenerator[SandboxAPI, None]:
    """Fresh SandboxAPI per test. Closed via finalizer."""

    client = SandboxAPI(base_url=BASE_URL, token="t-test", timeout=5.0)
    yield client
    await client.aclose()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@respx.mock
async def test_create_host_posts_payload_and_returns_parsed_host(api: SandboxAPI):
    expires_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    payload = _host_payload(expires_at=expires_at.isoformat())
    route = respx.post(f"{BASE_URL}/hosts").mock(
        return_value=httpx.Response(202, json=payload),
    )

    host = await api.create_host(
        image="ghcr.io/drukbox/sandbox:test",
        env={"FOO": "bar"},
        expires_at=expires_at,
        idempotency_key="agent-run-42",
    )

    assert route.called
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer t-test"
    assert sent.headers["Accept"] == "application/json"
    assert sent.headers["Idempotency-Key"] == "agent-run-42"
    body = sent.content.decode()
    assert '"image":"ghcr.io/drukbox/sandbox:test"' in body
    assert '"env":{"FOO":"bar"}' in body
    assert f'"expires_at":"{expires_at.isoformat()}"' in body

    assert isinstance(host, SandboxHost)
    assert host.id == payload["id"]
    assert host.external_ssh_host == payload["external_ssh_host"]
    assert host.external_ssh_port == payload["external_ssh_port"]
    assert host.ssh_username == payload["ssh_username"]
    assert host.internal_ssh_host == payload["internal_ssh_host"]
    assert host.tailscale_device_id is None
    assert host.activated_at is None
    assert host.expires_at == payload["expires_at"]


@respx.mock
async def test_create_host_omits_image_and_env_when_unset(api: SandboxAPI):
    """The service treats absence as "use defaults"; we must not send
    explicit nulls or empty dicts because the service distinguishes
    those from "key not present"."""

    payload = _host_payload()
    route = respx.post(f"{BASE_URL}/hosts").mock(
        return_value=httpx.Response(201, json=payload),
    )

    await api.create_host()

    body = route.calls.last.request.content.decode()
    assert "image" not in body
    assert "env" not in body
    assert "expires_at" not in body
    assert "provider" not in body
    assert "Idempotency-Key" not in route.calls.last.request.headers


@respx.mock
async def test_create_host_sends_provider_when_set(api: SandboxAPI):
    payload = _host_payload(provider="hetzner")
    route = respx.post(f"{BASE_URL}/hosts").mock(
        return_value=httpx.Response(201, json=payload),
    )

    host = await api.create_host(provider="hetzner")

    assert '"provider":"hetzner"' in route.calls.last.request.content.decode()
    assert host.provider == "hetzner"


@respx.mock
async def test_get_host_returns_parsed_host(api: SandboxAPI):
    payload = _host_payload(status="active")
    respx.get(f"{BASE_URL}/hosts/{payload['id']}").mock(
        return_value=httpx.Response(200, json=payload),
    )

    host = await api.get_host(payload["id"])

    assert host.status == "active"
    assert host.provider == "exe"


@respx.mock
async def test_attach_is_alias_for_get_host(api: SandboxAPI):
    payload = _host_payload()
    route = respx.get(f"{BASE_URL}/hosts/{payload['id']}").mock(
        return_value=httpx.Response(200, json=payload),
    )

    via_attach = await api.attach(payload["id"])
    via_get = await api.get_host(payload["id"])

    assert via_attach == via_get
    # Two HTTP roundtrips; attach is not memoized.
    assert route.call_count == 2


@respx.mock
async def test_list_hosts_parses_each_record(api: SandboxAPI):
    payloads = [_host_payload(), _host_payload()]
    respx.get(f"{BASE_URL}/hosts").mock(
        return_value=httpx.Response(200, json=payloads),
    )

    hosts = await api.list_hosts()

    assert len(hosts) == 2
    assert {h.id for h in hosts} == {p["id"] for p in payloads}


@respx.mock
async def test_delete_host_swallows_204(api: SandboxAPI):
    host_id = uuid4()
    respx.delete(f"{BASE_URL}/hosts/{host_id}").mock(return_value=httpx.Response(204))

    # Should not raise and should not need a body.
    await api.delete_host(host_id)


# ---------------------------------------------------------------------------
# Forwards compatibility — service adds fields
# ---------------------------------------------------------------------------


@respx.mock
async def test_unknown_fields_in_host_payload_are_ignored(api: SandboxAPI):
    """If the service ships a new field tomorrow, today's SDK must not
    break. The host parser picks known fields only."""

    payload = _host_payload(future_field="surprise", another_one=42)
    respx.get(f"{BASE_URL}/hosts/{payload['id']}").mock(
        return_value=httpx.Response(200, json=payload),
    )

    host = await api.get_host(payload["id"])

    assert host.id == payload["id"]  # Old fields still parsed.
    assert not hasattr(host, "future_field")  # New field silently dropped.


@respx.mock
async def test_create_host_returns_private_key_when_provider_mints_one(api: SandboxAPI):
    """AWS with Tailscale-off mints a per-VM ed25519 keypair and returns
    the private half exactly once at create time. The SDK must round-trip
    it through to the caller."""

    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----\n"
    payload = _host_payload(provider="aws", private_key=pem, external_ssh_host="ec2-x.example")
    respx.post(f"{BASE_URL}/hosts").mock(return_value=httpx.Response(201, json=payload))

    host = await api.create_host()

    assert host.provider == "aws"
    assert host.private_key == pem


@respx.mock
async def test_get_host_returns_none_private_key_after_create(api: SandboxAPI):
    """drukbox doesn't persist the key, so a subsequent GET should
    return None regardless of what was returned at create time."""

    payload = _host_payload(private_key=None)
    respx.get(f"{BASE_URL}/hosts/{payload['id']}").mock(
        return_value=httpx.Response(200, json=payload),
    )

    host = await api.get_host(payload["id"])

    assert host.private_key is None


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


@respx.mock
async def test_401_raises_sandbox_auth_error(api: SandboxAPI):
    respx.get(f"{BASE_URL}/hosts").mock(
        return_value=httpx.Response(401, json={"detail": "bad token"}),
    )

    with pytest.raises(SandboxAuthError, match="bad token"):
        await api.list_hosts()


@respx.mock
async def test_403_raises_sandbox_auth_error(api: SandboxAPI):
    """403 and 401 are both classified as auth; callers shouldn't have
    to care about the distinction — both mean "fix your credentials"."""

    respx.get(f"{BASE_URL}/hosts").mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"}),
    )

    with pytest.raises(SandboxAuthError):
        await api.list_hosts()


@respx.mock
async def test_404_raises_sandbox_not_found_error(api: SandboxAPI):
    host_id = uuid4()
    respx.get(f"{BASE_URL}/hosts/{host_id}").mock(
        return_value=httpx.Response(404, json={"detail": "host gone"}),
    )

    with pytest.raises(SandboxNotFoundError, match="host gone"):
        await api.get_host(host_id)


@respx.mock
async def test_409_raises_sandbox_conflict_error(api: SandboxAPI):
    host_id = uuid4()
    respx.delete(f"{BASE_URL}/hosts/{host_id}").mock(
        return_value=httpx.Response(409, json={"detail": "host is still provisioning"}),
    )

    with pytest.raises(SandboxConflictError, match="host is still provisioning"):
        await api.delete_host(host_id)


@respx.mock
async def test_500_raises_sandbox_response_error(api: SandboxAPI):
    respx.get(f"{BASE_URL}/hosts").mock(
        return_value=httpx.Response(500, json={"detail": "boom"}),
    )

    with pytest.raises(SandboxResponseError, match="boom"):
        await api.list_hosts()


@respx.mock
async def test_502_raises_sandbox_provisioning_error(api: SandboxAPI):
    """The service maps inline provisioning failures to 502; the SDK
    surfaces them as a dedicated error so callers can distinguish
    "provisioning broke" from generic server faults."""

    respx.post(f"{BASE_URL}/hosts").mock(
        return_value=httpx.Response(
            502,
            json={"detail": "ssh-keyscan never returned host keys"},
        ),
    )

    with pytest.raises(SandboxProvisioningError, match="ssh-keyscan"):
        await api.create_host()


@respx.mock
async def test_transport_error_raises_sandbox_unavailable_error(api: SandboxAPI):
    """Network-level failure (DNS, connection refused, TLS) — wrapped as
    SandboxUnavailableError so callers can distinguish "service can't be
    reached, retry with backoff" from "service responded with a refusal"."""

    respx.get(f"{BASE_URL}/hosts").mock(side_effect=httpx.ConnectError("nope"))

    with pytest.raises(SandboxUnavailableError, match="transport failed"):
        await api.list_hosts()


@respx.mock
async def test_503_raises_sandbox_unavailable_error(api: SandboxAPI):
    """A 503 from the service (host teardown failed, dependency outage)
    is the service's own "I'm broken, try again later" signal — same
    semantics as a transport failure on the caller's side."""

    respx.get(f"{BASE_URL}/hosts").mock(
        return_value=httpx.Response(503, json={"detail": "host teardown could not be completed"}),
    )

    with pytest.raises(SandboxUnavailableError, match="host teardown"):
        await api.list_hosts()


@respx.mock
async def test_non_json_body_raises_sandbox_response_error(api: SandboxAPI):
    respx.get(f"{BASE_URL}/hosts").mock(
        return_value=httpx.Response(200, text="<html>oops</html>"),
    )

    with pytest.raises(SandboxResponseError, match="non-JSON"):
        await api.list_hosts()


@respx.mock
async def test_error_detail_absent_uses_fallback_message(api: SandboxAPI):
    """The service contract returns ``{"detail": "..."}`` on errors but
    we shouldn't crash if a different shape arrives — fall back to a
    generic message rather than KeyError-ing on the caller."""

    respx.get(f"{BASE_URL}/hosts").mock(
        return_value=httpx.Response(500, json={"unexpected": "shape"}),
    )

    with pytest.raises(SandboxResponseError):
        await api.list_hosts()


# ---------------------------------------------------------------------------
# Lifecycle + loop affinity
# ---------------------------------------------------------------------------


@respx.mock
async def test_aclose_drops_client_and_is_idempotent(api: SandboxAPI):
    respx.get(f"{BASE_URL}/hosts").mock(return_value=httpx.Response(200, json=[]))
    await api.list_hosts()
    assert api._client is not None

    await api.aclose()
    assert api._client is None

    # Second close is a no-op (no AttributeError, no double-close).
    await api.aclose()


def test_cross_loop_reuse_rebinds_client_without_crashing():
    """The httpx ``AsyncClient`` is loop-bound. Reusing the same SDK
    instance across two ``asyncio.run`` invocations (the closest
    stand-in for two distinct event-loop lifetimes against one module-
    level SDK instance) should rebind instead of erroring.

    The pre-fix behaviour was a ``RuntimeError("Event loop is closed")``
    or ``Loop attached to a different loop`` on the second call — that
    not raising is the whole point. As a secondary check we capture
    the bound loop reference and assert they differ across the two
    runs.
    """

    sandbox = SandboxAPI(base_url=BASE_URL, token="t", timeout=5.0)
    bound_loops: list[asyncio.AbstractEventLoop] = []

    async def use_it() -> None:
        with respx.mock() as mock:
            mock.get(f"{BASE_URL}/hosts").mock(
                return_value=httpx.Response(200, json=[]),
            )
            await sandbox.list_hosts()
            assert sandbox._client_loop is not None
            bound_loops.append(sandbox._client_loop)

    asyncio.run(use_it())
    asyncio.run(use_it())

    # Two distinct loop objects — the SDK rebound on the second
    # invocation rather than reusing a stale (closed) loop.
    assert bound_loops[0] is not bound_loops[1]


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


def test_from_env_reads_prefixed_vars(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SANDBOX_SERVICE_URL", "https://from-env.test")
    monkeypatch.setenv("SANDBOX_SERVICE_TOKEN", "env-token")
    monkeypatch.setenv("SANDBOX_SERVICE_TIMEOUT", "60")

    sandbox = SandboxAPI.from_env()

    assert sandbox.base_url == "https://from-env.test"
    assert sandbox.token == "env-token"
    assert sandbox.timeout == 60.0


def test_from_env_supports_custom_prefix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CUSTOM_SERVICE_URL", "https://custom.test")
    monkeypatch.setenv("CUSTOM_SERVICE_TOKEN", "x")

    sandbox = SandboxAPI.from_env(prefix="CUSTOM_")

    assert sandbox.base_url == "https://custom.test"
    assert sandbox.timeout == 300.0  # default when unset


def test_from_env_strips_trailing_slash_via_constructor(
    monkeypatch: pytest.MonkeyPatch,
):
    """``base_url`` should be normalized so concatenation with ``/hosts``
    doesn't yield ``//hosts``. The constructor strips, not the env
    reader — covered here because that's the most common entry point."""

    monkeypatch.setenv("SANDBOX_SERVICE_URL", "https://trailing.test/")
    monkeypatch.setenv("SANDBOX_SERVICE_TOKEN", "x")

    sandbox = SandboxAPI.from_env()

    assert sandbox.base_url == "https://trailing.test"


@respx.mock
async def test_doctor_parses_report_and_checks(api: SandboxAPI):
    route = respx.get(f"{BASE_URL}/doctor").mock(
        return_value=httpx.Response(200, json=_doctor_payload()),
    )

    report = await api.doctor()

    assert route.called
    assert route.calls.last.request.headers["Authorization"] == "Bearer t-test"
    assert isinstance(report, DoctorReport)
    assert report.ok is True
    assert report.active_provider == "aws"
    assert report.tailscale_enabled is True
    assert [check.name for check in report.checks] == ["db", "provider", "tailscale"]
    provider = next(check for check in report.checks if check.name == "provider")
    assert provider.status == "ok"
    assert provider.latency_ms == 142
    assert provider.hint is None


@respx.mock
async def test_doctor_surfaces_failure_with_hint(api: SandboxAPI):
    """A failed probe round-trips status, detail, and the remediation hint;
    the HTTP status is still 200, so callers branch on report.ok."""

    payload = _doctor_payload(
        ok=False,
        checks=[
            {
                "name": "db",
                "status": "ok",
                "detail": "select 1 -> 1",
                "latency_ms": 2,
                "hint": None,
            },
            {
                "name": "tailscale",
                "status": "fail",
                "detail": "Tailscale API authentication failed",
                "latency_ms": 380,
                "hint": "check_tailscale_oauth_and_api_reachability",
            },
        ],
    )
    respx.get(f"{BASE_URL}/doctor").mock(return_value=httpx.Response(200, json=payload))

    report = await api.doctor()

    assert report.ok is False
    failed = next(check for check in report.checks if check.status == "fail")
    assert failed.name == "tailscale"
    assert failed.detail == "Tailscale API authentication failed"
    assert failed.hint == "check_tailscale_oauth_and_api_reachability"


@respx.mock
async def test_doctor_ignores_unknown_check_fields(api: SandboxAPI):
    """A future service adding a field to a check must not break today's SDK."""

    payload = _doctor_payload(
        checks=[
            {
                "name": "db",
                "status": "ok",
                "detail": "select 1 -> 1",
                "latency_ms": 2,
                "hint": None,
                "future_field": "surprise",
            },
        ],
    )
    respx.get(f"{BASE_URL}/doctor").mock(return_value=httpx.Response(200, json=payload))

    report = await api.doctor()

    assert report.checks[0].name == "db"
    assert not hasattr(report.checks[0], "future_field")


@respx.mock
async def test_doctor_401_raises_sandbox_auth_error(api: SandboxAPI):
    """Doctor is service-token only; a bad token raises like every other call."""

    respx.get(f"{BASE_URL}/doctor").mock(
        return_value=httpx.Response(401, json={"detail": "service token required"}),
    )

    with pytest.raises(SandboxAuthError, match="service token required"):
        await api.doctor()
