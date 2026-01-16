import warnings

warnings.warn(
    "tmautils.security is deprecated and will be removed in a future release. "
    "Use tmautils.pki instead.",
    DeprecationWarning,
    stacklevel=2
)

from tmautils.pki import (
    get_cert,
    get_cert_sync,
    get_cert_chain,
    get_cert_chain_sync,
)

__all__ = [
    'get_cert',
    'get_cert_sync',
    'get_cert_chain',
    'get_cert_chain_sync',
]
