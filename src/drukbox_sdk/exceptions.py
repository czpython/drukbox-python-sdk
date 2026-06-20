"""Typed errors for Drukbox interactions.

The hierarchy lets callers distinguish "I can't reach the service at
all" from "the service told me no" and lets them narrow on common HTTP
shapes (auth, not-found, conflict) without parsing status codes
themselves.
"""


class SandboxUnavailableError(RuntimeError):
    """Sandbox service is unreachable or transiently broken.

    Raised for both transport-level failures (DNS, connection refused,
    TLS, timeouts) and 503 responses from the service itself. Both
    signal "retry with backoff" rather than "the request was rejected."
    Distinct from :class:`SandboxAPIError`, which represents the
    service deliberately refusing a request.
    """


class SandboxAPIError(RuntimeError):
    """Base for any error the sandbox service returned via HTTP."""


class SandboxAuthError(SandboxAPIError):
    """401/403 from the sandbox service. Token wrong, missing, or revoked."""


class SandboxNotFoundError(SandboxAPIError):
    """404 — the resource ID isn't known to the sandbox service.

    Common cause during host-attach paths: the host was already torn
    down by a sibling worker / housekeeping sweep.
    """


class SandboxConflictError(SandboxAPIError):
    """409 — the operation conflicts with the current state.

    e.g. provisioning a Linux user on a host that already has one with
    the same name.
    """


class SandboxProvisioningError(SandboxAPIError):
    """502 — the service tried to provision the host and the provider,
    Tailscale, or ssh-keyscan step failed. The host row stays in
    ``error`` state on the service side; call :meth:`delete_host` to
    release any partial provider state.
    """


class SandboxValidationError(SandboxAPIError):
    """422 — the service rejected the request payload as invalid.

    e.g. an env key that isn't a valid environment-variable name, or a
    proxy target URL carrying a path or credentials. The request is
    malformed, so retrying it unchanged won't help — fix the input.
    """


class SandboxResponseError(SandboxAPIError):
    """Everything else the service returned that the SDK doesn't classify."""
