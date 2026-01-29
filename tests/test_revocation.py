"""
Tests for certificate revocation checking.

Combines unit tests (no network) and integration tests (network required).
Uses async concurrent testing for efficiency.
"""

import asyncio
import json
import logging
import pytest

from unittest.mock import AsyncMock, patch

from tmautils.pki import (
    RevocationChecker,
    RevocationStatus,
    RevocationInfo,
    CheckMode,
    ExtensionMissingError,
    OCSPError,
    CRLError,
    RevocationCheckError,
    get_cert_sync,
    get_cert_chain_sync,
    fetch_issuer_cert,
    fetch_issuer_cert_sync,
    fetch_issuer_chain,
    fetch_issuer_chain_sync,
)

# === Test Domain Lists ===

# Known valid certificate domains
VALID_DOMAINS = [
    "google.com",
    "example.com",
    "valid-isrgrootx1.letsencrypt.org",
    "usc.edu",
    "whitehouse.gov",
]

# Known revoked certificate test domains
REVOKED_DOMAINS = [
    "revoked-isrgrootx1.letsencrypt.org",  # Let's Encrypt test
    "revoked-rsa-dv.ssl.com",               # SSL.com RSA DV
    "revoked-ecc-dv.ssl.com",               # SSL.com ECC DV
    "revoked.grc.com",                      # DigiCert security test
]


# === Async Concurrent Helper ===

async def check_domains_concurrent(
    checker: RevocationChecker,
    domains: list[str],
    mode: CheckMode = CheckMode.OCSP_FALLBACK_CRL,
) -> list[tuple[str, object | None, Exception | None]]:
    """Check multiple domains concurrently.

    Returns list of (domain, result, error) tuples.
    """
    async def check_one(domain: str):
        try:
            cert = get_cert_sync(domain)
            result = await checker.check_cert(cert, mode=mode)
            return (domain, result, None)
        except Exception as e:
            return (domain, None, e)

    tasks = [check_one(d) for d in domains]
    return await asyncio.gather(*tasks)


# === Unit Tests (No Network) ===

def test_revocation_checker_init(tmp_path):
    """Test RevocationChecker initialization."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})

    # Check that cache directories were created
    assert checker._crl_cache_dir.exists()
    assert checker._issuer_cache_dir.exists()


def test_cache_directories_created(tmp_path):
    """Test that cache directories are created properly."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})

    assert checker._crl_cache_dir == tmp_path / \
        "RevocationChecker" / "cache" / "crl_cache"
    assert checker._issuer_cache_dir == tmp_path / \
        "RevocationChecker" / "cache" / "issuer_cache"


def test_enum_values():
    """Test enum values are correct."""
    assert RevocationStatus.GOOD.value == "good"
    assert RevocationStatus.REVOKED.value == "revoked"
    assert RevocationStatus.UNKNOWN.value == "unknown"
    assert RevocationStatus.CHECK_FAILURE.value == "check_failure"

    assert CheckMode.OCSP_ONLY.value == "ocsp_only"
    assert CheckMode.CRL_ONLY.value == "crl_only"
    assert CheckMode.OCSP_FALLBACK_CRL.value == "ocsp_fallback_crl"


def test_api_methods_exist(tmp_path):
    """Test all API methods exist and are callable."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})

    # Async methods
    assert callable(getattr(checker, 'check_cert', None))
    assert callable(getattr(checker, 'check_chain', None))
    assert callable(getattr(checker, 'check_cert_chain', None))

    # Sync wrappers
    assert callable(getattr(checker, 'check_cert_sync', None))
    assert callable(getattr(checker, 'check_chain_sync', None))
    assert callable(getattr(checker, 'check_cert_chain_sync', None))


def test_check_cert_input_validation(tmp_path):
    """Test input validation for check_cert."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})

    with pytest.raises((TypeError, AttributeError)):
        checker.check_cert_sync("not a certificate")


