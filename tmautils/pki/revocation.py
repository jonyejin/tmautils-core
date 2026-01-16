"""
Certificate revocation checking via OCSP and CRL.

This module adapts logic from the pki-tools package
(https://github.com/fulder/pki-tools) by Michal Sadowski.

Original Copyright (c) 2021-2023 Michal Sadowski <misad90@gmail.com>
Licensed under the MIT License.
"""

from pathlib import Path
from typing import Any
import asyncio
import aiohttp
import cryptography.x509 as x509

from tmautils.common import IOHelper, run_coro_sync

from .types import (
    RevocationStatus,
    RevocationInfo,
    CheckMode,
    RevocationCheckError,
    OCSPError,
    CRLError,
    ExtensionMissingError,
)
from ._ocsp import OCSPHelper
from ._crl import CRLHelper
from .cert import fetch_issuer_cert, fetch_issuer_chain


class RevocationChecker:
    """
    Check certificate revocation status via OCSP and/or CRL.

    Args:
        request_timeout: Timeout for each HTTP request attempt in seconds.
            Default is 10.0 seconds.
        max_attempts: Maximum number of retry attempts for HTTP requests.
            Default is 3.
        max_concurrent: Maximum number of concurrent HTTP requests.
            Default is 20.
        working_root: Base directory for IOHelper. If None, uses default.
        **kwargs: Additional arguments passed to IOHelper.

    Example:
        Basic usage with single certificate:
        ```python
        from tmautils.pki import RevocationChecker, get_cert

        checker = RevocationChecker()
        cert = get_cert("example.com")

        # Automatically downloads issuer cert via AIA (Authority Information Access)
        status = checker.check_cert_sync(cert)

        if status.status == RevocationStatus.REVOKED:
            print("Certificate is revoked!")
        ```

        Using explicit issuer (avoids download):
        ```python
        from tmautils.pki import get_cert_chain

        chain = get_cert_chain("example.com")
        status = checker.check_cert_sync(chain[0], issuer=chain[1])
        ```
    """

    def __init__(
        self,
        *,
        request_timeout: float = 10.0,
        max_attempts: int = 3,
        max_concurrent: int = 20,
        working_root: Path | None = None,
        **kwargs: Any,
    ):
        # Store configuration
        self._request_timeout = request_timeout
        self._max_attempts = max_attempts
        self._max_concurrent = max_concurrent

        # Semaphore for limiting concurrent HTTP requests
        self._http_semaphore = asyncio.Semaphore(max_concurrent)

        # Initialize IOHelper
        self._io_helper = IOHelper(
            self.__class__.__name__,
            working_root=working_root,
            **kwargs,
        )

        # Create cache directories
        self._crl_cache_dir = self._io_helper.raw / "crl_cache"
        self._issuer_cache_dir = self._io_helper.raw / "issuer_cache"
        self._crl_cache_dir.mkdir(exist_ok=True, parents=True)
        self._issuer_cache_dir.mkdir(exist_ok=True, parents=True)

        # Initialize helper modules
        self._ocsp_helper = OCSPHelper(
            self._http_semaphore,
            request_timeout=request_timeout,
            max_attempts=max_attempts,
            log_helper=self._io_helper.log_helper,
        )

        self._crl_helper = CRLHelper(
            self._http_semaphore,
            crl_cache_dir=self._crl_cache_dir,
            issuer_cache_dir=self._issuer_cache_dir,
            request_timeout=request_timeout,
            max_attempts=max_attempts,
            log_helper=self._io_helper.log_helper,
        )

        self._io_helper.logger.info(
            "RevocationChecker initialized: "
            "request_timeout=%ds, max_attempts=%d, max_concurrent=%d",
            request_timeout, max_attempts, max_concurrent,
        )

    async def check_cert(
        self,
        cert: x509.Certificate,
        issuer: x509.Certificate | None = None,
        *,
        mode: CheckMode = CheckMode.OCSP_FALLBACK_CRL,
        verify_signature: bool = True,
        ocsp_issuer_chain: list[x509.Certificate] | None = None,
        crl_issuer_chain: list[x509.Certificate] | None = None,
    ) -> RevocationInfo:
        """
        Check revocation status of a single certificate.

        Args:
            cert: Certificate to check.
            issuer: Issuer certificate.
                Required for signature verification.
                If not provided, and verify_signature=True, it will be auto-fetched
                using AIA (Authority Information Access) extension.
            mode: Checking mode (`OCSP_ONLY`, `CRL_ONLY`, or `OCSP_FALLBACK_CRL`).
            verify_signature: When False, skip OCSP/CRL signature verification.
            ocsp_issuer_chain: Optional chain of certificates to search for delegated
                OCSP responder. Per RFC 6960, OCSP responses may be signed by a
                CA-designated responder instead of the CA itself.
            crl_issuer_chain: Optional chain of certificates to search for delegated
                CRL signer. Per RFC 5280, CRLs may be signed by a different authority.

        Returns:
            RevocationInfo with status and revocation information.

        Raises:
            ExtensionMissingError:
                Certificate lacks required extensions when issuer=None and verify_signature=True.
            OCSPError:
                OCSP check failed and mode is OCSP_ONLY.
            CRLError:
                CRL check failed and mode is CRL_ONLY.
            RevocationCheckError:
                Both OCSP and CRL checks failed (in OCSP_FALLBACK_CRL mode).

        Examples:
            Check with user-provided issuer:
            >>> cert = get_cert("example.com")
            >>> chain = get_cert_chain("example.com")
            >>> status = await checker.check_cert(cert, issuer=chain[1])

            Check with auto-fetched issuer:
            >>> status = await checker.check_cert(cert)

            Check without signature verification:
            >>> status = await checker.check_cert(cert, verify_signature=False)

            Check with issuer chain for delegated responder support:
            >>> status = await checker.check_cert(
            ...     cert, issuer=chain[1],
            ...     ocsp_issuer_chain=chain[1:],
            ...     crl_issuer_chain=chain[1:],
            ... )
        """
        async with aiohttp.ClientSession() as session:
            # If issuer not provided and verification needed, fetch it
            if issuer is None and verify_signature:
                self._io_helper.logger.debug(
                    f"Checking certificate {cert.serial_number} (auto-fetching issuer)"
                )
                async with self._http_semaphore:
                    issuer = await fetch_issuer_cert(
                        cert,
                        cache_dir=self._issuer_cache_dir,
                        session=session,
                        timeout=self._request_timeout,
                        max_attempts=self._max_attempts,
                        log_helper=self._io_helper.log_helper,
                    )
            elif issuer is None:
                # verify_signature=False, no issuer needed
                self._io_helper.logger.debug(
                    f"Checking certificate {cert.serial_number} (no signature verification)"
                )
                # Use self as dummy issuer (no verification will be done)
                issuer = cert
            else:
                self._io_helper.logger.debug(
                    f"Checking certificate {cert.serial_number} with provided issuer"
                )

            return await self._check_single_cert(
                cert, issuer, session, mode,
                verify_signature=verify_signature,
                ocsp_issuer_chain=ocsp_issuer_chain,
                crl_issuer_chain=crl_issuer_chain,
            )

    def check_cert_sync(
        self,
        cert: x509.Certificate,
        issuer: x509.Certificate | None = None,
        *,
        mode: CheckMode = CheckMode.OCSP_FALLBACK_CRL,
        verify_signature: bool = True,
        ocsp_issuer_chain: list[x509.Certificate] | None = None,
        crl_issuer_chain: list[x509.Certificate] | None = None,
    ) -> RevocationInfo:
        """
        Check revocation status of a single certificate (sync wrapper).

        See `check_cert()` for detailed documentation.
        """
        return run_coro_sync(
            self.check_cert(
                cert, issuer,
                mode=mode,
                verify_signature=verify_signature,
                ocsp_issuer_chain=ocsp_issuer_chain,
                crl_issuer_chain=crl_issuer_chain,
            )
        )

    async def check_chain(
        self,
        certs: list[x509.Certificate],
        *,
        mode: CheckMode = CheckMode.OCSP_FALLBACK_CRL,
        verify_signature: bool = True,
        ocsp_issuer_chain: list[x509.Certificate] | None = None,
        crl_issuer_chain: list[x509.Certificate] | None = None,
    ) -> dict[int, RevocationInfo]:
        """
        Check revocation status of all certificates in a provided chain.

        Args:
            certs: Certificate chain [leaf, intermediate(s), root]
            mode: Checking mode (OCSP_ONLY, CRL_ONLY, or OCSP_FALLBACK_CRL).
            verify_signature: When False, skip signature verification for all checks.
            ocsp_issuer_chain: Optional chain for delegated OCSP responder lookup.
                If not provided, uses remaining chain from each cert's position.
            crl_issuer_chain: Optional chain for delegated CRL signer lookup.
                If not provided, uses remaining chain from each cert's position.

        Returns:
            Dict mapping serial numbers to RevocationInfo

        Raises:
            ValueError: Chain has fewer than 2 certificates
            Various RevocationCheckErrors for individual certificate failures

        Examples:
            Check user-provided chain:
            >>> chain = get_cert_chain("example.com")
            >>> results = await checker.check_chain(chain)
            >>> for serial, status in results.items():
            ...     print(f"Cert {serial}: {status.status}")

            Check without signature verification:
            >>> results = await checker.check_chain(chain, verify_signature=False)
        """
        if not isinstance(certs, list):
            raise TypeError(f"certs must be list, got {type(certs)}")

        if len(certs) < 2:
            raise ValueError("certs list must have at least 2 certificates")

        self._io_helper.logger.info(
            f"Checking certificate chain ({len(certs)} certificates)"
        )

        async with aiohttp.ClientSession() as session:
            return await self._check_chain(
                certs, session, mode,
                verify_signature=verify_signature,
                ocsp_issuer_chain=ocsp_issuer_chain,
                crl_issuer_chain=crl_issuer_chain,
            )

    def check_chain_sync(
        self,
        certs: list[x509.Certificate],
        *,
        mode: CheckMode = CheckMode.OCSP_FALLBACK_CRL,
        verify_signature: bool = True,
        ocsp_issuer_chain: list[x509.Certificate] | None = None,
        crl_issuer_chain: list[x509.Certificate] | None = None,
    ) -> dict[int, RevocationInfo]:
        """
        Check revocation status of all certificates in a provided chain (sync wrapper).

        See check_chain() for detailed documentation.
        """
        return run_coro_sync(
            self.check_chain(
                certs,
                mode=mode,
                verify_signature=verify_signature,
                ocsp_issuer_chain=ocsp_issuer_chain,
                crl_issuer_chain=crl_issuer_chain,
            )
        )

    async def check_cert_chain(
        self,
        leaf: x509.Certificate,
        *,
        mode: CheckMode = CheckMode.OCSP_FALLBACK_CRL,
        verify_signature: bool = True,
        max_depth: int = 10,
    ) -> dict[int, RevocationInfo]:
        """
        Auto-fetch full chain from leaf and check all certificates.

        Recursively fetches issuer certificates via AIA to build the complete
        chain, then checks revocation status for each certificate.

        For delegated OCSP responders (RFC 6960 §4.2.2.2), responder certificates
        are automatically extracted from OCSP responses. The fetched certificate
        chain is also used for CRL issuer lookup.

        Args:
            leaf: Leaf certificate (chain built automatically)
            mode: Checking mode (OCSP_ONLY, CRL_ONLY, or OCSP_FALLBACK_CRL).
            verify_signature: When False, skip signature verification for all checks.
            max_depth: Maximum chain depth to prevent infinite loops.

        Returns:
            Dict mapping serial numbers to RevocationInfo

        Raises:
            ExtensionMissingError: A certificate lacks AIA extension
            RevocationCheckError: Chain building or checking failed

        Examples:
            Auto-fetch and check full chain:
            >>> cert = get_cert("example.com")
            >>> results = await checker.check_cert_chain(cert)

            Without signature verification:
            >>> results = await checker.check_cert_chain(cert, verify_signature=False)
        """
        self._io_helper.logger.info(
            f"Auto-fetching and checking certificate chain starting from {leaf.serial_number}"
        )

        async with aiohttp.ClientSession() as session:
            # Fetch full chain
            async with self._http_semaphore:
                chain = await fetch_issuer_chain(
                    leaf,
                    max_depth=max_depth,
                    cache_dir=self._issuer_cache_dir,
                    session=session,
                    timeout=self._request_timeout,
                    max_attempts=self._max_attempts,
                    log_helper=self._io_helper.log_helper,
                )

            # Check all certificates in chain
            return await self._check_chain(
                chain, session, mode,
                verify_signature=verify_signature,
            )

    def check_cert_chain_sync(
        self,
        leaf: x509.Certificate,
        *,
        mode: CheckMode = CheckMode.OCSP_FALLBACK_CRL,
        verify_signature: bool = True,
        max_depth: int = 10,
    ) -> dict[int, RevocationInfo]:
        """
        Auto-fetch full chain from leaf and check all certificates (sync wrapper).

        See check_cert_chain() for detailed documentation.
        """
        return run_coro_sync(
            self.check_cert_chain(
                leaf,
                mode=mode,
                verify_signature=verify_signature,
                max_depth=max_depth,
            )
        )

    async def _check_single_cert(
        self,
        cert: x509.Certificate,
        issuer: x509.Certificate,
        session: aiohttp.ClientSession,
        mode: CheckMode,
        *,
        verify_signature: bool = True,
        ocsp_issuer_chain: list[x509.Certificate] | None = None,
        crl_issuer_chain: list[x509.Certificate] | None = None,
    ) -> RevocationInfo:
        if mode == CheckMode.OCSP_ONLY:
            return await self._ocsp_helper.check(
                cert, issuer, session,
                verify_sig=verify_signature,
                issuer_chain=ocsp_issuer_chain,
            )

        elif mode == CheckMode.CRL_ONLY:
            return await self._crl_helper.check(
                cert, issuer, session,
                verify_sig=verify_signature,
                issuer_chain=crl_issuer_chain,
            )

        else:  # OCSP_FALLBACK_CRL
            try:
                return await self._ocsp_helper.check(
                    cert, issuer, session,
                    verify_sig=verify_signature,
                    issuer_chain=ocsp_issuer_chain,
                )
            except (OCSPError, ExtensionMissingError) as e:
                self._io_helper.logger.info(
                    f"OCSP check failed for cert {cert.serial_number}, falling back to CRL: {e}"
                )
                try:
                    return await self._crl_helper.check(
                        cert, issuer, session,
                        verify_sig=verify_signature,
                        issuer_chain=crl_issuer_chain,
                    )
                except (CRLError, ExtensionMissingError) as e2:
                    raise RevocationCheckError(
                        f"Both OCSP and CRL checks failed: OCSP={e}, CRL={e2}"
                    ) from e2

    async def _check_chain(
        self,
        chain: list[x509.Certificate],
        session: aiohttp.ClientSession,
        mode: CheckMode,
        *,
        verify_signature: bool = True,
        ocsp_issuer_chain: list[x509.Certificate] | None = None,
        crl_issuer_chain: list[x509.Certificate] | None = None,
    ) -> dict[int, RevocationInfo]:
        # Check all certificates in parallel since they're independent
        async def check_one(i: int) -> tuple[int, RevocationInfo]:
            cert = chain[i]
            issuer = chain[i + 1]

            # For chain checking, use remaining chain as issuer chain if not explicitly provided
            effective_ocsp_chain = ocsp_issuer_chain if ocsp_issuer_chain else chain[i + 1:]
            effective_crl_chain = crl_issuer_chain if crl_issuer_chain else chain[i + 1:]

            try:
                status = await self._check_single_cert(
                    cert, issuer, session, mode,
                    verify_signature=verify_signature,
                    ocsp_issuer_chain=effective_ocsp_chain,
                    crl_issuer_chain=effective_crl_chain,
                )
                return (cert.serial_number, status)
            except RevocationCheckError as e:
                self._io_helper.logger.error(
                    f"Failed to check certificate {cert.serial_number}: {e}"
                )
                return (
                    cert.serial_number,
                    RevocationInfo(
                        status=RevocationStatus.UNKNOWN,
                        check_method=None,
                    )
                )

        # Run all checks in parallel
        tasks = [check_one(i) for i in range(len(chain) - 1)]
        results_list = await asyncio.gather(*tasks)

        return dict(results_list)
