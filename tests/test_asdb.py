# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from tmautils.bgp import ASdbCategoryUtil


# Sample ASdb categorized ASes data (CSV format)
SAMPLE_ASDB_DATA = """\
ASN,Category 1 Layer 1,Category 1 Layer 2,Category 2 Layer 1,Category 2 Layer 2,Category 3 Layer 1,Category 3 Layer 2
AS12345,Computer and Information Technology,Internet Service Provider (ISP),Media Publishing and Broadcasting,Online Music Streaming,,
AS67890,Education,University,,,,
AS11111,Government,Federal Agency,Infrastructure,Data Center,,
"""

# Sample NAICSlite category data
SAMPLE_CATEGORY_DATA = """\
Name,Level
Computer and Information Technology,1
Internet Service Provider (ISP),2
Cloud Provider,2
Phone Provider,2
Media Publishing and Broadcasting,1
Online Music Streaming,2
TV Broadcasting,2
,2
Education,1
University,2
K-12 School,2
Government,1
Federal Agency,2
State Agency,2
Infrastructure,1
Data Center,2
"""


@pytest.fixture
def sample_csv_files(tmp_path: Path) -> tuple[Path, Path]:
    """Create sample CSV files in the IOHelper-managed path."""
    # IOHelper creates: working_root / ClassName / raw /
    raw_dir = tmp_path / "ASdbCategoryUtil" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    data_path = raw_dir / "2024-01_categorized_ases.csv"
    data_path.write_text(SAMPLE_ASDB_DATA)

    category_path = raw_dir / "NAICSlite.csv"
    category_path.write_text(SAMPLE_CATEGORY_DATA)

    return data_path, category_path


@pytest.fixture
def util_with_data(tmp_path: Path, sample_csv_files: tuple[Path, Path]) -> ASdbCategoryUtil:
    """Create an ASdbCategoryUtil with pre-existing raw data."""
    return ASdbCategoryUtil(
        year=2024,
        month=1,
        working_root=tmp_path,
        setup_logging=False,
    )


