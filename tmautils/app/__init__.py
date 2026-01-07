import warnings

warnings.warn(
    "tmautils.app is deprecated and will be removed in a future release. "
    "Use tmautils.enrich_ip instead.",
    DeprecationWarning,
    stacklevel=2
)

from tmautils.enrich_ip import (
    VpnIpAz0,
    ListsVpnX4BNet,
    IpInfoPrivacyUtil,
    IpInfoCarrierUtil,
    ChromePrefetchUtil,
)

__all__ = [
    'VpnIpAz0',
    'ListsVpnX4BNet',
    'IpInfoPrivacyUtil',
    'IpInfoCarrierUtil',
    'ChromePrefetchUtil',
]
