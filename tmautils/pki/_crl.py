# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

"""
CRL certificate revocation checking.

This module adapts logic from the pki-tools package
(https://github.com/fulder/pki-tools) by Michal Sadowski.

Original Copyright (c) 2021-2023 Michal Sadowski <misad90@gmail.com>
Licensed under the MIT License.
"""

import asyncio
from pathlib import Path
from urllib.parse import urlparse
import datetime
import json

import aiohttp
import cryptography.x509 as x509
from cryptography import x509 as x509_module
from cryptography.x509.oid import ExtensionOID, CRLEntryExtensionOID
from cryptography.hazmat.primitives import serialization

from tmautils.common import LogHelper, get_logger_from_helper
from tmautils.web import request_with_retry

from .types import (
    RevocationStatus,
    RevocationInfo,
    CRLError,
    ExtensionMissingError,
)
from ._crypto import get_cache_key, parse_cert_lrucached, verify_signature


def is_crl_cache_valid_with_metadata(cache_path: Path) -> bool:
    if not cache_path.exists():
        return False

    meta_path = cache_path.with_suffix(".meta")
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            next_update_str = meta.get("next_update")
            if next_update_str:
                next_update = datetime.datetime.fromisoformat(next_update_str)
                now = datetime.datetime.now(datetime.timezone.utc)
                if now < next_update:
                    return True
                # next_update has passed => cache is stale
                return False
        except (json.JSONDecodeError, ValueError, KeyError):
            # Metadata corrupted => treat as invalid
            pass

    return False


