from pathlib import Path
import pandas as pd
import requests
import gzip

from tmautils.common import IOHelper


class CaidaAsOrgInfoUtil:
    """
    Utility class for interacting with the CAIDA AS-Organization dataset.

    Args:
        date_str (str):
            Date string in the ISO format (YYYY-MM-DD)

        working_root (Path | None):
            Base directory where the namespace directory will be created.
            If None, the current working directory will be used.

        data_dir (Path | None):
            Deprecated alias for `working_root`.

        **kwargs (dict):
            Additional arguments for IOHelper.
            See the IOHelper class for more details.
    """

    def __init__(
        self,
        date_str: str,
        *,
        working_root: Path | None = None,
        data_dir: Path | None = None,
        **kwargs,
    ):
        working_root = IOHelper.handle_working_root_data_dir(
            working_root, data_dir
        )
        self.io_helper = IOHelper.init_with_dirs(
            self.__class__.__name__,
            dirs={"raw", "processed", "logs"},
            working_root=working_root,
            **kwargs,
        )

        # Convert date_str from YYYY-MM-DD to YYYYMMDD
        self.date_str = date_str.replace('-', '')

        self.org_id_parquet = (
            self.io_helper.processed /
            f"{date_str}.as-org2info.v0.org_id.parquet"
        )
        self.aut_parquet = (
            self.io_helper.processed /
            f"{date_str}.as-org2info.v0.aut.parquet"
        )

        if (self.org_id_parquet.exists() and self.aut_parquet.exists()):
            self.df_org_id = pd.read_parquet(self.org_id_parquet)
            self.df_aut = pd.read_parquet(self.aut_parquet)
            self.io_helper.logger.info(
                f"Loaded {self.org_id_parquet.name} and {self.aut_parquet.name} from disk."
            )
        else:
            self.io_helper.logger.info(
                f"Parquet files not found, attempting to locate raw data."
            )

            gz_file = self.io_helper.raw / f"{date_str}.as-org2info.v0.txt.gz"
            if not gz_file.exists():
                self.io_helper.logger.info(
                    f"Raw gz file not found, downloading from CAIDA."
                )
                url = (f"https://publicdata.caida.org/datasets/as-organizations/versions/"
                       f"{self.date_str}.as-org2info.v0.txt.gz")
                try:
                    r = requests.get(url, timeout=30)
                except requests.exceptions.Timeout:
                    self.io_helper.logger.error(
                        f"Could not download CaidaAsOrgInfo dataset, cannot proceed"
                    )
                    raise
                else:
                    gz_file.write_bytes(r.content)

            self.df_org_id, self.df_aut = self._parse_tables(gz_file)
            self.df_org_id.to_parquet(self.org_id_parquet)
            self.df_aut.to_parquet(self.aut_parquet)
            self.io_helper.logger.info(
                f"Wrote {self.org_id_parquet.name} and {self.aut_parquet.name} to disk."
            )

        self.df_org_id.set_index('org_id', inplace=True)
        self.df_aut.set_index('aut', inplace=True)

    def _parse_tables(
        self,
        gz_file: Path,
    ):
        def flush_table():
            if current_cols and current_rows:
                tables.append((current_cols, current_rows))

        tables = []
        current_cols = None
        current_rows = []

        with gzip.open(gz_file, 'rt') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                if line.startswith('# format:'):
                    # Whenever we see a new format line, flush the existing table first
                    flush_table()
                    format_str = line.split(':', 1)[1].strip()
                    current_cols = format_str.split('|')
                    current_rows = []
                elif line.startswith('#'):
                    # Other comment lines
                    continue
                else:
                    # Normal data line
                    if current_cols:
                        fields = line.split('|')
                        current_rows.append(fields)
                    # If no current_cols, it means we haven't hit the first # format yet

        # Flush the last table
        flush_table()

        if len(tables) != 2:
            errormsg = f"Expected exactly 2 tables, but found {len(tables)} in {gz_file.name}"
            self.io_helper.logger.error(errormsg)
            raise ValueError(errormsg)

        # Unpack the two tables
        org_id_cols, org_id_data = tables[0]
        aut_cols, aut_data = tables[1]

        df_org_id = pd.DataFrame(org_id_data, columns=org_id_cols)
        df_aut = pd.DataFrame(aut_data, columns=aut_cols)

        return df_org_id, df_aut

    def lookup(
        self,
        asn: int,
    ) -> tuple[str | None, str | None]:
        """
        Lookup the organization name and autonomous system name for a given ASN.

        Args:
            asn (int):
                ASN to query.

        Returns:
            (aut_name, org_name):
                Tuple containing the autonomous system name and organization name.
                One or both may be None if not found.
        """

        aut_str = str(asn)
        if aut_str not in self.df_aut.index:
            return None, None

        row_aut = self.df_aut.loc[aut_str]
        aut_name = row_aut['aut_name']
        org_id = row_aut['org_id']
        if org_id not in self.df_org_id.index:
            return aut_name, None

        row_org_id = self.df_org_id.loc[org_id]
        org_name = row_org_id['org_name']

        return aut_name, org_name

    def annotate_df(
        self,
        df: pd.DataFrame,
        asn_col: str = 'asn',
        as_name_col: str = 'as_name',
        org_name_col: str = 'org_name',
    ) -> pd.DataFrame:
        """
        Annotate a DataFrame with autonomous system and organization names.

        Args:
            df (pandas.DataFrame):
                DataFrame to annotate.

            asn_col (str):
                Column name containing ASN values.

            as_name_col (str):
                Column name to store autonomous system names.

            org_name_col (str):
                Column name to store organization names.

        Returns:
            pandas.DataFrame:
                Annotated DataFrame with new columns for autonomous system and organization names.
        """

        name_map = {}
        org_map = {}

        unique_asns = df[asn_col].dropna().unique()
        for asn in unique_asns:
            name, org = self.lookup(asn)
            name_map[asn] = name if name else pd.NA
            org_map[asn] = org if org else pd.NA

        df[as_name_col] = df[asn_col].map(name_map).astype('string')
        df[org_name_col] = df[asn_col].map(org_map).astype('string')

        return df