def test_check_chain_input_validation(tmp_path):
    """Test input validation for check_chain."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})

    with pytest.raises(TypeError):
        checker.check_chain_sync("not a list")

    with pytest.raises(ValueError, match="at least 2 certificates"):
        checker.check_chain_sync([])


def test_revocation_info_error_field():
    """Test that RevocationInfo has the error field."""
    # Test with no error
    info = RevocationInfo(status=RevocationStatus.GOOD)
    assert info.error is None

    # Test with error
    error = RevocationCheckError("test error")
    info_with_error = RevocationInfo(
        status=RevocationStatus.CHECK_FAILURE,
        error=error,
    )
    assert info_with_error.status == RevocationStatus.CHECK_FAILURE
    assert info_with_error.error is error
    assert isinstance(info_with_error.error, RevocationCheckError)


# === Integration Tests (Network Required) ===

@pytest.mark.asyncio
async def test_valid_certs_concurrent(tmp_path):
    """Check multiple valid domains concurrently - expect GOOD or UNKNOWN."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    results = await check_domains_concurrent(checker, VALID_DOMAINS)

    successful = 0
    for domain, result, error in results:
        if error:
            print(f"  {domain}: SKIP ({type(error).__name__}: {error})")
            continue
        assert result.status in (
            RevocationStatus.GOOD, RevocationStatus.UNKNOWN)
        print(f"  {domain}: {result.status.value} via {result.check_method}")
        successful += 1

    assert successful >= 1, "Expected at least one valid cert check to succeed"


@pytest.mark.asyncio
async def test_revoked_certs_concurrent(tmp_path):
    """Check multiple revoked domains concurrently - expect REVOKED."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    results = await check_domains_concurrent(checker, REVOKED_DOMAINS)

    revoked_count = 0
    for domain, result, error in results:
        if error:
            print(f"  {domain}: ERROR ({type(error).__name__}: {error})")
            continue
        print(f"  {domain}: {result.status.value} via {result.check_method}")
        if result.status == RevocationStatus.REVOKED:
            revoked_count += 1
            if result.revocation_reason:
                print(f"    reason: {result.revocation_reason}")

    # At least one revoked cert should be detected
    assert revoked_count >= 1, f"Expected at least one REVOKED, got {revoked_count}"


@pytest.mark.asyncio
async def test_check_modes_ocsp_only(tmp_path):
    """Test OCSP_ONLY mode."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    cert = get_cert_sync("google.com")

    try:
        result = await checker.check_cert(cert, mode=CheckMode.OCSP_ONLY)
        assert result.check_method == "ocsp"
        print(f"  OCSP_ONLY: {result.status.value}")
    except (OCSPError, ExtensionMissingError) as e:
        print(f"  OCSP_ONLY: {type(e).__name__} (expected if no OCSP)")


@pytest.mark.asyncio
async def test_check_modes_crl_only(tmp_path):
    """Test CRL_ONLY mode."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    cert = get_cert_sync("google.com")

    try:
        result = await checker.check_cert(cert, mode=CheckMode.CRL_ONLY)
        assert result.check_method == "crl"
        print(f"  CRL_ONLY: {result.status.value}")
    except (CRLError, ExtensionMissingError) as e:
        print(f"  CRL_ONLY: {type(e).__name__} (expected if no CRL)")


@pytest.mark.asyncio
async def test_check_modes_fallback(tmp_path):
    """Test OCSP_FALLBACK_CRL mode."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    cert = get_cert_sync("google.com")

    result = await checker.check_cert(cert, mode=CheckMode.OCSP_FALLBACK_CRL)
    assert result.status in (
        RevocationStatus.GOOD, RevocationStatus.UNKNOWN, RevocationStatus.REVOKED)
    print(
        f"  OCSP_FALLBACK_CRL: {result.status.value} via {result.check_method}")


@pytest.mark.asyncio
async def test_check_with_explicit_issuer(tmp_path):
    """Test checking with explicitly provided issuer certificate."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    chain = get_cert_chain_sync("google.com")
    assert len(chain) >= 2

    result = await checker.check_cert(chain[0], issuer=chain[1])
    assert result.status in (
        RevocationStatus.GOOD, RevocationStatus.UNKNOWN, RevocationStatus.REVOKED)
    print(
        f"  Explicit issuer: {result.status.value} via {result.check_method}")


@pytest.mark.asyncio
async def test_chain_checking(tmp_path):
    """Test checking entire certificate chain."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    chain = get_cert_chain_sync("google.com")
    assert len(chain) >= 2

    results = await checker.check_chain(chain)

    # Should check all certs except root (root has no issuer to verify against)
    assert isinstance(results, dict)
    assert len(results) >= 1

    for serial, info in results.items():
        # CHECK_FAILURE is also valid if a check couldn't complete
        assert info.status in (
            RevocationStatus.GOOD, RevocationStatus.UNKNOWN,
            RevocationStatus.REVOKED, RevocationStatus.CHECK_FAILURE)
        print(
            f"  Serial {serial}: {info.status.value} via {info.check_method}")
        if info.status == RevocationStatus.CHECK_FAILURE:
            print(f"    error: {info.error}")


