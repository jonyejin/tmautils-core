import asyncio
from ipaddress import ip_address

from dns.resolver import (
    Answer,
    LRUCache,
    NXDOMAIN,
    NoAnswer,
    NoNameservers,
    LifetimeTimeout,
)
from dns.rdatatype import RdataType
import dns.asyncresolver

from tmautils.common import *


class AsyncDnsPythonUtil:
    """
    Asynchronous DNS resolver using dnspython library.

    Args:
        nameservers (list[str] | None):
            List of DNS nameservers to use for resolution.
            If None, system default nameservers will be used.

        max_concurrent_requests (int):
            Maximum number of concurrent DNS requests.
            Defaults to 500.

        cachesize (int):
            Size of the dnspython DNS cache.
            Defaults to 500000.

        working_root (Path | None):
            Base directory where the namespace directory will be created.
            If None, the current working directory will be used.

        data_dir (Path | None):
            Deprecated alias for `working_root`.

        **kwargs:
            Additional keyword arguments for IOHelper.
            See IOHelper documentation for more details.

    Example:
        ```
        async with AsyncDnsPythonUtil() as dns_util:
            addresses = await dns_util.resolve_domain("example.com")
        ```
    """

    def __init__(
        self,
        nameservers: Optional[list[str]] = None,
        max_concurrent_requests: int = 500,
        cachesize: int = 500000,
        working_root: Path | None = None,
        data_dir: Path | None = None,
        **kwargs
    ):
        working_root = IOHelper.handle_working_root_data_dir(
            working_root, data_dir
        )
        self.io_helper = IOHelper(
            self.__class__.__name__,
            working_root=working_root,
            **kwargs,
        )

        self.resolver = dns.asyncresolver.get_default_resolver()
        # Set custom nameservers if provided
        if nameservers is not None:
            self.resolver.nameservers = nameservers
        self.resolver.cache = LRUCache(max_size=cachesize)
        self.io_helper.logger.info(
            f"Initialized DNS resolver with nameservers: {nameservers},"
            f"cache size: {cachesize}, and "
            f"max concurrent requests: {max_concurrent_requests}"
        )

        # Semaphore to limit concurrent DNS requests
        self._dnsreq_semaphore = asyncio.Semaphore(max_concurrent_requests)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False  # Don't suppress exceptions

    async def resolve_domain(
        self,
        domain: str,
        a: bool = True,
        aaaa: bool = True,
        tcp: bool = False,
        lifetime: Optional[float] = None,
        raise_on_error: bool = False,
    ) -> list[IPv4Address | IPv6Address]:
        """
        Resolve a domain name to its IP addresses.

        Args:
            domain (str):
                The domain name to resolve.

            a (bool):
                Whether to resolve A records (IPv4).
                Defaults to True.

            aaaa (bool):
                Whether to resolve AAAA records (IPv6).
                Defaults to True.

        Returns:
            list[IPv4Address | IPv6Address]:
                List of IP addresses associated with the domain.
                May be empty if no records are found or if an error occurs.
        """

        if not a and not aaaa:
            return []

        ans_dict = await self.resolve_rdtypes(
            domain,
            a=a,
            aaaa=aaaa,
            tcp=tcp,
            lifetime=lifetime,
            raise_on_error=raise_on_error,
        )

        addr_list = []
        if a and ans_dict[RdataType.A] is not None:
            addr_list.extend([
                ip_address(rdata.address) for rdata in ans_dict[RdataType.A]
            ])
        if aaaa and ans_dict[RdataType.AAAA] is not None:
            addr_list.extend([
                ip_address(rdata.address) for rdata in ans_dict[RdataType.AAAA]
            ])

        return addr_list

    async def resolve_address(
        self,
        addr: str | IPAddress,
        tcp: bool = False,
        lifetime: Optional[float] = None,
        raise_on_error: bool = False,
    ) -> list[str]:
        """
        Reverse DNS resolution for an IP address.

        Args:
            addr (str | IPAddress):
                The IP address to resolve (IPv4 or IPv6).

        Returns:
            list[str]:
                List of domain names associated with the IP address.
                May be empty if no names are found.
        """
        addr_obj = ip_address(addr)
        if addr_obj.is_private:
            return []

        maybe_ans = await self.resolve_rdtype(
            addr_obj.reverse_pointer,
            RdataType.PTR.name,
            tcp=tcp,
            lifetime=lifetime,
            raise_on_error=raise_on_error,
        )

        return [rdata.to_text() for rdata in maybe_ans] if maybe_ans else []

    async def resolve_rdtypes(
        self,
        domain: str,
        a: bool = False,
        aaaa: bool = False,
        caa: bool = False,
        cname: bool = False,
        dmarc: bool = False,
        dnskey: bool = False,
        ds: bool = False,
        mx: bool = False,
        ns: bool = False,
        soa: bool = False,
        spf: bool = False,
        txt: bool = False,
        tcp: bool = False,
        lifetime: Optional[float] = None,
        raise_on_error: bool = False,
    ) -> dict[RdataType | str, Optional[Answer]]:
        """
        Resolve a domain name to multiple DNS record types.

        Returns a dictionary mapping record types (of type `RdataType`)
        to their answers (or None if resolution failed).

        dmarc and spf are special cases of TXT records, and will be queried
        as `_dmarc.<domain>` and `_spf.<domain>` respectively.
        For dmarc and spf, the keys in the returned dictionary will be
        "_dmarc" and "_spf".

        For more lower-level control, use `resolve_rdtype()`.
        """

        record_types = {
            RdataType.A: a,
            RdataType.AAAA: aaaa,
            RdataType.CAA: caa,
            RdataType.CNAME: cname,
            RdataType.DNSKEY: dnskey,
            RdataType.DS: ds,
            RdataType.MX: mx,
            RdataType.NS: ns,
            RdataType.SOA: soa,
            RdataType.TXT: txt,
        }
        tasks = {
            rtype: self.resolve_rdtype(
                domain,
                rtype.name,
                tcp=tcp,
                lifetime=lifetime,
                raise_on_error=raise_on_error,
            )
            for rtype, enabled in record_types.items() if enabled
        }

        # Handle special TXT records for DMARC and SPF
        # Hacky, but useful (I think)
        if dmarc:
            tasks["_dmarc"] = self.resolve_rdtype(
                f"_dmarc.{domain}",
                RdataType.TXT.name,
                tcp=tcp,
                lifetime=lifetime,
                raise_on_error=raise_on_error,
            )
        if spf:
            tasks["_spf"] = self.resolve_rdtype(
                f"_spf.{domain}",
                RdataType.TXT.name,
                tcp=tcp,
                lifetime=lifetime,
                raise_on_error=raise_on_error,
            )

        answer_list = await asyncio.gather(*tasks.values())
        answer_dict = dict(zip(tasks.keys(), answer_list))

        return answer_dict

    async def resolve_rdtype(
        self,
        domain: str,
        rdtype: str,
        tcp: bool = False,
        lifetime: Optional[float] = None,
        raise_on_error: bool = False,
    ) -> Optional[Answer]:
        """
        Resolve a domain name to its resource records of a specific type.

        Args:
            domain (str):
                The domain name to resolve.

            rdtype (str):
                The resource record type to resolve (e.g., "A", "AAAA", "MX").

            raise_on_error (bool):
                Whether to raise an exception on DNS resolution errors.
                Defaults to False.

        Returns:
            Optional[Answer]:
                The DNS answer object containing the resolved records.
                Returns None if resolution fails and raise_on_error is False.
        """
        async with self._dnsreq_semaphore:
            try:
                return await self.resolver.resolve(
                    domain,
                    rdtype=rdtype,
                    tcp=tcp,
                    lifetime=lifetime,
                )
            except (NXDOMAIN, NoAnswer, NoNameservers, LifetimeTimeout) as e:
                if raise_on_error:
                    raise e
                else:
                    self.io_helper.logger.warning(
                        f"DNS resolution failed for {domain} with type {rdtype}: {e}"
                    )
                    return None
