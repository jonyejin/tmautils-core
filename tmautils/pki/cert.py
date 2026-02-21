# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from typing import Optional
from pathlib import Path
import ssl
import socket
import cryptography.x509 as x509
from cryptography import x509 as x509_module
from cryptography.x509.oid import ExtensionOID
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs7
import certifi
import aiohttp
import asyncio
import contextlib

from tmautils.common import (
    IPAddress,
    run_coro_sync,
    LogHelper,
    get_logger_from_helper,
    AsyncRateLimiter,
)
from tmautils.web import request_with_retry

from ._crypto import get_cache_key, parse_cert_lrucached
from .types import ExtensionMissingError


def create_ssl_context(
    *,
    verify: bool = True,
    use_certifi: bool = True,
) -> ssl.SSLContext:
    """
    Create a reusable SSL context for certificate fetching.

    Args:
        verify: Whether to verify the server's TLS certificate.
            Default is True.
        use_certifi: Whether to use the `certifi` CA bundle for verification.
            If False, the system's default CA bundle is used.
            Ignored if `verify` is False.
            Default is True.

    Returns:
        An `ssl.SSLContext` configured for TLS client connections.
    """
    if verify:
        # default context sets CERT_REQUIRED and check_hostname=True
        cafile = certifi.where() if use_certifi else None
        ctx = ssl.create_default_context(cafile=cafile)
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get_cert_leaf_or_chain(
    host: str | IPAddress,
    port: int,
    sni: Optional[str],
    timeout: float,
    ssl_context: ssl.SSLContext,
    chain: bool,
):
    connect_host = str(host)
    server_hostname = str(sni) if sni is not None else connect_host

    with socket.create_connection((connect_host, port), timeout=timeout) as sock:
        with ssl_context.wrap_socket(sock, server_hostname=server_hostname) as sslsock:
            if chain:
                der_list = sslsock.get_verified_chain()  # works for verify=False too
                return [
                    x509.load_der_x509_certificate(der) for der in der_list
                ]
            else:
                der = sslsock.getpeercert(binary_form=True)
                return x509.load_der_x509_certificate(der)


def get_cert_sync(
    host: str | IPAddress,
    port: int = 443,
    *,
    sni: Optional[str] = None,
    timeout: float = 8.0,
    ssl_context: ssl.SSLContext | None = None,
    verify: bool = True,
    use_certifi: bool = True,
):
    """
    Retrieve the TLS certificate from a server (sync version).

    See `get_cert()` for async version with additional options.

    Args:
        host:
            The hostname or IP address of the server.

        port:
            The port number to connect to. Default is 443.

        sni:
            The Server Name Indication (SNI) to use. If None, `host` is used.
            Useful when connecting to an IP address but the certificate is for a domain name.

        timeout:
            Connection timeout in seconds. Default is 8.0 seconds.

        ssl_context:
            A pre-configured SSL context to use for the connection.
            If provided, `verify` and `use_certifi` are ignored.

        verify:
            Whether to verify the server's TLS certificate. Default is True.
            Ignored if `ssl_context` is provided.

        use_certifi:
            Whether to use the `certifi` CA bundle for verification. Default is True.
            If False, the system's default CA bundle is used.
            Ignored if `verify` is False or `ssl_context` is provided.

    Returns:
        An `x509.Certificate` object representing the server's TLS certificate.
    """
    ctx = ssl_context if ssl_context is not None else create_ssl_context(
        verify=verify, use_certifi=use_certifi
    )

    return _get_cert_leaf_or_chain(
        host=host, port=port, sni=sni,
        timeout=timeout,
        ssl_context=ctx,
        chain=False,
    )


