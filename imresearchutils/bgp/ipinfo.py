import pandas as pd
from pathlib import Path
from imresearchutils.common.io import IOHelper
from imresearchutils.common.utils import LongestPrefixSearch
from ipaddress import IPv4Address, IPv6Address


class IPInfoLiteUtil:
    # data form;
    # network,country,country_code,continent,continent_code,asn,as_name,as_domain
    # 1.0.0.0/24,Australia,AU,Oceania,OC,AS13335,"Cloudflare, Inc.",cloudflare.com
    DEFAULT_FIELDS = [
        "network",
        "country",
        "country_code",
        "continent",
        "continent_code",
        "asn",
        "as_name",
        "as_domain",
    ]

    def __init__(
            self,
            saved_csv_path: Path,
            fields: list[str] | None = None,
    ):
        # Load raw data file
        self.fields = fields if fields is not None else self.DEFAULT_FIELDS
        self.df = pd.read_csv(saved_csv_path,
                              usecols=self.fields,
                              low_memory=False)

        # Build Trie
        self.longestPrefixSearch = LongestPrefixSearch(self.df)

    def lookup(
            self,
            addr: IPv4Address | IPv6Address | str,
            fields: list[str] | None = None,
    ):
        result = self.longestPrefixSearch[addr]
        if result is None:
            return None
        else:
            idx, record = result
            return self.df.loc[idx, fields]
