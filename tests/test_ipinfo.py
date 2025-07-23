from pathlib import Path

from imresearchutils.bgp.ipinfo import IPInfoLiteUtil
import pandas as pd
import pytest
from ipaddress import IPv4Address

@pytest.fixture(scope="session")
def util():
    # 이 fixture는 테스트 세션 전체에서 단 한 번만 실행됩니다.
    csv_path = Path("/home/yejin/ssd-data/DualStack/carrier-grade-NAT-investigation/ipinfo_lite-2.csv")
    util = IPInfoLiteUtil(saved_csv_path=csv_path)
    return util

def test_lookup(util):
    # Lookup an IP within the first prefix
    result = util.lookup("1.2.3.45", "country_code")
    print(result)
