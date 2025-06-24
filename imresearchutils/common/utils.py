from functools import wraps
from ipaddress import ip_address

from .types import *


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

    # If input is a bytes object, try to decode it
    # This is useful for cases where the input might be a byte string representation of an IP
    if isinstance(ip, bytes):
        try:
            ip = ip.decode()
        except UnicodeDecodeError:
            pass

    # If raw bytes OR string, ip_address can directly handle it
    if isinstance(ip, (bytes, str)):
        try:
            return ip_address(ip)
        except ValueError:
            pass

    # Give up
    return ip


def is_internal_flow(src_ip: IPAddress, dst_ip: IPAddress):
    return src_ip.is_private and dst_ip.is_private


def is_external_flow(src_ip: IPAddress, dst_ip: IPAddress):
    return not is_internal_flow(src_ip, dst_ip)
