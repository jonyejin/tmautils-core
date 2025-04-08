import asyncio
from ipaddress import ip_address

import dns.resolver
import dns.asyncresolver

from imresearchutils.common import *


class AsyncDnsPythonUtil:
    def __init__(self,
                 nameservers: list[str] = ["127.0.0.1"],
                 max_concurrent_requests: int = 500,
                 cachesize: int = 500000,
                 data_dir: Path | None = None):
        self.io_helper = IOHelper(self.__class__.__name__, data_dir=data_dir)

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

    async def resolve_domain(self,
                             domain: str,
                             a: bool = True,
                             aaaa: bool = True) -> list[IPv4Address | IPv6Address]:
        if not a and not aaaa:
            return []

        async with self._dnsreq_semaphore:
            try:
                # Concurrently resolve A/AAAA records
                await_list = []
                if a:
                    await_list.append(self.resolver.resolve(domain, "A"))
                if aaaa:
                    await_list.append(self.resolver.resolve(domain, "AAAA"))
                results = await asyncio.gather(*await_list)

                # Convert results to IP addresses
                addresses = [
                    ip_address(rdata.to_text()) for rdata_list in results for rdata in rdata_list
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

    async def resolve_address(self, addr: IPv4Address | IPv6Address) -> list[str]:
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