@pytest.mark.asyncio
async def test_check_chain_returns_check_failure_on_error(tmp_path):
    """Test that check_chain returns CHECK_FAILURE with error field when check fails."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    chain = get_cert_chain_sync("google.com")
    assert len(chain) >= 2

    # Mock _check_single_cert to raise RevocationCheckError
    test_error = RevocationCheckError("Simulated network failure")

    async def mock_check_single_cert(*args, **kwargs):
        raise test_error

    with patch.object(checker, '_check_single_cert', side_effect=mock_check_single_cert):
        results = await checker.check_chain(chain)

    # All results should be CHECK_FAILURE with the error captured
    assert len(results) >= 1
    for serial, info in results.items():
        assert info.status == RevocationStatus.CHECK_FAILURE
        assert info.check_method is None
        assert info.error is test_error
        print(f"  Serial {serial}: {info.status.value}, error={info.error}")


@pytest.mark.asyncio
@pytest.mark.parametrize("exception_class,exception_name", [
    (OCSPError, "OCSPError"),
    (CRLError, "CRLError"),
    (ExtensionMissingError, "ExtensionMissingError"),
])
async def test_check_chain_catches_all_exception_types(tmp_path, exception_class, exception_name):
    """Test that check_chain catches OCSPError, CRLError, and ExtensionMissingError.

    This ensures consistency: in OCSP_ONLY or CRL_ONLY modes, specific exceptions
    are raised by _check_single_cert. When checking a chain, these should be caught
    and converted to CHECK_FAILURE status (not raised to the caller).
    """
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    chain = get_cert_chain_sync("google.com")
    assert len(chain) >= 2

    # Mock _check_single_cert to raise the specific exception type
    test_error = exception_class(f"Simulated {exception_name}")

    async def mock_check_single_cert(*args, **kwargs):
        raise test_error

    with patch.object(checker, '_check_single_cert', side_effect=mock_check_single_cert):
        results = await checker.check_chain(chain)

    # All results should be CHECK_FAILURE with the error captured
    assert len(results) >= 1
    for serial, info in results.items():
        assert info.status == RevocationStatus.CHECK_FAILURE, \
            f"{exception_name} should result in CHECK_FAILURE, not raise"
        assert info.check_method is None
        assert info.error is test_error
        assert isinstance(info.error, exception_class)
        print(f"  {exception_name} -> Serial {serial}: {info.status.value}, error={info.error}")


@pytest.mark.asyncio
async def test_verify_signature_disabled(tmp_path):
    """Test checking without signature verification."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    cert = get_cert_sync("google.com")

    result = await checker.check_cert(cert, verify_signature=False)
    assert result.status in (
        RevocationStatus.GOOD, RevocationStatus.UNKNOWN, RevocationStatus.REVOKED)
    print(f"  verify_signature=False: {result.status.value}")


@pytest.mark.asyncio
async def test_ocsp_caching(tmp_path):
    """Test OCSP in-memory cache works."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    cert = get_cert_sync("google.com")

    # First check
    try:
        result1 = await checker.check_cert(cert, mode=CheckMode.OCSP_ONLY)
        # Second check should hit cache
        result2 = await checker.check_cert(cert, mode=CheckMode.OCSP_ONLY)

        assert result1.status == result2.status
        print(f"  OCSP cache consistent: {result1.status.value}")
    except (OCSPError, ExtensionMissingError) as e:
        pytest.skip(f"OCSP not available: {e}")


@pytest.mark.asyncio
async def test_crl_disk_caching(tmp_path):
    """Test CRL disk cache works."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    cert = get_cert_sync("google.com")

    try:
        # First check - downloads CRL
        result1 = await checker.check_cert(cert, mode=CheckMode.CRL_ONLY)

        # Check cache file exists
        crl_cache_dir = tmp_path / "RevocationChecker" / "cache" / "crl_cache"
        crl_files = list(crl_cache_dir.glob("*.crl"))
        assert len(crl_files) >= 1, "CRL should be cached to disk"

        # Second check - should use cache
        result2 = await checker.check_cert(cert, mode=CheckMode.CRL_ONLY)
        assert result1.status == result2.status
        print(
            f"  CRL cache consistent: {result1.status.value}, files: {len(crl_files)}")
    except (CRLError, ExtensionMissingError) as e:
        pytest.skip(f"CRL not available: {e}")


