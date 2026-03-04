# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu
#
# vCard / jCard parsing adapted from whoisit
# (https://github.com/meeb/whoisit) by meeb.
# Original Copyright (c) meeb, licensed under BSD 3-Clause License.

from typing import Any
from pydantic import ValidationError

from .types import (
    AutnumResponse,
    DomainResponse,
    EntityResponse,
    ErrorResponse,
    IPNetworkResponse,
    NameserverResponse,
    ParseError,
)

# ---------------------------------------------------------------------------
# String cleaning helpers
# ---------------------------------------------------------------------------


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()


def _clean_address_part(value: Any) -> str:
    """Coerce an address component (may be a list) to a string."""
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(_clean_str(v) for v in value).strip()
    if not isinstance(value, str):
        value = str(value)
    return value.strip()


# ---------------------------------------------------------------------------
# jCard / vCard parsing  (RFC 7095)
# ---------------------------------------------------------------------------

# RFC 6350 S6.4.1 TEL capability types
_TEL_CAPABILITY_TYPES = frozenset(
    {"voice", "fax", "cell", "video", "pager", "textphone", "text"}
)


def _get_tel_type(params: dict) -> str | None:
    """Extract the TEL capability type from jCard params."""
    raw = params.get("type")
    if isinstance(raw, str):
        v = raw.lower()
        return v if v in _TEL_CAPABILITY_TYPES else None
    if isinstance(raw, list):
        for item in raw:
            v = item.lower() if isinstance(item, str) else ""
            if v in _TEL_CAPABILITY_TYPES:
                return v
    return None


def _parse_vcard_array(vcard_array: list) -> dict[str, Any] | None:
    """Parse a jCard ``vcardArray`` into a dict for :class:`~.types.Contact`."""

    if not isinstance(vcard_array, list) or len(vcard_array) != 2:
        return None
    tag, properties = vcard_array
    if tag != "vcard" or not isinstance(properties, list):
        return None

    contact: dict[str, Any] = {}
    emails: list[str] = []
    tels: list[str] = []
    faxes: list[str] = []

    for prop in properties:
        if not isinstance(prop, list) or len(prop) != 4:
            continue
        name, params, _, value = prop

        if name == "fn":
            text = _clean_str(value)
            if text:
                contact["fn"] = text
        elif name == "org":
            text = _clean_str(value)
            if text:
                contact["org"] = text
        elif name == "email":
            text = _clean_str(value)
            if text:
                emails.append(text)
        elif name == "tel":
            raw_tel = _clean_str(value)
            if raw_tel.lower().startswith("tel:"):
                raw_tel = raw_tel[4:]
            if raw_tel:
                tel_type = _get_tel_type(params)
                if tel_type == "fax":
                    faxes.append(raw_tel)
                else:
                    tels.append(raw_tel)
        elif name == "adr" and isinstance(value, list) and len(value) == 7:
            addr = {
                "po_box": _clean_address_part(value[0]),
                "extended_address": _clean_address_part(value[1]),
                "street_address": _clean_address_part(value[2]),
                "locality": _clean_address_part(value[3]),
                "region": _clean_address_part(value[4]),
                "postal_code": _clean_address_part(value[5]),
                "country": _clean_address_part(value[6]),
            }
            label = _clean_str(params.get("label"))
            if label:
                addr["label"] = label
            cc = _clean_str(params.get("cc"))
            if cc:
                addr["cc"] = cc
            contact["address"] = addr
        elif name == "kind":
            text = _clean_str(value)
            if text:
                contact["kind"] = text
        elif name == "contact-uri":
            text = _clean_str(value)
            if text:
                contact["contact_uri"] = text
        elif name == "url":
            text = _clean_str(value)
            if text:
                contact["url"] = text

    if emails:
        contact["emails"] = emails
    if tels:
        contact["tels"] = tels
    if faxes:
        contact["faxes"] = faxes

    return contact or None


# ---------------------------------------------------------------------------
# vcardArray extraction
# ---------------------------------------------------------------------------


def _extract_vcards(obj: Any) -> Any:
    """
    Recursively replace ``vcardArray`` entries with parsed contact dicts.
    """
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for key, value in obj.items():
            if key == "vcardArray":
                contact = _parse_vcard_array(value)
                if contact is not None:
                    result["contact"] = contact
            else:
                result[key] = _extract_vcards(value)
        return result
    if isinstance(obj, list):
        return [_extract_vcards(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _parse_response[T](
    raw: dict,
    model: type[T],
    expected_class: str,
    label: str,
) -> T:
    obj_class = raw.get("objectClassName")
    if obj_class is not None and obj_class != expected_class:
        raise ParseError(
            f'Expected objectClassName "{expected_class}", got "{obj_class}"'
        )
    converted = _extract_vcards(raw)
    try:
        return model.model_validate(converted)
    except ValidationError as exc:
        raise ParseError(f"Failed to validate {label} response: {exc}") from exc


def parse_domain_response(raw: dict) -> DomainResponse:
    """
    Parse a raw RDAP JSON dict into a :class:`~.types.DomainResponse`.

    Raises :class:`~.types.ParseError` if ``objectClassName`` is present
    and is not ``"domain"``, or if the response fails Pydantic validation.
    Missing or unknown fields are silently ignored.
    """
    return _parse_response(raw, DomainResponse, "domain", "domain")


def parse_ip_network_response(raw: dict) -> IPNetworkResponse:
    """
    Parse a raw RDAP JSON dict into an :class:`~.types.IPNetworkResponse`.

    Raises :class:`~.types.ParseError` if ``objectClassName`` is present
    and is not ``"ip network"``, or if the response fails Pydantic validation.
    Missing or unknown fields are silently ignored.
    """
    return _parse_response(raw, IPNetworkResponse, "ip network", "IP network")


def parse_autnum_response(raw: dict) -> AutnumResponse:
    """
    Parse a raw RDAP JSON dict into an :class:`~.types.AutnumResponse`.

    Raises :class:`~.types.ParseError` if ``objectClassName`` is present
    and is not ``"autnum"``, or if the response fails Pydantic validation.
    Missing or unknown fields are silently ignored.
    """
    return _parse_response(raw, AutnumResponse, "autnum", "autnum")


def parse_entity_response(raw: dict) -> EntityResponse:
    """
    Parse a raw RDAP JSON dict into an :class:`~.types.EntityResponse`.

    Raises :class:`~.types.ParseError` if ``objectClassName`` is present
    and is not ``"entity"``, or if the response fails Pydantic validation.
    Missing or unknown fields are silently ignored.
    """
    return _parse_response(raw, EntityResponse, "entity", "entity")


def parse_nameserver_response(raw: dict) -> NameserverResponse:
    """
    Parse a raw RDAP JSON dict into a :class:`~.types.NameserverResponse`.

    Raises :class:`~.types.ParseError` if ``objectClassName`` is present
    and is not ``"nameserver"``, or if the response fails Pydantic validation.
    Missing or unknown fields are silently ignored.
    """
    return _parse_response(raw, NameserverResponse, "nameserver", "nameserver")


def parse_error_response(raw: dict) -> ErrorResponse:
    """Parse an RDAP error response body."""
    try:
        return ErrorResponse.model_validate(raw)
    except ValidationError as exc:
        raise ParseError(f"Failed to validate error response: {exc}") from exc


def recursive_merge(base: dict, override: dict) -> None:
    """
    Deep-merge *override* into *base*, modifying *base* in place.

    Used to merge TLD override data into IANA bootstrap structures.
    """
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            recursive_merge(base[key], value)
        else:
            base[key] = value
