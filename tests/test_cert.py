import os
import ssl
import socket
import asyncio
import pytest
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from ipaddress import IPv4Address, IPv6Address
from datetime import datetime, timedelta, timezone

import cryptography.x509 as x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import certifi

from tmautils.security.cert import (
    get_cert,
    get_cert_chain,
    get_cert_async,
    get_cert_chain_async,
)
from tmautils.common import IPAddress


# --------------------------- Mock Classes ---------------------------

class MockSSLSocket:
    """Mock SSL socket for sync tests"""

    def __init__(self, cert_der: bytes, chain_der_list: list[bytes] | None = None):
        self.cert_der = cert_der
        self.chain_der_list = chain_der_list or [cert_der]
        self.closed = False

    def getpeercert(self, binary_form=False):
        """Mock getpeercert - returns DER bytes when binary_form=True"""
        if binary_form:
            return self.cert_der
        raise NotImplementedError("Dict form not needed for tests")

    def get_verified_chain(self):
        """Mock get_verified_chain - returns list of DER bytes"""
        return self.chain_der_list

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


class MockSSLObject:
    """Mock SSL object returned by asyncio connections"""

    def __init__(self, cert_der: bytes, chain_der_list: list[bytes] | None = None):
        self.cert_der = cert_der
        self.chain_der_list = chain_der_list or [cert_der]

    def getpeercert(self, binary_form=False):
        if binary_form:
            return self.cert_der
        raise NotImplementedError("Dict form not needed")

    def get_verified_chain(self):
        return self.chain_der_list


class MockStreamWriter:
    """Mock asyncio StreamWriter"""

    def __init__(self, ssl_object: MockSSLObject | None):
        self.ssl_object = ssl_object
        self._closed = False

    def get_extra_info(self, name):
        if name == "ssl_object":
            return self.ssl_object
        return None

    def close(self):
        self._closed = True

    async def wait_closed(self):
        await asyncio.sleep(0)  # Simulate async close


# --------------------------- Helpers ---------------------------

def _create_test_cert(
    cn: str = "test.example.com",
    san_list: list[str] | None = None,
    days_valid: int = 365
) -> tuple[bytes, x509.Certificate]:
    """
    Generate a self-signed certificate for testing.
    Returns (DER bytes, x509.Certificate object)
    """
    # Generate 2048-bit RSA key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Build certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ])

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=days_valid))
    )

    # Add SAN extension if provided
    if san_list:
        san_extension = x509.SubjectAlternativeName([
            x509.DNSName(name) for name in san_list
        ])
        builder = builder.add_extension(san_extension, critical=False)

    # Self-sign
    cert = builder.sign(private_key, hashes.SHA256())

    # Return both DER bytes and parsed certificate
    der_bytes = cert.public_bytes(serialization.Encoding.DER)
    return der_bytes, cert


def _run_async(coro):
    """Helper to run async test code in sync test"""
    return asyncio.run(coro)


def _assert_cert_equals(cert1: x509.Certificate, cert2: x509.Certificate):
    """Compare two certificates for equality"""
    assert cert1.subject == cert2.subject
    assert cert1.serial_number == cert2.serial_number
    assert cert1.issuer == cert2.issuer


# --------------------------- Fixtures ---------------------------

@pytest.fixture
def test_cert_single():
    """Single test certificate (leaf only)"""
    der, cert = _create_test_cert(cn="single.example.com")
    return {"der": der, "cert": cert}


@pytest.fixture
def test_cert_chain():
    """3-certificate chain (leaf → intermediate → root)"""
    leaf_der, leaf = _create_test_cert(cn="leaf.example.com")
    inter_der, inter = _create_test_cert(cn="intermediate-ca.example.com")
    root_der, root = _create_test_cert(cn="root-ca.example.com")
    return {
        "leaf": {"der": leaf_der, "cert": leaf},
        "intermediate": {"der": inter_der, "cert": inter},
        "root": {"der": root_der, "cert": root},
        "chain_ders": [leaf_der, inter_der, root_der]
    }


# --------------------------- Tests: get_cert ---------------------------

