import asyncio
from ipaddress import ip_address

import dns.resolver
import dns.asyncresolver

from imresearchutils.common import *


class AsyncDnsPythonUtil:
    """
    Asynchronous DNS resolver using dnspython library.

    Args:
        nameservers (list[str]):
            List of DNS nameservers to use for resolution.
            Defaults to ["127.0.0.1"].

        max_concurrent_requests (int):
            Maximum number of concurrent DNS requests.
            Defaults to 500.

        cachesize (int):
            Size of the dnspython DNS cache.
            Defaults to 500000.

        data_dir (Path | None):
            Base directory for data files.
            If None, the current working directory will be used.

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
        nameservers: list[str] = ["127.0.0.1"],
        max_concurrent_requests: int = 500,
        cachesize: int = 500000,
        data_dir: Path | None = None,
        **kwargs
    ):
        self.io_helper = IOHelper(
            self.__class__.__name__,
            data_dir=data_dir,
            **kwargs,
        )

        # Set up resolver parameters
        self.resolver = dns.asyncresolver.Resolver(configure=False)
        self.resolver.nameservers = nameservers
        self.resolver.retry_servfail = True  # Not sure if we need this
        self.resolver.cache = dns.resolver.LRUCache(max_size=cachesize)
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
        aaaa: bool = True
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

        try:
            # Concurrently resolve A/AAAA records
            await_list = []
            if a:
                await_list.append(self.resolve_rdtype(domain, "A"))
            if aaaa:
                await_list.append(self.resolve_rdtype(domain, "AAAA"))
            results: list[list[str]] = await asyncio.gather(*await_list)

            # Convert results to IP addresses
            addresses = [
                ip_address(addr) for addr_list in results for addr in addr_list
            ]
            return addresses
        except (dns.resolver.NXDOMAIN,
                dns.resolver.NoAnswer,
                dns.resolver.NoNameservers,
                dns.resolver.LifetimeTimeout) as e:
            self.io_helper.logger.warning(
                f"DNS resolution failed for {domain}: {e}"
            )
            return []

    async def resolve_rdtype(
        self,
        domain: str,
        rdtype: str,
        raise_on_error: bool = False,
    ) -> list[str]:
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
            list[str]:
                List of resource records of the specified type.
                May be empty if no records are found or if an error occurs.
        """
        async with self._dnsreq_semaphore:
            try:
                response = await self.resolver.resolve(domain, rdtype)
                return [rdata.to_text() for rdata in response]
            except (dns.resolver.NXDOMAIN,
                    dns.resolver.NoAnswer,
                    dns.resolver.NoNameservers,
                    dns.resolver.LifetimeTimeout) as e:
                if raise_on_error:
                    raise e
                else:
                    self.io_helper.logger.warning(
                        f"DNS resolution failed for {domain} with type {rdtype}: {e}"
                    )
                    return []

    async def resolve_address(
        self,
        addr: IPv4Address | IPv6Address
    ) -> list[str]:
        """
        Reverse DNS resolution for an IP address.

        Args:
            addr (IPv4Address | IPv6Address):
                The IP address to resolve.

        Returns:
            list[str]:
                List of domain names associated with the IP address.
                May be empty if no names are found.
        """
        if addr.is_private:
            return []

        async with self._dnsreq_semaphore:
            try:
                return [rdata.to_text() for rdata in
                        await self.resolver.resolve_address(str(addr))]
            except (dns.resolver.NXDOMAIN,
                    dns.resolver.NoAnswer,
                    dns.resolver.NoNameservers,
                    dns.resolver.LifetimeTimeout) as e:
                self.io_helper.logger.warning(
                    f"DNS resolution failed for {addr}: {e}"
                )
                return []
