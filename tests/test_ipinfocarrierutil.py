import unittest
from imresearchutils.app.is_carrier import IpInfoCarrierUtil
from pathlib import Path

class MyTestCase(unittest.TestCase):
    def test_something(self):
        ipinfocarrierutil = IpInfoCarrierUtil(
            ipinfo_carrier_dir=Path('/home/yejin/ssd-shared/datasets/ipinfo/ipinfo_carrier'),

        )
        # print(ipinfocarrierutil.lookup('1.0.204.0'))
        print(ipinfocarrierutil.lookup('2c0f:fe38:232d::'))
        self.assertEqual(ipinfocarrierutil.is_ip_carrier('123.45.67.89'), False)  # add assertion here

if __name__ == '__main__':
    unittest.main()