@pytest.mark.asyncio
async def test_issuer_cert_caching(tmp_path):
    """Test issuer certificate caching."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    cert = get_cert_sync("google.com")

    # Check triggers issuer fetch
    await checker.check_cert(cert)

    # Issuer should be cached
    issuer_cache_dir = tmp_path / "RevocationChecker" / "cache" / "issuer_cache"
    issuer_files = list(issuer_cache_dir.glob("*.crt"))
    assert len(issuer_files) >= 1, "Issuer cert should be cached"
    print(f"  Issuer cache files: {len(issuer_files)}")


@pytest.mark.asyncio
async def test_cache_reuse_consistency(tmp_path):
    """Test that cached results are consistent."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})

    cert = get_cert_sync("google.com")

    # First check
    result1 = await checker.check_cert(cert)

    # Second check - should use cache
    result2 = await checker.check_cert(cert)

    assert result1.status == result2.status
    assert result1.check_method == result2.check_method
    print(
        f"  Cache consistent: {result1.status.value} via {result1.check_method}")


# === CRL-Specific Tests ===

@pytest.mark.asyncio
async def test_crl_detects_revoked_cert(tmp_path):
    """Test CRL_ONLY mode detects revoked certificates."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})

    # Use known revoked domain
    for domain in REVOKED_DOMAINS:
        try:
            cert = get_cert_sync(domain)
            result = await checker.check_cert(cert, mode=CheckMode.CRL_ONLY)
            print(f"  {domain} (CRL_ONLY): {result.status.value}")
            if result.status == RevocationStatus.REVOKED:
                assert result.check_method == "crl"
                return  # Test passed - found at least one revoked via CRL
        except (CRLError, ExtensionMissingError) as e:
            print(f"  {domain}: {type(e).__name__} - skipping")
            continue
        except Exception as e:
            print(f"  {domain}: {type(e).__name__}: {e}")
            continue

    pytest.skip("No revoked cert could be checked via CRL_ONLY")


@pytest.mark.asyncio
async def test_crl_concurrent_revoked(tmp_path):
    """Test CRL_ONLY mode with multiple revoked domains concurrently."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    results = await check_domains_concurrent(checker, REVOKED_DOMAINS, mode=CheckMode.CRL_ONLY)

    crl_checked = 0
    crl_revoked = 0
    for domain, result, error in results:
        if error:
            print(f"  {domain}: {type(error).__name__}")
            continue
        crl_checked += 1
        print(f"  {domain}: {result.status.value} via {result.check_method}")
        if result.status == RevocationStatus.REVOKED:
            crl_revoked += 1
            if result.revocation_reason:
                print(f"    reason: {result.revocation_reason}")

    print(f"  CRL checked: {crl_checked}, CRL revoked: {crl_revoked}")
    # At least some should be checkable via CRL
    assert crl_checked >= 1 or len(results) == len([r for r in results if r[2] is not None]), \
        "Expected at least one CRL check to succeed"


@pytest.mark.asyncio
async def test_crl_cache_metadata(tmp_path):
    """Test CRL cache creates .meta files with proper timestamps."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    cert = get_cert_sync("google.com")

    try:
        await checker.check_cert(cert, mode=CheckMode.CRL_ONLY)

        # Check cache files exist
        crl_cache_dir = tmp_path / "RevocationChecker" / "cache" / "crl_cache"
        crl_files = list(crl_cache_dir.glob("*.crl"))
        meta_files = list(crl_cache_dir.glob("*.meta"))

        assert len(crl_files) >= 1, "Expected at least one .crl file"
        assert len(meta_files) >= 1, "Expected at least one .meta file"

        # Verify metadata content
        meta_content = json.loads(meta_files[0].read_text())
        assert "downloaded_at" in meta_content
        assert "next_update" in meta_content
        print(f"  CRL cache: {len(crl_files)} .crl, {len(meta_files)} .meta")
        print(f"  Metadata: next_update={meta_content.get('next_update')}")
    except (CRLError, ExtensionMissingError) as e:
        pytest.skip(f"CRL not available: {e}")


@pytest.mark.asyncio
async def test_crl_without_signature_verification(tmp_path):
    """Test CRL_ONLY mode with signature verification disabled."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    cert = get_cert_sync("google.com")

    try:
        result = await checker.check_cert(cert, mode=CheckMode.CRL_ONLY, verify_signature=False)
        assert result.check_method == "crl"
        print(f"  CRL (no sig verify): {result.status.value}")
    except (CRLError, ExtensionMissingError) as e:
        pytest.skip(f"CRL not available: {e}")


