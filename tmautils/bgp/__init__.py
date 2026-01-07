import warnings

from .pyasn import PyasnUtil
from .asdb import ASdbCategoryUtil
from .caida_as_org import CaidaAsOrgInfoUtil

__all__ = [
    'PyasnUtil',
    'ASdbCategoryUtil',
    'CaidaAsOrgInfoUtil',
    # Utils moved to enrich_ip
    'IPApiUtil',
    'IPApiBatchUtil',
    'IPInfoLiteUtil',
]


def __getattr__(name):
    if name == 'IPApiUtil':
        warnings.warn(
            "IPApiUtil is deprecated. "
            "Use IPApiBatchUtil from tmautils.enrich_ip instead.",
            DeprecationWarning,
            stacklevel=2
        )
        from tmautils.enrich_ip import IPApiUtil
        return IPApiUtil

    elif name == 'IPApiBatchUtil':
        warnings.warn(
            "Importing IPApiBatchUtil from tmautils.bgp is deprecated. "
            "It has moved to tmautils.enrich_ip "
            "and will be removed from bgp in a future release.",
            DeprecationWarning,
            stacklevel=2
        )
        from tmautils.enrich_ip import IPApiBatchUtil
        return IPApiBatchUtil

    elif name == 'IPInfoLiteUtil':
        warnings.warn(
            "Importing IPInfoLiteUtil from tmautils.bgp is deprecated. "
            "It has moved to tmautils.enrich_ip "
            "and will be removed from bgp in a future release.",
            DeprecationWarning,
            stacklevel=2
        )
        from tmautils.enrich_ip import IPInfoLiteUtil
        return IPInfoLiteUtil

    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
