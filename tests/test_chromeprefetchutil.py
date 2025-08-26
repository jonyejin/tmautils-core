import unittest
from .is_chrome_prefetch_proxy import ChromePrefetchUtil

class MyTestCase(unittest.TestCase):
    def test_something(self):
        util = ChromePrefetchUtil()
        assert(util.lookup('193.186.4.175').empty == False)
        print(util.lookup('193.186.4.175'))

if __name__ == '__main__':
    unittest.main()
