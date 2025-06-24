import requests
import pandas as pd
import csv

from imresearchutils.common import *


class ASdbCategoryUtil:
    """
    Utility class for interacting with the ASdb dataset.

    Args:
        year (int):
            Year of the ASdb dataset to download.
            Default is 2024.

        month (int):
            Month of the ASdb dataset to download.
            Default is 1.

        data_dir (Path | None):
            Base directory for data files.
            If None, the current working directory will be used.

        **kwargs (dict):
            Additional arguments for IOHelper.
            See the IOHelper class for more details.
    """

    def __init__(
        self,
        year: int = 2024,
        month: int = 1,
        data_dir: Path | None = None,
        **kwargs,
    ):
        self.io_helper = IOHelper(
            self.__class__.__name__,
            data_dir=data_dir,
            **kwargs,
        )

        data_url = f"https://asdb.stanford.edu/data/{year}-{month:02d}_categorized_ases.csv"
        category_url = f"https://asdb.stanford.edu/data/NAICSlite.csv"

        saved_data_file = self.io_helper.raw / data_url.split("/")[-1]
        saved_category_file = self.io_helper.raw / category_url.split("/")[-1]

        # Check if the files exist, if not, download them
        for (url, saved_file) in [
            (data_url, saved_data_file),
            (category_url, saved_category_file)
        ]:
            if saved_file.exists():
                continue

            try:
                self.io_helper.logger.info(
                    f"Downloading ASdb data file from {url} to {saved_file}"
                )
                r = requests.get(url, timeout=5)
            except requests.exceptions.Timeout:
                self.io_helper.logger.error(
                    f"Could not download ASdb data file from {url}, cannot proceed"
                )
                raise
            else:
                saved_file.write_text(r.text)

        # Load the category dataset
        self.category = self._build_category_dict(saved_category_file)
        self.io_helper.logger.info(
            f"Loaded ASdb category dataset from {saved_category_file}"
        )

        # Load raw data file
        raw_df = pd.read_csv(saved_data_file, low_memory=False)

        # 1) identify all Layer-1 and Layer-2 columns
        l1_cols = [c for c in raw_df.columns if "Layer 1" in c]
        l2_cols = [c for c in raw_df.columns if "Layer 2" in c]

        # 2) melt into long form
        df_l1 = raw_df.melt(
            id_vars=["ASN"], value_vars=l1_cols,
            var_name="cat_layer", value_name="layer1"
        )
        df_l2 = raw_df.melt(
            id_vars=["ASN"], value_vars=l2_cols,
            var_name="cat_layer", value_name="layer2"
        )

        # 3) extract category index (1..78)
        df_l1["cat_idx"] = df_l1["cat_layer"].str.extract(
            r"Category (\d+)"
        )[0].astype(int)
        df_l2["cat_idx"] = df_l2["cat_layer"].str.extract(
            r"Category (\d+)"
        )[0].astype(int)

        # 4) merge on ASN + cat_idx
        df_long = pd.merge(
            df_l1.drop(columns="cat_layer"),
            df_l2.drop(columns="cat_layer"),
            on=["ASN", "cat_idx"],
            how="left"
        )

        # 5) clean & convert
        df_long = (
            df_long
            # drop rows where layer1 is missing or empty
            .loc[lambda d: d["layer1"].notna() & (d["layer1"] != "")]
            # strip "AS" prefix and convert to int
            .assign(
                asn=lambda d: d["ASN"].str.lstrip("AS").astype(int),
                layer2=lambda d: d["layer2"]
                .where(d["layer2"].notna() & (d["layer2"] != ""), None)
            )
            .loc[:, ["asn", "layer1", "layer2"]]
            .reset_index(drop=True)
        )

        self.df = df_long.copy()

        self.io_helper.logger.info(
            f"Loaded ASdb dataset from {saved_data_file}"
        )

    def _build_category_dict(self, category_url: str | Path) -> dict[str, list[str]]:
        """
        Build a category dictinonary. Max Depth = 2.
        Accessing non-existing category will throw an KeyError.

        Returns:
            category (dict[str, list[str]]):
                Dictionary where keys are categories and values are lists of layers.

                Example:
                ```
                { "Computer and Information Technology": ["Internet Service Provider (ISP)", "Phone Provider", ...], 
                  "Media, Publishing, and Broadcasting": ["Online Music and Video Streaming Services", ...]
                }
                ```
        """
        category: dict[str, list[str | None]] = {}
        try:
            with open(category_url, 'r', newline='\n') as file:
                category_reader = csv.reader(file, delimiter=',')
                current_category = None
                for index, row in enumerate(category_reader):
                    if index == 0:
                        # Skip header row
                        continue
                    else:
                        name, level = row[:2]
                        if level == "1":
                            category[name] = []
                            current_category = name
                        elif level == "2":
                            if name == "":
                                category[current_category].append(None)
                            else:
                                category[current_category].append(name)
            return category
        except FileNotFoundError:
            self.io_helper.logger.error(
                f"Category file not found: {category_url}"
            )
            raise
        except KeyError:
            self.io_helper.logger.error(
                f"Invalid category format in file: {category_url}"
            )
            raise
        except Exception as e:
            self.io_helper.logger.error(f"Error reading category file: {e}")
            raise

    def get_full(
        self,
        asn: int,
    ):
        """
        Get the full ASdb category dictionary for a given ASN.

        Args:
            asn (int):
                ASN to query.

        Returns:
            asinfo (pd.DataFrame):
                DataFrame containing the ASdb category information for the given ASN.
                If the ASN is not found, an empty DataFrame is returned.

                Example:
                ```
                6837    Computer and Information Technology   Internet Service Provider (ISP)  
                6837    Computer and Information Technology   None
                ```
        """

        asinfo = self.df[self.df["asn"] == asn]

        return asinfo

    def get(
        self,
        asn: int,
        layer1: str | None = None,
        layer2: str | None = None,
    ):
        """
        Get the ASdb category for a given ASN, category, and layer.

        Args:
            asn (int):
                ASN to query.
            layer1 (str | None):
                layer 1 to query.
            layer2 (str | None):
                layer 2 to query.

        Returns:
            pd.DataFrame:
                DataFrame containing the ASdb category information for the given ASN, layer 1, and layer 2.
                If the ASN is not found, an empty DataFrame is returned.
        """

        asninfo = self.get_full(asn)
        if layer1 is not None:
            asninfo = asninfo[asninfo["layer 1"] == layer1]
        if layer2 is not None:
            asninfo = asninfo[asninfo["layer 2"] == layer2]
        return asninfo

    def find_ases_in_category(
        self,
        layer1_category: str,
        layer2_category: Optional[str] = None,
    ) -> list[int]:
        """
        Return all ASNs matching the given primary and (optional) secondary category.

        Args:
            layer1_category (str): Name of the Layer-1 category to match.
            layer2_category (Optional[str]): Name of the Layer-2 category to match (if any).

        Returns:
            df (pd.DataFrame):
            DataFrame containing the ASNs that match the given categories.
            If no ASNs match, an empty DataFrame is returned.
        """
        # Filter by layer1
        df_filtered = self.df[self.df["layer1"] == layer1_category]

        # If a specific layer2 is requested, filter further
        if layer2_category is not None:
            df_filtered = df_filtered[df_filtered["layer2"] == layer2_category]

        # Extract unique ASNs
        return df_filtered.drop_duplicates()