def get_cert_chain_sync(
    host: str | IPAddress,
    port: int = 443,
    *,
    sni: Optional[str] = None,
    timeout: float = 8.0,
    ssl_context: ssl.SSLContext | None = None,
    verify: bool = True,
    use_certifi: bool = True,
) -> list[x509.Certificate]:
    """
    Retrieve the TLS certificate chain from a server (sync version).

    See `get_cert_chain()` for async version with additional options.

    Args:
        host:
            The hostname or IP address of the server.

        port:
            The port number to connect to. Default is 443.

        sni:
            The Server Name Indication (SNI) to use. If None, `host` is used.
            Useful when connecting to an IP address but the certificate is for a domain name.

        timeout:
            Connection timeout in seconds. Default is 8.0 seconds.

        ssl_context:
            A pre-configured SSL context to use for the connection.
            If provided, `verify` and `use_certifi` are ignored.

        verify:
            Whether to verify the server's TLS certificate. Default is True.
            Ignored if `ssl_context` is provided.

        use_certifi:
            Whether to use the `certifi` CA bundle for verification. Default is True.
            If False, the system's default CA bundle is used.
            Ignored if `verify` is False or `ssl_context` is provided.

    Returns:
        A list of `x509.Certificate` objects representing the server's TLS certificate chain.
    """
    ctx = ssl_context if ssl_context is not None else create_ssl_context(
        verify=verify, use_certifi=use_certifi
    )

    return _get_cert_leaf_or_chain(
        host=host, port=port, sni=sni,
        timeout=timeout,
        ssl_context=ctx,
        chain=True,
    )


async def _get_cert_leaf_or_chain_async(
    host: str | IPAddress,
    port: int,
    sni: Optional[str],
    timeout: float,
    ssl_handshake_timeout: float,
    ssl_context: ssl.SSLContext,
    chain: bool,
):
    connect_host = str(host)
    server_hostname = str(sni) if sni is not None else connect_host

    writer = None
    try:
        async with asyncio.timeout(timeout):
            try:
                _, writer = await asyncio.open_connection(
                    connect_host,
                    port,
                    ssl=ssl_context,
                    server_hostname=server_hostname,
                    ssl_handshake_timeout=ssl_handshake_timeout,
                )

                sslobj: ssl.SSLObject | None = writer.get_extra_info(
                    "ssl_object"
                )
                if sslobj is None:
                    raise RuntimeError("TLS handshake did not complete")

                if chain:
                    der_list = sslobj.get_verified_chain()  # works for verify=False too
                    return [
                        x509.load_der_x509_certificate(der) for der in der_list
                    ]
                else:
                    der = sslobj.getpeercert(binary_form=True)
                    return x509.load_der_x509_certificate(der)
            finally:
                if writer is not None:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"get_cert_[chain_]async() for {host}:{port} timed out after {timeout}s"
        )


async def get_cert(
    host: str | IPAddress,
    port: int = 443,
    *,
    sni: Optional[str] = None,
    timeout: float = 8.0,
    ssl_handshake_timeout: float = 5.0,
    ssl_context: ssl.SSLContext | None = None,
    verify: bool = True,
    use_certifi: bool = True,
):
    """
    Asynchronously retrieve the TLS certificate from a server.

    See `get_cert_sync()` for sync version.

    Args:
        host:
            The hostname or IP address of the server.

        port:
            The port number to connect to. Default is 443.

        sni:
            The Server Name Indication (SNI) to use. If None, `host` is used.
            Useful when connecting to an IP address but the certificate is for a domain name.

        timeout:
            Connection timeout in seconds. Default is 8.0 seconds.

        ssl_handshake_timeout:
            Timeout for the SSL/TLS handshake in seconds. Default is 5.0 seconds.

        ssl_context:
            A pre-configured SSL context to use for the connection.
            If provided, `verify` and `use_certifi` are ignored.

        verify:
            Whether to verify the server's TLS certificate. Default is True.
            Ignored if `ssl_context` is provided.

        use_certifi:
            Whether to use the `certifi` CA bundle for verification. Default is True.
            If False, the system's default CA bundle is used.
            Ignored if `verify` is False or `ssl_context` is provided.

    Returns:
        An `x509.Certificate` object representing the server's TLS certificate.
    """
    ctx = ssl_context if ssl_context is not None else create_ssl_context(
        verify=verify, use_certifi=use_certifi
    )

    return await _get_cert_leaf_or_chain_async(
        host=host, port=port, sni=sni,
        timeout=timeout,
        ssl_handshake_timeout=ssl_handshake_timeout,
        ssl_context=ctx,
        chain=False,
    )


