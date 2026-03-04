# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

import gzip
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tmautils.bgp import CaidaAsOrgInfoUtil


# Sample CAIDA AS-Org data format
SAMPLE_AS_ORG_DATA = """\
# some header comment
# format:org_id|changed|org_name|country|source
ORG-001|20200101|Example Organization|US|ARIN
ORG-002|20200201|Another Org|DE|RIPE
ORG-003|20200301|Third Corp|JP|APNIC
# format:aut|changed|aut_name|org_id|opaque_id|source
12345|20200101|EXAMPLE-AS|ORG-001|abc123|ARIN
67890|20200201|ANOTHER-AS|ORG-002|def456|RIPE
11111|20200301|THIRD-AS|ORG-003|ghi789|APNIC
99999|20200401|ORPHAN-AS|ORG-MISSING|jkl012|ARIN
"""


@pytest.fixture
def sample_gz_file(tmp_path: Path) -> Path:
    """Create a sample gzipped AS-Org data file in the IOHelper-managed path."""
    # IOHelper creates: working_root / ClassName / raw /
    gz_path = tmp_path / "CaidaAsOrgInfoUtil" / \
        "raw" / "2020-01-01.as-org2info.v0.txt.gz"
    gz_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(gz_path, 'wt') as f:
        f.write(SAMPLE_AS_ORG_DATA)
    return gz_path


@pytest.fixture
def util_with_data(tmp_path: Path, sample_gz_file: Path) -> CaidaAsOrgInfoUtil:
    """Create a CaidaAsOrgInfoUtil with pre-existing raw data."""
    return CaidaAsOrgInfoUtil(
        "2020-01-01",
        working_root=tmp_path,
        setup_logging=False,
    )


class TestCaidaAsOrgInfoUtil:
    def test_init_creates_parquet(self, tmp_path: Path, sample_gz_file: Path):
        """Test that init parses raw data and creates parquet file."""
        util = CaidaAsOrgInfoUtil(
            "2020-01-01",
            working_root=tmp_path,
            setup_logging=False,
        )

        parquet_path = tmp_path / "CaidaAsOrgInfoUtil" / \
            "processed" / "2020-01-01.as-org2info.v0.parquet"
        assert parquet_path.exists()

    def test_init_loads_existing_parquet(self, tmp_path: Path, sample_gz_file: Path):
        """Test that init loads existing parquet without re-downloading."""
        # First init creates parquet
        util1 = CaidaAsOrgInfoUtil(
            "2020-01-01",
            working_root=tmp_path,
            setup_logging=False,
        )

        # Remove raw file to ensure it's not re-parsed
        sample_gz_file.unlink()

        # Second init should load from parquet
        util2 = CaidaAsOrgInfoUtil(
            "2020-01-01",
            working_root=tmp_path,
            setup_logging=False,
        )

        # Should still work
        assert util2.lookup(12345) == ("EXAMPLE-AS", "Example Organization")

    def test_lookup_found(self, util_with_data: CaidaAsOrgInfoUtil):
        """Test lookup returns correct AS name and org name."""
        aut_name, org_name = util_with_data.lookup(12345)
        assert aut_name == "EXAMPLE-AS"
        assert org_name == "Example Organization"

    def test_lookup_different_asn(self, util_with_data: CaidaAsOrgInfoUtil):
        """Test lookup for different ASN."""
        aut_name, org_name = util_with_data.lookup(67890)
        assert aut_name == "ANOTHER-AS"
        assert org_name == "Another Org"

    def test_lookup_not_found(self, util_with_data: CaidaAsOrgInfoUtil):
        """Test lookup returns None for unknown ASN."""
        aut_name, org_name = util_with_data.lookup(99999999)
        assert aut_name is None
        assert org_name is None

    def test_lookup_missing_org(self, util_with_data: CaidaAsOrgInfoUtil):
        """Test lookup when ASN exists but org_id not found."""
        aut_name, org_name = util_with_data.lookup(99999)
        assert aut_name == "ORPHAN-AS"
        assert org_name is None

    def test_lookup_caching(self, util_with_data: CaidaAsOrgInfoUtil):
        """Test that repeated lookups use cache."""
        # First lookup
        result1 = util_with_data.lookup(12345)
        # Second lookup should hit cache
        result2 = util_with_data.lookup(12345)

        assert result1 == result2
        # Check cache info
        cache_info = util_with_data._cached_lookup.cache_info()
        assert cache_info.hits >= 1

    def test_clear_cache(self, util_with_data: CaidaAsOrgInfoUtil):
        """Test clearing the lookup cache."""
        util_with_data.lookup(12345)
        util_with_data.clear_cache()

        cache_info = util_with_data._cached_lookup.cache_info()
        assert cache_info.hits == 0
        assert cache_info.misses == 0

    def test_annotate_df(self, util_with_data: CaidaAsOrgInfoUtil):
        """Test annotating a DataFrame with AS and org names."""
        df = pd.DataFrame({
            'asn': [12345, 67890, 99999999],
            'other_col': ['a', 'b', 'c'],
        })

        result = util_with_data.annotate_df(df)

        assert 'as_name' in result.columns
        assert 'org_name' in result.columns
        assert result.loc[result['asn'] == 12345,
                          'as_name'].iloc[0] == "EXAMPLE-AS"
        assert result.loc[result['asn'] == 12345,
                          'org_name'].iloc[0] == "Example Organization"
        assert result.loc[result['asn'] == 67890,
                          'as_name'].iloc[0] == "ANOTHER-AS"
        # Unknown ASN should have None
        assert pd.isna(result.loc[result['asn'] ==
                       99999999, 'as_name'].iloc[0])

    def test_annotate_df_custom_columns(self, util_with_data: CaidaAsOrgInfoUtil):
        """Test annotating with custom column names."""
        df = pd.DataFrame({
            'my_asn': [12345],
            'data': ['test'],
        })

        result = util_with_data.annotate_df(
            df,
            asn_col='my_asn',
            as_name_col='autonomous_system',
            org_name_col='organization',
        )

        assert 'autonomous_system' in result.columns
        assert 'organization' in result.columns
        assert result['autonomous_system'].iloc[0] == "EXAMPLE-AS"

    def test_df_aut_property(self, util_with_data: CaidaAsOrgInfoUtil):
        """Test backward-compatible df_aut property."""
        df_aut = util_with_data.df_aut

        assert isinstance(df_aut, pd.DataFrame)
        assert df_aut.index.name == 'aut'
        assert 12345 in df_aut.index
        assert df_aut.loc[12345, 'aut_name'] == "EXAMPLE-AS"
        assert df_aut.loc[12345, 'org_id'] == "ORG-001"

    def test_df_aut_lazy_loading(self, util_with_data: CaidaAsOrgInfoUtil):
        """Test that df_aut is lazily loaded."""
        assert util_with_data._df_aut is None
        _ = util_with_data.df_aut
        assert util_with_data._df_aut is not None

    def test_df_org_id_property(self, util_with_data: CaidaAsOrgInfoUtil):
        """Test backward-compatible df_org_id property."""
        df_org_id = util_with_data.df_org_id

        assert isinstance(df_org_id, pd.DataFrame)
        assert df_org_id.index.name == 'org_id'
        assert 'ORG-001' in df_org_id.index
        assert df_org_id.loc['ORG-001', 'org_name'] == "Example Organization"
        assert df_org_id.loc['ORG-001', 'country'] == "US"

    def test_df_org_id_lazy_loading(self, util_with_data: CaidaAsOrgInfoUtil):
        """Test that df_org_id is lazily loaded."""
        assert util_with_data._df_org_id is None
        _ = util_with_data.df_org_id
        assert util_with_data._df_org_id is not None


