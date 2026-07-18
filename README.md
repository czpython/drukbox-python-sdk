# drukbox-python-sdk

Async Python client for the [Drukbox] host API.

The SDK provisions sandbox VMs, reads host state, deletes hosts, and
returns the SSH connection details a caller needs. It speaks HTTP only:
SSH sessions, file transfer, command execution, and retry orchestration
belong in the caller.

## Install

```bash
pip install drukbox-python-sdk
uv add drukbox-python-sdk
```

## Usage

```python
from drukbox_sdk import SandboxAPI

sandbox = SandboxAPI(
    base_url="https://sandbox.internal.ts.net",
    token="...",
)

try:
    host = await sandbox.create_host(
        image="ghcr.io/drukbox/sandbox:abc123",
        env={"FOO": "bar"},
        idempotency_key="agent-run-42",
    )
    # Dial whichever reachable address fits your network: host.external_ssh_host
    # for the provider's public path (may be empty when the service runs an
    # AWS-with-Tailscale-on deployment) or host.internal_ssh_host for the
    # tailnet MagicDNS name (only when Tailscale is enabled). Use host.known_hosts
    # for SSH host-key verification. If the provider mints a per-VM keypair
    # (AWS with Tailscale off), host.private_key carries the private half —
    # returned exactly once at create time; subsequent get_host returns None.
finally:
    await sandbox.delete_host(host.id)
    await sandbox.aclose()
```

`create_host` blocks until the host is `active` — typically ~10–30s, up to a
few minutes worst case. The SDK's default `timeout` (300s) covers this. Pass
an `idempotency_key` for retry safety: a retry with the same key after a
successful provision returns the original host instead of creating a duplicate.

`SandboxAPI.from_env(prefix="SANDBOX_")` reads
`SANDBOX_SERVICE_URL`, `SANDBOX_SERVICE_TOKEN`, and optional
`SANDBOX_SERVICE_TIMEOUT`.

## Contract

Public exports live in `drukbox_sdk`:

- `SandboxAPI`
- `SandboxHost`
- `DoctorReport` and `DoctorCheck`
- `HTTPProxy` and `HTTPProxyAttachment`
- `SandboxAPIError` and typed subclasses for auth, not found, conflict,
  unavailable, and unclassified response errors

Supported host operations:

- `create_host`
- `get_host`
- `attach`
- `list_hosts`
- `renew_host`
- `delete_host`
- `doctor`
- `aclose`

`create_host` supports the service's optional `image`, `env`, `expires_at`,
`provider`, `instance_type`, `disk_gb`, and `Idempotency-Key` inputs.
`instance_type` (provider-native size, e.g. `t3.xlarge` / `cx33`) and `disk_gb`
pin the VM shape; omit either for the provider default. `expires_at` mirrors the
wire contract: omit it for the default lease, pass a datetime for an explicit
expiry, or pass `None` for a never-reaped (permanent) host.

`renew_host` extends a host's lease via `POST /hosts/{id}/renew`. Omit
`expires_at` to extend by the service's default TTL; renewal never makes a host
permanent.

Supported HTTP-proxy operations:

- `create_http_proxy`
- `delete_http_proxy`
- `attach_http_proxy`
- `detach_http_proxy`

HTTP proxies are account-bound exe.dev resources, not host state — deleting a
host does not remove proxies fronting it. `create_http_proxy` takes an
origin-only `target` (scheme + host, no path/query/fragment/credentials) and at
least one `headers` entry; `attach_http_proxy` / `detach_http_proxy` point a
proxy at a host's backing VM (the host must be `bootstrapping` or `active`).

`doctor` fetches `GET /doctor` — read-only dependency health. The service
runs one cheap, non-mutating probe per dependency (database, active VM
provider, Tailscale when enabled) and always responds 200, so callers branch
on `DoctorReport.ok` rather than the HTTP status. A failed `DoctorCheck`
carries a stable `hint` slug for remediation.

The SDK does not mint Tailscale auth keys, manage ACLs, establish SSH,
provision Linux users, transfer files, or run remote commands.

## Development

```bash
uv sync
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

Tests use `respx` to fake the Drukbox HTTP API. They do not need a
real network, VM provider, or Drukbox service.

[Drukbox]: https://github.com/czpython/drukbox
