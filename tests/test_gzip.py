import os
import logging
from pathlib import Path

import pytest

from imresearchutils.common import gzip_file, gunzip_file


def write_sample(path: Path, content: bytes):
    path.write_bytes(content)
    return content


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def test_roundtrip_compress_decompress_keep_original(tmp_path, caplog):
    original = tmp_path / "foo.txt"
    content = b"hello world" * 100
    write_sample(original, content)

    caplog.set_level(logging.INFO)
    logger = logging.getLogger("test")

    # compress but keep original
    gzip_file(original, force=False, delete_original=False, logger=logger)
    gz = original.with_suffix(f"{original.suffix}.gz")
    assert gz.exists(), "gzipped file should have been created"
    assert original.exists(), "original should still exist because delete_original=False"

    original.unlink(missing_ok=True)  # remove original to test decompression

    # decompress but keep gz
    unzipped = gunzip_file(gz, force=False, delete_gzip=False, logger=logger)
    assert unzipped.exists(), "unzipped file should have been created"
    assert read_bytes(
        unzipped
    ) == content, "decompressed content must equal original"

    # verify logging captured relevant messages
    messages = [rec.message for rec in caplog.records]
    assert any("Compressed" in m for m in messages), "should log compression"
    assert any("Decompressed" in m for m in messages), "should log decompression"


def test_skip_if_exists_without_force(tmp_path, caplog):
    original = tmp_path / "bar.txt"
    content = b"data123"
    write_sample(original, content)
    logger = logging.getLogger("test")
    caplog.set_level(logging.INFO)

    # first create gzip
    gzip_file(original, delete_original=False, force=False, logger=logger)
    gz = original.with_suffix(f"{original.suffix}.gz")
    assert gz.exists()

    # record mtime before second call
    mt_before = gz.stat().st_mtime
    # call again without force -> should skip and not modify existing .gz
    gzip_file(original, delete_original=False, force=False, logger=logger)
    mt_after = gz.stat().st_mtime
    assert mt_before == mt_after, "without force, existing gz file must not be overwritten"

    # decompress first time
    unzipped = gunzip_file(gz, delete_gzip=False, force=False, logger=logger)
    assert unzipped.exists()

    mt_before_unzip = unzipped.stat().st_mtime
    # call again without force -> skip decompression
    gunzip_file(gz, delete_gzip=False, force=False, logger=logger)
    mt_after_unzip = unzipped.stat().st_mtime
    assert mt_before_unzip == mt_after_unzip, "without force, existing unzipped file must not be overwritten"

    # check skip messages in logs
    messages = [rec.message for rec in caplog.records]
    assert any("already exists. Skipping compression." in m for m in messages)
    assert any("already exists. Skipping decompression." in m for m in messages)


def test_force_overwrite(tmp_path):
    original = tmp_path / "baz.txt"
    first_content = b"first version"
    write_sample(original, first_content)

    # create initial gzip
    gzip_file(original, delete_original=False, force=False)
    gz = original.with_suffix(f"{original.suffix}.gz")
    assert gz.exists()

    # overwrite original with new content
    new_content = b"second version different"
    original.write_bytes(new_content)

    # gzip again with force=True -> should overwrite existing .gz
    gzip_file(original, delete_original=False, force=True)
    # decompress and verify it reflects new content
    unzipped = gunzip_file(gz, delete_gzip=True, force=True)
    assert unzipped.exists()
    assert read_bytes(unzipped) == new_content


def test_delete_original_flags(tmp_path):
    original = tmp_path / "delme.txt"
    content = b"to be deleted"
    write_sample(original, content)

    # compress with delete_original=True: original should be removed
    gzip_file(original, delete_original=True, force=False)
    gz = original.with_suffix(f"{original.suffix}.gz")
    assert gz.exists()
    assert not original.exists(), "original should be deleted when delete_original=True"

    # decompress with delete_original=True: gz should be removed
    unzipped = gunzip_file(gz, delete_gzip=True, force=False)
    assert unzipped.exists()
    assert not gz.exists(), "gz file should be deleted when delete_original=True"


def test_compress_nonexistent_raises(tmp_path):
    missing = tmp_path / "does_not_exist.txt"
    with pytest.raises(Exception):
        gzip_file(missing, force=False, delete_original=False)


def test_decompress_invalid_gz_cleans_tmp(tmp_path):
    # create a fake .gz file with invalid content
    gz = tmp_path / "broken.txt.gz"
    gz.write_text("not a real gzip")

    tmp_unzipped = gz.with_suffix("").with_name(
        f"{gz.with_suffix('').name}.tmp")
    if tmp_unzipped.exists():
        tmp_unzipped.unlink()

    with pytest.raises(Exception):
        gunzip_file(gz, force=False, delete_gzip=False)

    # temporary file should have been cleaned up on failure
    assert not tmp_unzipped.exists(), "temporary unzipped file should be removed after failure"


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", os.path.abspath(__file__)])
