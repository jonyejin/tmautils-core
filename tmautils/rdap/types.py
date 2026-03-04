# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class RDAPError(Exception):
    """Base exception for all RDAP errors."""


class BootstrapError(RDAPError):
    """Bootstrap data fetch/parse errors."""


class ParseError(RDAPError):
    """RDAP response parsing errors."""


class QueryError(RDAPError):
    """RDAP query errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        response: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class ResourceDoesNotExist(QueryError):
    """RDAP resource returned 404."""


class ResourceAccessDeniedError(QueryError):
    """RDAP resource returned 401/403."""


class RateLimitedError(QueryError):
    """RDAP resource returned 429."""


class RemoteServerError(QueryError):
    """RDAP resource returned 5xx."""


# ---------------------------------------------------------------------------
# Enum base with IANA registry metadata
# ---------------------------------------------------------------------------


class _RDAPEnum(StrEnum):
    """
    StrEnum base that carries IANA registry metadata on each member.

    Each member may be defined as ``(value, description, reference)`` or
    as a plain string (description and reference default to ``""``).

    Attributes on each member:
        description: IANA registry description text.
        reference: Originating specification (e.g. ``"RFC 9083"``).
    """

    def __new__(cls, value: str, description: str = "", reference: str = "") -> _RDAPEnum:
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.description = description  # type: ignore[attr-defined]
        obj.reference = reference  # type: ignore[attr-defined]
        return obj

    description: str
    reference: str


# ---------------------------------------------------------------------------
# Enums — IANA RDAP JSON Values registry
# https://www.iana.org/assignments/rdap-json-values/
# ---------------------------------------------------------------------------


class EventAction(_RDAPEnum):
    REGISTRATION = (
        "registration",
        "The object instance was initially registered.",
        "RFC 9083",
    )
    REREGISTRATION = (
        "reregistration",
        "The object instance was registered subsequently to initial registration.",
        "RFC 9083",
    )
    LAST_CHANGED = (
        "last changed",
        "An action noting when the information in the object instance was last changed.",
        "RFC 9083",
    )
    EXPIRATION = (
        "expiration",
        "The object instance has been removed or will be removed at a predetermined date and time from the registry.",
        "RFC 9083",
    )
    DELETION = (
        "deletion",
        "The object instance was removed from the registry at a point in time that was not predetermined.",
        "RFC 9083",
    )
    REINSTANTIATION = (
        "reinstantiation",
        "The object instance was reregistered after having been removed from the registry.",
        "RFC 9083",
    )
    TRANSFER = (
        "transfer",
        "The object instance was transferred from one registrar to another.",
        "RFC 9083",
    )
    LOCKED = (
        "locked",
        "The object instance was locked (see the \"locked\" status).",
        "RFC 9083",
    )
    UNLOCKED = (
        "unlocked",
        "The object instance was unlocked (see the \"locked\" status).",
        "RFC 9083",
    )
    LAST_UPDATE_OF_RDAP_DATABASE = (
        "last update of RDAP database",
        "An action noting when the information in the object instance in the RDAP database was last synchronized from the authoritative database (e.g. registry database).",
        "ICANN",
    )
    REGISTRAR_EXPIRATION = (
        "registrar expiration",
        "An action noting the expiration date of the object in the registrar system.",
        "ICANN",
    )
    ENUM_VALIDATION_EXPIRATION = (
        "enum validation expiration",
        "Association of phone number represented by this ENUM domain to registrant has expired or will expire at a predetermined date and time.",
        "CZ.NIC z.s.p.o.",
    )


class Role(_RDAPEnum):
    REGISTRANT = (
        "registrant",
        "The entity object instance is the registrant of the registration. In some registries, this is known as a maintainer.",
        "RFC 9083",
    )
    TECHNICAL = (
        "technical",
        "The entity object instance is a technical contact for the registration.",
        "RFC 9083",
    )
    ADMINISTRATIVE = (
        "administrative",
        "The entity object instance is an administrative contact for the registration.",
        "RFC 9083",
    )
    ABUSE = (
        "abuse",
        "The entity object instance handles network abuse issues on behalf of the registrant of the registration.",
        "RFC 9083",
    )
    BILLING = (
        "billing",
        "The entity object instance handles payment and billing issues on behalf of the registrant of the registration.",
        "RFC 9083",
    )
    REGISTRAR = (
        "registrar",
        "The entity object instance represents the authority responsible for the registration in the registry.",
        "RFC 9083",
    )
    RESELLER = (
        "reseller",
        "The entity object instance represents a third party through which the registration was conducted (i.e., not the registry or registrar).",
        "RFC 9083",
    )
    SPONSOR = (
        "sponsor",
        "The entity object instance represents a domain policy sponsor, such as an ICANN-approved sponsor.",
        "RFC 9083",
    )
    PROXY = (
        "proxy",
        "The entity object instance represents a proxy for another entity object, such as a registrant.",
        "RFC 9083",
    )
    NOTIFICATIONS = (
        "notifications",
        "An entity object instance designated to receive notifications about association object instances.",
        "RFC 9083",
    )
    NOC = (
        "noc",
        "The entity object instance handles communications related to a network operations center (NOC).",
        "RFC 9083",
    )


class RDAPStatus(_RDAPEnum):
    VALIDATED = (
        "validated",
        "Signifies that the data of the object instance has been found to be accurate. This type of status is usually found on entity object instances to note the validity of identifying contact information.",
        "RFC 9083",
    )
    RENEW_PROHIBITED = (
        "renew prohibited",
        "Renewal or reregistration of the object instance is forbidden.",
        "RFC 9083",
    )
    UPDATE_PROHIBITED = (
        "update prohibited",
        "Updates to the object instance are forbidden.",
        "RFC 9083",
    )
    TRANSFER_PROHIBITED = (
        "transfer prohibited",
        "Transfers of the registration from one registrar to another are forbidden.",
        "RFC 9083",
    )
    DELETE_PROHIBITED = (
        "delete prohibited",
        "Deletion of the registration of the object instance is forbidden.",
        "RFC 9083",
    )
    PROXY = (
        "proxy",
        "The registration of the object instance has been performed by a third party.",
        "RFC 9083",
    )
    PRIVATE = (
        "private",
        "The information of the object instance is not designated for public consumption.",
        "RFC 9083",
    )
    REMOVED = (
        "removed",
        "Some of the information of the object instance has not been made available and has been removed.",
        "RFC 9083",
    )
    OBSCURED = (
        "obscured",
        "Some of the information of the object instance has been altered for the purposes of not readily revealing the actual information.",
        "RFC 9083",
    )
    ASSOCIATED = (
        "associated",
        "The object instance is associated with other object instances in the registry.",
        "RFC 9083",
    )
    ACTIVE = (
        "active",
        "The object instance is in use.",
        "RFC 9083",
    )
    INACTIVE = (
        "inactive",
        "The object instance is not in use.",
        "RFC 9083",
    )
    LOCKED = (
        "locked",
        "Changes to the object instance cannot be made, including the association of other object instances.",
        "RFC 9083",
    )
    PENDING_CREATE = (
        "pending create",
        "A request has been received for the creation of the object instance, but this action is not yet complete.",
        "RFC 9083",
    )
    PENDING_RENEW = (
        "pending renew",
        "A request has been received for the renewal of the object instance, but this action is not yet complete.",
        "RFC 9083",
    )
    PENDING_TRANSFER = (
        "pending transfer",
        "A request has been received for the transfer of the object instance, but this action is not yet complete.",
        "RFC 9083",
    )
    PENDING_UPDATE = (
        "pending update",
        "A request has been received for the update or modification of the object instance, but this action is not yet complete.",
        "RFC 9083",
    )
    PENDING_DELETE = (
        "pending delete",
        "A request has been received for the deletion or removal of the object instance, but this action is not yet complete.",
        "RFC 9083",
    )
    ADD_PERIOD = (
        "add period",
        "This grace period is provided after the initial registration of the object.",
        "RFC 8056",
    )
    AUTO_RENEW_PERIOD = (
        "auto renew period",
        "This grace period is provided after an object registration period expires and is extended automatically.",
        "RFC 8056",
    )
    CLIENT_DELETE_PROHIBITED = (
        "client delete prohibited",
        "The client requested that requests to delete the object MUST be rejected.",
        "RFC 8056",
    )
    CLIENT_HOLD = (
        "client hold",
        "The client requested that the DNS delegation information MUST NOT be published for the object.",
        "RFC 8056",
    )
    CLIENT_RENEW_PROHIBITED = (
        "client renew prohibited",
        "The client requested that requests to renew the object MUST be rejected.",
        "RFC 8056",
    )
    CLIENT_TRANSFER_PROHIBITED = (
        "client transfer prohibited",
        "The client requested that requests to transfer the object MUST be rejected.",
        "RFC 8056",
    )
    CLIENT_UPDATE_PROHIBITED = (
        "client update prohibited",
        "The client requested that requests to update the object (other than to remove this status) MUST be rejected.",
        "RFC 8056",
    )
    PENDING_RESTORE = (
        "pending restore",
        "An object is in the process of being restored after being in the redemption period state.",
        "RFC 8056",
    )
    REDEMPTION_PERIOD = (
        "redemption period",
        "A delete has been received, but the object has not yet been purged because an opportunity exists to restore the object and abort the deletion process.",
        "RFC 8056",
    )
    RENEW_PERIOD = (
        "renew period",
        "This grace period is provided after an object registration period is explicitly extended (renewed) by the client.",
        "RFC 8056",
    )
    SERVER_DELETE_PROHIBITED = (
        "server delete prohibited",
        "The server set the status so that requests to delete the object MUST be rejected.",
        "RFC 8056",
    )
    SERVER_HOLD = (
        "server hold",
        "The server set the status so that the DNS delegation information MUST NOT be published for the object.",
        "RFC 8056",
    )
    SERVER_RENEW_PROHIBITED = (
        "server renew prohibited",
        "The server set the status so that requests to renew the object MUST be rejected.",
        "RFC 8056",
    )
    SERVER_TRANSFER_PROHIBITED = (
        "server transfer prohibited",
        "The server set the status so that requests to transfer the object MUST be rejected.",
        "RFC 8056",
    )
    SERVER_UPDATE_PROHIBITED = (
        "server update prohibited",
        "The server set the status so that requests to update the object (other than to remove this status) MUST be rejected.",
        "RFC 8056",
    )
    TRANSFER_PERIOD = (
        "transfer period",
        "This grace period is provided after the successful transfer of object registration sponsorship from one client to another client.",
        "RFC 8056",
    )
    ADMINISTRATIVE = (
        "administrative",
        "The object instance has been allocated administratively (i.e., not for use by the operator in their own right in operational networks).",
        "NRO",
    )
    RESERVED = (
        "reserved",
        "The object instance has been allocated to an IANA special-purpose address registry.",
        "NRO",
    )
    # Non-standard (not in IANA registry, but commonly returned by servers)
    OK = (
        "ok",
        "Not in the IANA registry. Some servers return this instead of 'active'. EPP status 'ok' maps to RDAP 'active' per RFC 8056.",
        "non-standard",
    )


class NoticeRemarkType(_RDAPEnum):
    RESULT_SET_TRUNCATED_AUTHORIZATION = (
        "result set truncated due to authorization",
        "The list of results does not contain all results due to lack of authorization.",
        "RFC 9083",
    )
    RESULT_SET_TRUNCATED_LOAD = (
        "result set truncated due to excessive load",
        "The list of results does not contain all results due to an excessively heavy load on the server.",
        "RFC 9083",
    )
    RESULT_SET_TRUNCATED_UNEXPLAINABLE = (
        "result set truncated due to unexplainable reasons",
        "The list of results does not contain all results for an unexplainable reason.",
        "RFC 9083",
    )
    OBJECT_TRUNCATED_AUTHORIZATION = (
        "object truncated due to authorization",
        "The object does not contain all data due to lack of authorization.",
        "RFC 9083",
    )
    OBJECT_TRUNCATED_LOAD = (
        "object truncated due to excessive load",
        "The object does not contain all data due to an excessively heavy load on the server.",
        "RFC 9083",
    )
    OBJECT_TRUNCATED_UNEXPLAINABLE = (
        "object truncated due to unexplainable reasons",
        "The object does not contain all data for an unexplainable reason.",
        "RFC 9083",
    )
    OBJECT_REDACTED_AUTHORIZATION = (
        "object redacted due to authorization",
        "The object contains redacted data due to lack of authorization.",
        "RFC 9083",
    )


class VariantRelation(_RDAPEnum):
    REGISTERED = (
        "registered",
        "The variant names are registered in the registry.",
        "RFC 9083",
    )
    UNREGISTERED = (
        "unregistered",
        "The variant names are not found in the registry.",
        "RFC 9083",
    )
    REGISTRATION_RESTRICTED = (
        "registration restricted",
        "Registration of the variant names is restricted to certain parties or within certain rules.",
        "RFC 9083",
    )
    OPEN_REGISTRATION = (
        "open registration",
        "Registration of the variant names is available to generally qualified registrants.",
        "RFC 9083",
    )
    CONJOINED = (
        "conjoined",
        "Registration of the variant names occurs automatically with the registration of the containing domain registration.",
        "RFC 9083",
    )


class RedactionMethod(_RDAPEnum):
    REMOVAL = (
        "removal",
        "The redacted field is not present in the response.",
        "RFC 9537",
    )
    EMPTY_VALUE = (
        "emptyValue",
        "The redacted field is present in the response with an empty value.",
        "RFC 9537",
    )
    PARTIAL_VALUE = (
        "partialValue",
        "The redacted field is present in the response with a partial value.",
        "RFC 9537",
    )
    REPLACEMENT_VALUE = (
        "replacementValue",
        "The redacted field value has been replaced.",
        "RFC 9537",
    )


# ---------------------------------------------------------------------------
# Pydantic models — RFC 9083 RDAP response structures
# ---------------------------------------------------------------------------

_MODEL_CONFIG = ConfigDict(
    extra="ignore",
    frozen=True,
    alias_generator=to_camel,
    populate_by_name=True,
)


class Link(BaseModel):
    """RFC 9083 S4.2 link object."""

    model_config = _MODEL_CONFIG

    value: str | None = None
    rel: str | None = None
    href: str | None = None
    hreflang: list[str] | None = None
    title: str | None = None
    media: str | None = None
    type: str | None = None


class NoticeOrRemark(BaseModel):
    """RFC 9083 S4.3 notice or remark object."""

    model_config = _MODEL_CONFIG

    title: str | None = None
    type: str | None = None
    description: list[str] = []
    links: list[Link] = []


class Event(BaseModel):
    """RFC 9083 S4.5 event object."""

    model_config = _MODEL_CONFIG

    event_action: str | None = None
    event_date: datetime | None = None
    event_actor: str | None = None
    links: list[Link] = []


class PublicId(BaseModel):
    """RFC 9083 S4.8 public ID object."""

    model_config = _MODEL_CONFIG

    type: str | None = None
    identifier: str | None = None


class Address(BaseModel):
    """Structured postal address from jCard ADR property."""

    model_config = _MODEL_CONFIG

    label: str = ""  # freeform address from jCard ``label`` param
    po_box: str = ""
    extended_address: str = ""
    street_address: str = ""
    locality: str = ""
    region: str = ""
    postal_code: str = ""
    country: str = ""
    cc: str = ""  # ISO 3166-1 alpha-2 country code (RFC 8605)


class Contact(BaseModel):
    """Parsed contact information from a vcardArray (jCard)."""

    model_config = _MODEL_CONFIG

    fn: str | None = None
    org: str | None = None
    emails: list[str] = []
    tels: list[str] = []
    faxes: list[str] = []
    address: Address | None = None
    kind: str | None = None
    contact_uri: str | None = None  # RFC 8605
    url: str | None = None


class IpAddresses(BaseModel):
    """IP addresses for a nameserver object."""

    model_config = _MODEL_CONFIG

    v4: list[str] = []
    v6: list[str] = []


class DsData(BaseModel):
    """DNSSEC DS record data (RFC 9083 S5.3)."""

    model_config = _MODEL_CONFIG

    key_tag: int | None = None
    algorithm: int | None = None
    digest: str | None = None
    digest_type: int | None = None
    events: list[Event] = []
    links: list[Link] = []


class KeyData(BaseModel):
    """DNSSEC DNSKEY record data (RFC 9083 S5.3)."""

    model_config = _MODEL_CONFIG

    flags: int | None = None
    protocol: int | None = None
    public_key: str | None = None
    algorithm: int | None = None
    events: list[Event] = []
    links: list[Link] = []


class SecureDNS(BaseModel):
    """RFC 9083 S5.3 secureDNS object."""

    model_config = _MODEL_CONFIG

    zone_signed: bool | None = None
    delegation_signed: bool | None = None
    max_sig_life: int | None = None
    ds_data: list[DsData] = []
    key_data: list[KeyData] = []


class VariantName(BaseModel):
    """A single variant name within a variant set."""

    model_config = _MODEL_CONFIG

    ldh_name: str | None = None
    unicode_name: str | None = None


class Variant(BaseModel):
    """RFC 9083 S5.3 domain variant object (IDN variants)."""

    model_config = _MODEL_CONFIG

    relation: list[str] = []
    idn_table: str | None = None
    variant_names: list[VariantName] = []


class Entity(BaseModel):
    """RFC 9083 S5.1 entity object."""

    model_config = _MODEL_CONFIG

    object_class_name: str | None = None
    handle: str | None = None
    contact: Contact | None = None
    roles: list[str] = []
    public_ids: list[PublicId] = []
    events: list[Event] = []
    status: list[str] = []
    links: list[Link] = []
    port43: str | None = None
    entities: list[Entity] = []
    remarks: list[NoticeOrRemark] = []
    notices: list[NoticeOrRemark] = []
    networks: list[IPNetworkResponse] = []
    autnums: list[AutnumResponse] = []
    as_event_actor: list[Event] = []
    lang: str | None = None
    redacted: list[RedactedField] = []


class Nameserver(BaseModel):
    """RFC 9083 S5.2 nameserver object."""

    model_config = _MODEL_CONFIG

    object_class_name: str | None = None
    handle: str | None = None
    ldh_name: str | None = None
    unicode_name: str | None = None
    ip_addresses: IpAddresses | None = None
    events: list[Event] = []
    status: list[str] = []
    links: list[Link] = []
    port43: str | None = None
    entities: list[Entity] = []
    remarks: list[NoticeOrRemark] = []
    notices: list[NoticeOrRemark] = []
    lang: str | None = None
    redacted: list[RedactedField] = []


class TypeDescription(BaseModel):
    """
    A ``{type, description}`` pair used in RFC 9537 redacted fields.

    Used for both ``name`` and ``reason`` in :class:`RedactedField`.
    """

    model_config = _MODEL_CONFIG

    type: str | None = None
    description: str | None = None


class RedactedField(BaseModel):
    """
    RFC 9537 redacted field entry.

    Describes a single field that has been redacted from the RDAP response.
    """

    model_config = _MODEL_CONFIG

    name: TypeDescription = Field(default_factory=TypeDescription)
    pre_path: str | None = None
    post_path: str | None = None
    replacement_path: str | None = None
    path_lang: str | None = None
    method: str | None = None
    reason: TypeDescription | None = None


class ErrorResponse(BaseModel):
    """RFC 9083 S6 error response body."""

    model_config = _MODEL_CONFIG

    error_code: int | None = None
    title: str | None = None
    description: list[str] = []
    rdap_conformance: list[str] = []
    notices: list[NoticeOrRemark] = []
    lang: str | None = None


class CIDREntry(BaseModel):
    """RFC 9083 S5.4 CIDR0 CIDR entry within an IP network object."""

    model_config = _MODEL_CONFIG

    v4prefix: str | None = None
    v6prefix: str | None = None
    length: int | None = None


# ---------------------------------------------------------------------------
# Top-level response models — IP network, autnum, entity, nameserver
# ---------------------------------------------------------------------------


class IPNetworkResponse(BaseModel):
    """RFC 9083 S5.4 IP network object (top-level response)."""

    model_config = _MODEL_CONFIG

    # Conformance
    rdap_conformance: list[str] = []

    # Object identity
    object_class_name: str | None = None
    handle: str | None = None

    # IP network fields
    start_address: str | None = None
    end_address: str | None = None
    ip_version: str | None = None  # "v4" or "v6"
    name: str | None = None
    type: str | None = None
    country: str | None = None
    parent_handle: str | None = None
    cidr0_cidrs: list[CIDREntry] = []

    # Registration metadata
    entities: list[Entity] = []
    events: list[Event] = []
    status: list[str] = []
    public_ids: list[PublicId] = []

    # Informational
    links: list[Link] = []
    notices: list[NoticeOrRemark] = []
    remarks: list[NoticeOrRemark] = []
    port43: str | None = None
    lang: str | None = None

    # RFC 9537
    redacted: list[RedactedField] = []

    @property
    def registration_date(self) -> datetime | None:
        for e in self.events:
            if e.event_action == EventAction.REGISTRATION:
                return e.event_date
        return None

    @property
    def last_changed_date(self) -> datetime | None:
        for e in self.events:
            if e.event_action == EventAction.LAST_CHANGED:
                return e.event_date
        return None

    @property
    def self_link(self) -> str | None:
        for link in self.links:
            if link.rel == "self":
                return link.href
        return None


class AutnumResponse(BaseModel):
    """RFC 9083 S5.5 autonomous system number object (top-level response)."""

    model_config = _MODEL_CONFIG

    # Conformance
    rdap_conformance: list[str] = []

    # Object identity
    object_class_name: str | None = None
    handle: str | None = None

    # Autnum fields
    start_autnum: int | None = None
    end_autnum: int | None = None
    name: str | None = None
    type: str | None = None
    country: str | None = None

    # Registration metadata
    entities: list[Entity] = []
    events: list[Event] = []
    status: list[str] = []
    public_ids: list[PublicId] = []

    # Informational
    links: list[Link] = []
    notices: list[NoticeOrRemark] = []
    remarks: list[NoticeOrRemark] = []
    port43: str | None = None
    lang: str | None = None

    # RFC 9537
    redacted: list[RedactedField] = []

    @property
    def registration_date(self) -> datetime | None:
        for e in self.events:
            if e.event_action == EventAction.REGISTRATION:
                return e.event_date
        return None

    @property
    def last_changed_date(self) -> datetime | None:
        for e in self.events:
            if e.event_action == EventAction.LAST_CHANGED:
                return e.event_date
        return None

    @property
    def self_link(self) -> str | None:
        for link in self.links:
            if link.rel == "self":
                return link.href
        return None


# Resolve forward references now that all models are defined.
# Entity references IPNetworkResponse/AutnumResponse;
# Nameserver references RedactedField and Entity.
Entity.model_rebuild()
Nameserver.model_rebuild()


class EntityResponse(BaseModel):
    """RFC 9083 S5.1 entity object (top-level response)."""

    model_config = _MODEL_CONFIG

    # Conformance
    rdap_conformance: list[str] = []

    # Object identity
    object_class_name: str | None = None
    handle: str | None = None

    # Entity fields
    contact: Contact | None = None
    roles: list[str] = []
    public_ids: list[PublicId] = []
    entities: list[Entity] = []

    # Top-level only: associated resources
    networks: list[IPNetworkResponse] = []
    autnums: list[AutnumResponse] = []
    as_event_actor: list[Event] = []

    # Registration metadata
    events: list[Event] = []
    status: list[str] = []

    # Informational
    links: list[Link] = []
    notices: list[NoticeOrRemark] = []
    remarks: list[NoticeOrRemark] = []
    port43: str | None = None
    lang: str | None = None

    # RFC 9537
    redacted: list[RedactedField] = []

    @property
    def registration_date(self) -> datetime | None:
        for e in self.events:
            if e.event_action == EventAction.REGISTRATION:
                return e.event_date
        return None

    @property
    def last_changed_date(self) -> datetime | None:
        for e in self.events:
            if e.event_action == EventAction.LAST_CHANGED:
                return e.event_date
        return None

    @property
    def self_link(self) -> str | None:
        for link in self.links:
            if link.rel == "self":
                return link.href
        return None


class NameserverResponse(BaseModel):
    """RFC 9083 S5.2 nameserver object (top-level response)."""

    model_config = _MODEL_CONFIG

    # Conformance
    rdap_conformance: list[str] = []

    # Object identity
    object_class_name: str | None = None
    handle: str | None = None

    # Nameserver fields
    ldh_name: str | None = None
    unicode_name: str | None = None
    ip_addresses: IpAddresses | None = None

    # Registration metadata
    entities: list[Entity] = []
    events: list[Event] = []
    status: list[str] = []
    public_ids: list[PublicId] = []

    # Informational
    links: list[Link] = []
    notices: list[NoticeOrRemark] = []
    remarks: list[NoticeOrRemark] = []
    port43: str | None = None
    lang: str | None = None

    # RFC 9537
    redacted: list[RedactedField] = []

    @property
    def registration_date(self) -> datetime | None:
        for e in self.events:
            if e.event_action == EventAction.REGISTRATION:
                return e.event_date
        return None

    @property
    def last_changed_date(self) -> datetime | None:
        for e in self.events:
            if e.event_action == EventAction.LAST_CHANGED:
                return e.event_date
        return None

    @property
    def self_link(self) -> str | None:
        for link in self.links:
            if link.rel == "self":
                return link.href
        return None


class DomainResponse(BaseModel):
    """RFC 9083 S5.3 complete domain object."""

    model_config = _MODEL_CONFIG

    # Conformance
    rdap_conformance: list[str] = []

    # Object identity
    object_class_name: str | None = None
    handle: str | None = None

    # Domain names
    ldh_name: str | None = None
    unicode_name: str | None = None

    # IDN variants
    variants: list[Variant] = []

    # Delegation
    nameservers: list[Nameserver] = []
    secure_dns: SecureDNS | None = Field(None, alias="secureDNS")

    # IP network (for reverse DNS domains)
    network: IPNetworkResponse | None = None

    # Registration metadata
    entities: list[Entity] = []
    events: list[Event] = []
    status: list[str] = []
    public_ids: list[PublicId] = []

    # Informational
    links: list[Link] = []
    notices: list[NoticeOrRemark] = []
    remarks: list[NoticeOrRemark] = []
    port43: str | None = None
    lang: str | None = None

    # RFC 9537 — Redacted fields
    redacted: list[RedactedField] = []

    # -- Convenience properties --

    @property
    def registration_date(self) -> datetime | None:
        for e in self.events:
            if e.event_action == EventAction.REGISTRATION:
                return e.event_date
        return None

    @property
    def expiration_date(self) -> datetime | None:
        for e in self.events:
            if e.event_action == EventAction.EXPIRATION:
                return e.event_date
        return None

    @property
    def last_changed_date(self) -> datetime | None:
        for e in self.events:
            if e.event_action == EventAction.LAST_CHANGED:
                return e.event_date
        return None

    @property
    def delegation_signed(self) -> bool:
        if self.secure_dns is not None:
            return self.secure_dns.delegation_signed or False
        return False

    @property
    def self_link(self) -> str | None:
        for link in self.links:
            if link.rel == "self":
                return link.href
        return None

    @property
    def registrar(self) -> Entity | None:
        for entity in self.entities:
            if Role.REGISTRAR in entity.roles:
                return entity
        return None

    @property
    def nameserver_names(self) -> list[str]:
        return [
            ns.ldh_name
            for ns in self.nameservers
            if ns.ldh_name is not None
        ]


# ---------------------------------------------------------------------------
# Result wrapper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainQueryResult:
    """
    Result of an RDAP domain query.

    Carries both the parsed Pydantic model and the raw JSON dict.
    On error, ``parsed`` is ``None`` and ``error`` is set.
    """

    domain: str
    primary_url: str
    related_urls: list[str] = field(default_factory=list)
    parsed: DomainResponse | None = None
    raw: dict = field(default_factory=dict)
    error: Exception | None = None
    error_response: ErrorResponse | None = None


@dataclass(frozen=True)
class IPNetworkQueryResult:
    """Result of an RDAP IP network query."""

    query: str
    primary_url: str
    related_urls: list[str] = field(default_factory=list)
    parsed: IPNetworkResponse | None = None
    raw: dict = field(default_factory=dict)
    error: Exception | None = None
    error_response: ErrorResponse | None = None


@dataclass(frozen=True)
class AutnumQueryResult:
    """Result of an RDAP autonomous system number query."""

    asn: int
    primary_url: str
    related_urls: list[str] = field(default_factory=list)
    parsed: AutnumResponse | None = None
    raw: dict = field(default_factory=dict)
    error: Exception | None = None
    error_response: ErrorResponse | None = None


@dataclass(frozen=True)
class EntityQueryResult:
    """Result of an RDAP entity query."""

    handle: str
    primary_url: str
    related_urls: list[str] = field(default_factory=list)
    parsed: EntityResponse | None = None
    raw: dict = field(default_factory=dict)
    error: Exception | None = None
    error_response: ErrorResponse | None = None


@dataclass(frozen=True)
class NameserverQueryResult:
    """Result of an RDAP nameserver query."""

    nameserver: str
    primary_url: str
    related_urls: list[str] = field(default_factory=list)
    parsed: NameserverResponse | None = None
    raw: dict = field(default_factory=dict)
    error: Exception | None = None
    error_response: ErrorResponse | None = None
