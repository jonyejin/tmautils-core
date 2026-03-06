"""Tests for IOHelper improvements."""

from pathlib import Path

import pytest

from tmautils.common import (
    DirCreationMode,
    DirConfig,
    IOConfig,
    IOHelper,
    LogConfig,
    LogHelper,
)


class TestLegacyAPI:
    """Tests that legacy API works unchanged."""

    def test_legacy_api_creates_all_dirs_eagerly(self, tmp_path: Path):
        """Legacy API (no config) creates all 4 directories at init."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        # All directories should exist immediately
        assert (tmp_path / "TestUtil" / "raw").is_dir()
        assert (tmp_path / "TestUtil" / "processed").is_dir()
        assert (tmp_path / "TestUtil" / "logs").is_dir()
        assert (tmp_path / "TestUtil" / "results").is_dir()

    def test_legacy_api_dir_access(self, tmp_path: Path):
        """Legacy API directory access works via attributes."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        assert io.raw == tmp_path / "TestUtil" / "raw"
        assert io.processed == tmp_path / "TestUtil" / "processed"
        assert io.logs == tmp_path / "TestUtil" / "logs"
        assert io.results == tmp_path / "TestUtil" / "results"

    def test_legacy_api_with_instance_name(self, tmp_path: Path):
        """Legacy API with instance_name works correctly."""
        io = IOHelper(
            "TestUtil",
            instance_name="prod",
            working_root=tmp_path,
            setup_logging=False,
        )

        assert io.top_level_dir == tmp_path / "TestUtil" / "prod"
        assert io.raw == tmp_path / "TestUtil" / "prod" / "raw"

    def test_legacy_symlink_to_params(self, tmp_path: Path):
        """Legacy symlink_to parameters work correctly."""
        target = tmp_path / "external_raw"
        target.mkdir()

        io = IOHelper(
            "TestUtil",
            working_root=tmp_path,
            raw_dir_symlink_to=target,
            setup_logging=False,
        )

        raw_path = tmp_path / "TestUtil" / "raw"
        assert raw_path.is_symlink()
        assert raw_path.resolve() == target


