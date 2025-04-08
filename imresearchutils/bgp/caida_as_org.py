import pandas
import requests
import gzip

from imresearchutils.common import *


class CaidaAsOrgInfoUtil:
    def __init__(self, date_str: str, data_dir: Path | None = None):
        self.io_helper = IOHelper(self.__class__.__name__, data_dir=data_dir)
        self.date_str = date_str

        self.org_id_parquet = (self.io_helper.processed /
                               f"{date_str}.as-org2info.v0.org_id.parquet")
        self.aut_parquet = (self.io_helper.processed /
                            f"{date_str}.as-org2info.v0.aut.parquet")

        if (self.org_id_parquet.exists() and self.aut_parquet.exists()):
            self.df_org_id = pandas.read_parquet(self.org_id_parquet)
            self.df_aut = pandas.read_parquet(self.aut_parquet)
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

            self.df_org_id, self.df_aut = self.parse_tables(gz_file)
            self.df_org_id.to_parquet(self.org_id_parquet)
            self.df_aut.to_parquet(self.aut_parquet)
            self.io_helper.logger.info(
                f"Wrote {self.org_id_parquet.name} and {self.aut_parquet.name} to disk."
            )

        self.df_org_id.set_index('org_id', inplace=True)
        self.df_aut.set_index('aut', inplace=True)

    def parse_tables(self, gz_file: Path):
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

        df_org_id = pandas.DataFrame(org_id_data, columns=org_id_cols)
        df_aut = pandas.DataFrame(aut_data, columns=aut_cols)

        return df_org_id, df_aut

    def lookup(self, asn: int) -> tuple[str | None, str | None]:
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
