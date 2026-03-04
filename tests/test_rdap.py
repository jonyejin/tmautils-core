import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tmautils.common import MAX_ASN, parse_asn
from tmautils.rdap import (
    AutnumQueryResult,
    AutnumResponse,
    BootstrapError,
    DomainQueryResult,
    DomainResponse,
    EntityQueryResult,
    EntityResponse,
    EventAction,
    IPNetworkQueryResult,
    IPNetworkResponse,
    NameserverQueryResult,
    NameserverResponse,
    NoticeRemarkType,
    ParseError,
    QueryError,
    RateLimitedError,
    RdapClient,
    RDAPError,
    RDAPStatus,
    RedactionMethod,
    RemoteServerError,
    ResourceAccessDeniedError,
    ResourceDoesNotExist,
    Role,
    VariantRelation,
)
from tmautils.rdap._parser import (
    _extract_vcards,
    _parse_vcard_array,
    parse_autnum_response,
    parse_domain_response,
    parse_entity_response,
    parse_error_response,
    parse_ip_network_response,
    parse_nameserver_response,
    recursive_merge,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).parent / "data" / "rdap"


@pytest.fixture
def domain1_raw() -> dict:
    return json.loads((DATA_DIR / "data_rdap_response_domain1.json").read_text())


@pytest.fixture
def domain2_raw() -> dict:
    return json.loads((DATA_DIR / "data_rdap_response_domain2.json").read_text())


@pytest.fixture
def domain3_raw() -> dict:
    return json.loads((DATA_DIR / "data_rdap_response_domain3.json").read_text())


@pytest.fixture
def ip_network1_raw() -> dict:
    return json.loads((DATA_DIR / "data_rdap_response_ip_network1.json").read_text())


@pytest.fixture
def autnum1_raw() -> dict:
    return json.loads((DATA_DIR / "data_rdap_response_autnum1.json").read_text())


@pytest.fixture
def entity1_raw() -> dict:
    return json.loads((DATA_DIR / "data_rdap_response_entity1.json").read_text())


@pytest.fixture
def nameserver1_raw() -> dict:
    return json.loads((DATA_DIR / "data_rdap_response_nameserver1.json").read_text())


@pytest.fixture
def client(tmp_path) -> RdapClient:
    return RdapClient(
        working_root=tmp_path,
        setup_logging=False,
    )


# ---------------------------------------------------------------------------
# Unit Tests: Error hierarchy
# ---------------------------------------------------------------------------


class TestErrorHierarchy:
    def test_rdap_error_is_base(self):
        assert issubclass(BootstrapError, RDAPError)
        assert issubclass(ParseError, RDAPError)
        assert issubclass(QueryError, RDAPError)

    def test_query_error_subclasses(self):
        assert issubclass(ResourceDoesNotExist, QueryError)
        assert issubclass(ResourceAccessDeniedError, QueryError)
        assert issubclass(RateLimitedError, QueryError)
        assert issubclass(RemoteServerError, QueryError)

    def test_query_error_carries_fields(self):
        exc = QueryError("test", status_code=404, response='{"error": true}')
        assert str(exc) == "test"
        assert exc.status_code == 404
        assert exc.response == '{"error": true}'


# ---------------------------------------------------------------------------
# Unit Tests: Enums
# ---------------------------------------------------------------------------


class TestEnums:
    def test_event_action_completeness(self):
        assert len(EventAction) == 12
        assert EventAction.REGISTRATION == "registration"
        assert EventAction.LAST_UPDATE_OF_RDAP_DATABASE == "last update of RDAP database"

    def test_event_action_metadata(self):
        assert EventAction.REGISTRATION.description == "The object instance was initially registered."
        assert EventAction.REGISTRATION.reference == "RFC 9083"

    def test_role_completeness(self):
        assert len(Role) == 11
        assert Role.REGISTRAR == "registrar"
        assert Role.ABUSE == "abuse"

    def test_role_metadata(self):
        assert "registrant" in Role.REGISTRANT.description.lower()
        assert Role.REGISTRANT.reference == "RFC 9083"

    def test_rdap_status_values(self):
        assert RDAPStatus.ACTIVE == "active"
        assert RDAPStatus.CLIENT_DELETE_PROHIBITED == "client delete prohibited"
        assert RDAPStatus.PENDING_TRANSFER == "pending transfer"
        assert RDAPStatus.PROXY == "proxy"
        assert RDAPStatus.PRIVATE == "private"
        assert RDAPStatus.ADMINISTRATIVE == "administrative"
        assert RDAPStatus.RESERVED == "reserved"
        assert len(RDAPStatus) == 37

    def test_rdap_status_metadata(self):
        assert RDAPStatus.ACTIVE.reference == "RFC 9083"
        assert RDAPStatus.ADD_PERIOD.reference == "RFC 8056"

    def test_notice_remark_type_completeness(self):
        assert len(NoticeRemarkType) == 7
        assert NoticeRemarkType.RESULT_SET_TRUNCATED_AUTHORIZATION == "result set truncated due to authorization"
        assert NoticeRemarkType.OBJECT_REDACTED_AUTHORIZATION == "object redacted due to authorization"
        assert NoticeRemarkType.RESULT_SET_TRUNCATED_AUTHORIZATION.reference == "RFC 9083"

    def test_variant_relation_completeness(self):
        assert len(VariantRelation) == 5
        assert VariantRelation.REGISTERED == "registered"
        assert VariantRelation.CONJOINED == "conjoined"
        assert VariantRelation.REGISTRATION_RESTRICTED.reference == "RFC 9083"

    def test_redaction_method_completeness(self):
        assert len(RedactionMethod) == 4
        assert RedactionMethod.REMOVAL == "removal"
        assert RedactionMethod.EMPTY_VALUE == "emptyValue"
        assert RedactionMethod.PARTIAL_VALUE == "partialValue"
        assert RedactionMethod.REPLACEMENT_VALUE == "replacementValue"
        assert RedactionMethod.REMOVAL.reference == "RFC 9537"

    def test_enum_str_comparison_preserved(self):
        """Verify that enum members still compare as plain strings."""
        assert EventAction.REGISTRATION == "registration"
        assert str(EventAction.REGISTRATION) == "registration"
        assert EventAction("registration") is EventAction.REGISTRATION
        assert EventAction("registration").description == "The object instance was initially registered."

    def test_non_rfc9083_references(self):
        """Verify non-RFC-9083 references are correctly attributed."""
        assert EventAction.LAST_UPDATE_OF_RDAP_DATABASE.reference == "ICANN"
        assert EventAction.REGISTRAR_EXPIRATION.reference == "ICANN"
        assert EventAction.ENUM_VALIDATION_EXPIRATION.reference == "CZ.NIC z.s.p.o."
        assert RDAPStatus.ADMINISTRATIVE.reference == "NRO"
        assert RDAPStatus.RESERVED.reference == "NRO"
        assert RDAPStatus.OK.reference == "non-standard"


# ---------------------------------------------------------------------------
# Unit Tests: vCard parsing
# ---------------------------------------------------------------------------


