from enum import Enum, StrEnum
from typing import (
    Union, Any, Optional,
    Callable, Awaitable, Iterable,
    Literal, Dict, List, Tuple,
    Type, TypeVar, TYPE_CHECKING,
    ClassVar,
)
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from pathlib import Path
from dataclasses import dataclass, asdict, field

IPAddress = Union[IPv4Address, IPv6Address]


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
    ICMPV6 = 58


@dataclass(frozen=True)
class FQDN:
    name: str

    @property
    def registered_domain(self):
        from tldextract import extract
        return extract(
            self.name,
            include_psl_private_domains=True
        ).registered_domain