async def get_cert_chain(
    host: str | IPAddress,
    port: int = 443,
    *,
    sni: Optional[str] = None,
    timeout: float = 8.0,
    ssl_handshake_timeout: float = 5.0,
    ssl_context: ssl.SSLContext | None = None,
    verify: bool = True,
    use_certifi: bool = True,
) -> list[x509.Certificate]:
    """
    Asynchronously retrieve the TLS certificate chain from a server.

    See `get_cert_chain_sync()` for sync version.

    Args:
        host:
            The hostname or IP address of the server.

        port:
            The port number to connect to. Default is 443.

        sni:
            The Server Name Indication (SNI) to use. If None, `host` is used.
            Useful when connecting to an IP address but the certificate is for a domain name.

        timeout:
            Connection timeout in seconds. Default is 8.0 seconds.

        ssl_handshake_timeout:
            Timeout for the SSL/TLS handshake in seconds. Default is 5.0 seconds.

        ssl_context:
            A pre-configured SSL context to use for the connection.
            If provided, `verify` and `use_certifi` are ignored.

        verify:
            Whether to verify the server's TLS certificate. Default is True.
            Ignored if `ssl_context` is provided.

        use_certifi:
            Whether to use the `certifi` CA bundle for verification. Default is True.
            If False, the system's default CA bundle is used.
            Ignored if `verify` is False or `ssl_context` is provided.

    Returns:
        A list of `x509.Certificate` objects representing the server's TLS certificate chain.
    """
    ctx = ssl_context if ssl_context is not None else create_ssl_context(
        verify=verify, use_certifi=use_certifi
    )

    return await _get_cert_leaf_or_chain_async(
        host=host, port=port, sni=sni,
        timeout=timeout,
        ssl_handshake_timeout=ssl_handshake_timeout,
        ssl_context=ctx,
        chain=True,
    )


class IssuerFetchError(Exception):
    """Error fetching issuer certificate."""
    pass


def _find_matching_cert_pkcs7(
    certs: list[x509.Certificate],
    expected_subject: x509.Name | None,
) -> x509.Certificate:
    if not certs:
        raise ValueError("PKCS#7 contains no certificates")

    if expected_subject is None or len(certs) == 1:
        return certs[0]

    for cert in certs:
        if cert.subject == expected_subject:
            return cert

    # Fallback to first if no match
    return certs[0]


def _parse_certificate(
    content: bytes,
    expected_subject: x509.Name | None = None,
) -> x509.Certificate:
    is_pem = content.strip().startswith(b"-----BEGIN")

    if is_pem:
        parsers = [
            x509.load_pem_x509_certificate,
            pkcs7.load_pem_pkcs7_certificates,
        ]
    else:
        parsers = [
            x509.load_der_x509_certificate,
            pkcs7.load_der_pkcs7_certificates,
        ]

    for parser in parsers:
        try:
            result = parser(content)
            # Handle single cert vs list from PKCS#7
            if isinstance(result, list):
                return _find_matching_cert_pkcs7(result, expected_subject)
            return result
        except (ValueError, IndexError):
            continue

    raise ValueError("Not a valid DER, PEM, or PKCS#7 certificate")