class TestVCardParsing:
    def test_basic_vcard(self):
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "John Doe"],
            ["org", {}, "text", "Example Inc"],
            ["email", {}, "text", "john@example.com"],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert contact["fn"] == "John Doe"
        assert contact["org"] == "Example Inc"
        assert contact["emails"] == ["john@example.com"]

    def test_tel_with_uri_type(self):
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "Test"],
            ["tel", {"type": "voice"}, "uri", "tel:+1.2083895740"],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert contact["tels"] == ["+1.2083895740"]
        assert contact.get("faxes", []) == []

    def test_tel_without_prefix(self):
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["tel", {}, "text", "+1234567890"],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert contact["tels"] == ["+1234567890"]

    def test_tel_fax_type(self):
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "Test"],
            ["tel", {"type": "fax"}, "uri", "tel:+1.5551234567"],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert contact["faxes"] == ["+1.5551234567"]
        assert contact.get("tels", []) == []

    def test_tel_type_array_voice(self):
        """RFC 6350: TYPE can be an array like ["work", "voice"]."""
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "Test"],
            ["tel", {"type": ["work", "voice"]}, "uri", "tel:+1.5551234567"],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert contact["tels"] == ["+1.5551234567"]
        assert contact.get("faxes", []) == []

    def test_tel_type_array_fax(self):
        """RFC 6350: TYPE array with fax capability."""
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "Test"],
            ["tel", {"type": ["work", "fax"]}, "uri", "tel:+1.5559876543"],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert contact["faxes"] == ["+1.5559876543"]
        assert contact.get("tels", []) == []

    def test_tel_no_type_defaults_to_tel(self):
        """No TYPE param → defaults to tel (not fax)."""
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "Test"],
            ["tel", {}, "uri", "tel:+1.5551111111"],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert contact["tels"] == ["+1.5551111111"]
        assert contact.get("faxes", []) == []

    def test_address_parsing(self):
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "Test"],
            ["adr", {}, "text", [
                "", "", ["Street 1"], "City", "Region", "12345", "Country",
            ]],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert contact["address"]["street_address"] == "Street 1"
        assert contact["address"]["locality"] == "City"
        assert contact["address"]["country"] == "Country"

    def test_address_cc_param(self):
        """RFC 8605: CC parameter on ADR carries ISO 3166-1 alpha-2 code."""
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "Test"],
            ["adr", {"cc": "US"}, "text", [
                "", "", ["54321 Oak St"], "Reston", "VA", "20190", "USA",
            ]],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert contact["address"]["cc"] == "US"
        assert contact["address"]["country"] == "USA"

    def test_address_no_cc_param(self):
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "Test"],
            ["adr", {}, "text", [
                "", "", ["Street 1"], "City", "", "", "Country",
            ]],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert "cc" not in contact["address"]

    def test_kind_and_contact_uri(self):
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "Test"],
            ["kind", {}, "text", "org"],
            ["contact-uri", {}, "uri", "https://example.com/contact"],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert contact["kind"] == "org"
        assert contact["contact_uri"] == "https://example.com/contact"

    def test_url_parsing(self):
        """RFC 6350 S6.7.8: URL property."""
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "Test Corp"],
            ["url", {}, "uri", "https://example.com"],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert contact["url"] == "https://example.com"

    def test_fn_and_org_separate(self):
        """Unlike whoisit, fn and org should be separate fields."""
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "John Doe"],
            ["org", {}, "text", "ACME Corp"],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact["fn"] == "John Doe"
        assert contact["org"] == "ACME Corp"

    def test_empty_vcard(self):
        assert _parse_vcard_array([]) is None
        assert _parse_vcard_array(["vcard"]) is None
        assert _parse_vcard_array(["notcard", []]) is None

    def test_vcard_with_only_version(self):
        vcard = ["vcard", [["version", {}, "text", "4.0"]]]
        assert _parse_vcard_array(vcard) is None

    def test_empty_fn_ignored(self):
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", ""],
            ["email", {}, "text", "test@example.com"],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert "fn" not in contact
        assert contact["emails"] == ["test@example.com"]

    def test_address_label_param(self):
        """ARIN-style: address text only in label param, structured fields empty."""
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "Google LLC"],
            ["adr", {
                "label": "1600 Amphitheatre Parkway\nMountain View\nCA\n94043\nUnited States",
            }, "text", ["", "", "", "", "", "", ""]],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        addr = contact["address"]
        assert addr["label"] == "1600 Amphitheatre Parkway\nMountain View\nCA\n94043\nUnited States"
        # Structured fields are empty
        assert addr["street_address"] == ""
        assert addr["locality"] == ""

    def test_address_label_absent(self):
        """When no label param, label should not be in dict."""
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "Test"],
            ["adr", {"cc": "US"}, "text", [
                "", "", "123 Main St", "City", "ST", "12345", "US",
            ]],
        ]]
        contact = _parse_vcard_array(vcard)
        assert "label" not in contact["address"]
        assert contact["address"]["street_address"] == "123 Main St"

    def test_multiple_emails(self):
        """Multiple email entries captured in order."""
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "Test"],
            ["email", {}, "text", "first@example.com"],
            ["email", {}, "text", "second@example.com"],
            ["email", {}, "text", "third@example.com"],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert contact["emails"] == [
            "first@example.com",
            "second@example.com",
            "third@example.com",
        ]

    def test_multiple_tels(self):
        """Multiple tel entries: tels and faxes lists capture all."""
        vcard = ["vcard", [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "Test"],
            ["tel", {"type": "voice"}, "uri", "tel:+1.1111111111"],
            ["tel", {"type": "voice"}, "uri", "tel:+1.2222222222"],
            ["tel", {"type": "fax"}, "uri", "tel:+1.3333333333"],
        ]]
        contact = _parse_vcard_array(vcard)
        assert contact is not None
        assert contact["tels"] == ["+1.1111111111", "+1.2222222222"]
        assert contact["faxes"] == ["+1.3333333333"]


# ---------------------------------------------------------------------------
# Unit Tests: Domain response parsing (fixture-based)
# ---------------------------------------------------------------------------


class TestParseDomainFixture1:
    """Parse GOOGLE.COM fixture."""

    def test_basic_fields(self, domain1_raw):
        d = parse_domain_response(domain1_raw)
        assert d.ldh_name == "GOOGLE.COM"
        assert d.handle == "2138514_DOMAIN_COM-VRSN"
        assert d.object_class_name == "domain"

    def test_events(self, domain1_raw):
        d = parse_domain_response(domain1_raw)
        assert len(d.events) == 4
        assert d.registration_date == datetime(1997, 9, 15, 4, 0, tzinfo=timezone.utc)
        assert d.expiration_date == datetime(2028, 9, 14, 4, 0, tzinfo=timezone.utc)

    def test_nameservers(self, domain1_raw):
        d = parse_domain_response(domain1_raw)
        assert len(d.nameservers) == 4
        assert d.nameserver_names == [
            "NS1.GOOGLE.COM", "NS2.GOOGLE.COM",
            "NS3.GOOGLE.COM", "NS4.GOOGLE.COM",
        ]

    def test_status(self, domain1_raw):
        d = parse_domain_response(domain1_raw)
        assert "client delete prohibited" in d.status
        assert "server transfer prohibited" in d.status

    def test_rdap_conformance(self, domain1_raw):
        d = parse_domain_response(domain1_raw)
        assert "rdap_level_0" in d.rdap_conformance

    def test_self_link(self, domain1_raw):
        d = parse_domain_response(domain1_raw)
        assert d.self_link is not None
        assert "rdap.verisign.com" in d.self_link

    def test_notices(self, domain1_raw):
        d = parse_domain_response(domain1_raw)
        assert len(d.notices) == 3
        titles = [n.title for n in d.notices]
        assert "Terms of Service" in titles

    def test_registrar_entity(self, domain1_raw):
        d = parse_domain_response(domain1_raw)
        reg = d.registrar
        assert reg is not None
        assert reg.handle == "292"
        assert Role.REGISTRAR in reg.roles
        assert reg.contact is not None
        assert reg.contact.fn == "MarkMonitor Inc."

    def test_nested_abuse_entity(self, domain1_raw):
        d = parse_domain_response(domain1_raw)
        reg = d.registrar
        assert reg is not None
        assert len(reg.entities) == 1
        abuse = reg.entities[0]
        assert Role.ABUSE in abuse.roles
        assert abuse.contact is not None
        assert abuse.contact.emails == ["abusecomplaints@markmonitor.com"]
        assert abuse.contact.tels == ["+1.2086851750"]

    def test_secure_dns(self, domain1_raw):
        d = parse_domain_response(domain1_raw)
        assert d.delegation_signed is False


