# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

"""
OCSP certificate revocation checking.

This module adapts logic from the pki-tools package
(https://github.com/fulder/pki-tools) by Michal Sadowski.

Original Copyright (c) 2021-2023 Michal Sadowski <misad90@gmail.com>
Licensed under the MIT License.
"""

from collections import OrderedDict
import asyncio
import datetime
import hashlib

import aiohttp
import cryptography.x509 as x509
from cryptography import x509 as x509_module
from cryptography.x509 import ocsp
from cryptography.x509.oid import ExtensionOID, ExtendedKeyUsageOID, SignatureAlgorithmOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tmautils.common import LogHelper, get_logger_from_helper, AsyncRateLimiter
from tmautils.web import request_with_retry, RetryConfig

from .types import (
    RevocationStatus,
    RevocationInfo,
    OCSPError,
    ExtensionMissingError,
)
from ._crypto import verify_signature, extract_spki_public_key_bytes


class OCSPHelper:
    """
    Helper class for OCSP operations.

    This is an internal implementation class. Users should use
    RevocationChecker as the public API.
    """

    def __init__(
        self,
        rate_limiter: AsyncRateLimiter,
        *,
        request_timeout: float = 10.0,
        retry_config: RetryConfig | None = None,
        default_ttl: datetime.timedelta = datetime.timedelta(hours=6),
        max_cache_size: int = 1024,
        log_helper: LogHelper | None = None,
    ):
        self._rate_limiter = rate_limiter
        self._request_timeout = request_timeout
        self._retry_config = retry_config or RetryConfig()
        self._default_ttl = default_ttl
        self._max_cache_size = max_cache_size
        self._log_helper = log_helper
        self._logger = get_logger_from_helper(log_helper)

        # In-memory LRU cache: (serial, issuer_key_hash) -> (RevocationInfo, expiry)
        self._cache: OrderedDict[tuple[int, bytes],
                                 tuple[RevocationInfo, datetime.datetime]] = OrderedDict()

    async def check(
        self,
        cert: x509.Certificate,
        issuer_cert: x509.Certificate,
        session: aiohttp.ClientSession,
        *,
        verify_sig: bool = True,
        issuer_chain: list[x509.Certificate] | None = None,
    ) -> RevocationInfo:
        # Check cache first
        cached = self._get_cached(cert, issuer_cert)
        if cached is not None:
            return cached

        ocsp_url = self._get_ocsp_url_from_aia(cert)

        # Try multiple hash algorithms
        req_hash_algs = [hashes.SHA256(), hashes.SHA512(), hashes.SHA1()]

        last_non_success = None
        for req_hash in req_hash_algs:
            ocsp_resp = await self._fetch_response(
                cert, issuer_cert, session, ocsp_url, req_hash
            )

            if ocsp_resp.response_status != ocsp.OCSPResponseStatus.SUCCESSFUL:
                last_non_success = OCSPError(
                    f"OCSP response status: {ocsp_resp.response_status}"
                )
                continue

            # Validate OCSP response (RFC 6960 §3.2)
            self._validate_response(cert, ocsp_resp)

            if verify_sig:
                # Build effective issuer chain including embedded response certs
                effective_chain = list(issuer_chain) if issuer_chain else []

                # Extract embedded responder certs from OCSP response (RFC 6960 §4.2.2.2)
                try:
                    embedded_certs = ocsp_resp.certificates
                    if embedded_certs:
                        self._logger.debug(
                            "Found %d embedded responder cert(s) in OCSP response",
                            len(embedded_certs)
                        )
                        # Prepend embedded certs (most likely to be responder)
                        effective_chain = embedded_certs + effective_chain
                except ValueError:
                    # certificates property raises ValueError if response not successful
                    pass

                self._verify_ocsp_signature(
                    ocsp_resp, issuer_cert,
                    issuer_chain=effective_chain if effective_chain else None
                )
            else:
                self._logger.debug(
                    "Skipping OCSP signature verification (verify_sig=False)"
                )

            info = self._info_from_response(
                cert.serial_number, ocsp_resp, ocsp_url
            )
            # Cache the successful response
            self._cache_response(cert, issuer_cert, info)
            return info

        # We exhausted all hash algorithms without success
        if last_non_success:
            raise last_non_success
        raise OCSPError("OCSP check failed with all hash algorithms")

    def _get_cache_key(
        self,
        cert: x509.Certificate,
        issuer_cert: x509.Certificate,
    ) -> tuple[int, bytes]:
        issuer_key_hash = hashlib.sha256(
            issuer_cert.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo
            )
        ).digest()
        return (cert.serial_number, issuer_key_hash)

    def _get_cached(
        self,
        cert: x509.Certificate,
        issuer_cert: x509.Certificate,
    ) -> RevocationInfo | None:
        key = self._get_cache_key(cert, issuer_cert)
        if key not in self._cache:
            return None

        info, expiry = self._cache[key]
        now = datetime.datetime.now(datetime.timezone.utc)

        if now >= expiry:
            # Cache expired, remove entry
            del self._cache[key]
            self._logger.debug(
                "OCSP cache expired for cert %s", cert.serial_number
            )
            return None

        # Move to end (most recently used)
        self._cache.move_to_end(key)
        self._logger.debug(
            "OCSP cache hit for cert %s (expires %s)", cert.serial_number, expiry
        )
        return info

    def _cache_response(
        self,
        cert: x509.Certificate,
        issuer_cert: x509.Certificate,
        info: RevocationInfo,
    ) -> None:
        key = self._get_cache_key(cert, issuer_cert)
        now = datetime.datetime.now(datetime.timezone.utc)

        # Use next_update if available, otherwise use default TTL
        if info.next_update is not None:
            expiry = info.next_update
        else:
            expiry = now + self._default_ttl

        self._cache[key] = (info, expiry)
        self._cache.move_to_end(key)

        # Evict oldest entries if over limit
        while len(self._cache) > self._max_cache_size:
            self._cache.popitem(last=False)

        self._logger.debug(
            "Cached OCSP response for cert %s (expires %s)", cert.serial_number, expiry
        )

    def _get_ocsp_url_from_aia(self, cert: x509.Certificate) -> str:
        try:
            aia_ext = cert.extensions.get_extension_for_oid(
                ExtensionOID.AUTHORITY_INFORMATION_ACCESS
            )
        except x509.ExtensionNotFound as e:
            raise ExtensionMissingError(
                f"Certificate {cert.serial_number} lacks AIA extension"
            ) from e

        for desc in aia_ext.value:
            if desc.access_method == x509_module.oid.AuthorityInformationAccessOID.OCSP:
                url = desc.access_location.value
                self._logger.debug(f"OCSP URL from AIA: {url}")
                return url

        raise ExtensionMissingError(
            f"Certificate {cert.serial_number} has no OCSP URL in AIA"
        )

    async def _fetch_response(
        self,
        cert: x509.Certificate,
        issuer_cert: x509.Certificate,
        session: aiohttp.ClientSession,
        ocsp_url: str,
        request_hash: hashes.HashAlgorithm,
    ) -> ocsp.OCSPResponse:
        ocsp_req = ocsp.OCSPRequestBuilder().add_certificate(
            cert, issuer_cert, request_hash
        ).build()
        req_bytes = ocsp_req.public_bytes(serialization.Encoding.DER)

        self._logger.debug(
            "Sending OCSP POST request with %s to %s", request_hash.name, ocsp_url
        )

        try:
            async with request_with_retry(
                session,
                "POST",
                ocsp_url,
                data=req_bytes,
                headers={"Content-Type": "application/ocsp-request"},
                rate_limiter=self._rate_limiter,
                retry_config=self._retry_config,
                attempt_timeout=self._request_timeout,
                log_helper=self._log_helper,
            ) as resp:
                resp.raise_for_status()
                ocsp_resp_bytes = await resp.read()
        except Exception as e:
            raise OCSPError(
                f"OCSP HTTP request failed ({request_hash.name}) to {ocsp_url}: "
                f"{type(e).__name__}: {e}"
            ) from e

        # Yield before CPU-bound parsing
        await asyncio.sleep(0)

        try:
            return ocsp.load_der_ocsp_response(ocsp_resp_bytes)
        except Exception as e:
            raise OCSPError(
                f"Failed to parse OCSP response ({request_hash.name}): {e}"
            ) from e

    def _validate_response(
        self,
        cert: x509.Certificate,
        ocsp_resp: ocsp.OCSPResponse,
    ) -> None:
        # 1. Verify serial number matches (RFC 6960 §3.2(1))
        if ocsp_resp.serial_number != cert.serial_number:
            raise OCSPError(
                f"OCSP response serial mismatch: response has {ocsp_resp.serial_number}, "
                f"expected {cert.serial_number}"
            )

        # Get current time in UTC
        now = datetime.datetime.now(datetime.timezone.utc)

        # 2. Verify thisUpdate is not in the future (RFC 6960 §3.2(5))
        # Allow clock skew of 5 minutes
        this_update = ocsp_resp.this_update_utc
        if this_update > now + datetime.timedelta(minutes=5):
            raise OCSPError(
                f"OCSP thisUpdate is in the future: {this_update}"
            )

        # 3. Verify nextUpdate has not passed (RFC 6960 §3.2(6))
        next_update = ocsp_resp.next_update_utc
        if next_update is not None and next_update < now:
            raise OCSPError(
                f"OCSP response has expired (nextUpdate: {next_update})"
            )

        self._logger.debug(
            "OCSP response validation passed: serial=%s, thisUpdate=%s, nextUpdate=%s",
            ocsp_resp.serial_number, this_update, next_update
        )

    def _validate_responder_authorization(
        self,
        responder_cert: x509.Certificate,
        issuer_cert: x509.Certificate,
    ) -> None:
        # Compare public keys to determine if this is the CA itself or a delegate
        responder_key_bytes = responder_cert.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo
        )
        issuer_key_bytes = issuer_cert.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo
        )

        if responder_key_bytes == issuer_key_bytes:
            # Same key = CA is signing directly, no delegation validation needed
            self._logger.debug(
                "OCSP response signed by CA directly (same key)"
            )
            return

        # Delegated responder - validate authorization per RFC 6960 §4.2.2.2
        self._logger.debug(
            "Validating delegated OCSP responder authorization"
        )

        # 1. Check for id-kp-OCSPSigning EKU
        try:
            eku_ext = responder_cert.extensions.get_extension_for_oid(
                ExtensionOID.EXTENDED_KEY_USAGE
            )
            if ExtendedKeyUsageOID.OCSP_SIGNING not in eku_ext.value:
                raise OCSPError(
                    "Delegated OCSP responder lacks id-kp-OCSPSigning EKU"
                )
        except x509.ExtensionNotFound:
            raise OCSPError(
                "Delegated OCSP responder lacks Extended Key Usage extension"
            )

        # 2. Verify responder cert was directly issued by CA
        try:
            responder_cert.verify_directly_issued_by(issuer_cert)
        except Exception as e:
            raise OCSPError(
                f"Cannot validate that OCSP responder cert was directly issued by CA: {e}"
            ) from e

        # 4. Check for id-pkix-ocsp-nocheck extension (RFC 6960 §4.2.2.2.1)
        try:
            responder_cert.extensions.get_extension_for_oid(
                ExtensionOID.OCSP_NO_CHECK)
            self._logger.debug(
                "OCSP responder has id-pkix-ocsp-nocheck extension, "
                "skipping responder cert revocation check"
            )
        except x509.ExtensionNotFound:
            # Per RFC 6960, we should check responder cert revocation status
            # but we don't do that yet. Log a warning and continue.
            self._logger.warning(
                "Delegated OCSP responder %s lacks id-pkix-ocsp-nocheck extension. "
                "Responder cert revocation status check has not been implemented. "
                "Proceeding without revocation check.",
                responder_cert.subject.rfc4514_string()
            )

        self._logger.debug(
            "Delegated OCSP responder authorization valid: %s",
            responder_cert.subject.rfc4514_string()
        )

    def _find_issuer_by_key_hash(
        self,
        ocsp_resp: ocsp.OCSPResponse,
        issuer_chain: list[x509.Certificate],
    ) -> x509.Certificate | None:
        # Per RFC 6960, issuerKeyHash is the hash of the issuer's public key,
        # calculated over "the value (excluding tag and length) of the subject
        # public key field in the issuer's certificate."

        response_key_hash = ocsp_resp.issuer_key_hash
        hash_alg = ocsp_resp.hash_algorithm

        for cert in issuer_chain:
            try:
                public_key = cert.public_key()
                key_bytes = extract_spki_public_key_bytes(public_key)

                # Hash with the same algorithm used in the OCSP response
                digest = hashes.Hash(hash_alg)
                digest.update(key_bytes)
                computed_hash = digest.finalize()

                if computed_hash == response_key_hash:
                    self._logger.debug(
                        "Found OCSP issuer by key hash match: %s",
                        cert.subject.rfc4514_string()
                    )
                    return cert
            except Exception as e:
                self._logger.debug(
                    "Error computing key hash for cert %s: %s",
                    cert.serial_number, e
                )
                continue

        return None

    def _do_signature_verify(
        self,
        ocsp_resp: ocsp.OCSPResponse,
        cert: x509.Certificate,
    ) -> None:
        public_key = cert.public_key()

        params = None
        if isinstance(public_key, rsa.RSAPublicKey):
            # Fail early for RSASSA-PSS (as of now we don't know the padding params)
            if ocsp_resp.signature_algorithm_oid == SignatureAlgorithmOID.RSASSA_PSS:
                raise OCSPError(
                    "OCSP response uses RSASSA-PSS, but PSS params are not available"
                )
            params = padding.PKCS1v15()

        verify_signature(
            public_key,
            ocsp_resp.tbs_response_bytes,
            ocsp_resp.signature,
            ocsp_resp.signature_hash_algorithm,
            params,
        )

    def _verify_ocsp_signature(
        self,
        ocsp_resp: ocsp.OCSPResponse,
        issuer_cert: x509.Certificate,
        issuer_chain: list[x509.Certificate] | None = None,
    ) -> None:
        if issuer_chain:
            # Try key hash matching first
            matched = self._find_issuer_by_key_hash(ocsp_resp, issuer_chain)
            if matched:
                try:
                    self._do_signature_verify(ocsp_resp, matched)
                    # Validate responder authorization (RFC 6960 §4.2.2.2)
                    self._validate_responder_authorization(
                        matched, issuer_cert
                    )
                    return
                except OCSPError:
                    raise  # Re-raise authorization errors
                except Exception as e:
                    self._logger.debug(
                        "Key hash matched cert failed signature verification: %s", e
                    )

            # Fallback: try all certs in chain
            self._logger.debug(
                "No OCSP issuer matched by key hash, trying all certs in chain"
            )
            last_error = None
            for cert in issuer_chain:
                try:
                    self._do_signature_verify(ocsp_resp, cert)
                    # Validate responder authorization (RFC 6960 §4.2.2.2)
                    self._validate_responder_authorization(cert, issuer_cert)
                    self._logger.debug(
                        "OCSP signature verified against: %s",
                        cert.subject.rfc4514_string()
                    )
                    return
                except OCSPError:
                    raise  # Re-raise authorization errors
                except Exception as e:
                    last_error = e
                    continue

            raise OCSPError(
                f"Could not verify OCSP signature against any cert in chain: {last_error}"
            )

        # No chain provided, use single issuer
        try:
            self._do_signature_verify(ocsp_resp, issuer_cert)
            # Validate responder authorization (RFC 6960 §4.2.2.2)
            self._validate_responder_authorization(issuer_cert, issuer_cert)
        except Exception as e:
            raise OCSPError(f"OCSP signature verification failed: {e}") from e

    def _info_from_response(
        self,
        serial: int,
        ocsp_resp: ocsp.OCSPResponse,
        ocsp_url: str,
    ) -> RevocationInfo:
        cert_status = ocsp_resp.certificate_status

        # Base info common to all statuses
        this_update = ocsp_resp.this_update_utc
        next_update = ocsp_resp.next_update_utc

        if cert_status == ocsp.OCSPCertStatus.REVOKED:
            self._logger.debug(
                "OCSP: Certificate %s is REVOKED", serial
            )
            return RevocationInfo(
                status=RevocationStatus.REVOKED,
                revocation_time=ocsp_resp.revocation_time_utc,
                revocation_reason=ocsp_resp.revocation_reason,
                check_method="ocsp",
                source_url=ocsp_url,
                this_update=this_update,
                next_update=next_update,
            )

        if cert_status == ocsp.OCSPCertStatus.GOOD:
            self._logger.debug(
                "OCSP: Certificate %s is GOOD", serial
            )
            return RevocationInfo(
                status=RevocationStatus.GOOD,
                check_method="ocsp",
                source_url=ocsp_url,
                this_update=this_update,
                next_update=next_update,
            )

        self._logger.debug(
            "OCSP: Certificate %s has UNKNOWN status", serial
        )
        return RevocationInfo(
            status=RevocationStatus.UNKNOWN,
            check_method="ocsp",
            source_url=ocsp_url,
            this_update=this_update,
            next_update=next_update,
        )
