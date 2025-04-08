import requests
import pandas

from imresearchutils.common import *


class ASdbCategoryUtil:
    def __init__(self,
                 year: int = 2024,
                 month: int = 1,
                 data_dir: Path | None = None):
        self.io_helper = IOHelper(self.__class__.__name__, data_dir=data_dir)

        url = f"https://asdb.stanford.edu/data/{year}-{month:02d}_categorized_ases.csv"
        saved_file = self.io_helper.processed / url.split("/")[-1]
        if not saved_file.exists():
            try:
                self.io_helper.logger.info(
                    f"Downloading ASdb dataset from {url} to {saved_file}"
                )
                r = requests.get(url, timeout=5)
            except requests.exceptions.Timeout:
                self.io_helper.logger.error(
                    f"Could not download ASdb dataset from {url}, cannot proceed"
                )
                raise
            else:
                saved_file.write_text(r.text)
        self.db = pandas.read_csv(
            saved_file,
            index_col=0,
            low_memory=False,
        ).iloc[:, :6].to_dict(orient='index')
        self.io_helper.logger.info(
            f"Loaded ASdb dataset from {saved_file}"
        )

    def get_full(self, asn: int):
        catdict_flat: dict[str, str | Any] = self.db.get(f"AS{asn}", {})

        catdict: dict[str, dict[str, str]] = {}
        for k, v in catdict_flat.items():
            # Skip entries where the value is NaN
            if pandas.isna(v):
                continue

            # Split the key into category and layer
            try:
                category, layer = k.split(" - ", 1)
            except ValueError:
                continue

            if category not in catdict:
                catdict[category] = {"Layer 1": None, "Layer 2": None}
            catdict[category][layer] = v

        return catdict

    def get(self, asn: int, category: str = "Category 1", layer: str = "Layer 1"):
        catdict = self.get_full(asn)
        if category not in catdict:
            return None
        if layer not in catdict[category]:
            return None
        return catdict[category][layer]
