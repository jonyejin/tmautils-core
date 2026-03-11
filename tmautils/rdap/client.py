# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu
#
# IANA bootstrap logic, TLD overrides and RIR fallbacks adapted from whoisit
# (https://github.com/meeb/whoisit) by meeb.
# Original Copyright (c) meeb, licensed under BSD 3-Clause License.

import asyncio
import json
import random
from ipaddress import ip_address
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit
import idna

import aiohttp
from pytricia import PyTricia

from tmautils.common import AsyncRateLimiter, IOHelper, parse_asn, run_coro_sync
from tmautils.web import RetryConfig
from tmautils.web.http import request_with_retry

from ._parser import (
    parse_autnum_response,
    parse_domain_response,
    parse_entity_response,
    parse_error_response,
    parse_ip_network_response,
    parse_nameserver_response,
)
from .types import (
    AutnumQueryResult,
    BootstrapError,
    DomainQueryResult,
    EntityQueryResult,
    ErrorResponse,
    IPNetworkQueryResult,
    NameserverQueryResult,
    QueryError,
    RateLimitedError,
    RemoteServerError,
    ResourceAccessDeniedError,
    ResourceDoesNotExist,
)


_IANA_OVERRIDES: dict[str, list[str]] = {
    # ---------------------------------------------------------------------------
    # IANA bootstrap endpoint overrides for TLDs with incorrect/missing entries.
    # Adapted from whoisit/overrides.py. Last updated: 2026-01-05 03:46:00 UTC
    # ---------------------------------------------------------------------------

    "ac": ["https://rdap.identitydigital.services/rdap/"],
    "ag": ["https://rdap.identitydigital.services/rdap/"],
    "bh": ["https://rdap.centralnic.com/bh/"],
    "bz": ["https://rdap.identitydigital.services/rdap/"],
    "ch": ["https://rdap.nic.ch/"],
    "co": ["https://rdap.registry.co/co/"],
    "de": ["https://rdap.denic.de/"],
    "gl": ["https://rdap.centralnic.com/gl/"],
    "io": ["https://rdap.identitydigital.services/rdap/"],
    "lc": ["https://rdap.identitydigital.services/rdap/"],
    "li": ["https://rdap.nic.li/"],
    "me": ["https://rdap.identitydigital.services/rdap/"],
    "mn": ["https://rdap.identitydigital.services/rdap/"],
    "my": ["https://rdap.mynic.my/rdap/"],
    "pr": ["https://rdap.identitydigital.services/rdap/"],
    "sc": ["https://rdap.identitydigital.services/rdap/"],
    "sh": ["https://rdap.identitydigital.services/rdap/"],
    "us": ["https://rdap.nic.us/"],
    "vc": ["https://rdap.identitydigital.services/rdap/"],
}

# Fallback RDAP endpoints for IP/ASN queries with no bootstrap match
_RIR_FALLBACK_ENDPOINTS: list[str] = [
    "https://rdap.arin.net/registry/",
    "https://rdap.db.ripe.net/",
    "https://rdap.apnic.net/",
]


def _to_alabel(domain: str) -> str:
    try:
        return idna.encode(domain, uts46=True).decode("ascii")
    except idna.core.IDNAError as exc:
        raise QueryError(f"Invalid domain name '{domain}': {exc}") from exc


# RDAP list-of-dict keys and the field used to deduplicate when merging
# a related response into the primary response.
_LIST_DEDUP_KEYS: dict[str, str] = {
    "events": "eventAction",
    "notices": "title",
    "remarks": "title",
    "links": "href",
    "entities": "handle",
    "nameservers": "ldhName",
    "publicIds": "identifier",
}

# RDAP keys that are plain string lists; merged as order-preserving set unions.
_STRING_LIST_KEYS: frozenset[str] = frozenset({"status", "rdapConformance"})


def _merge_rdap_response(base: dict, related: dict) -> None:
    """
    Merge a related RDAP response into *base*, modifying it in place.

    List-type keys with known dedup keys (events, links, entities, etc.)
    are merged and deduplicated. String-list keys (status, rdapConformance)
    are merged as set unions. Dict values are merged recursively; scalar
    values in `related` overwrite `base` only when truthy.
    """
    for key, value in related.items():
        # Nested dicts (e.g. secureDNS): recurse
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _merge_rdap_response(base[key], value)
        # Plain string lists (status, rdapConformance): order-preserving set union
        elif key in _STRING_LIST_KEYS and isinstance(value, list):
            existing = base.get(key) or []
            base[key] = list(dict.fromkeys(existing + value))
        # List-of-dict keys (events, links, entities, etc.): merge and deduplicate
        elif key in _LIST_DEDUP_KEYS and isinstance(value, list):
            existing = base.get(key) or []
            base[key] = _merge_dedup_lists(
                existing, value, key=_LIST_DEDUP_KEYS[key]
            )
        # Unknown list keys: concatenate to avoid silent data loss
        elif isinstance(value, list) and isinstance(base.get(key), list):
            base[key] = base[key] + value
        # Scalars: related overwrites base only when truthy
        elif value:
            base[key] = value