def test_get_cert_basic_hostname(test_cert_single):
    """Test basic get_cert call with hostname"""
    cert_der = test_cert_single["der"]
    expected_cert = test_cert_single["cert"]

    mock_socket = Mock()
    mock_sslsock = MockSSLSocket(cert_der)

    with patch('socket.create_connection') as mock_create_conn, \
         patch('ssl.create_default_context') as mock_create_ctx:

        mock_create_conn.return_value.__enter__.return_value = mock_socket
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_sslsock
        mock_create_ctx.return_value = mock_ctx

        result = get_cert("example.com")

        # Verify connection parameters
        mock_create_conn.assert_called_once_with(
            ("example.com", 443),
            timeout=8.0
        )

        # Verify SSL wrapping
        mock_ctx.wrap_socket.assert_called_once_with(
            mock_socket,
            server_hostname="example.com"
        )

        # Verify result
        assert isinstance(result, x509.Certificate)
        _assert_cert_equals(result, expected_cert)


def test_get_cert_with_custom_sni(test_cert_single):
    """Test get_cert with custom SNI (e.g., IP address + SNI hostname)"""
    cert_der = test_cert_single["der"]

    mock_socket = Mock()
    mock_sslsock = MockSSLSocket(cert_der)

    with patch('socket.create_connection') as mock_create_conn, \
         patch('ssl.create_default_context') as mock_create_ctx:

        mock_create_conn.return_value.__enter__.return_value = mock_socket
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_sslsock
        mock_create_ctx.return_value = mock_ctx

        result = get_cert("192.0.2.1", sni="example.com")

        # Verify connection to IP
        mock_create_conn.assert_called_once_with(
            ("192.0.2.1", 443),
            timeout=8.0
        )

        # Verify SNI is the hostname
        mock_ctx.wrap_socket.assert_called_once_with(
            mock_socket,
            server_hostname="example.com"
        )

        assert isinstance(result, x509.Certificate)


def test_get_cert_verify_false(test_cert_single):
    """Test get_cert with verification disabled"""
    cert_der = test_cert_single["der"]

    mock_socket = Mock()
    mock_sslsock = MockSSLSocket(cert_der)

    with patch('socket.create_connection') as mock_create_conn, \
         patch('ssl.SSLContext') as mock_ssl_context_cls:

        mock_create_conn.return_value.__enter__.return_value = mock_socket
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_sslsock
        mock_ssl_context_cls.return_value = mock_ctx

        result = get_cert("example.com", verify=False)

        # Verify SSL context created with CERT_NONE
        mock_ssl_context_cls.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
        assert mock_ctx.check_hostname is False
        assert mock_ctx.verify_mode == ssl.CERT_NONE

        assert isinstance(result, x509.Certificate)


def test_get_cert_connection_timeout():
    """Test get_cert handles connection timeout"""
    with patch('socket.create_connection') as mock_create_conn:
        mock_create_conn.side_effect = socket.timeout("Connection timed out")

        with pytest.raises(socket.timeout):
            get_cert("example.com")


# --------------------------- Tests: get_cert_chain ---------------------------

def test_get_cert_chain_basic(test_cert_chain):
    """Test basic get_cert_chain call with 3-cert chain"""
    chain_ders = test_cert_chain["chain_ders"]

    mock_socket = Mock()
    mock_sslsock = MockSSLSocket(chain_ders[0], chain_ders)

    with patch('socket.create_connection') as mock_create_conn, \
         patch('ssl.create_default_context') as mock_create_ctx:

        mock_create_conn.return_value.__enter__.return_value = mock_socket
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_sslsock
        mock_create_ctx.return_value = mock_ctx

        result = get_cert_chain("example.com")

        # Verify returns list of certificates
        assert isinstance(result, list)
        assert len(result) == 3
        assert all(isinstance(cert, x509.Certificate) for cert in result)


# --------------------------- Tests: get_cert_async ---------------------------

def test_get_cert_async_basic(test_cert_single):
    """Test basic get_cert_async call"""
    cert_der = test_cert_single["der"]
    expected_cert = test_cert_single["cert"]

    async def _test():
        mock_ssl_obj = MockSSLObject(cert_der)
        mock_writer = MockStreamWriter(mock_ssl_obj)

        with patch('asyncio.open_connection', new_callable=AsyncMock) as mock_open_conn:
            mock_open_conn.return_value = (None, mock_writer)

            result = await get_cert_async("example.com")

            # Verify connection parameters
            mock_open_conn.assert_called_once()
            call_kwargs = mock_open_conn.call_args[1]
            assert call_kwargs['server_hostname'] == "example.com"
            assert call_kwargs['ssl_handshake_timeout'] == 5.0

            # Verify result
            assert isinstance(result, x509.Certificate)
            _assert_cert_equals(result, expected_cert)

            return result

    result = _run_async(_test())
    assert isinstance(result, x509.Certificate)