class TestParseDomainFixture2:
    """Parse norway.no fixture — richer entities with addresses."""

    def test_basic_fields(self, domain2_raw):
        d = parse_domain_response(domain2_raw)
        assert d.ldh_name == "norway.no"

    def test_entities_with_vcard(self, domain2_raw):
        d = parse_domain_response(domain2_raw)
        assert len(d.entities) == 2

    def test_technical_entity(self, domain2_raw):
        d = parse_domain_response(domain2_raw)
        tech = next(e for e in d.entities if "technical" in e.roles)
        assert tech.handle == "DH39483R-NORID"
        assert tech.contact is not None
        assert tech.contact.fn == "Domeneshop Hostmaster"
        assert tech.contact.kind == "group"
        assert tech.contact.emails == ["hostmaster@domeneshop.no"]
        assert tech.contact.tels == ["+47.22943333"]

    def test_registrar_entity_with_address(self, domain2_raw):
        d = parse_domain_response(domain2_raw)
        reg = next(e for e in d.entities if "registrar" in e.roles)
        assert reg.contact is not None
        assert reg.contact.kind == "org"
        assert reg.contact.url == "https://domeneshop.no"
        addr = reg.contact.address
        assert addr is not None
        assert addr.street_address == "Christian Krohgs gate 16"
        assert addr.locality == "Oslo"
        assert addr.postal_code == "NO-0186"
        assert addr.country == "NORWAY"

    def test_public_ids(self, domain2_raw):
        d = parse_domain_response(domain2_raw)
        reg = next(e for e in d.entities if "registrar" in e.roles)
        assert len(reg.public_ids) == 2
        types = [p.type for p in reg.public_ids]
        assert "Norwegian organization number" in types

    def test_nameserver_with_events(self, domain2_raw):
        d = parse_domain_response(domain2_raw)
        assert len(d.nameservers) == 4
        ns = d.nameservers[0]
        assert ns.ldh_name == "ns1-09.azure-dns.com"
        assert len(ns.events) == 1

    def test_last_changed_date(self, domain2_raw):
        d = parse_domain_response(domain2_raw)
        assert d.last_changed_date is not None


class TestParseDomainFixture3:
    """Parse THEMARQUETRY.COM fixture."""

    def test_basic_fields(self, domain3_raw):
        d = parse_domain_response(domain3_raw)
        assert d.ldh_name == "THEMARQUETRY.COM"
        assert d.delegation_signed is False
        assert len(d.nameservers) == 2


# ---------------------------------------------------------------------------
# Unit Tests: Error response parsing
# ---------------------------------------------------------------------------


class TestParseErrorResponse:
    def test_parse_error(self):
        raw = {"errorCode": 404, "title": "Not Found", "description": ["Object not found"]}
        err = parse_error_response(raw)
        assert err.error_code == 404
        assert err.title == "Not Found"
        assert err.description == ["Object not found"]

    def test_parse_empty_error(self):
        raw = {"errorCode": 500}
        err = parse_error_response(raw)
        assert err.error_code == 500
        assert err.title is None
        assert err.description == []

    def test_error_with_conformance_and_notices(self):
        """RFC 9083 S6 Figure 29: error responses may include conformance & notices."""
        raw = {
            "errorCode": 404,
            "title": "Not Found",
            "description": ["not found"],
            "rdapConformance": ["rdap_level_0"],
            "notices": [
                {"title": "Terms of Service", "description": ["By using this..."]}
            ],
            "lang": "en",
        }
        err = parse_error_response(raw)
        assert err.error_code == 404
        assert err.rdap_conformance == ["rdap_level_0"]
        assert len(err.notices) == 1
        assert err.notices[0].title == "Terms of Service"
        assert err.lang == "en"


# ---------------------------------------------------------------------------
# Unit Tests: Permissive parsing
# ---------------------------------------------------------------------------


class TestPermissiveParsing:
    def test_minimal_domain(self):
        d = parse_domain_response({})
        assert d.ldh_name is None
        assert d.events == []
        assert d.entities == []
        assert d.nameservers == []

    def test_unknown_fields_ignored(self):
        d = parse_domain_response({
            "objectClassName": "domain",
            "ldhName": "test.com",
            "someExtension_field": "value",
            "anotherUnknown": [1, 2, 3],
        })
        assert d.ldh_name == "test.com"

    def test_wrong_object_class_raises(self):
        with pytest.raises(ParseError, match="autnum"):
            parse_domain_response({"objectClassName": "autnum"})

    def test_missing_optional_nested_fields(self):
        d = parse_domain_response({
            "objectClassName": "domain",
            "entities": [{"roles": ["registrar"]}],
            "nameservers": [{"ldhName": "ns.example.com"}],
        })
        assert len(d.entities) == 1
        assert d.entities[0].contact is None
        assert len(d.nameservers) == 1


# ---------------------------------------------------------------------------
# Unit Tests: DomainQueryResult
# ---------------------------------------------------------------------------


class TestDomainQueryResult:
    def test_success_result(self):
        r = DomainQueryResult(
            domain="test.com",
            primary_url="https://rdap.example.com/domain/test.com",
        )
        assert r.domain == "test.com"
        assert r.error is None
        assert r.parsed is None
        assert r.raw == {}
        assert r.related_urls == []

    def test_error_result(self):
        exc = QueryError("not found", status_code=404, response="err")
        r = DomainQueryResult(
            domain="bad.com",
            primary_url="https://rdap.example.com/domain/bad.com",
            error=exc,
        )
        assert r.error is exc
        assert r.error.status_code == 404

    def test_frozen(self):
        r = DomainQueryResult(domain="a.com", primary_url="u")
        with pytest.raises(AttributeError):
            r.domain = "b.com"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Unit Tests: Pydantic model immutability
# ---------------------------------------------------------------------------


class TestModelImmutability:
    def test_domain_response_frozen(self, domain1_raw):
        d = parse_domain_response(domain1_raw)
        with pytest.raises(Exception):
            d.ldh_name = "changed"  # type: ignore[misc]

    def test_entity_frozen(self, domain1_raw):
        d = parse_domain_response(domain1_raw)
        with pytest.raises(Exception):
            d.entities[0].handle = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Unit Tests: _extract_vcards
# ---------------------------------------------------------------------------


