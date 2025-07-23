import unittest
import pandas as pd
from ipaddress import ip_address, ip_network
from imresearchutils.common.utils import LongestPrefixSearch  # replace with actual import path

class TestLongestPrefixSearch(unittest.TestCase):
    def setUp(self):
        # Prepare a fake "db" DataFrame with some IPv4 and IPv6 prefixes
        self.df = pd.DataFrame({
            "network": [
                "192.0.2.0/24",  # index 0
                "192.0.0.0/16",  # index 1
                "1.2.3.0/24",     # covers 1.2.3.0 – 1.2.3.255
                "10.0.0.0/8",     # covers 10.0.0.0 – 10.255.255.255
                "2001:db8::/32",  # covers 2001:db8:: – 2001:db8:ffff:ffff:ffff:ffff:ffff:ffff
            ]
        })

        # Monkey‑patch the class so __init__ sees our fake db
        self.lpm = LongestPrefixSearch(self.df)

    def test_ipv4_exact_and_longer_prefix(self):
        # 192.0.2.123 → 가장 구체적인 /24 (index 0)
        result = self.lpm["192.0.2.123"]
        self.assertIsNotNone(result)
        idx, row = result
        self.assertEqual(idx, 0)
        self.assertEqual(row["network"], "192.0.2.0/24")


if __name__ == "__main__":
    unittest.main()
