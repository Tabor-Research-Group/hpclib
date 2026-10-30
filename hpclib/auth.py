"""Public first-party hpclib authentication API.

REST still exports TokenAuth and TokenIdentity for existing callers. Applications
can import this module; standalone tunnel packages embed the same implementation
with console_packages' core_helpers. Kerberos and external applications' secrets
are outside this API. TokenRegistry issuance methods require a trusted issuer.
"""
from .servers.token_auth import (
    TokenAuth, TokenIdentity, TokenRegistry, ServiceToken, CapabilityTickets,
    ResourceSigner, REST_AUDIENCE,
)

__all__ = ["TokenAuth", "TokenIdentity", "TokenRegistry", "ServiceToken",
           "CapabilityTickets", "ResourceSigner", "REST_AUDIENCE"]