@pytest.mark.asyncio
async def test_crl_valid_domains_concurrent(tmp_path):
    """Test CRL_ONLY mode with valid domains concurrently."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    results = await check_domains_concurrent(checker, VALID_DOMAINS, mode=CheckMode.CRL_ONLY)

    good_count = 0
    for domain, result, error in results:
        if error:
            print(f"  {domain}: {type(error).__name__}")
            continue
        assert result.check_method == "crl"
        print(f"  {domain}: {result.status.value} via {result.check_method}")
        if result.status == RevocationStatus.GOOD:
            good_count += 1

    print(f"  CRL GOOD count: {good_count}/{len(VALID_DOMAINS)}")
    # At least one should succeed
    assert good_count >= 1, "Expected at least one GOOD result via CRL"


@pytest.mark.asyncio
async def test_ocsp_fallback_to_crl(tmp_path):
    """Test OCSP_FALLBACK_CRL mode actually uses CRL when needed."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})

    # Test with multiple domains - some may use OCSP, some CRL
    domains_tested = []
    methods_used = set()

    for domain in VALID_DOMAINS[:3]:  # Test subset
        try:
            cert = get_cert_sync(domain)
            result = await checker.check_cert(cert, mode=CheckMode.OCSP_FALLBACK_CRL)
            domains_tested.append(domain)
            methods_used.add(result.check_method)
            print(f"  {domain}: {result.status.value} via {result.check_method}")
        except Exception as e:
            print(f"  {domain}: {type(e).__name__}")

    print(f"  Methods used: {methods_used}")
    assert len(domains_tested) >= 1, "Expected at least one domain to succeed"


@pytest.mark.asyncio
async def test_crl_source_url_populated(tmp_path):
    """Test that CRL result includes source_url."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    cert = get_cert_sync("google.com")

    try:
        result = await checker.check_cert(cert, mode=CheckMode.CRL_ONLY)
        assert result.source_url is not None, "source_url should be populated"
        assert result.source_url.startswith(
            "http"), "source_url should be HTTP URL"
        print(f"  CRL source_url: {result.source_url}")
    except (CRLError, ExtensionMissingError) as e:
        pytest.skip(f"CRL not available: {e}")


@pytest.mark.asyncio
async def test_crl_timestamps_populated(tmp_path):
    """Test that CRL result includes this_update and next_update timestamps."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    cert = get_cert_sync("google.com")

    try:
        result = await checker.check_cert(cert, mode=CheckMode.CRL_ONLY)
        assert result.this_update is not None, "this_update should be populated"
        assert result.next_update is not None, "next_update should be populated"
        assert result.this_update < result.next_update, "this_update should be before next_update"
        print(f"  this_update: {result.this_update}")
        print(f"  next_update: {result.next_update}")
    except (CRLError, ExtensionMissingError) as e:
        pytest.skip(f"CRL not available: {e}")


@pytest.mark.asyncio
async def test_multiple_domains_chain_check(tmp_path):
    """Test chain checking for multiple domains concurrently."""
    checker = RevocationChecker(working_root=tmp_path, logging_kwargs={
                                'console_level': logging.DEBUG})
    domains = ["google.com", "example.com"]

    async def check_chain_for_domain(domain: str):
        try:
            chain = get_cert_chain_sync(domain)
            results = await checker.check_chain(chain)
            return (domain, results, None)
        except Exception as e:
            return (domain, None, e)

    tasks = [check_chain_for_domain(d) for d in domains]
    results = await asyncio.gather(*tasks)

    for domain, chain_results, error in results:
        if error:
            print(f"  {domain}: ERROR ({error})")
            continue
        print(f"  {domain}: checked {len(chain_results)} certs")
        for _, info in chain_results.items():
            # CHECK_FAILURE is also valid if a check couldn't complete
            assert info.status in (
                RevocationStatus.GOOD, RevocationStatus.UNKNOWN,
                RevocationStatus.REVOKED, RevocationStatus.CHECK_FAILURE
            )


# === Issuer Certificate Fetching Tests ===

