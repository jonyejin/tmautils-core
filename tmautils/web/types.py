# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from typing import Optional
from enum import Enum, StrEnum
from ipaddress import IPv4Address, IPv6Address
from dataclasses import dataclass, field
from urllib.parse import urlparse

from tmautils.common import FQDN, DomainAFSupport


#######
# Enums
#######


class HappyEyeballsResult(Enum):
    DUAL_AF_USED_AAAA = 1
    DUAL_AF_USED_A = 2
    A_ONLY = 3
    AAAA_ONLY = 4


class FirefoxWebResource(StrEnum):
    BEACON = 'beacon'
    CSP_REPORT = 'csp_report'
    FONT = 'font'
    IMAGE = 'image'
    IMAGESET = 'imageset'
    MAIN_FRAME = 'main_frame'
    MEDIA = 'media'
    OBJECT = 'object'
    OBJECT_SUBREQUEST = 'object_subrequest'
    PING = 'ping'
    SCRIPT = 'script'
    SPECULATIVE = 'speculative'
    STYLESHEET = 'stylesheet'
    SUB_FRAME = 'sub_frame'
    WEB_MANIFEST = 'web_manifest'
    WEBSOCKET = 'websocket'
    XML_DTD = 'xml_dtd'
    XMLHTTPREQUEST = 'xmlhttprequest'
    XSLT = 'xslt'
    OTHER = 'other'


class FirefoxCrawlFailureReason(StrEnum):
    DNSNOTFOUND = 'dnsNotFound'
    NETRESET = 'netReset'
    CONNECTIONFAILURE = 'connectionFailure'
    NSSFAILURE2 = 'nssFailure2'
    DENIEDPORTACCESS = 'deniedPortAccess'
    DNS_NOT_FOUND_TRR_ONLY = 'dns-not-found-trr-only2'
    FILENOTFOUND = 'fileNotFound'
    FILEACCESSDENIED = 'fileAccessDenied'
    GENERIC = 'generic'
    CAPTIVEPORTAL = 'captivePortal'
    MALFORMEDURI = 'malformedURI'
    NETINTERRUPT = 'netInterrupt'
    NOTCACHED = 'notCached'
    NETOFFLINE = 'netOffline'
    CONTENTENCODINGERROR = 'contentEncodingError'
    UNSAFECONTENTTYPE = 'unsafeContentType'
    NETTIMEOUT = 'netTimeout'
    SERVERERROR = 'serverError'
    UNKNOWNPROTOCOLFOUND = 'unknownProtocolFound'
    PROXYCONNECTFAILURE = 'proxyConnectFailure'
    PROXYRESOLVEFAILURE = 'proxyResolveFailure'
    REDIRECTLOOP = 'redirectLoop'
    UNKNOWNSOCKETTYPE = 'unknownSocketType'
    CSP_XFO_ERROR = 'csp-xfo-error'
    CORRUPTEDCONTENTERROR = 'corruptedContentError'
    CORRUPTEDCONTENTERRORV2 = 'corruptedContentErrorv2'
    SSLV3USED = 'sslv3Used'
    INADEQUATESECURITYERROR = 'inadequateSecurityError'
    BLOCKEDBYPOLICY = 'blockedByPolicy'
    CLOCKSKEWERROR = 'clockSkewError'
    NETWORKPROTOCOLERROR = 'networkProtocolError'
    NSSBADCERT = 'nssBadCert'
    NSSBADCERT_STS = 'nssBadCert-sts'
    CERTERROR_MITM = 'certerror-mitm'

#############
# Dataclasses
#############


@dataclass(frozen=True)
class UsedDnsRecord:
    hostname: FQDN
    cname: FQDN
    addresses: list[IPv4Address | IPv6Address]
    used_address: IPv4Address | IPv6Address

    @property
    def has_a(self):
        return self.used_a or any(
            map(lambda x: x.version == 4, self.addresses)
        )

    @property
    def has_aaaa(self):
        return self.used_aaaa or any(
            map(lambda x: x.version == 6, self.addresses)
        )

    @property
    def used_a(self):
        return self.used_address.version == 4

    @property
    def used_aaaa(self):
        return self.used_address.version == 6

    @property
    def af_support(self):
        if self.has_a and self.has_aaaa:
            return DomainAFSupport.DUAL_STACK
        elif self.has_a:
            return DomainAFSupport.A_ONLY
        elif self.has_aaaa:
            return DomainAFSupport.AAAA_ONLY
        else:
            raise ValueError("No A or AAAA records found")

    @property
    def happy_eyeballs_result(self):
        if self.used_a:
            if self.has_aaaa:
                return HappyEyeballsResult.DUAL_AF_USED_A
            else:
                return HappyEyeballsResult.A_ONLY
        else:
            if self.has_a:
                return HappyEyeballsResult.DUAL_AF_USED_AAAA
            else:
                return HappyEyeballsResult.AAAA_ONLY


@dataclass
class CompletedHttpRequest:
    request_id: int
    url: str
    resource_type: FirefoxWebResource
    dns_record: UsedDnsRecord


@dataclass
class OpenWpmSiteCrawlResult:
    rank: int
    url: str
    visit_id: int

    failure_reason: Optional[FirefoxCrawlFailureReason] = field(
        init=False,
        default=None
    )
    root_page_url: str = field(init=False)
    http_requests: list[CompletedHttpRequest] = field(
        init=False,
        default_factory=list
    )

    @property
    def fqdn(self):
        return FQDN(urlparse(self.url).hostname)

    @property
    def root_happy_eyeballs_result(self):
        for req in self.http_requests:
            if req.url == self.root_page_url:
                return req.dns_record.happy_eyeballs_result
        return None

    @property
    def root_has_aaaa(self):
        return self.root_happy_eyeballs_result in (
            HappyEyeballsResult.AAAA_ONLY,
            HappyEyeballsResult.DUAL_AF_USED_A,
            HappyEyeballsResult.DUAL_AF_USED_AAAA
        )

    @property
    def third_party_domains(self):
        third_party_domains: dict[FQDN, DomainAFSupport] = {}
        for req in self.http_requests:
            if req.dns_record.hostname.registered_domain == self.fqdn.registered_domain:
                # This is a first-party resource
                continue
            third_party_domains[req.dns_record.hostname] = req.dns_record.af_support
        return third_party_domains

    @property
    def all_domains_have_aaaa(self):
        for req in self.http_requests:
            if not req.dns_record.has_aaaa:
                return False
        return True

    @property
    def all_domains_used_aaaa(self):
        for req in self.http_requests:
            if not req.dns_record.used_aaaa:
                return False
        return True