async def fetch_issuer_cert(
    cert: x509.Certificate,
    *,
    cache_dir: Path | None = None,
    session: aiohttp.ClientSession | None = None,
    timeout: float = 10.0,
    max_attempts: int = 3,
    rate_limiter: AsyncRateLimiter | None = None,
    log_helper: LogHelper | None = None,
) -> x509.Certificate:
    """
    Fetch issuer certificate via AIA extension (CA_ISSUERS).

    Args:
        cert: Certificate to fetch issuer for
        cache_dir: Directory to cache downloaded certs (Optional)
        session: Existing aiohttp session (creates one if not provided)
        timeout: Request timeout in seconds
        max_attempts: Retry attempts for failed downloads
        rate_limiter: Optional AsyncRateLimiter for HTTP request rate limiting
        log_helper: Optional LogHelper for logging

    Returns:
        Issuer certificate

    Raises:
        ExtensionMissingError: If cert lacks AIA or CA_ISSUERS entry
        IssuerFetchError: If download/parsing fails
    """
    logger = get_logger_from_helper(log_helper)

    # Extract AIA extension
    try:
        aia_ext = cert.extensions.get_extension_for_oid(
            ExtensionOID.AUTHORITY_INFORMATION_ACCESS
        )
    except x509.ExtensionNotFound:
        raise ExtensionMissingError(
            f"Certificate {cert.serial_number} lacks AIA extension"
        )
    except ValueError as e:
        raise IssuerFetchError(
            f"Parsing AIA extension failed for cert {cert.serial_number}: {e}"
        ) from e

    # Find CA Issuers URL
    issuer_url = None
    for desc in aia_ext.value:
        if desc.access_method == x509_module.oid.AuthorityInformationAccessOID.CA_ISSUERS:
            issuer_url = desc.access_location.value
            break

    if not issuer_url:
        raise ExtensionMissingError(
            f"Certificate {cert.serial_number} has no CA Issuers in AIA"
        )

    logger.debug("Issuer URL from AIA: %s", issuer_url)

    # Check cache
    if cache_dir is not None:
        cache_key_str = get_cache_key(issuer_url) + ".crt"
        cache_path = cache_dir / cache_key_str
        if cache_path.exists():
            try:
                issuer_cert = parse_cert_lrucached(cache_path.read_bytes())
                logger.debug("Issuer cert cache hit: %s", issuer_url)
                return issuer_cert
            except Exception:
                pass  # Cache corrupted, re-download
    else:
        cache_path = None

    logger.debug("Downloading issuer certificate: %s", issuer_url)

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()

    try:
        async with request_with_retry(
            session,
            "GET",
            issuer_url,
            rate_limiter=rate_limiter,
            attempt_timeout=timeout,
            max_attempts=max_attempts,
            log_helper=log_helper,
        ) as resp:
            resp.raise_for_status()
            content = await resp.read()
    except Exception as e:
        raise IssuerFetchError(
            f"Failed to download issuer cert from {issuer_url}: {e}"
        ) from e
    finally:
        if owns_session:
            await session.close()

    # Yield before CPU-bound parsing
    await asyncio.sleep(0)

    try:
        issuer_cert = _parse_certificate(content, expected_subject=cert.issuer)
    except ValueError as e:
        raise IssuerFetchError(
            f"Failed to parse issuer cert from {issuer_url}: {e}"
        ) from e

    # Cache to disk as DER if cache_dir is provided
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(
            issuer_cert.public_bytes(serialization.Encoding.DER)
        )
        logger.debug("Cached issuer cert: %s", cache_path)

    return issuer_cert


def fetch_issuer_cert_sync(
    cert: x509.Certificate,
    *,
    cache_dir: Path | None = None,
    timeout: float = 10.0,
    max_attempts: int = 3,
    log_helper: LogHelper | None = None,
) -> x509.Certificate:
    """
    Sync wrapper for fetch_issuer_cert().

    Note: Cannot pass an existing aiohttp session in sync mode.
    """
    return run_coro_sync(fetch_issuer_cert(
        cert,
        cache_dir=cache_dir,
        session=None,
        timeout=timeout,
        max_attempts=max_attempts,
        log_helper=log_helper,
    ))


