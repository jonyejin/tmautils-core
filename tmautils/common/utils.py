# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from typing import Callable, Union
from functools import wraps
from ipaddress import ip_address, IPv4Address, IPv6Address

from .types import IPAddress


def maybe_apply(fn: Callable):
    """
    Decorator to apply a function if it does not raise an exception.
    If an exception is raised, the original input is returned.
    """

    @wraps(fn)
    def wrapper(x):
        try:
            return fn(x)
        except Exception:
            return x
    return wrapper


def try_convert_ip(ip: Union[str, bytes, IPAddress]) -> Union[str, bytes, IPAddress]:
    """
    Convert a string or raw bytes to an IPAddress object if possible.
    If the input is already an IPAddress or cannot be converted, it returns the input unchanged.

    """

    # Short-circuit if the input is already an IPAddress
    if isinstance(ip, (IPv4Address, IPv6Address)):
        return ip

    # If **raw** bytes OR string, ip_address can directly handle it
    if isinstance(ip, (bytes, str)):
        try:
            return ip_address(ip)
        except ValueError:
            pass

    # If input is the byte string representation of an IP, decode and try again
    if isinstance(ip, bytes):
        try:
            return ip_address(ip.decode())
        except (UnicodeDecodeError, ValueError):
            pass

    # Give up
    return ip


def is_ipv4(ip: Union[str, bytes, IPAddress]) -> bool:
    """
    Check if the input is an IPv4 address.
    """
    ip_obj = try_convert_ip(ip)
    return isinstance(ip_obj, IPv4Address)


def is_ipv6(ip: Union[str, bytes, IPAddress]) -> bool:
    """
    Check if the input is an IPv6 address.
    """
    ip_obj = try_convert_ip(ip)
    return isinstance(ip_obj, IPv6Address)


def is_internal_flow(src_ip: IPAddress, dst_ip: IPAddress):
    return src_ip.is_private and dst_ip.is_private


def is_external_flow(src_ip: IPAddress, dst_ip: IPAddress):
    return not is_internal_flow(src_ip, dst_ip)


def is_internal_flow_or_same_v6_upper_64(src_ip: IPAddress, dst_ip: IPAddress):
    """
    Check if the flow is internal or, for IPv6, if the upper 64 bits of
    the source and destination addresses are the same.

    This may be useful in cases where nodes within the same network use
    IPv6 GUA addresses to communicate with each other.
    """
    if is_internal_flow(src_ip, dst_ip):
        return True
    if src_ip.version == 6:  # We can skip the check for dst_ip
        return src_ip.packed[:8] == dst_ip.packed[:8]
    return False
