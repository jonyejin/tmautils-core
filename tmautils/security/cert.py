import ssl
import socket
import cryptography.x509 as x509
import certifi
import asyncio
import contextlib

from tmautils.common import *


def _build_ctx(*, verify: bool, use_certifi: bool):
    if verify:
        # default context sets CERT_REQUIRED and check_hostname=True
        cafile = certifi.where() if use_certifi else None
        ctx = ssl.create_default_context(cafile=cafile)
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def get_cert(
    host: str | IPAddress,
    port: int = 443,
    *,
    sni: Optional[str] = None,
    timeout: float = 8.0,
    verify: bool = True,
    use_certifi: bool = True,
):
    """
    Retrieve the TLS certificate from a server.
    See `get_cert_async()` for async version.

    Args:
        host:
            The hostname or IP address of the server.

        port:
            The port number to connect to. Default is 443.

        sni:
            The Server Name Indication (SNI) to use. If None, `host` is used.
            Useful when connecting to an IP address but the certificate is for a domain name.

        timeout:
            Connection timeout in seconds. Default is 5.0 seconds.

        verify:
            Whether to verify the server's TLS certificate. Default is True.

        use_certifi:
            Whether to use the `certifi` CA bundle for verification. Default is True.
            If False, the system's default CA bundle is used.
            Ignored if `verify` is False.

    Returns:
        An `x509.Certificate` object representing the server's TLS certificate.
    """

    connect_host = str(host)
    server_hostname = str(sni) if sni is not None else connect_host
    ctx = _build_ctx(verify=verify, use_certifi=use_certifi)

    with socket.create_connection((connect_host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=server_hostname) as sslsock:
            der = sslsock.getpeercert(binary_form=True)
            return x509.load_der_x509_certificate(der)


async def get_cert_async(
    host: str | IPAddress,
    port: int = 443,
    *,
    sni: Optional[str] = None,
    timeout: float = 8.0,
    ssl_handshake_timeout: float = 5.0,
    verify: bool = True,
    use_certifi: bool = True,
):
    """
    Asynchronously retrieve the TLS certificate from a server.

    See `get_cert()` (synchronous version) for details.
    """

    connect_host = str(host)
    server_hostname = str(sni) if sni is not None else connect_host
    ctx = _build_ctx(verify=verify, use_certifi=use_certifi)

    writer = None
    try:
        async with asyncio.timeout(timeout):
            try:
                _, writer = await asyncio.open_connection(
                    connect_host,
                    port,
                    ssl=ctx,
                    server_hostname=server_hostname,
                    ssl_handshake_timeout=ssl_handshake_timeout,
                )

                sslobj: ssl.SSLObject | None = writer.get_extra_info(
                    "ssl_object"
                )
                if sslobj is None:
                    raise RuntimeError("TLS handshake did not complete")
                der = sslobj.getpeercert(binary_form=True)
                return x509.load_der_x509_certificate(der)
            finally:
                if writer is not None:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"get_cert_async() for {host}:{port} timed out after {timeout}s"
        )