class TestCaidaAsOrgResilience:
    """Tests for resilience features (corrupt file handling, force refresh)."""

    def test_force_refresh(self, tmp_path: Path, sample_gz_file: Path):
        """Test that force_refresh re-downloads and reprocesses data."""
        # First init creates parquet
        util1 = CaidaAsOrgInfoUtil(
            "2020-01-01",
            working_root=tmp_path,
            setup_logging=False,
        )
        parquet_path = tmp_path / "CaidaAsOrgInfoUtil" / \
            "processed" / "2020-01-01.as-org2info.v0.parquet"
        assert parquet_path.exists()
        original_mtime = parquet_path.stat().st_mtime

        # Recreate gz file (was consumed)
        gz_path = tmp_path / "CaidaAsOrgInfoUtil" / \
            "raw" / "2020-01-01.as-org2info.v0.txt.gz"
        with gzip.open(gz_path, 'wt') as f:
            f.write(SAMPLE_AS_ORG_DATA)

        # Force refresh should recreate parquet
        import time
        time.sleep(0.01)  # Ensure mtime differs
        util2 = CaidaAsOrgInfoUtil(
            "2020-01-01",
            working_root=tmp_path,
            force_refresh=True,
            setup_logging=False,
        )
        new_mtime = parquet_path.stat().st_mtime
        assert new_mtime > original_mtime

    def test_corrupt_parquet_triggers_reprocess(self, tmp_path: Path, sample_gz_file: Path):
        """Test that corrupt parquet file triggers reprocessing."""
        # First init creates valid parquet
        util1 = CaidaAsOrgInfoUtil(
            "2020-01-01",
            working_root=tmp_path,
            setup_logging=False,
        )
        parquet_path = tmp_path / "CaidaAsOrgInfoUtil" / \
            "processed" / "2020-01-01.as-org2info.v0.parquet"
        assert parquet_path.exists()

        # Corrupt the parquet file
        parquet_path.write_bytes(b"corrupted data")

        # Second init should detect corruption and reprocess
        util2 = CaidaAsOrgInfoUtil(
            "2020-01-01",
            working_root=tmp_path,
            setup_logging=False,
        )

        # Should still work
        assert util2.lookup(12345) == ("EXAMPLE-AS", "Example Organization")

    def test_corrupt_gz_triggers_redownload(self, tmp_path: Path):
        """Test that corrupt gz file triggers re-download."""
        # Create corrupt gz file
        gz_path = tmp_path / "CaidaAsOrgInfoUtil" / \
            "raw" / "2020-01-01.as-org2info.v0.txt.gz"
        gz_path.parent.mkdir(parents=True, exist_ok=True)
        gz_path.write_bytes(b"not a valid gzip file")

        # Mock the download
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.content = gzip.compress(SAMPLE_AS_ORG_DATA.encode())

        with patch('tmautils.bgp.caida_as_org.requests.get', return_value=mock_resp):
            # Should detect corrupt gz and re-download
            util = CaidaAsOrgInfoUtil(
                "2020-01-01",
                working_root=tmp_path,
                setup_logging=False,
            )

            assert util.lookup(12345) == (
                "EXAMPLE-AS", "Example Organization")