class TestIOConfig:
    """Tests for IOConfig usage."""

    def test_config_basic(self, tmp_path: Path):
        """IOConfig with default settings works."""
        config = IOConfig()
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        # All dirs should be lazy by default in config
        assert not (tmp_path / "TestUtil" / "raw").exists()

        # Access creates
        _ = io.raw
        assert (tmp_path / "TestUtil" / "raw").is_dir()

    def test_config_disabled_dirs(self, tmp_path: Path):
        """IOConfig can disable directories."""
        config = IOConfig(
            raw=DirConfig(enabled=True),
            processed=DirConfig(enabled=False),
            logs=DirConfig(enabled=True),
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        # raw should work
        _ = io.raw
        assert (tmp_path / "TestUtil" / "raw").is_dir()

        # processed should raise
        with pytest.raises(RuntimeError):
            _ = io.processed

        # results not configured in IOConfig, so should raise AttributeError
        with pytest.raises(AttributeError):
            _ = io.results

    def test_config_eager_creation(self, tmp_path: Path):
        """IOConfig with EAGER mode creates at init."""
        config = IOConfig(
            raw=DirConfig(creation_mode=DirCreationMode.EAGER),
            processed=DirConfig(creation_mode=DirCreationMode.LAZY),
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        # raw should exist (eager)
        assert (tmp_path / "TestUtil" / "raw").is_dir()
        # processed should not (lazy)
        assert not (tmp_path / "TestUtil" / "processed").exists()

    def test_config_with_symlinks(self, tmp_path: Path):
        """IOConfig can specify symlinks."""
        target = tmp_path / "external_raw"
        target.mkdir()

        config = IOConfig(
            raw=DirConfig(symlink_to=target),
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        # Access raw to trigger creation
        _ = io.raw

        raw_path = tmp_path / "TestUtil" / "raw"
        assert raw_path.is_symlink()
        assert raw_path.resolve() == target

    def test_config_top_level_symlink(self, tmp_path: Path):
        """IOConfig top_level_symlink_to works."""
        target = tmp_path / "external_data"
        target.mkdir()

        config = IOConfig(top_level_symlink_to=target)
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        top_level = tmp_path / "TestUtil"
        assert top_level.is_symlink()
        assert top_level.resolve() == target

    def test_config_top_level_symlink_with_instance_name(self, tmp_path: Path):
        """IOConfig top_level_symlink_to works with instance_name."""
        target = tmp_path / "external_data"
        target.mkdir()

        config = IOConfig(top_level_symlink_to=target)
        io = IOHelper(
            "TestUtil",
            instance_name="prod",
            config=config,
            working_root=tmp_path,
            setup_logging=False,
        )

        top_level = tmp_path / "TestUtil" / "prod"
        assert top_level.is_symlink()
        assert top_level.resolve() == target

    def test_lazy_dirs_creates_on_access(self, tmp_path: Path):
        """Directories are created when accessed (lazy by default)."""
        config = IOConfig()
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        # raw should not exist yet
        assert not (tmp_path / "TestUtil" / "raw").exists()

        # Access raw - should create it
        _ = io.raw

        # Now it should exist
        assert (tmp_path / "TestUtil" / "raw").is_dir()

    def test_multiple_accesses_idempotent(self, tmp_path: Path):
        """Multiple accesses to same directory are idempotent."""
        config = IOConfig()
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        path1 = io.raw
        path2 = io.raw
        path3 = io.raw

        assert path1 == path2 == path3
        assert (tmp_path / "TestUtil" / "raw").is_dir()


class TestCustomDirs:
    """Tests for custom directories."""

    def test_custom_dirs_via_config(self, tmp_path: Path):
        """Custom directories can be added via IOConfig."""
        config = IOConfig(
            custom_dirs={
                "cache": DirConfig(),
                "temp": DirConfig(),
            }
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        # Custom dirs should be accessible
        cache_path = io.cache
        temp_path = io.temp

        assert cache_path == tmp_path / "TestUtil" / "cache"
        assert temp_path == tmp_path / "TestUtil" / "temp"
        assert cache_path.is_dir()
        assert temp_path.is_dir()

    def test_custom_dir_with_symlink(self, tmp_path: Path):
        """Custom directories can be symlinked."""
        target = tmp_path / "external_cache"
        target.mkdir()

        config = IOConfig(
            custom_dirs={
                "cache": DirConfig(symlink_to=target),
            }
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        _ = io.cache
        cache_path = tmp_path / "TestUtil" / "cache"
        assert cache_path.is_symlink()
        assert cache_path.resolve() == target

    def test_custom_dir_cannot_use_ioconfig_name(self, tmp_path: Path):
        """Cannot use IOConfig directory names (raw, processed, logs) in custom_dirs."""
        config = IOConfig(
            custom_dirs={"raw": DirConfig()}  # "raw" is an IOConfig member
        )
        with pytest.raises(ValueError, match="IOConfig directory name"):
            IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

    def test_results_can_be_custom_dir(self, tmp_path: Path):
        """results/ can be added via custom_dirs since it's not in IOConfig."""
        config = IOConfig(
            custom_dirs={"results": DirConfig()}
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        # results should be accessible
        _ = io.results
        assert (tmp_path / "TestUtil" / "results").is_dir()


class TestDirAccess:
    """Tests for directory access via __getattr__."""

    def test_standard_dirs_via_getattr(self, tmp_path: Path):
        """Standard directories accessed via __getattr__."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        # All should work (legacy API)
        assert io.raw.name == "raw"
        assert io.processed.name == "processed"
        assert io.logs.name == "logs"
        assert io.results.name == "results"

    def test_ioconfig_dirs_via_getattr(self, tmp_path: Path):
        """IOConfig directories accessed via __getattr__."""
        config = IOConfig()
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        # Only 3 core dirs (no results)
        assert io.raw.name == "raw"
        assert io.processed.name == "processed"
        assert io.logs.name == "logs"

        # results should raise (not in IOConfig)
        with pytest.raises(AttributeError):
            _ = io.results

    def test_unknown_attr_raises(self, tmp_path: Path):
        """Unknown attributes raise AttributeError."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        with pytest.raises(AttributeError):
            _ = io.unknown_dir

    def test_private_attrs_raise(self, tmp_path: Path):
        """Private attributes (starting with _) raise AttributeError properly."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        with pytest.raises(AttributeError):
            _ = io._nonexistent


class TestLogging:
    """Tests for logging functionality."""

    def test_logging_enabled_by_default(self, tmp_path: Path):
        """Logging is enabled by default."""
        io = IOHelper("TestUtil", working_root=tmp_path)

        assert io.has_logger
        assert io.logger is not None

    def test_logging_disabled(self, tmp_path: Path):
        """setup_logging=False disables logging."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        assert not io.has_logger

    def test_logging_creates_logs_dir_with_ioconfig(self, tmp_path: Path):
        """Logging setup creates logs directory even with IOConfig."""
        config = IOConfig(
            raw=DirConfig(enabled=False),
            processed=DirConfig(enabled=False),
            logs=DirConfig(enabled=True),
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path)

        # logs dir should exist for the log file
        assert (tmp_path / "TestUtil" / "logs").is_dir()

    def test_logging_via_config(self, tmp_path: Path):
        """setup_logging can be controlled via IOConfig."""
        config = IOConfig(setup_logging=False)
        io = IOHelper("TestUtil", config=config, working_root=tmp_path)

        assert not io.has_logger

    def test_logs_disabled_with_file_logging_raises(self, tmp_path: Path):
        """Disabling logs dir while file logging is enabled raises error."""
        config = IOConfig(
            logs=DirConfig(enabled=False),
            # setup_logging=True by default, file_level=INFO by default
        )
        with pytest.raises(ValueError, match="Cannot enable file logging"):
            IOHelper("TestUtil", config=config, working_root=tmp_path)

    def test_logs_disabled_with_console_only_logging_ok(self, tmp_path: Path):
        """Disabling logs dir is OK if file logging is also disabled."""
        config = IOConfig(
            logs=DirConfig(enabled=False),
            logging_kwargs={"file_level": None},  # Disable file logging
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path)

        assert io.has_logger
        # logs dir should not exist
        assert not (tmp_path / "TestUtil" / "logs").exists()

    def test_log_helper_returns_none_when_no_logger(self, tmp_path: Path):
        """log_helper property returns None when logging is disabled."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        assert io.log_helper is None

    def test_get_worker_logging_config(self, tmp_path: Path):
        """get_worker_logging_config returns a worker config."""
        io = IOHelper("TestUtil", working_root=tmp_path)

        worker_config = io.get_worker_logging_config()
        assert worker_config.is_worker is True
        assert worker_config.log_queue is not None

    def test_get_worker_logging_config_raises_when_no_logger(self, tmp_path: Path):
        """get_worker_logging_config raises when logging is disabled."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        with pytest.raises(RuntimeError, match="no logger is set up"):
            io.get_worker_logging_config()


class TestBringYourOwnLogHelper:
    """Tests for injecting an external LogHelper."""

    def test_ioconfig_with_log_helper(self, tmp_path: Path):
        """IOHelper uses the provided LogHelper from IOConfig."""
        lh = LogHelper(LogConfig(name="External", file_level=None))
        config = IOConfig(log_helper=lh, logs=DirConfig(enabled=False))
        io = IOHelper("TestUtil", config=config, working_root=tmp_path)

        assert io.log_helper is lh
        assert io.has_logger is True
        # Logger should come from the injected LogHelper
        assert io.logger is lh.logger

    def test_init_with_dirs_log_helper(self, tmp_path: Path):
        """init_with_dirs passes through log_helper kwarg."""
        lh = LogHelper(LogConfig(name="Shared", file_level=None))
        io = IOHelper.init_with_dirs(
            "TestUtil",
            dirs={"cache"},
            working_root=tmp_path,
            log_helper=lh,
        )

        assert io.log_helper is lh
        assert io.logger is lh.logger

    def test_log_helper_skips_internal_setup(self, tmp_path: Path):
        """When log_helper is provided, setup_logging/logging_config are ignored."""
        lh = LogHelper(LogConfig(name="External", file_level=None))
        config = IOConfig(
            log_helper=lh,
            setup_logging=True,  # should be ignored
            logging_config=LogConfig(name="ShouldNotBeUsed", file_level=None),
            logs=DirConfig(enabled=False),
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path)

        # Should use the injected one, not create a new one from logging_config
        assert io.log_helper is lh


class TestCreateSymlink:
    """Tests for create_symlink method."""

    def test_create_symlink_to_file(self, tmp_path: Path):
        """create_symlink works with file targets."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        # Create a target file
        target_file = tmp_path / "target.txt"
        target_file.write_text("hello")

        link_path = io.create_symlink(io.raw, target_file)

        assert link_path == io.raw / "target.txt"
        assert link_path.is_symlink()
        assert link_path.read_text() == "hello"

    def test_create_symlink_to_directory(self, tmp_path: Path):
        """create_symlink works with directory targets."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        # Create a target directory
        target_dir = tmp_path / "target_dir"
        target_dir.mkdir()
        (target_dir / "file.txt").write_text("content")

        link_path = io.create_symlink(io.raw, target_dir)

        assert link_path == io.raw / "target_dir"
        assert link_path.is_symlink()
        assert (link_path / "file.txt").read_text() == "content"

    def test_create_symlink_custom_name(self, tmp_path: Path):
        """create_symlink with custom link_name."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        target_file = tmp_path / "target.txt"
        target_file.write_text("hello")

        link_path = io.create_symlink(io.raw, target_file, link_name="custom.txt")

        assert link_path == io.raw / "custom.txt"
        assert link_path.is_symlink()

    def test_create_symlink_replaces_existing_symlink(self, tmp_path: Path):
        """create_symlink replaces existing symlink."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        # Create two target files
        target1 = tmp_path / "target1.txt"
        target1.write_text("first")
        target2 = tmp_path / "target2.txt"
        target2.write_text("second")

        # Create symlink to first target
        io.create_symlink(io.raw, target1, link_name="link.txt")
        assert (io.raw / "link.txt").read_text() == "first"

        # Replace with symlink to second target
        io.create_symlink(io.raw, target2, link_name="link.txt")
        assert (io.raw / "link.txt").read_text() == "second"

    def test_create_symlink_fails_on_existing_file(self, tmp_path: Path):
        """create_symlink raises if target path is a regular file."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        # Create a regular file where the symlink would go
        existing_file = io.raw / "existing.txt"
        existing_file.write_text("existing")

        target = tmp_path / "target.txt"
        target.write_text("target")

        with pytest.raises(FileExistsError, match="not a symlink"):
            io.create_symlink(io.raw, target, link_name="existing.txt")

    def test_create_symlink_target_not_found(self, tmp_path: Path):
        """create_symlink raises if target doesn't exist."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        nonexistent = tmp_path / "nonexistent.txt"

        with pytest.raises(FileNotFoundError):
            io.create_symlink(io.raw, nonexistent)

    def test_create_symlink_with_custom_dir(self, tmp_path: Path):
        """create_symlink works with custom directories."""
        config = IOConfig(
            custom_dirs={"cache": DirConfig()}
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        target = tmp_path / "data.txt"
        target.write_text("cached")

        link_path = io.create_symlink(io.cache, target)

        assert link_path == io.cache / "data.txt"
        assert link_path.is_symlink()


class TestSymlinkToSymlink:
    """Tests for symlink-to-symlink behavior (preserving intermediate symlinks)."""

    def test_dir_symlink_to_symlink_preserves_intermediate(self, tmp_path: Path):
        """DirConfig symlink_to a symlink preserves the intermediate symlink."""
        # Create chain: A -> B -> real_dir
        real_dir = tmp_path / "real_dir"
        real_dir.mkdir()
        (real_dir / "test.txt").write_text("content")

        symlink_b = tmp_path / "symlink_b"
        symlink_b.symlink_to(real_dir, target_is_directory=True)

        symlink_a = tmp_path / "symlink_a"
        symlink_a.symlink_to(symlink_b, target_is_directory=True)

        # Create IOHelper with raw pointing to symlink_a
        config = IOConfig(
            raw=DirConfig(symlink_to=symlink_a),
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        # Access raw to trigger creation
        raw_path = io.raw

        # Verify: raw is a symlink pointing to symlink_a (not resolved to real_dir)
        assert raw_path.is_symlink()
        assert raw_path.readlink() == symlink_a  # Points to symlink_a, not real_dir

        # But the content should still be accessible
        assert (raw_path / "test.txt").read_text() == "content"

    def test_top_level_symlink_to_symlink_preserves_intermediate(self, tmp_path: Path):
        """top_level_symlink_to a symlink preserves the intermediate symlink."""
        # Create chain: A -> B -> real_dir
        real_dir = tmp_path / "real_dir"
        real_dir.mkdir()

        symlink_b = tmp_path / "symlink_b"
        symlink_b.symlink_to(real_dir, target_is_directory=True)

        symlink_a = tmp_path / "symlink_a"
        symlink_a.symlink_to(symlink_b, target_is_directory=True)

        # Create IOHelper with top_level pointing to symlink_a
        config = IOConfig(top_level_symlink_to=symlink_a)
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        top_level = tmp_path / "TestUtil"
        assert top_level.is_symlink()
        assert top_level.readlink() == symlink_a  # Points to symlink_a, not real_dir

    def test_create_symlink_to_symlink_preserves_intermediate(self, tmp_path: Path):
        """create_symlink to a symlink preserves the intermediate symlink."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        # Create chain: A -> B -> real_file
        real_file = tmp_path / "real_file.txt"
        real_file.write_text("content")

        symlink_b = tmp_path / "symlink_b.txt"
        symlink_b.symlink_to(real_file)

        symlink_a = tmp_path / "symlink_a.txt"
        symlink_a.symlink_to(symlink_b)

        # Create symlink to symlink_a
        link_path = io.create_symlink(io.raw, symlink_a)

        # Verify: link points to symlink_a (not resolved to real_file)
        assert link_path.is_symlink()
        assert link_path.readlink() == symlink_a

        # Content should still be accessible
        assert link_path.read_text() == "content"

    def test_create_symlink_to_dir_symlink_preserves_intermediate(self, tmp_path: Path):
        """create_symlink to a directory symlink preserves the intermediate symlink."""
        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        # Create chain: A -> B -> real_dir
        real_dir = tmp_path / "real_dir"
        real_dir.mkdir()
        (real_dir / "file.txt").write_text("content")

        symlink_b = tmp_path / "symlink_b"
        symlink_b.symlink_to(real_dir, target_is_directory=True)

        symlink_a = tmp_path / "symlink_a"
        symlink_a.symlink_to(symlink_b, target_is_directory=True)

        # Create symlink to symlink_a
        link_path = io.create_symlink(io.raw, symlink_a)

        # Verify: link points to symlink_a (not resolved to real_dir)
        assert link_path.is_symlink()
        assert link_path.readlink() == symlink_a

        # Content should still be accessible
        assert (link_path / "file.txt").read_text() == "content"

    def test_dir_symlink_relative_path_becomes_absolute(self, tmp_path: Path):
        """Relative paths are converted to absolute paths."""
        import os

        # Create target directory
        target_dir = tmp_path / "external" / "data"
        target_dir.mkdir(parents=True)
        (target_dir / "test.txt").write_text("content")

        # Change to tmp_path so relative path works
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)

            # Use relative path
            config = IOConfig(
                raw=DirConfig(symlink_to=Path("external/data")),
            )
            io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

            raw_path = io.raw

            # Symlink should point to absolute path (not relative)
            assert raw_path.is_symlink()
            link_target = raw_path.readlink()
            assert link_target.is_absolute()
            assert link_target == target_dir

            # Content should be accessible
            assert (raw_path / "test.txt").read_text() == "content"
        finally:
            os.chdir(old_cwd)

    def test_create_symlink_relative_path_becomes_absolute(self, tmp_path: Path):
        """create_symlink converts relative paths to absolute."""
        import os

        io = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)

        # Create target file
        target_file = tmp_path / "external" / "data.txt"
        target_file.parent.mkdir(parents=True)
        target_file.write_text("content")

        # Change to tmp_path so relative path works
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)

            # Use relative path
            link_path = io.create_symlink(io.raw, Path("external/data.txt"))

            # Symlink should point to absolute path
            assert link_path.is_symlink()
            link_target = link_path.readlink()
            assert link_target.is_absolute()
            assert link_target == target_file

            # Content should be accessible
            assert link_path.read_text() == "content"
        finally:
            os.chdir(old_cwd)


class TestSymlinkErrors:
    """Tests for symlink error handling."""

    def test_dir_symlink_target_not_found(self, tmp_path: Path):
        """DirConfig symlink_to raises if target doesn't exist."""
        nonexistent = tmp_path / "nonexistent_dir"

        config = IOConfig(
            raw=DirConfig(symlink_to=nonexistent),
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        with pytest.raises(FileNotFoundError):
            _ = io.raw

    def test_dir_symlink_target_is_file(self, tmp_path: Path):
        """DirConfig symlink_to raises if target is a file, not directory."""
        target_file = tmp_path / "file.txt"
        target_file.write_text("not a directory")

        config = IOConfig(
            raw=DirConfig(symlink_to=target_file),
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        with pytest.raises(FileNotFoundError, match="does not resolve to a directory"):
            _ = io.raw

    def test_top_level_symlink_target_not_found(self, tmp_path: Path):
        """top_level_symlink_to raises if target doesn't exist."""
        nonexistent = tmp_path / "nonexistent_dir"

        config = IOConfig(top_level_symlink_to=nonexistent)

        with pytest.raises(FileNotFoundError):
            IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

    def test_top_level_symlink_target_is_file(self, tmp_path: Path):
        """top_level_symlink_to raises if target is a file."""
        target_file = tmp_path / "file.txt"
        target_file.write_text("not a directory")

        config = IOConfig(top_level_symlink_to=target_file)

        with pytest.raises(FileNotFoundError, match="does not resolve to a directory"):
            IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

    def test_top_level_symlink_existing_dir_raises(self, tmp_path: Path):
        """top_level_symlink_to raises if top_level_dir already exists as real dir."""
        # Create the namespace dir first
        existing_dir = tmp_path / "TestUtil"
        existing_dir.mkdir()

        target = tmp_path / "external"
        target.mkdir()

        config = IOConfig(top_level_symlink_to=target)

        with pytest.raises(FileExistsError, match="not a symlink"):
            IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

    def test_dir_symlink_existing_dir_raises(self, tmp_path: Path):
        """DirConfig symlink_to raises if dir already exists as real dir."""
        # First create IOHelper without symlink to create the raw dir
        io1 = IOHelper("TestUtil", working_root=tmp_path, setup_logging=False)
        assert io1.raw.is_dir()
        assert not io1.raw.is_symlink()

        # Now try to create another IOHelper with symlink - should fail
        target = tmp_path / "external_raw"
        target.mkdir()

        config = IOConfig(
            raw=DirConfig(symlink_to=target, creation_mode=DirCreationMode.EAGER),
        )

        with pytest.raises(FileExistsError, match="not a symlink"):
            IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)


class TestCustomDirsAdvanced:
    """Advanced tests for custom directories."""

    def test_custom_dir_eager_creation(self, tmp_path: Path):
        """Custom directories can use EAGER creation mode."""
        config = IOConfig(
            custom_dirs={
                "cache": DirConfig(creation_mode=DirCreationMode.EAGER),
                "temp": DirConfig(creation_mode=DirCreationMode.LAZY),
            }
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        # cache should exist (eager)
        assert (tmp_path / "TestUtil" / "cache").is_dir()
        # temp should not (lazy)
        assert not (tmp_path / "TestUtil" / "temp").exists()

    def test_custom_dir_disabled(self, tmp_path: Path):
        """Disabled custom directories raise RuntimeError."""
        config = IOConfig(
            custom_dirs={
                "cache": DirConfig(enabled=False),
            }
        )
        io = IOHelper("TestUtil", config=config, working_root=tmp_path, setup_logging=False)

        with pytest.raises(RuntimeError, match="disabled"):
            _ = io.cache


class TestDeprecatedDataDir:
    """Tests for deprecated data_dir parameter."""

    def test_data_dir_works_with_warning(self, tmp_path: Path):
        """data_dir parameter works but emits deprecation warning."""
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            io = IOHelper("TestUtil", data_dir=tmp_path, setup_logging=False)

            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "data_dir" in str(w[0].message)

        assert io.working_root == tmp_path

    def test_data_dir_and_working_root_same_value_ok(self, tmp_path: Path):
        """data_dir and working_root with same value is allowed."""
        import warnings

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            io = IOHelper("TestUtil", data_dir=tmp_path, working_root=tmp_path, setup_logging=False)

        assert io.working_root == tmp_path

    def test_data_dir_and_working_root_different_raises(self, tmp_path: Path):
        """data_dir and working_root with different values raises."""
        other_path = tmp_path / "other"
        other_path.mkdir()

        with pytest.raises(ValueError, match="different values"):
            IOHelper("TestUtil", data_dir=tmp_path, working_root=other_path, setup_logging=False)


class TestInitWithDirs:
    """Tests for IOHelper.init_with_dirs classmethod."""

    def test_basic_structure(self, tmp_path: Path):
        """Basic init_with_dirs creates only specified dirs."""
        io = IOHelper.init_with_dirs(
            "TestUtil",
            dirs={"raw", "logs"},
            working_root=tmp_path,
            setup_logging=False,
        )

        # raw and logs should exist (lazy on access)
        assert io.raw.is_dir()
        assert io.logs.is_dir()

        # processed should not exist and accessing it should raise
        assert not (tmp_path / "TestUtil" / "processed").exists()
        with pytest.raises(RuntimeError, match="disabled"):
            _ = io.processed

    def test_custom_dirs_in_structure(self, tmp_path: Path):
        """init_with_dirs handles custom dirs correctly."""
        io = IOHelper.init_with_dirs(
            "TestUtil",
            dirs={"cache", "logs"},  # cache is not a standard dir
            working_root=tmp_path,
            setup_logging=False,
        )

        # cache should be accessible
        assert io.cache.is_dir()
        assert io.logs.is_dir()

        # Standard dirs not in the set should be disabled
        with pytest.raises(RuntimeError, match="disabled"):
            _ = io.raw

    def test_symlink_for_dirs_in_structure(self, tmp_path: Path):
        """init_with_dirs allows symlinks for dirs in structure."""
        target = tmp_path / "external_raw"
        target.mkdir()

        io = IOHelper.init_with_dirs(
            "TestUtil",
            dirs={"raw", "logs"},
            working_root=tmp_path,
            raw_dir_symlink_to=target,
            setup_logging=False,
        )

        assert io.raw.is_symlink()
        assert io.raw.resolve() == target

    def test_symlink_for_custom_dir_in_structure(self, tmp_path: Path):
        """init_with_dirs allows symlinks for custom dirs."""
        target = tmp_path / "external_cache"
        target.mkdir()

        io = IOHelper.init_with_dirs(
            "TestUtil",
            dirs={"cache", "logs"},
            working_root=tmp_path,
            cache_dir_symlink_to=target,
            setup_logging=False,
        )

        assert io.cache.is_symlink()
        assert io.cache.resolve() == target

    def test_symlink_for_dir_not_in_structure_raises(self, tmp_path: Path):
        """init_with_dirs rejects symlinks for dirs not in structure."""
        target = tmp_path / "external_processed"
        target.mkdir()

        with pytest.raises(ValueError, match="Cannot symlink 'processed'"):
            IOHelper.init_with_dirs(
                "TestUtil",
                dirs={"raw", "logs"},  # processed not in structure
                working_root=tmp_path,
                processed_dir_symlink_to=target,
                setup_logging=False,
            )

    def test_top_level_symlink(self, tmp_path: Path):
        """init_with_dirs supports top_level_symlink_to."""
        target = tmp_path / "external"
        target.mkdir()

        io = IOHelper.init_with_dirs(
            "TestUtil",
            dirs={"raw", "logs"},
            working_root=tmp_path,
            top_level_symlink_to=target,
            setup_logging=False,
        )

        assert io.top_level_dir.is_symlink()
        assert io.top_level_dir.resolve() == target

    def test_instance_name(self, tmp_path: Path):
        """init_with_dirs supports instance_name."""
        io = IOHelper.init_with_dirs(
            "TestUtil",
            dirs={"raw", "logs"},
            working_root=tmp_path,
            instance_name="prod",
            setup_logging=False,
        )

        assert io.top_level_dir == tmp_path / "TestUtil" / "prod"
        assert io.raw == tmp_path / "TestUtil" / "prod" / "raw"

    def test_logging_config(self, tmp_path: Path):
        """init_with_dirs passes through logging config."""
        io = IOHelper.init_with_dirs(
            "TestUtil",
            dirs={"logs"},  # Only logs
            working_root=tmp_path,
            setup_logging=True,
            logging_kwargs={"file_level": None},  # Disable file logging
        )

        assert io.has_logger
        assert io.logger is not None

    def test_unknown_kwargs_warns(self, tmp_path: Path):
        """init_with_dirs warns about unknown kwargs."""
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            IOHelper.init_with_dirs(
                "TestUtil",
                dirs={"raw", "logs"},
                working_root=tmp_path,
                setup_logging=False,
                unknown_param="value",
            )

            assert len(w) == 1
            assert "Unknown kwargs" in str(w[0].message)
            assert "unknown_param" in str(w[0].message)
