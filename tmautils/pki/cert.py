from typing import Optional
import ssl
import socket
import cryptography.x509 as x509
import certifi
import asyncio
import contextlib

from tmautils.common import IPAddress


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


def _get_cert_leaf_or_chain(
    host: str | IPAddress,
    port: int,
    sni: Optional[str],
    timeout: float,
    verify: bool,
    use_certifi: bool,
    chain: bool,
):
    connect_host = str(host)
    server_hostname = str(sni) if sni is not None else connect_host
    ctx = _build_ctx(verify=verify, use_certifi=use_certifi)

    with socket.create_connection((connect_host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=server_hostname) as sslsock:
            if chain:
                der_list = sslsock.get_verified_chain()  # works for verify=False too
                return [
                    x509.load_der_x509_certificate(der) for der in der_list
                ]
            else:
                der = sslsock.getpeercert(binary_form=True)
                return x509.load_der_x509_certificate(der)


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
            Connection timeout in seconds. Default is 8.0 seconds.

        verify:
            Whether to verify the server's TLS certificate. Default is True.

        use_certifi:
            Whether to use the `certifi` CA bundle for verification. Default is True.
            If False, the system's default CA bundle is used.
            Ignored if `verify` is False.

    Returns:
        An `x509.Certificate` object representing the server's TLS certificate.
    """

    return _get_cert_leaf_or_chain(
        host=host, port=port, sni=sni,
        timeout=timeout,
        verify=verify,
        use_certifi=use_certifi,
        chain=False,
    )


def get_cert_chain(
    host: str | IPAddress,
    port: int = 443,
    *,
    sni: Optional[str] = None,
    timeout: float = 8.0,
    verify: bool = True,
    use_certifi: bool = True,
) -> list[x509.Certificate]:
    """
    Retrieve the TLS certificate chain from a server.
    See `get_cert_chain_async()` for async version.

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

        verify:
            Whether to verify the server's TLS certificate. Default is True.

        use_certifi:
            Whether to use the `certifi` CA bundle for verification. Default is True.
            If False, the system's default CA bundle is used.
            Ignored if `verify` is False.

    Returns:
        A list of `x509.Certificate` objects representing the server's TLS certificate chain.
    """

    return _get_cert_leaf_or_chain(
        host=host, port=port, sni=sni,
        timeout=timeout,
        verify=verify,
        use_certifi=use_certifi,
        chain=True,
    )


async def _get_cert_leaf_or_chain_async(
    host: str | IPAddress,
    port: int,
    sni: Optional[str],
    timeout: float,
    ssl_handshake_timeout: float,
    verify: bool,
    use_certifi: bool,
    chain: bool,
):
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

        verify:
            Whether to verify the server's TLS certificate. Default is True.

        use_certifi:
            Whether to use the `certifi` CA bundle for verification. Default is True.
            If False, the system's default CA bundle is used.
            Ignored if `verify` is False.

    Returns:
        An `x509.Certificate` object representing the server's TLS certificate.
    """

    return await _get_cert_leaf_or_chain_async(
        host=host, port=port, sni=sni,
        timeout=timeout,
        ssl_handshake_timeout=ssl_handshake_timeout,
        verify=verify,
        use_certifi=use_certifi,
        chain=False,
    )


async def get_cert_chain_async(
    host: str | IPAddress,
    port: int = 443,
    *,
    sni: Optional[str] = None,
    timeout: float = 8.0,
    ssl_handshake_timeout: float = 5.0,
    verify: bool = True,
    use_certifi: bool = True,
) -> list[x509.Certificate]:
    """
    Asynchronously retrieve the TLS certificate chain from a server.

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

        verify:
            Whether to verify the server's TLS certificate. Default is True.

        use_certifi:
            Whether to use the `certifi` CA bundle for verification. Default is True.
            If False, the system's default CA bundle is used.
            Ignored if `verify` is False.

    Returns:
        A list of `x509.Certificate` objects representing the server's TLS certificate chain.
    """

    return await _get_cert_leaf_or_chain_async(
        host=host, port=port, sni=sni,
        timeout=timeout,
        ssl_handshake_timeout=ssl_handshake_timeout,
        verify=verify,
        use_certifi=use_certifi,
        chain=True,
    )
