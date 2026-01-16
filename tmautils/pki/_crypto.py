from functools import lru_cache
from typing import Any
import hashlib

import cryptography.x509 as x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ec, dsa, ed25519, ed448, types


def get_cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


@lru_cache(maxsize=1024)
def parse_cert_lrucached(der_bytes: bytes) -> x509.Certificate:
    return x509.load_der_x509_certificate(der_bytes)


def verify_signature(
    public_key: Any,
    data: bytes,
    signature: bytes,
    signature_hash_alg: hashes.HashAlgorithm | None,
    signature_hash_alg_params: Any | None,
) -> None:
    # RSA, ECDSA and DSA need hash algorithm specified
    if signature_hash_alg is None and isinstance(
        public_key,
        (rsa.RSAPublicKey, ec.EllipticCurvePublicKey, dsa.DSAPublicKey)
    ):
        raise ValueError(
            "RSA/ECDSA/DSA signature verification requires hash algorithm"
        )

    if isinstance(public_key, rsa.RSAPublicKey):
        if signature_hash_alg_params is None:
            raise ValueError(
                "RSA signature verification requires padding parameters"
            )
        public_key.verify(
            signature, data, signature_hash_alg_params, signature_hash_alg,
        )

    elif isinstance(public_key, ec.EllipticCurvePublicKey):
        if isinstance(signature_hash_alg_params, ec.ECDSA):
            algo = signature_hash_alg_params
        else:
            algo = ec.ECDSA(signature_hash_alg)
        public_key.verify(signature, data, algo)

    elif isinstance(public_key, dsa.DSAPublicKey):
        public_key.verify(signature, data, signature_hash_alg)

    elif isinstance(public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
        public_key.verify(signature, data)

    else:
        raise TypeError(f"Unsupported public key type: {type(public_key)}")


def _read_len(buf: bytes, i: int) -> tuple[int, int]:
    """Return (length, new_index_after_length_octets)."""

    if i >= len(buf):
        raise ValueError("Invalid DER: truncated length")

    first = buf[i]
    if first & 0x80:  # long form
        num_octets = first & 0x7F
        if num_octets == 0:
            raise ValueError(
                "Invalid DER: indefinite length is not allowed"
            )
        if i + 1 + num_octets > len(buf):
            raise ValueError("Invalid DER: truncated long-form length")
        length = int.from_bytes(buf[i + 1:i + 1 + num_octets], "big")
        return length, i + 1 + num_octets

    # short form
    return first, i + 1


def extract_spki_public_key_bytes(public_key: types.CertificatePublicKeyTypes) -> bytes:
    # According to RFC 6960, issuerKeyHash is computed over
    # "the value (excluding tag and length) of the subject public key field
    # in the issuer's certificate."

    # Get SubjectPublicKeyInfo DER encoding
    spki_der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo
    )

    # SubjectPublicKeyInfo ::= SEQUENCE {
    #     algorithm AlgorithmIdentifier,
    #     subjectPublicKey BIT STRING
    # }

    # Parse the outer SEQUENCE
    if not spki_der or spki_der[0] != 0x30:  # SEQUENCE tag
        raise ValueError("Invalid SPKI: expected SEQUENCE")

    # Skip SEQUENCE tag and length
    idx = 1
    _, idx = _read_len(spki_der, idx)

    # Now at AlgorithmIdentifier SEQUENCE - skip it
    if idx >= len(spki_der) or spki_der[idx] != 0x30:  # SEQUENCE tag
        raise ValueError("Invalid SPKI: expected AlgorithmIdentifier SEQUENCE")
    idx += 1
    alg_len, idx = _read_len(spki_der, idx)
    if idx + alg_len > len(spki_der):
        raise ValueError("Invalid SPKI: truncated AlgorithmIdentifier")
    idx += alg_len

    # Now at BIT STRING containing the public key
    if idx >= len(spki_der) or spki_der[idx] != 0x03:  # BIT STRING tag
        raise ValueError("Invalid SPKI: expected BIT STRING")
    idx += 1

    # Parse BIT STRING length
    bit_string_len, idx = _read_len(spki_der, idx)
    if idx + bit_string_len > len(spki_der):
        raise ValueError("Invalid SPKI: truncated BIT STRING")

    # Return BIT STRING value bytes (including unused bits octet)
    return spki_der[idx:idx + bit_string_len]
