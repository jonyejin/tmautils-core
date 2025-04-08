from enum import Enum, StrEnum


class IPVersion(Enum):
    IPV4 = 4
    IPV6 = 6


class DomainAFSupport(Enum):
    A_ONLY = 4
    AAAA_ONLY = 6
    DUAL_STACK = 46


class L4Proto(Enum):
    ICMP = 1
    TCP = 6
    UDP = 17
    ICMPv6 = 58


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


class HappyEyeballsResult(Enum):
    DUAL_AF_USED_AAAA = 1
    DUAL_AF_USED_A = 2
    A_ONLY = 3
    AAAA_ONLY = 4
