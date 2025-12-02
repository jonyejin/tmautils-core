from dns.message import QueryMessage
from dns.name import Name
from dns.rrset import RRset
import dns.flags
import hashlib


def dns_msg_semantic_hash(
    msg: QueryMessage,
    keep_flags=("AA", "AD", "TC"),
    keep_rcode=True,
    ignore_opt=True,
):
    """
    Compute a semantic hash of a DNS message.
    The hash is invariant to:
    - Order of RRs in each section
    - Order of sections
    - TTL values
    - Presence of OPT RR (if ignore_opt is True)
    - Flags not in keep_flags
    - RCODE (if keep_rcode is False)

    Args:
        msg:
            The DNS message to hash.

        keep_flags:
            A tuple of flag names to include in the hash.
            Other flags will be ignored.
            Default: ("AA", "AD", "TC")

        keep_rcode:
            Whether to include the RCODE in the hash.

        ignore_opt:
            Whether to ignore OPT RRs in the additional section.

    Returns:
        A hex string representing the semantic hash of the DNS message.
    """

    def _canon_name(name: Name): return name.canonicalize().to_text()

    def _rrset_rdata_fingerprint(rrset: RRset):
        # Order-insensitive and no TTL
        rdatas = sorted((rdata.to_wire() for rdata in rrset), key=lambda b: b)
        blob = b"\x00".join(rdatas)
        return hashlib.sha256(blob).hexdigest()

    parts = []

    # Questions
    for q in msg.question:
        parts += [
            b"Q",
            _canon_name(q.name).encode(),
            str(q.rdtype).encode(),
            str(q.rdclass).encode(),
        ]

    # Header
    if keep_rcode:
        parts += [b"RCODE", str(msg.rcode()).encode()]
    for k in keep_flags:
        parts += [k.encode(), b"1" if (msg.flags & dns.flags.Flag[k]) else b"0"]

    # Sections
    for tag, section in {
        "ANS": msg.answer,
        "AUTH": msg.authority,
        "ADD": msg.additional,
    }.items():
        parts.append(tag.encode())
        for rrset in section:
            if ignore_opt and rrset.rdtype == dns.rdatatype.OPT:
                continue
            rr_fprint = _rrset_rdata_fingerprint(rrset)
            parts += [
                b"N", _canon_name(rrset.name).encode(),
                b"T", str(rrset.rdtype).encode(),
                b"F", rr_fprint.encode(),
            ]

    return hashlib.sha256(b"\x1f".join(parts)).hexdigest()