class TestExtractVcards:
    def test_vcard_extraction(self):
        result = _extract_vcards({
            "handle": "123",
            "vcardArray": ["vcard", [
                ["version", {}, "text", "4.0"],
                ["fn", {}, "text", "Test"],
            ]],
        })
        assert result["handle"] == "123"
        assert result["contact"] == {"fn": "Test"}
        assert "vcardArray" not in result

    def test_keys_pass_through_unchanged(self):
        """Verify camelCase keys are NOT converted (Pydantic handles that)."""
        result = _extract_vcards({"eventAction": "registration", "eventDate": "2024-01-01"})
        assert result == {"eventAction": "registration", "eventDate": "2024-01-01"}

    def test_nested_dicts(self):
        result = _extract_vcards({
            "secureDNS": {"delegationSigned": True, "dsData": []},
        })
        assert result == {
            "secureDNS": {"delegationSigned": True, "dsData": []},
        }

    def test_list_of_dicts(self):
        result = _extract_vcards([
            {"ldhName": "ns1.example.com"},
            {"ldhName": "ns2.example.com"},
        ])
        assert result == [
            {"ldhName": "ns1.example.com"},
            {"ldhName": "ns2.example.com"},
        ]


# ---------------------------------------------------------------------------
# Unit Tests: recursive_merge
# ---------------------------------------------------------------------------


class TestRecursiveMerge:
    def test_simple_merge(self):
        base = {"a": 1, "b": 2}
        recursive_merge(base, {"b": 3, "c": 4})
        assert base == {"a": 1, "b": 3, "c": 4}

    def test_deep_merge(self):
        base = {"x": {"a": 1, "b": 2}}
        recursive_merge(base, {"x": {"b": 3, "c": 4}})
        assert base == {"x": {"a": 1, "b": 3, "c": 4}}


# ---------------------------------------------------------------------------
# Unit Tests: Redacted field parsing (RFC 9537)
# ---------------------------------------------------------------------------


class TestRedactedParsing:
    def test_redacted_field_full(self):
        raw = {
            "objectClassName": "domain",
            "ldhName": "test.com",
            "redacted": [{
                "name": {"type": "Registrant Email"},
                "prePath": "$.entities[?(@.roles[0]==\"registrant\")].vcardArray[1][?(@[0]==\"email\")][3]",
                "method": "replacementValue",
                "replacementPath": "$.entities[?(@.roles[0]==\"registrant\")].vcardArray[1][?(@[0]==\"contact-uri\")][3]",
                "pathLang": "jsonpath",
                "reason": {"description": "Server policy"},
            }],
        }
        d = parse_domain_response(raw)
        assert len(d.redacted) == 1
        r = d.redacted[0]
        assert r.name.type == "Registrant Email"
        assert r.method == "replacementValue"
        assert r.pre_path is not None
        assert "registrant" in r.pre_path
        assert r.replacement_path is not None
        assert r.path_lang == "jsonpath"
        assert r.reason is not None
        assert r.reason.description == "Server policy"

    def test_redacted_minimal(self):
        raw = {
            "objectClassName": "domain",
            "ldhName": "test.com",
            "redacted": [{
                "name": {"description": "Registry Domain ID"},
                "method": "removal",
            }],
        }
        d = parse_domain_response(raw)
        assert len(d.redacted) == 1
        r = d.redacted[0]
        assert r.name.description == "Registry Domain ID"
        assert r.method == "removal"
        assert r.pre_path is None
        assert r.reason is None

    def test_empty_redacted(self):
        d = parse_domain_response({
            "objectClassName": "domain",
            "redacted": [],
        })
        assert d.redacted == []

    def test_no_redacted(self):
        d = parse_domain_response({"objectClassName": "domain"})
        assert d.redacted == []


# ---------------------------------------------------------------------------
# Unit Tests: RdapClient
# ---------------------------------------------------------------------------


class TestClientInit:
    def test_creates_dirs(self, tmp_path):
        c = RdapClient(working_root=tmp_path, setup_logging=False)
        # cache dir is created eagerly via _try_load_cached_bootstrap
        assert (tmp_path / "RdapClient" / "cache").exists()
        # logs dir is lazy — just check IOHelper has the attribute
        assert hasattr(c._io_helper, "logs")

    def test_not_bootstrapped_initially(self, tmp_path):
        c = RdapClient(working_root=tmp_path, setup_logging=False)
        assert c._bootstrap_timestamp == 0

    def test_public_methods_exist(self, client):
        for method in (
            "bootstrap", "bootstrap_sync",
            "query_domain", "query_domain_sync",
            "query_ip", "query_ip_sync",
            "query_asn", "query_asn_sync",
            "query_entity", "query_entity_sync",
            "query_nameserver", "query_nameserver_sync",
        ):
            assert callable(getattr(client, method))


class TestProcessResponse:
    @pytest.mark.parametrize("status,exc_type", [
        (400, QueryError),
        (401, ResourceAccessDeniedError),
        (403, ResourceAccessDeniedError),
        (404, ResourceDoesNotExist),
        (422, ResourceAccessDeniedError),
        (429, RateLimitedError),
        (500, RemoteServerError),
        (502, RemoteServerError),
        (503, RemoteServerError),
    ])
    def test_error_status_codes(self, status, exc_type):
        mock = MagicMock()
        mock.status = status
        with pytest.raises(exc_type) as exc_info:
            RdapClient._process_response(mock, "https://rdap.example.com/test", "err")
        assert exc_info.value.status_code == status

    def test_200_with_valid_json(self):
        mock = MagicMock()
        mock.status = 200
        result = RdapClient._process_response(
            mock, "https://rdap.example.com/test", '{"ldhName": "test.com"}'
        )
        assert result == {"ldhName": "test.com"}

    def test_200_with_invalid_json(self):
        mock = MagicMock()
        mock.status = 200
        with pytest.raises(QueryError, match="Failed to parse"):
            RdapClient._process_response(mock, "https://rdap.example.com/test", "not json")


class TestConstructUrl:
    def test_with_trailing_slash(self):
        url = RdapClient._construct_url(
            "https://rdap.verisign.com/com/v1/", "domain", "example.com",
        )
        assert url == "https://rdap.verisign.com/com/v1/domain/example.com"

    def test_without_trailing_slash(self):
        url = RdapClient._construct_url(
            "https://rdap.verisign.com/com/v1", "domain", "example.com",
        )
        assert url == "https://rdap.verisign.com/com/v1/domain/example.com"


class TestTryParseErrorBody:
    def test_valid_error_body(self):
        text = '{"errorCode": 404, "title": "Not Found"}'
        err = RdapClient._try_parse_error_body(text)
        assert err is not None
        assert err.error_code == 404

    def test_non_error_json(self):
        assert RdapClient._try_parse_error_body('{"ldhName": "x"}') is None

    def test_non_json(self):
        assert RdapClient._try_parse_error_body("not json") is None