def _merge_dedup_lists(
    l1: list, l2: list, *, key: str = "title",
) -> list:
    merged: dict[str, dict] = {}
    no_key: list = []
    for item in l1:
        if not isinstance(item, dict):
            no_key.append(item)
            continue
        k = item.get(key)
        if k is not None:
            merged[k] = item
        else:
            no_key.append(item)
    for item in l2:
        if not isinstance(item, dict):
            no_key.append(item)
            continue
        k = item.get(key)
        if k is not None:
            merged[k] = item
        else:
            no_key.append(item)
    return list(merged.values()) + no_key


class RdapClient:
    """
    RDAP client for domain, IP, ASN, entity, and nameserver queries.

    Args:
        rate_limiter: Rate limiter for RDAP requests.
            Limits are applied per-host (per RDAP server).
            If None, no rate limiting is applied.
        request_timeout: Timeout per HTTP request attempt in seconds.
            Default is 10 seconds.
        retry_config: Retry configuration for RDAP queries.
            If None, uses default :class:`RetryConfig`.
        proxy: HTTP/SOCKS proxy URL for all RDAP requests
            (e.g. `http://user:pass@proxy.example.com:8080`).
            Passed through to :func:`aiohttp.ClientSession.request`.
        overrides: Apply IANA endpoint overrides for TLDs with issues.
            Default is True.
        use_rir_fallbacks: Use RIR fallback endpoints for
            IP/ASN queries with no bootstrap match.
            Default is True.
        bootstrap_max_age_days: Maximum age of cached bootstrap data.
            Default is 7 days.
        follow_related: Default for following related/registration links.
            Default is True.
        related_retry_config: Retry configuration for related/registration link queries.
            If None, uses the same config as `retry_config`.
            Unused if `follow_related` is False.
        related_timeout: Wall-clock timeout in seconds for related/registration link queries.
            Covers the entire query including rate limiter wait time, retries, and backoff.
            If None, no timeout is applied.
            Unused if `follow_related` is False.
            Default is None.
        close_connection: Close the underlying connection after each request
            instead of returning it to the pool.
            Useful with rotating proxies to ensure a new exit IP per request.
            Default is False.
        working_root: Base directory where the namespace directory will be created.
            If None, the current working directory will be used.
        **kwargs: Additional arguments passed to IOHelper.
    """

    # IANA bootstrap URLs for all 5 registry types.
    _BOOTSTRAP_URLS: dict[str, str] = {
        "asn": "https://data.iana.org/rdap/asn.json",
        "dns": "https://data.iana.org/rdap/dns.json",
        "ipv4": "https://data.iana.org/rdap/ipv4.json",
        "ipv6": "https://data.iana.org/rdap/ipv6.json",
        "object": "https://data.iana.org/rdap/object-tags.json",
    }
    _RDAP_ACCEPT = "application/rdap+json"  # RFC 7480 §4.2
    _CACHE_FILE = "bootstrap_data.json"

    def __init__(
        self,
        *,
        rate_limiter: AsyncRateLimiter | None = None,
        request_timeout: float = 10.0,
        retry_config: RetryConfig | None = None,
        proxy: str | None = None,
        overrides: bool = True,
        use_rir_fallbacks: bool = True,
        bootstrap_max_age_days: int = 7,
        follow_related: bool = True,
        related_retry_config: RetryConfig | None = None,
        related_timeout: float | None = None,
        close_connection: bool = False,
        working_root: Path | None = None,
        **kwargs: Any,
    ) -> None:
        self._rate_limiter = rate_limiter or AsyncRateLimiter()
        self._request_timeout = request_timeout
        self._retry_config = retry_config or RetryConfig()
        self._proxy = proxy
        self._use_overrides = overrides
        self._use_rir_fallbacks = use_rir_fallbacks
        self._bootstrap_max_age_days = bootstrap_max_age_days
        self._follow_related = follow_related
        self._related_retry_config = related_retry_config or self._retry_config
        self._related_timeout = related_timeout or self._request_timeout
        self._close_connection = close_connection

        self._io_helper = IOHelper.init_with_dirs(
            self.__class__.__name__,
            dirs={"cache", "logs"},
            working_root=working_root,
            **kwargs,
        )

        # Bootstrap state
        self._bootstrap_timestamp: int = 0
        self._raw_data: dict[str, dict] = {k: {} for k in self._BOOTSTRAP_URLS}
        self._parsed_data: dict[str, dict] = {}
        # PyTricia tries for fast LPM IP lookups
        self._ipv4_trie: PyTricia | None = None
        self._ipv6_trie: PyTricia | None = None
        # Load bootstrap data from disk if possible
        self._try_load_cached_bootstrap()

        self._io_helper.logger.info(
            "RdapClient initialized: request_timeout=%ds, retry_config=%s",
            request_timeout, self._retry_config,
        )

    def _needs_bootstrap(self) -> bool:
        if not self._bootstrap_timestamp:
            return True
        age_days = (time() - self._bootstrap_timestamp) / 86400
        return age_days > self._bootstrap_max_age_days

    @property
    def _cache_path(self) -> Path:
        return self._io_helper.cache / self._CACHE_FILE

    def _try_load_cached_bootstrap(self) -> None:
        path = self._cache_path
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text())
            ts = raw.get("timestamp")
            if not isinstance(ts, int):
                raise ValueError("Missing or invalid timestamp")
            age_days = (int(time()) - ts) / 86400
            if age_days > self._bootstrap_max_age_days:
                self._io_helper.logger.info(
                    "Cached bootstrap data is %.1f days old (max %d), will re-fetch",
                    age_days, self._bootstrap_max_age_days,
                )
                return

            for name in self._BOOTSTRAP_URLS:
                if name not in raw:
                    raise ValueError(f"Missing registry data: {name}")
                self._raw_data[name] = raw[name]
            self._bootstrap_timestamp = ts
            self._parse_bootstrap_data()
            self._io_helper.logger.info(
                "Loaded bootstrap data from cache (%.1f days old)", age_days,
            )
        except Exception as e:
            self._io_helper.logger.warning(
                "Failed to load cached bootstrap data: %s", e,
            )
            self._bootstrap_timestamp = 0

    def _save_bootstrap_cache(self) -> None:
        try:
            data: dict[str, Any] = {"timestamp": self._bootstrap_timestamp}
            data.update(self._raw_data)
            self._cache_path.write_text(json.dumps(data))
            self._io_helper.logger.debug("Saved bootstrap data to cache")
        except Exception as e:
            self._io_helper.logger.warning(
                "Failed to save bootstrap cache: %s", e
            )

    async def bootstrap(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        force: bool = False,
    ) -> None:
        """
        Download IANA bootstrap data and cache to disk.

        Downloads all 5 IANA registries (DNS, ASN, IPv4, IPv6, object-tags).
        No-op when cached data is fresh unless `force` is `True`.

        Args:
            session: aiohttp session for HTTP requests.
                If ``None``, a temporary session is created.
            force: Force re-download even if cached data is fresh.
        """
        if not force and not self._needs_bootstrap():
            return
        if session is None:
            async with aiohttp.ClientSession() as s:
                await self._do_bootstrap(s)
        else:
            await self._do_bootstrap(session)

    async def _do_bootstrap(self, session: aiohttp.ClientSession) -> None:
        self._io_helper.logger.debug("Downloading IANA bootstrap data")
        items_loaded: set[str] = set()
        expected = set(self._BOOTSTRAP_URLS)

        for name, url in self._BOOTSTRAP_URLS.items():
            self._io_helper.logger.debug("Fetching bootstrap: %s", url)
            async with request_with_retry(
                session,
                "GET",
                url,
                attempt_timeout=self._request_timeout,
                retry_config=self._retry_config,
                log_helper=self._io_helper.log_helper,
                proxy=self._proxy,
            ) as resp:
                if resp.status != 200:
                    raise BootstrapError(
                        f"Failed to download bootstrap data from {url}, "
                        f"got status {resp.status}"
                    )
                try:
                    data = await resp.json(content_type=None)
                except Exception as e:
                    raise BootstrapError(
                        f"Failed to parse bootstrap JSON from {url}: {e}"
                    ) from e
                if data:
                    self._raw_data[name] = data
                    items_loaded.add(name)

        if items_loaded != expected:
            missing = expected - items_loaded
            raise BootstrapError(
                f"Failed to load some bootstrap data, missing: {missing}"
            )

        self._bootstrap_timestamp = int(time())
        self._parse_bootstrap_data()
        self._save_bootstrap_cache()
        self._io_helper.logger.info(
            "Bootstrap complete: %d TLDs, %d ASN ranges, %d IPv4 prefixes, "
            "%d IPv6 prefixes, %d object-tags",
            len(self._parsed_data.get("dns", {})),
            len(self._parsed_data.get("asn", {})),
            len(self._parsed_data.get("ipv4", {})),
            len(self._parsed_data.get("ipv6", {})),
            len(self._parsed_data.get("object", {})),
        )

    def bootstrap_sync(self, *, force: bool = False) -> None:
        """Sync wrapper for :meth:`bootstrap`."""
        run_coro_sync(self.bootstrap(force=force))

    def _parse_bootstrap_data(self) -> None:
        """Parse raw IANA bootstrap JSON into fast-lookup structures."""
        self._parsed_data = {}
        parsers: dict[str, Any] = {
            "dns": self._parse_dns_services,
            "asn": self._parse_asn_services,
            "ipv4": self._parse_ip_services,
            "ipv6": self._parse_ip_services,
            "object": self._parse_object_services,
        }
        for name, parser in parsers.items():
            services = self._raw_data.get(name, {}).get("services", [])
            if not services:
                raise BootstrapError(
                    f"Unable to parse '{name}' bootstrap data: no 'services' found"
                )
            self._parsed_data[name] = parser(services)

        # Build PyTricia tries
        self._ipv4_trie = PyTricia(32)
        for prefix, urls in self._parsed_data.get("ipv4", {}).items():
            try:
                self._ipv4_trie.insert(prefix, urls)
            except Exception:
                pass
        self._ipv6_trie = PyTricia(128)
        for prefix, urls in self._parsed_data.get("ipv6", {}).items():
            try:
                self._ipv6_trie.insert(prefix, urls)
            except Exception:
                pass

    def _parse_dns_services(self, services: list[list]) -> dict[str, list[str]]:
        """TLD -> RDAP endpoint URLs."""
        parsed: dict[str, list[str]] = {}
        for selector, urls in services:
            validated = self._validate_rdap_urls(urls)
            if not validated:
                continue
            for tld in selector:
                parsed[tld.strip()] = validated
        return parsed

    def _parse_asn_services(self, services: list[list]) -> dict[tuple[int, int], list[str]]:
        """ASN range -> RDAP endpoint URLs."""
        parsed: dict[tuple[int, int], list[str]] = {}
        for selector, urls in services:
            validated = self._validate_rdap_urls(urls)
            if not validated:
                continue
            for asn_range in selector:
                parts = str(asn_range).split("-")
                if len(parts) == 1:
                    start = end = int(parts[0])
                elif len(parts) == 2:
                    start, end = int(parts[0]), int(parts[1])
                else:
                    continue
                parsed[(start, end)] = validated
        return parsed

    def _parse_ip_services(self, services: list[list]) -> dict[str, list[str]]:
        """IP prefix (string) -> RDAP endpoint URLs."""
        parsed: dict[str, list[str]] = {}
        for selector, urls in services:
            validated = self._validate_rdap_urls(urls)
            if not validated:
                continue
            for prefix in selector:
                parsed[prefix.strip()] = validated
        return parsed

    def _parse_object_services(self, services: list[list]) -> dict[str, list[str]]:
        """Service provider identifier -> RDAP endpoint URLs."""
        parsed: dict[str, list[str]] = {}
        for entry in services:
            if len(entry) != 3:
                continue
            identifiers, urls = entry[1], entry[2]
            validated = self._validate_rdap_urls(urls)
            if not validated:
                continue
            for ident in identifiers:
                parsed[str(ident).strip().upper()] = validated
        return parsed

    @staticmethod
    def _validate_rdap_urls(urls: list[str]) -> list[str]:
        """Filter to HTTPS-only URLs (RFC 7480 §3.4)."""
        return [
            u.strip()
            for u in urls
            if urlsplit(u.strip()).scheme.lower() == "https"
        ]

    def _get_dns_endpoints(self, tld: str) -> list[str]:
        if not self._bootstrap_timestamp:
            raise BootstrapError("No bootstrap data is loaded")
        if self._use_overrides:
            if override := _IANA_OVERRIDES.get(tld):
                return override
        if endpoints := self._parsed_data.get("dns", {}).get(tld):
            return endpoints
        raise BootstrapError(
            f"TLD '{tld}' has no known RDAP endpoint. Try with overrides=True."
        )

    def _get_ip_endpoints(self, address: str) -> list[str]:
        if not self._bootstrap_timestamp:
            raise BootstrapError("No bootstrap data is loaded")
        try:
            addr = ip_address(address.split("/")[0])
        except ValueError:
            raise QueryError(f"Invalid IP address: {address}")
        trie = self._ipv4_trie if addr.version == 4 else self._ipv6_trie
        if trie is None:
            raise BootstrapError("No bootstrap data is loaded")
        try:
            return trie[str(addr)]
        except KeyError:
            if self._use_rir_fallbacks:
                self._io_helper.logger.debug(
                    "No bootstrap match for IP %s, using RIR fallbacks",
                    address,
                )
                return list(_RIR_FALLBACK_ENDPOINTS)
            raise BootstrapError(
                f"No RDAP endpoint found for IP address {address}"
            )

    def _get_asn_endpoints(self, asn: int) -> list[str]:
        if not self._bootstrap_timestamp:
            raise BootstrapError("No bootstrap data is loaded")
        for (start, end), urls in self._parsed_data.get("asn", {}).items():
            if start <= asn <= end:
                return urls
        if self._use_rir_fallbacks:
            self._io_helper.logger.debug(
                "No bootstrap match for ASN %d, using RIR fallbacks", asn
            )
            return list(_RIR_FALLBACK_ENDPOINTS)
        raise BootstrapError(f"No RDAP endpoint found for ASN {asn}")

    def _get_entity_endpoints(self, handle: str) -> list[str]:
        if not self._bootstrap_timestamp:
            raise BootstrapError("No bootstrap data is loaded")
        object_data = self._parsed_data.get("object", {})
        parts = handle.upper().split("-")
        if len(parts) >= 2:
            # Try suffix ("ENTITY-ARIN" -> "ARIN")
            if endpoints := object_data.get(parts[-1]):
                return endpoints
            # Try prefix ("ARIN-ENTITY" -> "ARIN")
            if endpoints := object_data.get(parts[0]):
                return endpoints
        raise BootstrapError(
            f"No RDAP endpoint found for entity handle '{handle}'. "
            "Could not match handle prefix or suffix to any known registry."
        )

    @staticmethod
    def _parse_asn_input(value: str | int) -> int:
        try:
            return parse_asn(value)
        except ValueError as exc:
            raise QueryError(str(exc)) from exc

    def _build_domain_url(self, domain: str) -> str:
        """Build an RDAP query URL for `domain`."""
        domain = _to_alabel(domain.strip())
        parts = domain.split(".")
        if len(parts) < 2:
            raise QueryError(f'Failed to extract TLD from domain "{domain}"')
        try:
            # SLD first (e.g., "co.uk")
            tld = ".".join(parts[-2:])
            endpoints = self._get_dns_endpoints(tld)
        except BootstrapError:
            # TLD next
            tld = parts[-1]
            endpoints = self._get_dns_endpoints(tld)
        endpoint = random.choice(endpoints)
        return self._construct_url(endpoint, "domain", domain)

    def _build_ip_url(self, address: str) -> str:
        """Build an RDAP query URL for an IP address or prefix."""
        address = address.strip()
        endpoints = self._get_ip_endpoints(address)
        endpoint = random.choice(endpoints)
        return self._construct_url(endpoint, "ip", address)

    def _build_autnum_url(self, asn: int) -> str:
        """Build an RDAP query URL for an ASN."""
        endpoints = self._get_asn_endpoints(asn)
        endpoint = random.choice(endpoints)
        return self._construct_url(endpoint, "autnum", str(asn))

    def _build_entity_url(self, handle: str) -> str:
        """Build an RDAP query URL for an entity handle."""
        handle = handle.strip()
        endpoints = self._get_entity_endpoints(handle)
        endpoint = random.choice(endpoints)
        return self._construct_url(endpoint, "entity", handle)

    def _build_nameserver_url(self, name: str) -> str:
        """Build an RDAP query URL for a nameserver."""
        name = _to_alabel(name.strip())
        parts = name.split(".")
        if len(parts) < 2:
            raise QueryError(f'Failed to extract TLD from nameserver "{name}"')
        try:
            # SLD first (e.g., "co.uk")
            tld = ".".join(parts[-2:])
            endpoints = self._get_dns_endpoints(tld)
        except BootstrapError:
            # TLD next
            tld = parts[-1]
            endpoints = self._get_dns_endpoints(tld)
        endpoint = random.choice(endpoints)
        return self._construct_url(endpoint, "nameserver", name)

    @staticmethod
    def _construct_url(base_url: str, what: str, value: str) -> str:
        """Build RDAP query URL: ``base_url/what/value``."""
        if not base_url.endswith("/"):
            base_url += "/"
        resource = urljoin(base_url, str(what))
        if not resource.endswith("/"):
            resource += "/"
        return unquote(urljoin(resource, quote(str(value))))

    async def _rdap_get(
        self,
        session: aiohttp.ClientSession,
        url: str,
        retry_config: RetryConfig | None = None,
    ) -> dict:
        try:
            async with request_with_retry(
                session,
                "GET",
                url,
                rate_limiter=self._rate_limiter,
                attempt_timeout=self._request_timeout,
                retry_config=retry_config or self._retry_config,
                close_connection=self._close_connection,
                log_helper=self._io_helper.log_helper,
                headers={"Accept": self._RDAP_ACCEPT},
                proxy=self._proxy,
            ) as resp:
                text = await resp.text()
                return self._process_response(resp, url, text)
        except QueryError:
            # Already wrapped by _process_response
            raise
        except Exception as exc:
            # Wrap in QueryError
            raise QueryError(
                f"RDAP GET {url} failed: {exc}",
            ) from exc

    @staticmethod
    def _process_response(
        resp: aiohttp.ClientResponse,
        url: str,
        text: str,
    ) -> dict:
        """Map HTTP response to a dict or raise the appropriate error."""
        status = resp.status

        status_map: dict[int, type[QueryError]] = {
            400: QueryError,
            401: ResourceAccessDeniedError,
            403: ResourceAccessDeniedError,
            404: ResourceDoesNotExist,
            422: ResourceAccessDeniedError,
            429: RateLimitedError,
        }

        if status in status_map:
            raise status_map[status](
                f"RDAP GET {url} returned {status}",
                status_code=status,
                response=text,
            )
        if 500 <= status < 600:
            raise RemoteServerError(
                f"RDAP GET {url} returned {status}",
                status_code=status,
                response=text,
            )
        if status != 200:
            raise QueryError(
                f"RDAP GET {url} returned non-200 status {status}",
                status_code=status,
                response=text,
            )

        try:
            data = json.loads(text)
        except (TypeError, ValueError) as e:
            raise QueryError(
                f"Failed to parse RDAP response as JSON: {e}",
                response=text,
            ) from e
        if not isinstance(data, dict):
            raise QueryError(
                f"RDAP response is not a JSON object (got {type(data).__name__})",
                response=text,
            )
        return data

    @staticmethod
    def _try_parse_error_body(text: str) -> ErrorResponse | None:
        """Attempt to parse an RDAP error response body (RFC 9083 §6)."""
        try:
            raw = json.loads(text)
            if isinstance(raw, dict) and "errorCode" in raw:
                return parse_error_response(raw)
        except Exception:
            pass
        return None

    async def _execute_query(
        self,
        session: aiohttp.ClientSession,
        url: str,
        follow_related: bool,
    ) -> tuple[dict | None, list[str], QueryError | None, ErrorResponse | None]:
        try:
            raw = await self._rdap_get(session, url)
        except QueryError as exc:
            error_resp = self._try_parse_error_body(exc.response)
            return None, [], exc, error_resp

        # Collect all related/registration URLs from the original response.
        related_urls: list[str] = []
        if follow_related:
            for link in raw.get("links", []):
                if not isinstance(link, dict):
                    continue
                rel = link.get("rel", "")
                if rel in ("related", "registration"):
                    href = link.get("href", "")
                    link_type = link.get("type") or ""
                    if href and not link_type.startswith("text/html"):
                        related_urls.append(href)

            # Follow the first one for richer data.
            if related_urls:
                try:
                    href = related_urls[0]
                    self._io_helper.logger.debug(
                        "Following related link: %s", href
                    )
                    coro = self._rdap_get(
                        session, href, self._related_retry_config,
                    )
                    if self._related_timeout is not None:
                        rel_raw = await asyncio.wait_for(
                            coro, timeout=self._related_timeout,
                        )
                    else:
                        rel_raw = await coro
                    if isinstance(rel_raw, dict):
                        _merge_rdap_response(raw, rel_raw)
                except Exception as e:
                    self._io_helper.logger.debug(
                        "Failed to follow related link %s: %s", href, e
                    )

        return raw, related_urls, None, None

    async def _do_query_domain(
        self,
        session: aiohttp.ClientSession,
        domain: str,
        follow_related: bool | None,
    ) -> DomainQueryResult:
        await self.bootstrap(session=session)
        follow = follow_related if follow_related is not None else self._follow_related
        url = self._build_domain_url(domain)
        self._io_helper.logger.debug(
            "Querying RDAP domain: %s -> %s", domain, url
        )

        try:
            raw, related_urls, error, error_resp = await self._execute_query(
                session, url, follow
            )
        except Exception as exc:
            return DomainQueryResult(
                domain=domain,
                primary_url=url,
                error=QueryError(f"Unexpected error querying {domain}: {exc}"),
            )
        if error is not None:
            return DomainQueryResult(
                domain=domain,
                primary_url=url,
                error=error,
                error_response=error_resp
            )

        parsed = None
        try:
            parsed = parse_domain_response(raw)
        except Exception as exc:
            self._io_helper.logger.warning(
                "Failed to parse RDAP response for %s: %s", domain, exc
            )

        return DomainQueryResult(
            domain=domain,
            primary_url=url,
            related_urls=related_urls,
            parsed=parsed,
            raw=raw,
        )

    async def _do_query_ip(
        self,
        session: aiohttp.ClientSession,
        address: str,
        follow_related: bool | None,
    ) -> IPNetworkQueryResult:
        await self.bootstrap(session=session)
        follow = follow_related if follow_related is not None else self._follow_related
        url = self._build_ip_url(address)
        self._io_helper.logger.debug(
            "Querying RDAP IP: %s -> %s", address, url
        )

        try:
            raw, related_urls, error, error_resp = await self._execute_query(
                session, url, follow
            )
        except Exception as exc:
            return IPNetworkQueryResult(
                query=address,
                primary_url=url,
                error=QueryError(
                    f"Unexpected error querying {address}: {exc}"
                ),
            )
        if error is not None:
            return IPNetworkQueryResult(
                query=address,
                primary_url=url,
                error=error,
                error_response=error_resp
            )

        parsed = None
        try:
            parsed = parse_ip_network_response(raw)
        except Exception as exc:
            self._io_helper.logger.warning(
                "Failed to parse RDAP IP response for %s: %s", address, exc
            )

        return IPNetworkQueryResult(
            query=address,
            primary_url=url,
            related_urls=related_urls,
            parsed=parsed,
            raw=raw,
        )

    async def _do_query_asn(
        self,
        session: aiohttp.ClientSession,
        asn: int,
        follow_related: bool | None,
    ) -> AutnumQueryResult:
        await self.bootstrap(session=session)
        follow = follow_related if follow_related is not None else self._follow_related
        url = self._build_autnum_url(asn)
        self._io_helper.logger.debug("Querying RDAP ASN: %d -> %s", asn, url)

        try:
            raw, related_urls, error, error_resp = await self._execute_query(
                session, url, follow
            )
        except Exception as exc:
            return AutnumQueryResult(
                asn=asn,
                primary_url=url,
                error=QueryError(f"Unexpected error querying AS {asn}: {exc}"),
            )
        if error is not None:
            return AutnumQueryResult(
                asn=asn,
                primary_url=url,
                error=error,
                error_response=error_resp
            )

        parsed = None
        try:
            parsed = parse_autnum_response(raw)
        except Exception as exc:
            self._io_helper.logger.warning(
                "Failed to parse RDAP autnum response for AS%d: %s", asn, exc
            )

        return AutnumQueryResult(
            asn=asn,
            primary_url=url,
            related_urls=related_urls,
            parsed=parsed,
            raw=raw,
        )

    async def _do_query_entity(
        self,
        session: aiohttp.ClientSession,
        handle: str,
        follow_related: bool | None,
    ) -> EntityQueryResult:
        await self.bootstrap(session=session)
        follow = follow_related if follow_related is not None else self._follow_related
        url = self._build_entity_url(handle)
        self._io_helper.logger.debug(
            "Querying RDAP entity: %s -> %s", handle, url
        )

        try:
            raw, related_urls, error, error_resp = await self._execute_query(
                session, url, follow
            )
        except Exception as exc:
            return EntityQueryResult(
                handle=handle,
                primary_url=url,
                error=QueryError(
                    f"Unexpected error querying entity {handle}: {exc}"),
            )
        if error is not None:
            return EntityQueryResult(
                handle=handle,
                primary_url=url,
                error=error,
                error_response=error_resp
            )

        parsed = None
        try:
            parsed = parse_entity_response(raw)
        except Exception as exc:
            self._io_helper.logger.warning(
                "Failed to parse RDAP entity response for %s: %s", handle, exc
            )

        return EntityQueryResult(
            handle=handle,
            primary_url=url,
            related_urls=related_urls,
            parsed=parsed,
            raw=raw,
        )

    async def _do_query_nameserver(
        self,
        session: aiohttp.ClientSession,
        name: str,
        follow_related: bool | None,
    ) -> NameserverQueryResult:
        await self.bootstrap(session=session)
        follow = follow_related if follow_related is not None else self._follow_related
        url = self._build_nameserver_url(name)
        self._io_helper.logger.debug(
            "Querying RDAP nameserver: %s -> %s", name, url
        )

        try:
            raw, related_urls, error, error_resp = await self._execute_query(
                session, url, follow
            )
        except Exception as exc:
            return NameserverQueryResult(
                nameserver=name,
                primary_url=url,
                error=QueryError(
                    f"Unexpected error querying nameserver {name}: {exc}"),
            )
        if error is not None:
            return NameserverQueryResult(
                nameserver=name,
                primary_url=url,
                error=error,
                error_response=error_resp
            )

        parsed = None
        try:
            parsed = parse_nameserver_response(raw)
        except Exception as exc:
            self._io_helper.logger.warning(
                "Failed to parse RDAP nameserver response for %s: %s", name, exc
            )

        return NameserverQueryResult(
            nameserver=name,
            primary_url=url,
            related_urls=related_urls,
            parsed=parsed,
            raw=raw,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def query_domain(
        self,
        domain: str,
        *,
        session: aiohttp.ClientSession | None = None,
        follow_related: bool | None = None,
    ) -> DomainQueryResult:
        """
        Query RDAP for a single domain.

        RDAP errors (4xx/5xx) are captured in
        :attr:`DomainQueryResult.error` rather than raised.
        Other errors propagate.

        Args:
            domain: Domain name to query.
            session: aiohttp session.
                If `None`, a temporary session is created.
            follow_related: Follow related/registration links for richer data.
                Defaults to the instance-level setting.
        """
        if session is None:
            async with aiohttp.ClientSession() as s:
                return await self._do_query_domain(s, domain, follow_related)
        return await self._do_query_domain(session, domain, follow_related)

    def query_domain_sync(
        self,
        domain: str,
        *,
        follow_related: bool | None = None,
    ) -> DomainQueryResult:
        """Sync wrapper for :meth:`query_domain`."""
        return run_coro_sync(self.query_domain(domain, follow_related=follow_related))

    async def query_ip(
        self,
        address: str,
        *,
        session: aiohttp.ClientSession | None = None,
        follow_related: bool | None = None,
    ) -> IPNetworkQueryResult:
        """
        Query RDAP for an IP address or prefix.

        Args:
            address: IPv4/IPv6 address or CIDR prefix
                (e.g. `8.8.8.8` or `2001:db8::/32`).
            session: aiohttp session.
                If `None`, a temporary session is created.
            follow_related: Follow related/registration links for richer data.
                Defaults to the instance-level setting.
        """
        if session is None:
            async with aiohttp.ClientSession() as s:
                return await self._do_query_ip(s, address, follow_related)
        return await self._do_query_ip(session, address, follow_related)

    def query_ip_sync(
        self,
        address: str,
        *,
        follow_related: bool | None = None,
    ) -> IPNetworkQueryResult:
        """Sync wrapper for :meth:`query_ip`."""
        return run_coro_sync(self.query_ip(address, follow_related=follow_related))

    async def query_asn(
        self,
        asn: str | int,
        *,
        session: aiohttp.ClientSession | None = None,
        follow_related: bool | None = None,
    ) -> AutnumQueryResult:
        """
        Query RDAP for an autonomous system number.

        Args:
            asn: ASN as `AS12345`, `12345`, or `12345`.
            session: aiohttp session.
                If `None`, a temporary session is created.
            follow_related: Follow related/registration links for richer data.
                Defaults to the instance-level setting.
        """
        parsed_asn = self._parse_asn_input(asn)
        if session is None:
            async with aiohttp.ClientSession() as s:
                return await self._do_query_asn(s, parsed_asn, follow_related)
        return await self._do_query_asn(session, parsed_asn, follow_related)

    def query_asn_sync(
        self,
        asn: str | int,
        *,
        follow_related: bool | None = None,
    ) -> AutnumQueryResult:
        """Sync wrapper for :meth:`query_asn`."""
        return run_coro_sync(self.query_asn(asn, follow_related=follow_related))

    async def query_entity(
        self,
        handle: str,
        *,
        session: aiohttp.ClientSession | None = None,
        follow_related: bool | None = None,
    ) -> EntityQueryResult:
        """
        Query RDAP for an entity by handle.

        Uses object-tags bootstrap to resolve the handle's registry
        via suffix matching (e.g. `ADMIN2521-ARIN` -> ARIN).

        Args:
            handle: Entity handle (e.g. `GOGL-ARIN`).
            session: aiohttp session.
                If `None`, a temporary session is created.
            follow_related: Follow related/registration links for richer data.
                Defaults to the instance-level setting.
        """
        if session is None:
            async with aiohttp.ClientSession() as s:
                return await self._do_query_entity(s, handle, follow_related)
        return await self._do_query_entity(session, handle, follow_related)

    def query_entity_sync(
        self,
        handle: str,
        *,
        follow_related: bool | None = None,
    ) -> EntityQueryResult:
        """Sync wrapper for :meth:`query_entity`."""
        return run_coro_sync(self.query_entity(handle, follow_related=follow_related))

    async def query_nameserver(
        self,
        name: str,
        *,
        session: aiohttp.ClientSession | None = None,
        follow_related: bool | None = None,
    ) -> NameserverQueryResult:
        """
        Query RDAP for a nameserver by hostname.

        Uses DNS bootstrap to resolve the nameserver's TLD to a registry.

        Args:
            name: Nameserver hostname (e.g. `ns1.google.com`).
            session: aiohttp session.
                If `None`, a temporary session is created.
            follow_related: Follow related/registration links for richer data.
                Defaults to the instance-level setting.
        """
        if session is None:
            async with aiohttp.ClientSession() as s:
                return await self._do_query_nameserver(s, name, follow_related)
        return await self._do_query_nameserver(session, name, follow_related)

    def query_nameserver_sync(
        self,
        name: str,
        *,
        follow_related: bool | None = None,
    ) -> NameserverQueryResult:
        """Sync wrapper for :meth:`query_nameserver`."""
        return run_coro_sync(self.query_nameserver(name, follow_related=follow_related))