class TestASdbCategoryUtil:
    def test_init_creates_parquet(self, tmp_path: Path, sample_csv_files: tuple[Path, Path]):
        """Test that init parses raw data and creates parquet file."""
        util = ASdbCategoryUtil(
            year=2024,
            month=1,
            working_root=tmp_path,
            setup_logging=False,
        )

        parquet_path = tmp_path / "ASdbCategoryUtil" / \
            "processed" / "2024-01_asdb.parquet"
        assert parquet_path.exists()

    def test_init_loads_existing_parquet(self, tmp_path: Path, sample_csv_files: tuple[Path, Path]):
        """Test that init loads existing parquet without re-parsing."""
        # First init creates parquet
        util1 = ASdbCategoryUtil(
            year=2024,
            month=1,
            working_root=tmp_path,
            setup_logging=False,
        )

        # Remove raw CSV file to ensure it's not re-parsed
        data_path, _ = sample_csv_files
        data_path.unlink()

        # Second init should load from parquet
        util2 = ASdbCategoryUtil(
            year=2024,
            month=1,
            working_root=tmp_path,
            setup_logging=False,
        )

        # Should still work
        result = util2.get_full(12345)
        assert len(result) > 0

    def test_get_full_found(self, util_with_data: ASdbCategoryUtil):
        """Test get_full returns all categories for an ASN."""
        result = util_with_data.get_full(12345)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 2  # Has 2 categories
        assert 'asn' in result.columns
        assert 'cat_idx' in result.columns
        assert 'layer1' in result.columns
        assert 'layer2' in result.columns

        # Check first category
        row1 = result[result['cat_idx'] == 1].iloc[0]
        assert row1['layer1'] == "Computer and Information Technology"
        assert row1['layer2'] == "Internet Service Provider (ISP)"

        # Check second category
        row2 = result[result['cat_idx'] == 2].iloc[0]
        assert row2['layer1'] == "Media Publishing and Broadcasting"
        assert row2['layer2'] == "Online Music Streaming"

    def test_get_full_not_found(self, util_with_data: ASdbCategoryUtil):
        """Test get_full returns empty DataFrame for unknown ASN."""
        result = util_with_data.get_full(99999999)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0
        assert list(result.columns) == ['asn', 'cat_idx', 'layer1', 'layer2']

    def test_get_with_filter(self, util_with_data: ASdbCategoryUtil):
        """Test get with layer filtering."""
        result = util_with_data.get(
            12345,
            layer1="Computer and Information Technology"
        )

        assert len(result) == 1
        assert result.iloc[0]['layer2'] == "Internet Service Provider (ISP)"

    def test_get_with_both_filters(self, util_with_data: ASdbCategoryUtil):
        """Test get with both layer1 and layer2 filtering."""
        result = util_with_data.get(
            12345,
            layer1="Computer and Information Technology",
            layer2="Internet Service Provider (ISP)"
        )

        assert len(result) == 1

        # Non-matching filter should return empty
        result_empty = util_with_data.get(
            12345,
            layer1="Computer and Information Technology",
            layer2="Cloud Provider"
        )
        assert len(result_empty) == 0

    def test_find_ases_in_category_layer1_only(self, util_with_data: ASdbCategoryUtil):
        """Test finding ASNs by layer1 category."""
        result = util_with_data.find_ases_in_category("Education")

        assert len(result) == 1
        assert result.iloc[0]['asn'] == 67890
        assert result.iloc[0]['layer2'] == "University"

    def test_find_ases_in_category_both_layers(self, util_with_data: ASdbCategoryUtil):
        """Test finding ASNs by both layers."""
        result = util_with_data.find_ases_in_category(
            "Infrastructure",
            "Data Center"
        )

        assert len(result) == 1
        assert result.iloc[0]['asn'] == 11111

    def test_find_ases_no_match(self, util_with_data: ASdbCategoryUtil):
        """Test finding ASNs returns empty for non-existent category."""
        result = util_with_data.find_ases_in_category("Non Existent Category")

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0

    def test_lookup_caching(self, util_with_data: ASdbCategoryUtil):
        """Test that repeated lookups use cache."""
        # First lookup
        result1 = util_with_data.get_full(12345)
        # Second lookup should hit cache
        result2 = util_with_data.get_full(12345)

        assert len(result1) == len(result2)
        # Check cache info
        cache_info = util_with_data._cached_get_full.cache_info()
        assert cache_info.hits >= 1

    def test_clear_cache(self, util_with_data: ASdbCategoryUtil):
        """Test clearing the lookup cache."""
        util_with_data.get_full(12345)
        util_with_data.clear_cache()

        cache_info = util_with_data._cached_get_full.cache_info()
        assert cache_info.hits == 0
        assert cache_info.misses == 0

    def test_df_property(self, util_with_data: ASdbCategoryUtil):
        """Test backward-compatible df property."""
        df = util_with_data.df

        assert isinstance(df, pd.DataFrame)
        assert 'asn' in df.columns
        assert 'cat_idx' in df.columns
        assert 'layer1' in df.columns
        assert 'layer2' in df.columns

        # Should have all data
        assert len(df) > 0
        # Should include all ASNs from sample data
        assert 12345 in df['asn'].values
        assert 67890 in df['asn'].values
        assert 11111 in df['asn'].values

    def test_df_lazy_loading(self, util_with_data: ASdbCategoryUtil):
        """Test that df is lazily loaded."""
        assert util_with_data._df is None
        _ = util_with_data.df
        assert util_with_data._df is not None

    def test_category_dict(self, util_with_data: ASdbCategoryUtil):
        """Test that category dict is built correctly."""
        cat = util_with_data.category

        assert cat is not None
        assert "Computer and Information Technology" in cat
        assert "Internet Service Provider (ISP)" in cat["Computer and Information Technology"]

        # Check that empty rows are skipped (no None values)
        assert "Media Publishing and Broadcasting" in cat
        assert None not in cat["Media Publishing and Broadcasting"]
        assert "Online Music Streaming" in cat["Media Publishing and Broadcasting"]

    def test_null_layer2_handling(self, util_with_data: ASdbCategoryUtil):
        """Test that empty layer2 becomes null."""
        result = util_with_data.get_full(12345)

        # Both categories for AS12345 have layer2 values
        # Category 3 was empty so won't show up
        assert len(result) == 2

        # Check the Education entry (only has University, no second entry)
        edu_result = util_with_data.get_full(67890)
        assert len(edu_result) == 1
        assert edu_result.iloc[0]['layer2'] == "University"


class TestASdbCategoryResilience:
    """Tests for resilience features (corrupt file handling, force refresh)."""

    def test_force_refresh(self, tmp_path: Path, sample_csv_files: tuple[Path, Path]):
        """Test that force_refresh re-downloads and reprocesses data."""
        # First init creates parquet
        util1 = ASdbCategoryUtil(
            year=2024,
            month=1,
            working_root=tmp_path,
            setup_logging=False,
        )
        parquet_path = tmp_path / "ASdbCategoryUtil" / \
            "processed" / "2024-01_asdb.parquet"
        assert parquet_path.exists()
        original_mtime = parquet_path.stat().st_mtime

        # Recreate CSV file
        data_path, _ = sample_csv_files
        data_path.write_text(SAMPLE_ASDB_DATA)

        # Force refresh should recreate parquet
        import time
        time.sleep(0.01)  # Ensure mtime differs
        util2 = ASdbCategoryUtil(
            year=2024,
            month=1,
            working_root=tmp_path,
            force_refresh=True,
            setup_logging=False,
        )
        new_mtime = parquet_path.stat().st_mtime
        assert new_mtime > original_mtime

    def test_corrupt_parquet_triggers_reprocess(self, tmp_path: Path, sample_csv_files: tuple[Path, Path]):
        """Test that corrupt parquet file triggers reprocessing."""
        # First init creates valid parquet
        util1 = ASdbCategoryUtil(
            year=2024,
            month=1,
            working_root=tmp_path,
            setup_logging=False,
        )
        parquet_path = tmp_path / "ASdbCategoryUtil" / \
            "processed" / "2024-01_asdb.parquet"
        assert parquet_path.exists()

        # Corrupt the parquet file
        parquet_path.write_bytes(b"corrupted data")

        # Second init should detect corruption and reprocess
        util2 = ASdbCategoryUtil(
            year=2024,
            month=1,
            working_root=tmp_path,
            setup_logging=False,
        )

        # Should still work
        result = util2.get_full(12345)
        assert len(result) > 0


