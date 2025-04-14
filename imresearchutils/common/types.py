from enum import Enum
from dataclasses import dataclass
from tldextract import extract


class IPVersion(Enum):
    IPV4 = 4
    IPV6 = 6


class DomainAFSupport(Enum):
    A_ONLY = 4
    AAAA_ONLY = 6
    DUAL_STACK = 46


class L4Proto(Enum):
    ICMP = 1
    TCP = 6
    UDP = 17
    ICMPv6 = 58


@dataclass
class FQDN:
    name: str

    @property
    def registered_domain(self):
        return extract(
            self.name,
            include_psl_private_domains=True
        ).registered_domain
