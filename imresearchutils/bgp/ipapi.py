from ipaddress import ip_address
import requests
import pandas

from imresearchutils.common import *


class IPApiUtil:
    def __init__(self,
                 year: int = 2024,
                 month: int = 1,
                 data_dir: Path | None = None):
        self.io_helper = IOHelper(self.__class__.__name__, data_dir=data_dir)

        saved_file = IOHelper(module_name="", data_dir=Path("/home/yejin/ssd-shared/PyASN")).module_dir / "ip_cache_asn_info.csv"
        if not saved_file.exists():
            raise FileNotFoundError(f"{saved_file} not found.")
        
        self.db = pandas.read_csv(
            saved_file,
            index_col=0,
            low_memory=False,
        ).to_dict(orient='index')
        self.io_helper.logger.info(
            f"Loaded IPApiUtil dataset from {saved_file}"
        )
    def get_asn_info(self, asn:int):
        return self.db.get(asn, {})
    
    def is_mobile_proxy_hosting(self, asn):
        info = self.get_asn_info(asn)
        return (
            bool(info.get("mobile", False)),
            bool(info.get("proxy", False)),
            bool(info.get("hosting", False)),
        )
    