class TestCaidaAsOrgDownload:
    def test_download_on_missing_data(self, tmp_path: Path):
        """Test that data is downloaded when raw file doesn't exist."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.content = gzip.compress(SAMPLE_AS_ORG_DATA.encode())

        with patch('tmautils.bgp.caida_as_org.requests.get', return_value=mock_resp):
            util = CaidaAsOrgInfoUtil(
                "2020-01-01",
                working_root=tmp_path,
                setup_logging=False,
            )

            # Should have downloaded and created parquet
            parquet_path = tmp_path / "CaidaAsOrgInfoUtil" / \
                "processed" / "2020-01-01.as-org2info.v0.parquet"
            assert parquet_path.exists()

            # Lookup should work
            assert util.lookup(12345) == (
                "EXAMPLE-AS", "Example Organization")


@pytest.fixture(scope="session")
def caida_real_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Shared temp directory for real CAIDA data tests."""
    return tmp_path_factory.mktemp("caida_real_data")


@pytest.mark.network
class TestCaidaAsOrgRealData:
    """Integration tests using real CAIDA data. Requires network access."""

    def test_real_download_and_lookup(self, caida_real_data_dir: Path):
        """Test downloading and querying real CAIDA AS-Org data."""
        date_str = "2024-01-01"

        util = CaidaAsOrgInfoUtil(
            date_str,
            working_root=caida_real_data_dir,
            setup_logging=False,
        )

        # Verify parquet was created
        parquet_path = caida_real_data_dir / "CaidaAsOrgInfoUtil" / \
            "processed" / f"{date_str}.as-org2info.v0.parquet"
        assert parquet_path.exists()

        # Test lookup for well-known ASNs
        # AS15169 is Google
        aut_name, org_name = util.lookup(15169)
        assert aut_name is not None
        assert "GOOGLE" in aut_name.upper()

        # AS13335 is Cloudflare
        aut_name, org_name = util.lookup(13335)
        assert aut_name is not None
        assert "CLOUDFLARE" in aut_name.upper() or (
            org_name and "CLOUDFLARE" in org_name.upper())

        # AS16509 is Amazon
        aut_name, org_name = util.lookup(16509)
        assert aut_name is not None

    def test_real_annotate_df(self, caida_real_data_dir: Path):
        """Test annotating a DataFrame with real CAIDA data."""
        date_str = "2024-01-01"

        util = CaidaAsOrgInfoUtil(
            date_str,
            working_root=caida_real_data_dir,
            setup_logging=False,
        )

        df = pd.DataFrame({
            'asn': [15169, 13335, 16509, 99999999],
            'name': ['google', 'cloudflare', 'amazon', 'unknown'],
        })

        result = util.annotate_df(df)

        assert 'as_name' in result.columns
        assert 'org_name' in result.columns
        assert len(result) == 4

        # Google should have AS name
        google_row = result[result['asn'] == 15169].iloc[0]
        assert pd.notna(google_row['as_name'])

        # Unknown ASN should have null
        unknown_row = result[result['asn'] == 99999999].iloc[0]
        assert pd.isna(unknown_row['as_name'])

    def test_real_df_properties(self, caida_real_data_dir: Path):
        """Test backward-compatible DataFrame properties with real data."""
        date_str = "2024-01-01"

        util = CaidaAsOrgInfoUtil(
            date_str,
            working_root=caida_real_data_dir,
            setup_logging=False,
        )

        # Test df_aut property
        df_aut = util.df_aut
        assert len(df_aut) > 10000  # Should have many ASNs
        assert 'aut_name' in df_aut.columns
        assert 15169 in df_aut.index  # Google

        # Test df_org_id property
        df_org_id = util.df_org_id
        assert len(df_org_id) > 1000  # Should have many orgs
        assert 'org_name' in df_org_id.columns


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", os.path.abspath(__file__)])
