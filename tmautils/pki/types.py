# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from dataclasses import dataclass
from enum import StrEnum
import datetime

import cryptography.x509 as x509


class RevocationStatus(StrEnum):
    """Certificate revocation status.

    Values:
        GOOD: Certificate is not revoked.
        REVOKED: Certificate is revoked.
        UNKNOWN: Responder explicitly returned "unknown" status.
        CHECK_FAILURE: Check could not be completed (network error, missing extension, etc.)
    """
    GOOD = "good"
    REVOKED = "revoked"
    UNKNOWN = "unknown"
    CHECK_FAILURE = "check_failure"


@dataclass
class RevocationInfo:
    """
    Information about a certificate's revocation status.

    Attributes:
        status: The revocation status (GOOD, REVOKED, UNKNOWN, or CHECK_FAILURE)
        revocation_time: If revoked, when the certificate was revoked (UTC)
        revocation_reason: If revoked, the reason for revocation
        check_method: How status was determined (`ocsp` or `crl`)
        source_url: The OCSP responder URL or CRL distribution point used
        this_update: When the revocation info was produced (UTC)
        next_update: When the revocation info expires (UTC)
        error: If CHECK_FAILURE, the exception that caused the failure
    """
    status: RevocationStatus
    revocation_time: datetime.datetime | None = None
    revocation_reason: x509.ReasonFlags | None = None
    check_method: str | None = None
    source_url: str | None = None
    this_update: datetime.datetime | None = None
    next_update: datetime.datetime | None = None
    error: Exception | None = None


class CheckMode(StrEnum):
    """Mode for revocation checking."""
    OCSP_ONLY = "ocsp_only"
    CRL_ONLY = "crl_only"
    OCSP_FALLBACK_CRL = "ocsp_fallback_crl"


class RevocationCheckError(Exception):
    """Base exception for revocation checking errors."""
    pass


class OCSPError(RevocationCheckError):
    """OCSP-specific errors."""
    pass


class CRLError(RevocationCheckError):
    """CRL-specific errors."""
    pass


class ExtensionMissingError(RevocationCheckError):
    """Certificate lacks required extensions (e.g., AIA, CRL Distribution Point)"""
    pass