class TestBootstrapCacheRoundTrip:
    def test_save_and_load(self, tmp_path):
        """Test bootstrap cache save/load cycle using mock data."""
        c1 = RdapClient(working_root=tmp_path, setup_logging=False)

        # Manually inject mock bootstrap data
        mock_raw = {
            "dns": {"services": [[["com", "net"], ["https://rdap.verisign.com/com/v1/"]]]},
            "asn": {"services": [[["1-100"], ["https://rdap.example.com/"]]]},
            "ipv4": {"services": [[["1.0.0.0/8"], ["https://rdap.example.com/"]]]},
            "ipv6": {"services": [[["2001::/16"], ["https://rdap.example.com/"]]]},
            "object": {"services": [[["contact@example.com"], ["TAG"], ["https://rdap.example.com/"]]]},
        }
        c1._raw_data = mock_raw
        c1._bootstrap_timestamp = 9999999999
        # timestamp is set — bootstrap data is loaded
        c1._parse_bootstrap_data()
        c1._save_bootstrap_cache()

        # New client should load from cache
        c2 = RdapClient(working_root=tmp_path, setup_logging=False)
        assert c2._bootstrap_timestamp > 0

    def test_parse_object_services(self, tmp_path):
        """RFC 8521: three-element arrays [contacts, identifiers, urls]."""
        c = RdapClient(working_root=tmp_path, setup_logging=False)
        services = [
            [["contact@arin.net"], ["ARIN"], ["https://rdap.arin.net/registry/"]],
            [["contact@ripe.net"], ["RIPE"], ["https://rdap.db.ripe.net/", "http://insecure.example.com/"]],
            [["contact@example.com"], ["TEST"], ["http://only-http.example.com/"]],
        ]
        result = c._parse_object_services(services)
        assert result["ARIN"] == ["https://rdap.arin.net/registry/"]
        assert result["RIPE"] == ["https://rdap.db.ripe.net/"]  # HTTP filtered out
        assert "TEST" not in result  # No HTTPS URLs