async def fetch_issuer_chain(
    cert: x509.Certificate,
    *,
    max_depth: int = 10,
    cache_dir: Path | None = None,
    session: aiohttp.ClientSession | None = None,
    timeout: float = 10.0,
    max_attempts: int = 3,
    rate_limiter: AsyncRateLimiter | None = None,
    log_helper: LogHelper | None = None,
) -> list[x509.Certificate]:
    """
    Build certificate chain starting from the given certificate
    by fetching issuer certificates using AIA.

    This differs from `get_cert_chain()` which fetches the chain from a TLS
    handshake.

    Args:
        cert: Certificate to start from
        max_depth: Maximum chain length (prevents infinite loops)
        cache_dir: Directory to cache downloaded certs (Optional)
        session: Existing aiohttp session (creates one if not provided)
        timeout: Request timeout per fetch
        max_attempts: Retry attempts per download
        rate_limiter: Optional AsyncRateLimiter for HTTP request rate limiting
        log_helper: Optional LogHelper for logging

    Returns:
        Chain as [cert, issuer, issuer's issuer, ..., root]
        Stops at self-signed cert or max_depth.

    Raises:
        ExtensionMissingError: Certificate lacks AIA extension or CA_ISSUERS entry
        IssuerFetchError: Failed to download/parse issuer certificate,
            circular reference detected, or max depth exceeded
    """
    logger = get_logger_from_helper(log_helper)

    chain = [cert]
    seen_serials = {cert.serial_number}

    logger.debug(
        "Building certificate chain starting from %s",
        cert.serial_number
    )

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()

    try:
        for depth in range(max_depth):
            current_cert = chain[-1]

            # Check if current cert is self-signed (root)
            if current_cert.issuer == current_cert.subject:
                try:
                    current_cert.verify_directly_issued_by(current_cert)
                    logger.debug(
                        "Reached self-signed root certificate at depth %d", depth
                    )
                    break
                except Exception:
                    # Not a valid root; continue trying to fetch issuer
                    logger.warning(
                        "Certificate has issuer == subject "
                        "but invalid self-signature at depth %d",
                        depth
                    )

            # Fetch issuer
            try:
                issuer_cert = await fetch_issuer_cert(
                    current_cert,
                    cache_dir=cache_dir,
                    session=session,
                    timeout=timeout,
                    max_attempts=max_attempts,
                    rate_limiter=rate_limiter,
                    log_helper=log_helper,
                )
            except ExtensionMissingError as e:
                logger.debug(
                    "No AIA/CA_ISSUERS for cert %s at depth %d: %s",
                    current_cert.serial_number, depth, e
                )
                raise
            except IssuerFetchError as e:
                logger.warning(
                    "Failed to fetch issuer for cert %s at depth %d: %s",
                    current_cert.serial_number, depth, e
                )
                raise

            # Check for circular reference
            if issuer_cert.serial_number in seen_serials:
                raise IssuerFetchError(
                    f"Circular reference detected in certificate chain "
                    f"(serial {issuer_cert.serial_number})"
                )

            # Validate signature relationship before adding to chain
            try:
                current_cert.verify_directly_issued_by(issuer_cert)
            except Exception as e:
                raise IssuerFetchError(
                    f"Certificate {current_cert.serial_number} not properly signed by "
                    f"fetched issuer {issuer_cert.serial_number}: {e}"
                ) from e

            # Add issuer to chain
            chain.append(issuer_cert)
            seen_serials.add(issuer_cert.serial_number)

            logger.debug(
                "Added certificate %s to chain (depth %d)",
                issuer_cert.serial_number, depth + 1
            )
        else:
            # Loop exhausted without finding root
            raise IssuerFetchError(
                f"Maximum chain depth ({max_depth}) reached without finding root certificate"
            )
    finally:
        if owns_session:
            await session.close()

    logger.debug("Built certificate chain with %d certificates", len(chain))
    return chain


def fetch_issuer_chain_sync(
    cert: x509.Certificate,
    *,
    max_depth: int = 10,
    cache_dir: Path | None = None,
    timeout: float = 10.0,
    max_attempts: int = 3,
    log_helper: LogHelper | None = None,
) -> list[x509.Certificate]:
    """
    Sync wrapper for `fetch_issuer_chain()`.

    Note: Cannot pass an existing aiohttp session in sync mode.
    """
    return run_coro_sync(fetch_issuer_chain(
        cert,
        max_depth=max_depth,
        cache_dir=cache_dir,
        session=None,
        timeout=timeout,
        max_attempts=max_attempts,
        log_helper=log_helper,
    ))