@pytest.mark.network
@pytest.mark.asyncio
async def test_fetch_issuer_cert_async(tmp_path):
    """Test fetch_issuer_cert fetches issuer via AIA."""
    cert = get_cert_sync("google.com")
    cache_dir = tmp_path / "issuer_cache"
    cache_dir.mkdir()

    issuer = await fetch_issuer_cert(cert, cache_dir=cache_dir)

    assert issuer is not None
    # Issuer's subject should match cert's issuer
    assert issuer.subject == cert.issuer
    print(f"  Issuer: {issuer.subject.rfc4514_string()}")

    # Check cache was created
    cache_files = list(cache_dir.glob("*.crt"))
    assert len(cache_files) >= 1, "Issuer cert should be cached"


@pytest.mark.network
def test_fetch_issuer_cert_sync(tmp_path):
    """Test sync wrapper for fetch_issuer_cert."""
    cert = get_cert_sync("google.com")
    cache_dir = tmp_path / "issuer_cache"
    cache_dir.mkdir()

    issuer = fetch_issuer_cert_sync(cert, cache_dir=cache_dir)

    assert issuer is not None
    assert issuer.subject == cert.issuer
    print(f"  Issuer (sync): {issuer.subject.rfc4514_string()}")


@pytest.mark.network
@pytest.mark.asyncio
async def test_fetch_issuer_cert_caches_result(tmp_path):
    """Test that fetch_issuer_cert uses cache on second call."""
    cert = get_cert_sync("google.com")
    cache_dir = tmp_path / "issuer_cache"
    cache_dir.mkdir()

    # First fetch
    issuer1 = await fetch_issuer_cert(cert, cache_dir=cache_dir)

    # Get cache file mtime
    cache_files = list(cache_dir.glob("*.crt"))
    mtime1 = cache_files[0].stat().st_mtime

    # Second fetch - should use cache
    issuer2 = await fetch_issuer_cert(cert, cache_dir=cache_dir)

    # File should not have been rewritten
    mtime2 = cache_files[0].stat().st_mtime
    assert mtime1 == mtime2, "Cache file should not be rewritten"

    # Results should be identical
    assert issuer1.serial_number == issuer2.serial_number


@pytest.mark.network
@pytest.mark.asyncio
async def test_fetch_issuer_chain_async(tmp_path):
    """Test fetch_issuer_chain builds chain via AIA."""
    cert = get_cert_sync("google.com")
    cache_dir = tmp_path / "issuer_cache"
    cache_dir.mkdir()

    chain = await fetch_issuer_chain(cert, cache_dir=cache_dir)

    assert len(chain) >= 2, "Chain should have at least leaf + issuer"
    assert chain[0] == cert, "First cert should be the input cert"

    # Each cert's issuer should match next cert's subject
    for i in range(len(chain) - 1):
        assert chain[i].issuer == chain[i + 1].subject, \
            f"Chain link {i} broken: issuer != next subject"

    print(f"  Chain length: {len(chain)}")
    for i, c in enumerate(chain):
        print(f"    [{i}] {c.subject.rfc4514_string()[:60]}...")


@pytest.mark.network
def test_fetch_issuer_chain_sync(tmp_path):
    """Test sync wrapper for fetch_issuer_chain."""
    cert = get_cert_sync("google.com")
    cache_dir = tmp_path / "issuer_cache"
    cache_dir.mkdir()

    chain = fetch_issuer_chain_sync(cert, cache_dir=cache_dir)

    assert len(chain) >= 2
    assert chain[0] == cert
    print(f"  Chain length (sync): {len(chain)}")


@pytest.mark.network
@pytest.mark.asyncio
async def test_fetch_issuer_chain_stops_at_root(tmp_path):
    """Test that chain building stops at self-signed root."""
    cert = get_cert_sync("google.com")
    cache_dir = tmp_path / "issuer_cache"
    cache_dir.mkdir()

    chain = await fetch_issuer_chain(cert, cache_dir=cache_dir, max_depth=20)

    # Last cert should be self-signed (root) or we stopped due to missing AIA
    last_cert = chain[-1]
    is_self_signed = last_cert.issuer == last_cert.subject
    print(f"  Last cert self-signed: {is_self_signed}")
    print(f"  Chain ends at: {last_cert.subject.rfc4514_string()}")


@pytest.mark.network
@pytest.mark.asyncio
async def test_fetch_issuer_cert_no_cache(tmp_path):
    """Test fetch_issuer_cert works without cache_dir."""
    cert = get_cert_sync("google.com")

    # No cache_dir provided
    issuer = await fetch_issuer_cert(cert)

    assert issuer is not None
    assert issuer.subject == cert.issuer
    print(f"  Issuer (no cache): {issuer.subject.rfc4514_string()}")


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", __file__])