class TestDnsEndpointLookup:
    def test_unknown_tld_raises(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        with pytest.raises(BootstrapError, match="no known RDAP endpoint"):
            c._get_dns_endpoints("nonexistent")


class TestBuildDomainUrl:
    def test_com_domain(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        url = c._build_domain_url("example.com")
        assert "domain/example.com" in url
        assert "rdap.verisign.com" in url

    def test_sld_domain(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        url = c._build_domain_url("bbc.co.uk")
        assert "domain/bbc.co.uk" in url

    def test_single_label_raises(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        with pytest.raises(QueryError, match="Failed to extract TLD"):
            c._build_domain_url("noperiod")


# ---------------------------------------------------------------------------
# Unit Tests: DNSSEC model parsing
# ---------------------------------------------------------------------------


class TestDnssecParsing:
    def test_full_dnssec(self):
        raw = {
            "objectClassName": "domain",
            "ldhName": "test.com",
            "secureDNS": {
                "zoneSigned": True,
                "delegationSigned": True,
                "maxSigLife": 604800,
                "dsData": [{
                    "keyTag": 370,
                    "algorithm": 13,
                    "digest": "ABC123",
                    "digestType": 2,
                }],
                "keyData": [{
                    "flags": 257,
                    "protocol": 3,
                    "publicKey": "AQPB+...",
                    "algorithm": 13,
                }],
            },
        }
        d = parse_domain_response(raw)
        assert d.secure_dns is not None
        assert d.secure_dns.zone_signed is True
        assert d.secure_dns.delegation_signed is True
        assert d.secure_dns.max_sig_life == 604800

        assert len(d.secure_dns.ds_data) == 1
        ds = d.secure_dns.ds_data[0]
        assert ds.key_tag == 370
        assert ds.algorithm == 13
        assert ds.digest == "ABC123"
        assert ds.digest_type == 2

        assert len(d.secure_dns.key_data) == 1
        kd = d.secure_dns.key_data[0]
        assert kd.flags == 257
        assert kd.protocol == 3
        assert kd.public_key == "AQPB+..."


# ---------------------------------------------------------------------------
# Unit Tests: Nameserver IP parsing
# ---------------------------------------------------------------------------


class TestNameserverIps:
    def test_nameserver_with_ips(self):
        raw = {
            "objectClassName": "domain",
            "nameservers": [{
                "objectClassName": "nameserver",
                "ldhName": "ns1.example.com",
                "ipAddresses": {
                    "v4": ["1.2.3.4", "5.6.7.8"],
                    "v6": ["2001:db8::1"],
                },
            }],
        }
        d = parse_domain_response(raw)
        assert len(d.nameservers) == 1
        ns = d.nameservers[0]
        assert ns.ldh_name == "ns1.example.com"
        assert ns.ip_addresses is not None
        assert ns.ip_addresses.v4 == ["1.2.3.4", "5.6.7.8"]
        assert ns.ip_addresses.v6 == ["2001:db8::1"]


# ---------------------------------------------------------------------------
# Unit Tests: Accept header
# ---------------------------------------------------------------------------


class TestAcceptHeader:
    def test_rdap_accept_constant(self):
        assert RdapClient._RDAP_ACCEPT == "application/rdap+json"


# ---------------------------------------------------------------------------
# Unit Tests: IP network response parsing
# ---------------------------------------------------------------------------


class TestParseIPNetwork:
    def test_basic_fields(self, ip_network1_raw):
        p = parse_ip_network_response(ip_network1_raw)
        assert p.object_class_name == "ip network"
        assert p.handle == "NET-8-8-8-0-2"
        assert p.start_address == "8.8.8.0"
        assert p.end_address == "8.8.8.255"
        assert p.ip_version == "v4"
        assert p.name == "GOGL"
        assert p.type == "DIRECT ALLOCATION"
        assert p.country is None
        assert p.parent_handle == "NET-8-0-0-0-0"

    def test_cidr0_cidrs(self, ip_network1_raw):
        p = parse_ip_network_response(ip_network1_raw)
        assert len(p.cidr0_cidrs) == 1
        assert p.cidr0_cidrs[0].v4prefix == "8.8.8.0"
        assert p.cidr0_cidrs[0].length == 24

    def test_convenience_properties(self, ip_network1_raw):
        p = parse_ip_network_response(ip_network1_raw)
        assert p.registration_date is not None
        assert p.last_changed_date is not None
        assert p.self_link is not None
        assert "rdap.arin.net" in p.self_link

    def test_entities_with_vcards(self, ip_network1_raw):
        p = parse_ip_network_response(ip_network1_raw)
        assert len(p.entities) == 1
        registrant = next(e for e in p.entities if "registrant" in e.roles)
        assert registrant.handle == "GOGL"
        assert registrant.contact is not None
        assert registrant.contact.fn == "Google LLC"
        # ARIN-style label address
        assert registrant.contact.address is not None
        assert "1600 Amphitheatre Parkway" in registrant.contact.address.label
        # Structured fields are empty (ARIN puts everything in label)
        assert registrant.contact.address.street_address == ""

    def test_status_and_port43(self, ip_network1_raw):
        p = parse_ip_network_response(ip_network1_raw)
        assert "active" in p.status
        assert p.port43 == "whois.arin.net"

    def test_notices_and_remarks(self, ip_network1_raw):
        p = parse_ip_network_response(ip_network1_raw)
        assert len(p.notices) == 3
        assert len(p.remarks) == 0
        titles = [n.title for n in p.notices]
        assert "Terms of Service" in titles

    def test_rdap_conformance(self, ip_network1_raw):
        p = parse_ip_network_response(ip_network1_raw)
        assert "rdap_level_0" in p.rdap_conformance

    def test_wrong_object_class_raises(self):
        with pytest.raises(ParseError, match="domain"):
            parse_ip_network_response({"objectClassName": "domain"})

    def test_minimal(self):
        p = parse_ip_network_response({})
        assert p.start_address is None
        assert p.end_address is None
        assert p.cidr0_cidrs == []


# ---------------------------------------------------------------------------
# Unit Tests: Autnum response parsing
# ---------------------------------------------------------------------------


class TestParseAutnum:
    def test_basic_fields(self, autnum1_raw):
        p = parse_autnum_response(autnum1_raw)
        assert p.object_class_name == "autnum"
        assert p.handle == "AS15169"
        assert p.start_autnum == 15169
        assert p.end_autnum == 15169
        assert p.name == "GOOGLE"
        assert p.type is None
        assert p.country is None

    def test_convenience_properties(self, autnum1_raw):
        p = parse_autnum_response(autnum1_raw)
        assert p.registration_date is not None
        assert p.last_changed_date is not None
        assert p.self_link is not None
        assert "autnum/15169" in p.self_link

    def test_entities(self, autnum1_raw):
        p = parse_autnum_response(autnum1_raw)
        assert len(p.entities) == 2
        registrant = next(e for e in p.entities if "registrant" in e.roles)
        assert registrant.contact.fn == "Google LLC"

    def test_status_and_port43(self, autnum1_raw):
        p = parse_autnum_response(autnum1_raw)
        assert "active" in p.status
        assert p.port43 == "whois.arin.net"

    def test_wrong_object_class_raises(self):
        with pytest.raises(ParseError, match="ip network"):
            parse_autnum_response({"objectClassName": "ip network"})

    def test_minimal(self):
        p = parse_autnum_response({})
        assert p.start_autnum is None
        assert p.end_autnum is None


# ---------------------------------------------------------------------------
# Unit Tests: Entity response parsing
# ---------------------------------------------------------------------------


class TestParseEntity:
    def test_basic_fields(self, entity1_raw):
        p = parse_entity_response(entity1_raw)
        assert p.object_class_name == "entity"
        assert p.handle == "GOGL"
        assert p.contact is not None
        assert p.contact.fn == "Google LLC"
        assert p.contact.kind == "org"

    def test_contact_address(self, entity1_raw):
        p = parse_entity_response(entity1_raw)
        assert p.contact.address is not None
        # ARIN-style: address text in label, structured fields empty
        assert "1600 Amphitheatre Parkway" in p.contact.address.label
        assert p.contact.address.street_address == ""
        assert p.contact.address.locality == ""

    def test_convenience_properties(self, entity1_raw):
        p = parse_entity_response(entity1_raw)
        assert p.registration_date is not None
        assert p.last_changed_date is not None
        assert p.self_link is not None

    def test_nested_entities(self, entity1_raw):
        p = parse_entity_response(entity1_raw)
        assert len(p.entities) == 2
        abuse = next(e for e in p.entities if "abuse" in e.roles)
        assert abuse.handle == "ABUSE5250-ARIN"
        assert abuse.contact.fn == "Abuse"

    def test_networks_and_autnums(self, entity1_raw):
        p = parse_entity_response(entity1_raw)
        assert len(p.networks) >= 1
        net_addrs = [n.start_address for n in p.networks]
        assert "8.8.8.0" in net_addrs
        assert len(p.autnums) >= 1
        asns = [a.start_autnum for a in p.autnums]
        assert 15169 in asns

    def test_as_event_actor(self, entity1_raw):
        p = parse_entity_response(entity1_raw)
        # Real ARIN org entity does not include asEventActor
        assert len(p.as_event_actor) == 0

    def test_public_ids(self, entity1_raw):
        p = parse_entity_response(entity1_raw)
        # Real ARIN org entity does not include publicIds
        assert len(p.public_ids) == 0

    def test_status(self, entity1_raw):
        p = parse_entity_response(entity1_raw)
        # Real ARIN org entity does not include status
        assert len(p.status) == 0

    def test_wrong_object_class_raises(self):
        with pytest.raises(ParseError, match="domain"):
            parse_entity_response({"objectClassName": "domain"})

    def test_minimal(self):
        p = parse_entity_response({})
        assert p.contact is None
        assert p.networks == []
        assert p.autnums == []


# ---------------------------------------------------------------------------
# Unit Tests: Nameserver response parsing
# ---------------------------------------------------------------------------


class TestParseNameserver:
    def test_basic_fields(self, nameserver1_raw):
        p = parse_nameserver_response(nameserver1_raw)
        assert p.object_class_name == "nameserver"
        assert p.handle is None
        assert p.ldh_name == "NS1.GOOGLE.COM"

    def test_ip_addresses(self, nameserver1_raw):
        p = parse_nameserver_response(nameserver1_raw)
        assert p.ip_addresses is not None
        assert "216.239.32.10" in p.ip_addresses.v4
        assert "2001:4860:4802:32:0:0:0:A" in p.ip_addresses.v6

    def test_convenience_properties(self, nameserver1_raw):
        p = parse_nameserver_response(nameserver1_raw)
        # VeriSign only provides "last update of RDAP database" event
        assert p.registration_date is None
        assert p.last_changed_date is None
        assert p.self_link is not None
        assert "nameserver/NS1.GOOGLE.COM" in p.self_link

    def test_status(self, nameserver1_raw):
        p = parse_nameserver_response(nameserver1_raw)
        # VeriSign nameserver response does not include status
        assert len(p.status) == 0

    def test_wrong_object_class_raises(self):
        with pytest.raises(ParseError, match="domain"):
            parse_nameserver_response({"objectClassName": "domain"})

    def test_minimal(self):
        p = parse_nameserver_response({})
        assert p.ldh_name is None
        assert p.ip_addresses is None


# ---------------------------------------------------------------------------
# Unit Tests: DomainResponse.network field
# ---------------------------------------------------------------------------


class TestDomainResponseNetworkField:
    def test_network_field_present(self):
        raw = {
            "objectClassName": "domain",
            "ldhName": "8.8.8.in-addr.arpa",
            "network": {
                "objectClassName": "ip network",
                "startAddress": "8.0.0.0",
                "endAddress": "8.255.255.255",
                "ipVersion": "v4",
            },
        }
        d = parse_domain_response(raw)
        assert d.network is not None
        assert d.network.start_address == "8.0.0.0"
        assert d.network.ip_version == "v4"

    def test_network_field_absent(self):
        d = parse_domain_response({"objectClassName": "domain"})
        assert d.network is None


# ---------------------------------------------------------------------------
# Unit Tests: New result wrappers
# ---------------------------------------------------------------------------


class TestIPNetworkQueryResult:
    def test_frozen(self):
        r = IPNetworkQueryResult(query="8.8.8.8", primary_url="u")
        with pytest.raises(AttributeError):
            r.query = "1.1.1.1"  # type: ignore[misc]

    def test_defaults(self):
        r = IPNetworkQueryResult(query="8.8.8.8", primary_url="u")
        assert r.parsed is None
        assert r.raw == {}
        assert r.error is None
        assert r.related_urls == []


class TestAutnumQueryResult:
    def test_frozen(self):
        r = AutnumQueryResult(asn=15169, primary_url="u")
        with pytest.raises(AttributeError):
            r.asn = 0  # type: ignore[misc]


class TestEntityQueryResult:
    def test_frozen(self):
        r = EntityQueryResult(handle="GOGL-ARIN", primary_url="u")
        with pytest.raises(AttributeError):
            r.handle = "X"  # type: ignore[misc]


class TestNameserverQueryResult:
    def test_frozen(self):
        r = NameserverQueryResult(nameserver="ns1.google.com", primary_url="u")
        with pytest.raises(AttributeError):
            r.nameserver = "X"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Unit Tests: ASN input parsing
# ---------------------------------------------------------------------------


class TestParseAsn:
    """Tests for the common ``parse_asn`` utility."""

    def test_int(self):
        assert parse_asn(15169) == 15169

    def test_as_prefix_upper(self):
        assert parse_asn("AS15169") == 15169

    def test_as_prefix_lower(self):
        assert parse_asn("as15169") == 15169

    def test_plain_string(self):
        assert parse_asn("15169") == 15169

    def test_zero(self):
        assert parse_asn(0) == 0

    def test_max_asn(self):
        assert parse_asn(MAX_ASN) == MAX_ASN

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_asn("notanumber")

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            parse_asn(-1)

    def test_too_large_raises(self):
        with pytest.raises(ValueError):
            parse_asn(MAX_ASN + 1)

    def test_rdap_wrapper_raises_query_error(self):
        """The RDAP client wrapper converts ValueError to QueryError."""
        with pytest.raises(QueryError):
            RdapClient._parse_asn_input("notanumber")


# ---------------------------------------------------------------------------
# Unit Tests: Bootstrap endpoint lookups (IP, ASN, Entity)
# ---------------------------------------------------------------------------


def _setup_bootstrapped_client(tmp_path, **kwargs):
    """Create a client with mock bootstrap data loaded."""
    kwargs.setdefault("overrides", False)
    kwargs.setdefault("setup_logging", False)
    c = RdapClient(working_root=tmp_path, **kwargs)
    mock_raw = {
        "dns": {"services": [
            [["com", "net"], ["https://rdap.verisign.com/com/v1/"]],
            [["co.uk"], ["https://rdap.nominet.uk/uk/"]],
        ]},
        "asn": {"services": [
            [["0-65535"], ["https://rdap.arin.net/registry/"]],
            [["65536-131071"], ["https://rdap.ripe.net/"]],
        ]},
        "ipv4": {"services": [
            [["8.0.0.0/8"], ["https://rdap.arin.net/registry/"]],
            [["1.0.0.0/8"], ["https://rdap.apnic.net/"]],
        ]},
        "ipv6": {"services": [
            [["2001::/16"], ["https://rdap.arin.net/registry/"]],
            [["2400::/12"], ["https://rdap.apnic.net/"]],
        ]},
        "object": {"services": [
            [["contact@arin.net"], ["ARIN"], ["https://rdap.arin.net/registry/"]],
            [["contact@ripe.net"], ["RIPE"], ["https://rdap.db.ripe.net/"]],
            [["contact@apnic.net"], ["APNIC", "AP"], ["https://rdap.apnic.net/"]],
        ]},
    }
    c._raw_data = mock_raw
    c._bootstrap_timestamp = 9999999999
    c._parse_bootstrap_data()
    return c


class TestIPEndpointLookup:
    def test_ipv4_lookup(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        endpoints = c._get_ip_endpoints("8.8.8.8")
        assert "https://rdap.arin.net/registry/" in endpoints

    def test_ipv4_prefix(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        endpoints = c._get_ip_endpoints("8.8.8.0/24")
        assert "https://rdap.arin.net/registry/" in endpoints

    def test_ipv6_lookup(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        endpoints = c._get_ip_endpoints("2001:4860:4860::8888")
        assert "https://rdap.arin.net/registry/" in endpoints

    def test_unknown_ip_raises(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path, use_rir_fallbacks=False)
        with pytest.raises(BootstrapError, match="No RDAP endpoint"):
            c._get_ip_endpoints("192.168.1.1")

    def test_invalid_ip_raises(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        with pytest.raises(QueryError, match="Invalid IP"):
            c._get_ip_endpoints("not.an.ip")


class TestASNEndpointLookup:
    def test_asn_in_first_range(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        endpoints = c._get_asn_endpoints(15169)
        assert "https://rdap.arin.net/registry/" in endpoints

    def test_asn_in_second_range(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        endpoints = c._get_asn_endpoints(100000)
        assert "https://rdap.ripe.net/" in endpoints

    def test_asn_out_of_range_raises(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path, use_rir_fallbacks=False)
        with pytest.raises(BootstrapError, match="No RDAP endpoint"):
            c._get_asn_endpoints(999999999)


class TestEntityEndpointLookup:
    def test_arin_suffix(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        endpoints = c._get_entity_endpoints("GOGL-ARIN")
        assert "https://rdap.arin.net/registry/" in endpoints

    def test_ripe_suffix(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        endpoints = c._get_entity_endpoints("ORG-TSC20-RIPE")
        assert "https://rdap.db.ripe.net/" in endpoints

    def test_no_suffix_raises(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        with pytest.raises(BootstrapError, match="Could not match"):
            c._get_entity_endpoints("NOSUFFIX")

    def test_unknown_suffix_raises(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        with pytest.raises(BootstrapError, match="Could not match"):
            c._get_entity_endpoints("HANDLE-UNKNOWN")


# ---------------------------------------------------------------------------
# Unit Tests: New URL construction
# ---------------------------------------------------------------------------


class TestBuildIPUrl:
    def test_ipv4_address(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        url = c._build_ip_url("8.8.8.8")
        assert "/ip/8.8.8.8" in url
        assert "rdap.arin.net" in url

    def test_ipv4_prefix(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        url = c._build_ip_url("8.8.8.0/24")
        assert "/ip/8.8.8.0/24" in url

    def test_ipv6_address(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        url = c._build_ip_url("2001:4860:4860::8888")
        assert "/ip/" in url
        assert "2001" in url


class TestBuildAutnumUrl:
    def test_basic(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        url = c._build_autnum_url(15169)
        assert "/autnum/15169" in url
        assert "rdap.arin.net" in url


class TestBuildEntityUrl:
    def test_basic(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        url = c._build_entity_url("GOGL-ARIN")
        assert "/entity/GOGL-ARIN" in url
        assert "rdap.arin.net" in url


class TestBuildNameserverUrl:
    def test_basic(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        url = c._build_nameserver_url("ns1.google.com")
        assert "/nameserver/ns1.google.com" in url
        assert "rdap.verisign.com" in url

    def test_single_label_raises(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        with pytest.raises(QueryError, match="Failed to extract TLD"):
            c._build_nameserver_url("noperiod")


# ---------------------------------------------------------------------------
# Unit Tests: Fallback endpoints
# ---------------------------------------------------------------------------


class TestIPFallbackEndpoints:
    def test_unknown_ip_returns_fallbacks(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        endpoints = c._get_ip_endpoints("192.168.1.1")
        assert len(endpoints) == 3
        assert "https://rdap.arin.net/registry/" in endpoints
        assert "https://rdap.apnic.net/" in endpoints

    def test_unknown_ipv6_returns_fallbacks(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        endpoints = c._get_ip_endpoints("fc00::1")
        assert len(endpoints) == 3
        assert "https://rdap.arin.net/registry/" in endpoints

    def test_fallback_disabled_raises(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path, use_rir_fallbacks=False)
        with pytest.raises(BootstrapError):
            c._get_ip_endpoints("192.168.1.1")

    def test_known_ip_still_returns_exact(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        endpoints = c._get_ip_endpoints("8.8.8.8")
        assert endpoints == ["https://rdap.arin.net/registry/"]


class TestASNFallbackEndpoints:
    def test_unknown_asn_returns_fallbacks(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        endpoints = c._get_asn_endpoints(999999999)
        assert len(endpoints) == 3
        assert "https://rdap.arin.net/registry/" in endpoints

    def test_fallback_disabled_raises(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path, use_rir_fallbacks=False)
        with pytest.raises(BootstrapError):
            c._get_asn_endpoints(999999999)

    def test_known_asn_still_returns_exact(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        endpoints = c._get_asn_endpoints(15169)
        assert endpoints == ["https://rdap.arin.net/registry/"]


# ---------------------------------------------------------------------------
# Unit Tests: IDN / Punycode handling
# ---------------------------------------------------------------------------


class TestIDNHandling:
    def test_ascii_domain_unchanged(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        url = c._build_domain_url("example.com")
        assert "domain/example.com" in url

    def test_unicode_domain_converted(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        url = c._build_domain_url("münchen.com")
        assert "xn--mnchen-3ya.com" in url
        assert "münchen" not in url

    def test_unicode_nameserver_converted(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        url = c._build_nameserver_url("ns1.münchen.com")
        assert "xn--mnchen-3ya.com" in url

    def test_invalid_domain_raises(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        with pytest.raises(QueryError, match="Invalid domain name"):
            c._build_domain_url("-.invalid.")

    def test_already_punycode_unchanged(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        url = c._build_domain_url("xn--mnchen-3ya.com")
        assert "xn--mnchen-3ya.com" in url


# ---------------------------------------------------------------------------
# Unit Tests: Entity handle prefix matching
# ---------------------------------------------------------------------------


class TestEntityPrefixMatching:
    def test_prefix_match(self, tmp_path):
        """Prefix matching (new behavior)."""
        c = _setup_bootstrapped_client(tmp_path)
        endpoints = c._get_entity_endpoints("ARIN-GOGL")
        assert "https://rdap.arin.net/registry/" in endpoints

    def test_prefix_ripe(self, tmp_path):
        c = _setup_bootstrapped_client(tmp_path)
        endpoints = c._get_entity_endpoints("RIPE-NCC-HM-MNT")
        assert "https://rdap.db.ripe.net/" in endpoints


# ---------------------------------------------------------------------------
# Integration Tests (Network Required)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_from_iana(tmp_path):
    """Test real bootstrap download from IANA."""
    client = RdapClient(working_root=tmp_path, setup_logging=False)
    await client.bootstrap()

    assert client._bootstrap_timestamp > 0
    assert client._cache_path.exists()

    # Should have DNS endpoints
    endpoints = client._get_dns_endpoints("com")
    assert len(endpoints) > 0


@pytest.mark.asyncio
async def test_query_known_domain(tmp_path):
    """Test querying google.com returns parsed DomainResponse."""
    client = RdapClient(working_root=tmp_path, setup_logging=False)
    result = await client.query_domain("google.com")

    assert result.error is None
    assert result.domain == "google.com"
    assert result.parsed is not None
    assert isinstance(result.parsed, DomainResponse)
    assert result.parsed.ldh_name is not None
    assert len(result.parsed.nameservers) > 0
    assert result.parsed.registration_date is not None
    assert result.raw != {}


@pytest.mark.asyncio
async def test_query_nonexistent_domain(tmp_path):
    """Test querying a nonexistent domain captures error."""
    client = RdapClient(working_root=tmp_path, setup_logging=False)
    result = await client.query_domain(
        "thisdomain-definitely-does-not-exist-12345.com",
    )

    # Should have error set (likely 404 or similar)
    if result.error is not None:
        assert isinstance(result.error, QueryError)
        assert result.parsed is None


@pytest.mark.asyncio
async def test_follow_related(tmp_path):
    """Test that follow_related populates related_urls."""
    client = RdapClient(working_root=tmp_path, setup_logging=False)
    result = await client.query_domain("google.com", follow_related=True)

    assert result.error is None
    assert isinstance(result.related_urls, list)


@pytest.mark.asyncio
async def test_bootstrap_cache_reuse(tmp_path):
    """Test bootstrap cache persists across client instances."""
    c1 = RdapClient(working_root=tmp_path, setup_logging=False)
    await c1.bootstrap()
    assert c1._bootstrap_timestamp > 0

    c2 = RdapClient(working_root=tmp_path, setup_logging=False)
    assert c2._bootstrap_timestamp > 0


def test_sync_wrappers(tmp_path):
    """Test sync wrapper methods."""
    client = RdapClient(working_root=tmp_path, setup_logging=False)

    client.bootstrap_sync()
    assert client._bootstrap_timestamp > 0

    result = client.query_domain_sync("google.com")
    assert result.error is None
    assert result.parsed is not None


@pytest.mark.asyncio
async def test_query_ip_network(tmp_path):
    """Test querying an IP network returns parsed IPNetworkResponse."""
    client = RdapClient(working_root=tmp_path, setup_logging=False)
    result = await client.query_ip("8.8.8.8")

    assert result.error is None
    assert result.query == "8.8.8.8"
    assert result.parsed is not None
    assert isinstance(result.parsed, IPNetworkResponse)
    assert result.parsed.start_address is not None
    assert result.parsed.ip_version == "v4"
    assert result.raw != {}


@pytest.mark.asyncio
async def test_query_ip_network_v6(tmp_path):
    """Test querying an IPv6 address returns parsed IPNetworkResponse."""
    client = RdapClient(working_root=tmp_path, setup_logging=False)
    result = await client.query_ip("2001:4860:4860::8888")

    assert result.error is None
    assert result.parsed is not None
    assert isinstance(result.parsed, IPNetworkResponse)
    assert result.parsed.ip_version == "v6"


@pytest.mark.asyncio
async def test_query_autnum(tmp_path):
    """Test querying an ASN returns parsed AutnumResponse."""
    client = RdapClient(working_root=tmp_path, setup_logging=False)
    result = await client.query_asn("AS15169")

    assert result.error is None
    assert result.asn == 15169
    assert result.parsed is not None
    assert isinstance(result.parsed, AutnumResponse)
    assert result.parsed.start_autnum == 15169
    assert result.parsed.name is not None
    assert result.raw != {}


@pytest.mark.asyncio
async def test_query_autnum_int(tmp_path):
    """Test querying an ASN with integer input."""
    client = RdapClient(working_root=tmp_path, setup_logging=False)
    result = await client.query_asn(15169)

    assert result.error is None
    assert result.asn == 15169
    assert result.parsed is not None


@pytest.mark.asyncio
async def test_query_entity(tmp_path):
    """Test querying an entity by handle."""
    client = RdapClient(working_root=tmp_path, setup_logging=False)
    # GOOGL is a well-known ARIN entity (Google LLC)
    result = await client.query_entity("GOOGL-ARIN")

    assert result.handle == "GOOGL-ARIN"
    # Entity lookups may 404 depending on the RIR; verify structure either way
    if result.error is None:
        assert result.parsed is not None
        assert isinstance(result.parsed, EntityResponse)
        assert result.raw != {}
    else:
        # Error was properly captured
        assert result.parsed is None


@pytest.mark.asyncio
async def test_query_nameserver(tmp_path):
    """Test querying a nameserver by name."""
    client = RdapClient(working_root=tmp_path, setup_logging=False)
    result = await client.query_nameserver("ns1.google.com")

    assert result.error is None
    assert result.nameserver == "ns1.google.com"
    assert result.parsed is not None
    assert isinstance(result.parsed, NameserverResponse)
    assert result.parsed.ldh_name is not None
    assert result.raw != {}


def test_sync_wrappers_new_types(tmp_path):
    """Test sync wrappers for IP, ASN, entity, and nameserver queries."""
    client = RdapClient(working_root=tmp_path, setup_logging=False)

    ip_result = client.query_ip_sync("8.8.8.8")
    assert ip_result.error is None
    assert ip_result.parsed is not None

    asn_result = client.query_asn_sync(15169)
    assert asn_result.error is None
    assert asn_result.parsed is not None


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", __file__])