def test_get_cert_async_ssl_object_none_raises(test_cert_single):
    """Test get_cert_async raises RuntimeError when SSL object is None"""

    async def _test():
        # Mock writer that returns None for ssl_object
        mock_writer = MockStreamWriter(None)

        with patch('asyncio.open_connection', new_callable=AsyncMock) as mock_open_conn:
            mock_open_conn.return_value = (None, mock_writer)

            with pytest.raises(RuntimeError, match="TLS handshake did not complete"):
                await get_cert_async("example.com")

    _run_async(_test())


def test_get_cert_async_timeout_error_message():
    """Test that get_cert_async converts asyncio.TimeoutError with proper message"""

    async def _test():
        with patch('asyncio.open_connection', new_callable=AsyncMock) as mock_open_conn:
            # Simulate timeout
            mock_open_conn.side_effect = asyncio.TimeoutError()

            with pytest.raises(TimeoutError, match="get_cert.*timed out"):
                await get_cert_async("example.com", port=8443)

    _run_async(_test())


# --------------------------- Tests: get_cert_chain_async ---------------------------

def test_get_cert_chain_async_basic(test_cert_chain):
    """Test basic get_cert_chain_async call"""
    chain_ders = test_cert_chain["chain_ders"]

    async def _test():
        mock_ssl_obj = MockSSLObject(chain_ders[0], chain_ders)
        mock_writer = MockStreamWriter(mock_ssl_obj)

        with patch('asyncio.open_connection', new_callable=AsyncMock) as mock_open_conn:
            mock_open_conn.return_value = (None, mock_writer)

            result = await get_cert_chain_async("example.com")

            # Verify returns list of certificates
            assert isinstance(result, list)
            assert len(result) == 3
            assert all(isinstance(cert, x509.Certificate) for cert in result)

            return result

    result = _run_async(_test())
    assert isinstance(result, list)
    assert len(result) == 3


# --------------------------- Tests: Errors & Edge Cases ---------------------------

def test_sync_connection_refused():
    """Test sync functions handle ConnectionRefusedError"""
    with patch('socket.create_connection') as mock_create_conn:
        mock_create_conn.side_effect = ConnectionRefusedError("Connection refused")

        with pytest.raises(ConnectionRefusedError):
            get_cert("example.com")


def test_sync_ssl_error():
    """Test sync functions handle SSLError"""
    with patch('socket.create_connection') as mock_create_conn, \
         patch('ssl.create_default_context') as mock_create_ctx:

        mock_socket = Mock()
        mock_create_conn.return_value.__enter__.return_value = mock_socket
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.side_effect = ssl.SSLError("SSL handshake failed")
        mock_create_ctx.return_value = mock_ctx

        with pytest.raises(ssl.SSLError):
            get_cert("example.com")


def test_async_cleanup_on_exception(test_cert_single):
    """Test async functions clean up resources even when exceptions occur"""

    async def _test():
        mock_ssl_obj = MockSSLObject(test_cert_single["der"])
        mock_writer = MockStreamWriter(mock_ssl_obj)

        # Make getpeercert raise an exception
        mock_ssl_obj.getpeercert = Mock(side_effect=RuntimeError("Test error"))

        with patch('asyncio.open_connection', new_callable=AsyncMock) as mock_open_conn:
            mock_open_conn.return_value = (None, mock_writer)

            with pytest.raises(RuntimeError):
                await get_cert_async("example.com")

            # Verify cleanup still happened
            assert mock_writer._closed is True

    _run_async(_test())


# --------------------------- Tests: Parametrized ---------------------------

@pytest.mark.parametrize("host", [
    "example.com",
    IPv4Address("192.0.2.1"),
    IPv6Address("2001:db8::1"),
    "192.0.2.1",
])
def test_host_type_variations(host, test_cert_single):
    """Test various host type inputs (str, IPv4Address, IPv6Address)"""
    cert_der = test_cert_single["der"]

    mock_socket = Mock()
    mock_sslsock = MockSSLSocket(cert_der)

    with patch('socket.create_connection') as mock_create_conn, \
         patch('ssl.create_default_context') as mock_create_ctx:

        mock_create_conn.return_value.__enter__.return_value = mock_socket
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_sslsock
        mock_create_ctx.return_value = mock_ctx

        result = get_cert(host)

        # Verify host is converted to string for connection
        call_args = mock_create_conn.call_args[0]
        assert isinstance(call_args[0][0], str)
        assert isinstance(result, x509.Certificate)


# --------------------------- Main ---------------------------

if __name__ == "__main__":
    pytest.main(["-vv", "-rA", os.path.abspath(__file__)])