class TestASdbCategoryDownload:
    async def test_download_on_missing_data(self, tmp_path: Path):
        """Test that data is downloaded when raw files don't exist."""
        mock_data_response = AsyncMock()
        mock_data_response.raise_for_status = lambda: None
        mock_data_response.read = AsyncMock(
            return_value=SAMPLE_ASDB_DATA.encode())

        mock_category_response = AsyncMock()
        mock_category_response.raise_for_status = lambda: None
        mock_category_response.read = AsyncMock(
            return_value=SAMPLE_CATEGORY_DATA.encode())

        call_count = 0

        def create_mock_context():
            nonlocal call_count
            call_count += 1
            mock_context = AsyncMock()
            if call_count == 1:
                mock_context.__aenter__.return_value = mock_data_response
            else:
                mock_context.__aenter__.return_value = mock_category_response
            mock_context.__aexit__.return_value = None
            return mock_context

        with patch('tmautils.bgp.asdb.request_with_retry', side_effect=lambda *a, **k: create_mock_context()):
            with patch('tmautils.bgp.asdb.aiohttp.ClientSession') as mock_session_class:
                mock_session = AsyncMock()
                mock_session.__aenter__.return_value = mock_session
                mock_session.__aexit__.return_value = None
                mock_session_class.return_value = mock_session

                util = ASdbCategoryUtil(
                    year=2024,
                    month=1,
                    working_root=tmp_path,
                    setup_logging=False,
                )

                # Should have downloaded and created parquet
                parquet_path = tmp_path / "ASdbCategoryUtil" / \
                    "processed" / "2024-01_asdb.parquet"
                assert parquet_path.exists()

                # Lookup should work
                result = util.get_full(12345)
                assert len(result) > 0


@pytest.fixture(scope="session")
def asdb_real_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Shared temp directory for real ASdb data tests."""
    return tmp_path_factory.mktemp("asdb_real_data")


@pytest.mark.network
class TestASdbCategoryRealData:
    """Integration tests using real ASdb data. Requires network access."""

    def test_real_download_and_lookup(self, asdb_real_data_dir: Path):
        """Test downloading and querying real ASdb data."""
        year, month = 2024, 1

        util = ASdbCategoryUtil(
            year=year,
            month=month,
            working_root=asdb_real_data_dir,
            setup_logging=False,
        )

        # Verify parquet was created
        parquet_path = asdb_real_data_dir / "ASdbCategoryUtil" / \
            "processed" / f"{year}-{month:02d}_asdb.parquet"
        assert parquet_path.exists()

        # Test lookup for well-known ASNs
        # AS15169 is Google - should have categories
        result = util.get_full(15169)
        assert len(result) > 0

        # AS13335 is Cloudflare - should have categories
        result = util.get_full(13335)
        assert len(result) > 0

    def test_real_find_ases_in_category(self, asdb_real_data_dir: Path):
        """Test finding ASNs by category with real data."""
        year, month = 2024, 1

        util = ASdbCategoryUtil(
            year=year,
            month=month,
            working_root=asdb_real_data_dir,
            setup_logging=False,
        )

        # Look for ISPs - should have many results
        result = util.find_ases_in_category(
            "Computer and Information Technology")
        assert len(result) > 100  # Should have many tech companies

    def test_real_df_property(self, asdb_real_data_dir: Path):
        """Test backward-compatible df property with real data."""
        year, month = 2024, 1

        util = ASdbCategoryUtil(
            year=year,
            month=month,
            working_root=asdb_real_data_dir,
            setup_logging=False,
        )

        df = util.df
        assert len(df) > 10000  # Should have many category assignments
        assert 'asn' in df.columns
        assert 'cat_idx' in df.columns
        assert 'layer1' in df.columns
        assert 'layer2' in df.columns

    def test_real_category_dict(self, asdb_real_data_dir: Path):
        """Test category dictionary with real data."""
        year, month = 2024, 1

        util = ASdbCategoryUtil(
            year=year,
            month=month,
            working_root=asdb_real_data_dir,
            setup_logging=False,
        )

        cat = util.category
        assert cat is not None
        assert len(cat) > 5  # Should have multiple layer1 categories


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", os.path.abspath(__file__)])