class CRLHelper:
    """
    Helper class for CRL operations.

    This is an internal implementation class. Users should use
    RevocationChecker as the public API.
    """

    def __init__(
        self,
        http_semaphore: asyncio.Semaphore,
        *,
        crl_cache_dir: Path,
        issuer_cache_dir: Path,
        request_timeout: float = 10.0,
        max_attempts: int = 3,
        log_helper: LogHelper | None = None,
    ):
        self._http_semaphore = http_semaphore
        self._crl_cache_dir = crl_cache_dir
        self._issuer_cache_dir = issuer_cache_dir
        self._request_timeout = request_timeout
        self._max_attempts = max_attempts
        self._log_helper = log_helper
        self._logger = get_logger_from_helper(log_helper)

    async def check(
        self,
        cert: x509.Certificate,
        issuer_cert: x509.Certificate,
        session: aiohttp.ClientSession,
        *,
        verify_sig: bool = True,
        issuer_chain: list[x509.Certificate] | None = None,
    ) -> RevocationInfo:
        urls = self._get_urls_from_cert(cert)
        if not urls:
            raise CRLError("No HTTP CRL distribution points found")

        last_error: Exception | None = None

        for url in urls:
            try:
                crl = await self._download_cache(url, session)

                # Check CRL hasn't expired (RFC 5280 §5.1.2.5)
                now = datetime.datetime.now(datetime.timezone.utc)
                next_update = crl.next_update_utc
                if next_update is not None and next_update < now:
                    self._logger.warning(
                        "CRL from %s has expired (nextUpdate: %s)", url, next_update
                    )
                    last_error = CRLError(
                        f"CRL has expired (nextUpdate: {next_update})")
                    continue

                # Validate IDP matches CDP and scope (RFC 5280 §5.2.5)
                try:
                    self._validate_idp(crl, url, cert)
                except CRLError as e:
                    self._logger.warning(
                        "CRL IDP validation failed for %s: %s", url, e
                    )
                    last_error = e
                    continue

                if verify_sig:
                    # Determine which cert to use for signature verification
                    actual_issuer = None

                    # 1. Try chain first if provided
                    if issuer_chain:
                        try:
                            actual_issuer = self._find_issuer(
                                crl, issuer_chain
                            )
                        except CRLError:
                            pass  # Will try other sources

                    # 2. Try issuer_cert if it matches CRL issuer DN
                    if actual_issuer is None and issuer_cert.subject == crl.issuer:
                        actual_issuer = issuer_cert

                    # 3. Try AIA fallback (RFC 5280 §5.2.7)
                    if actual_issuer is None:
                        self._logger.debug(
                            "CRL issuer not in chain or issuer_cert, trying AIA extension"
                        )
                        actual_issuer = await self._get_signer_cert(crl, session)

                    if actual_issuer is None:
                        last_error = CRLError(
                            f"Cannot find CRL signer cert for issuer: "
                            f"{crl.issuer.rfc4514_string()}"
                        )
                        self._logger.warning(
                            "CRL signer not found for %s: %s", url, last_error
                        )
                        continue

                    # Yield to other tasks before CPU-bound signature verification
                    await asyncio.sleep(0)

                    try:
                        verify_signature(
                            actual_issuer.public_key(),
                            crl.tbs_certlist_bytes,
                            crl.signature,
                            crl.signature_hash_algorithm,
                            crl.signature_algorithm_parameters,
                        )
                    except Exception as e:
                        self._logger.warning(
                            "CRL signature verification failed for %s: %s", url, e
                        )
                        last_error = CRLError(
                            f"CRL signature verification failed for {url}: {e}"
                        )
                        continue
                else:
                    self._logger.debug(
                        "Skipping CRL signature verification (verify_sig=False)"
                    )

                revoked_cert = crl.get_revoked_certificate_by_serial_number(
                    cert.serial_number
                )

                if revoked_cert is not None:
                    # Extract revocation reason from CRL entry extension if present
                    revocation_reason: x509.ReasonFlags | None = None
                    try:
                        reason_ext = revoked_cert.extensions.get_extension_for_oid(
                            CRLEntryExtensionOID.CRL_REASON
                        )
                        revocation_reason = reason_ext.value.reason
                    except x509.ExtensionNotFound:
                        pass  # Reason is optional

                    self._logger.info(
                        f"CRL: Certificate {cert.serial_number} is REVOKED"
                    )
                    return RevocationInfo(
                        status=RevocationStatus.REVOKED,
                        revocation_time=revoked_cert.revocation_date_utc,
                        revocation_reason=revocation_reason,
                        check_method="crl",
                        source_url=url,
                        this_update=crl.last_update_utc,
                        next_update=crl.next_update_utc,
                    )

                self._logger.debug(
                    "CRL: Certificate %s is GOOD; not in CRL from %s", cert.serial_number, url
                )
                return RevocationInfo(
                    status=RevocationStatus.GOOD,
                    check_method="crl",
                    source_url=url,
                    this_update=crl.last_update_utc,
                    next_update=crl.next_update_utc,
                )

            except CRLError as e:
                self._logger.warning(
                    "Failed to check CRL from %s: %s", url, e
                )
                last_error = e
                continue
            except Exception as e:
                self._logger.warning(
                    "Unexpected error checking CRL from %s: %s", url, e
                )
                last_error = CRLError(
                    f"Unexpected error checking CRL from {url}: {e}"
                )
                continue

        # All URLs failed
        raise last_error or CRLError("No valid CRL distribution points found")

    def _get_urls_from_cert(self, cert: x509.Certificate) -> list[str]:
        try:
            cdp_ext = cert.extensions.get_extension_for_oid(
                ExtensionOID.CRL_DISTRIBUTION_POINTS
            )
        except x509.ExtensionNotFound as e:
            raise ExtensionMissingError(
                f"Certificate {cert.serial_number} lacks CRL Distribution Points extension"
            ) from e

        urls: list[str] = []
        for dp in cdp_ext.value:
            if not dp.full_name:
                continue
            for name in dp.full_name:
                if isinstance(
                    name, x509.UniformResourceIdentifier
                ) and name.value.startswith("http"):
                    urls.append(name.value)
        return urls

    async def _download_cache(
        self,
        url: str,
        session: aiohttp.ClientSession
    ) -> x509.CertificateRevocationList:
        # Use cached CRL if valid
        cache_path = self._crl_cache_dir / (get_cache_key(url) + ".crl")
        if is_crl_cache_valid_with_metadata(cache_path):
            try:
                crl = x509.load_der_x509_crl(cache_path.read_bytes())
                self._logger.debug("CRL cache hit: %s", url)
                return crl
            except Exception as e:
                # Cache is corrupted => fall through to re-download.
                self._logger.warning(
                    "Failed to load cached CRL (%s), re-downloading: %s", url, e
                )

        self._logger.info("Downloading CRL: %s", url)

        try:
            async with self._http_semaphore:
                async with request_with_retry(
                    session,
                    "GET",
                    url,
                    attempt_timeout=self._request_timeout,
                    max_attempts=self._max_attempts,
                    log_helper=self._log_helper,
                ) as resp:
                    resp.raise_for_status()
                    content = await resp.read()
        except Exception as e:
            raise CRLError(f"Failed to download CRL from {url}: {e}") from e

        # Yield before CPU-bound parsing
        await asyncio.sleep(0)

        # Try parsing as DER first, then PEM
        try:
            crl = x509.load_der_x509_crl(content)
        except ValueError:
            try:
                crl = x509.load_pem_x509_crl(content)
            except ValueError as e:
                raise CRLError(f"Failed to parse CRL from {url}: {e}") from e

        # Cache to disk as DER
        cache_path.write_bytes(crl.public_bytes(serialization.Encoding.DER))

        # Write metadata file
        meta_path = cache_path.with_suffix(".meta")
        now = datetime.datetime.now(datetime.timezone.utc)
        meta = {
            "downloaded_at": now.isoformat(),
            "last_update": crl.last_update_utc.isoformat() if crl.last_update_utc else None,
            "next_update": crl.next_update_utc.isoformat() if crl.next_update_utc else None,
        }
        try:
            meta_path.write_text(json.dumps(meta))
        except Exception as e:
            self._logger.warning(
                "Failed to write CRL cache metadata: %s", e
            )

        self._logger.debug(
            "Cached CRL: %s (next_update: %s)",
            cache_path, crl.next_update_utc
        )

        return crl

    def _find_issuer(
        self,
        crl: x509.CertificateRevocationList,
        issuer_chain: list[x509.Certificate],
    ) -> x509.Certificate:
        # Per RFC 5280, the CRL issuer is identified by the issuer field,
        # which must match the subject DN of the signing certificate.

        for cert in issuer_chain:
            if cert.subject == crl.issuer:
                self._logger.debug(
                    "Found CRL issuer by DN match: %s",
                    cert.subject.rfc4514_string()
                )
                return cert

        raise CRLError(
            f"No cert in chain matches CRL issuer DN: {crl.issuer.rfc4514_string()}"
        )

    async def _get_signer_cert(
        self,
        crl: x509.CertificateRevocationList,
        session: aiohttp.ClientSession,
    ) -> x509.Certificate | None:
        # Per RFC 5280 §5.2.7, CRLs MAY include an AIA extension with
        # id-ad-caIssuers access method pointing to the signer certificate.

        try:
            aia_ext = crl.extensions.get_extension_for_oid(
                ExtensionOID.AUTHORITY_INFORMATION_ACCESS
            )
        except x509.ExtensionNotFound:
            return None

        for desc in aia_ext.value:
            if desc.access_method != x509_module.oid.AuthorityInformationAccessOID.CA_ISSUERS:
                continue

            signer_url = desc.access_location.value
            self._logger.debug(
                "CRL signer URL from AIA: %s", signer_url
            )

            # Check issuer cert cache
            cache_key_str = get_cache_key(signer_url) + ".crt"
            cache_path = self._issuer_cache_dir / cache_key_str
            if cache_path.exists():
                try:
                    cert = parse_cert_lrucached(cache_path.read_bytes())
                    if cert.subject == crl.issuer:
                        self._logger.debug(
                            "CRL signer cert cache hit: %s", signer_url
                        )
                        return cert
                except Exception:
                    pass  # Cache corrupted, re-download

            # Download
            self._logger.info(
                "Downloading CRL signer cert: %s", signer_url
            )
            try:
                async with self._http_semaphore:
                    async with request_with_retry(
                        session, "GET", signer_url,
                        attempt_timeout=self._request_timeout,
                        max_attempts=self._max_attempts,
                        log_helper=self._log_helper,
                    ) as resp:
                        resp.raise_for_status()
                        content = await resp.read()
            except Exception as e:
                self._logger.warning(
                    "Failed to download CRL signer cert from %s: %s", signer_url, e
                )
                continue

            # Parse (DER or PEM)
            try:
                cert = x509.load_der_x509_certificate(content)
            except ValueError:
                try:
                    cert = x509.load_pem_x509_certificate(content)
                except ValueError as e:
                    self._logger.warning(
                        "Failed to parse CRL signer cert from %s: %s", signer_url, e
                    )
                    continue

            # Verify it matches CRL issuer
            if cert.subject != crl.issuer:
                self._logger.debug(
                    "Downloaded cert subject doesn't match CRL issuer DN"
                )
                continue

            # Cache and return
            cache_path.write_bytes(
                cert.public_bytes(serialization.Encoding.DER)
            )
            self._logger.debug(
                "Cached CRL signer cert: %s", cache_path
            )
            return cert

        return None

    def _validate_idp(
        self,
        crl: x509.CertificateRevocationList,
        cdp_url: str,
        cert: x509.Certificate,
    ) -> None:
        # Per RFC 5280 §5.2.5, when the IDP extension is present:
        # 1. The CRL scope must be validated against the certificate's CDP
        # 2. onlyContainsUserCerts/onlyContainsCACerts must match cert type

        try:
            idp_ext = crl.extensions.get_extension_for_oid(
                ExtensionOID.ISSUING_DISTRIBUTION_POINT
            )
        except x509.ExtensionNotFound:
            # No IDP extension, validation not required
            return

        idp = idp_ext.value

        # Check certificate type constraints (RFC 5280 §5.2.5)
        # Determine if cert is a CA cert
        cert_is_ca = False
        try:
            bc_ext = cert.extensions.get_extension_for_oid(
                ExtensionOID.BASIC_CONSTRAINTS
            )
            cert_is_ca = bc_ext.value.ca
        except x509.ExtensionNotFound:
            # No BasicConstraints = end entity cert (not CA)
            pass

        # onlyContainsUserCerts: CRL only covers end-entity certs
        if idp.only_contains_user_certs and cert_is_ca:
            raise CRLError(
                "CRL only contains user certificates (onlyContainsUserCerts=true), "
                "but certificate being checked is a CA certificate"
            )

        # onlyContainsCACerts: CRL only covers CA certs
        if idp.only_contains_ca_certs and not cert_is_ca:
            raise CRLError(
                "CRL only contains CA certificates (onlyContainsCACerts=true), "
                "but certificate being checked is not a CA certificate"
            )

        # onlyContainsAttributeCerts: CRL only covers attribute certs
        if idp.only_contains_attribute_certs:
            raise CRLError(
                "CRL only contains attribute certificates (onlyContainsAttributeCerts=true), "
                "cannot use for X.509 certificate revocation checking"
            )

        # onlySomeReasons: CRL only covers specific reason codes (RFC 5280 §5.2.5)
        if idp.only_some_reasons is not None:
            covered_reasons = []
            for flag in idp.only_some_reasons:
                covered_reasons.append(
                    flag.name if hasattr(flag, 'name') else str(flag)
                )
            self._logger.warning(
                "CRL only covers some revocation reasons (onlySomeReasons). "
                "Covered reasons: %s. Certificate may be revoked for other reasons "
                "not covered by this CRL.",
                ", ".join(covered_reasons)
            )

        # Validate distribution point names if present
        if not idp.full_name:
            # IDP has no full_name, scope validation passed
            self._logger.debug(
                "CRL IDP scope validation passed (no fullName, scope constraints checked)"
            )
            return

        cdp_parsed = urlparse(cdp_url)

        for name in idp.full_name:
            if not isinstance(name, x509.UniformResourceIdentifier):
                continue

            idp_parsed = urlparse(name.value)

            # Compare scheme
            if cdp_parsed.scheme != idp_parsed.scheme:
                continue

            # Compare hostname (exact match required)
            if cdp_parsed.hostname != idp_parsed.hostname:
                continue

            # Compare path filename (last component)
            cdp_filename = cdp_parsed.path.rstrip("/").split("/")[-1]
            idp_filename = idp_parsed.path.rstrip("/").split("/")[-1]
            if cdp_filename != idp_filename:
                continue

            # Match found
            self._logger.debug(
                "CRL IDP validation passed: CDP=%s, IDP=%s",
                cdp_url, name.value
            )
            return

        # No matching IDP found
        idp_uris = [
            n.value for n in idp.full_name
            if isinstance(n, x509.UniformResourceIdentifier)
        ]
        raise CRLError(
            f"CRL IDP does not match CDP URL {cdp_url}. IDP URIs: {idp_uris}"
        )
