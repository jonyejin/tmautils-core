# tmautils-core
Core infrastructure for the [tmautils](https://github.com/sulyabtv/tmautils) package family.

Installation:
```bash
pip install tmautils-core
```

## `tmautils.core`

| Utility / Function | What it does |
| --- | --- |
| `IOHelper` | Directory structure and logging for utilities |
| `LogHelper` | Logging configuration |
| `AsyncRateLimiter` | Rate limiter with concurrency and throughput limits |
| `RetryConfig` | Configuration for HTTP retry behavior |
| `get_logger_from_helper()` | Get the configured or no-op logger from LogHelper |
| `run_coro_sync()` | Run async code from sync context |
| `request_with_retry()` | HTTP requests with retry and backoff |
| `get_with_retry()` | Convenience wrapper for GET with retry |
| `gzip_file()`, `gunzip_file()` | Compress/decompress a file with gzip |
| `try_convert_ip()` | Try to convert an IP address string to an IP address object |
| `is_ipv4()`, `is_ipv6()` | Check if string is valid IPv4/IPv6 address |

## `tmautils.db`

| Utility / Function | What it does |
| --- | --- |
| `BufferedWriter` | Batched writes to storage backends |
| `DuckDbStore` | DuckDB storage interface |
| `DuckDbBackend` | DuckDB backend for BufferedWriter |
| `DuckLakeStore` | DuckLake storage interface |
| `DuckLakeBackend` | DuckLake backend for BufferedWriter |
| `ParquetBackend` | Parquet backend for BufferedWriter |
| `DuckDbInetLpmIndex` | In-memory LPM index for fast IP lookups |
| `pydantic_to_arrow()` | Convert Pydantic models to Arrow tables |

## License
This project is licensed under [MPL-2.0](LICENSE) (Mozilla Public License 2.0).

What this means in practice:
- If you modify an existing file, your modifications must remain MPL-2.0.
- You can license new files however you want. (But I won't merge them into `tmautils-core` unless they are MPL-2.0.)
- You can use this code alongside code under other licenses.

## Contributing
Contributions are highly welcome! See the [tmautils README](https://github.com/sulyabtv/tmautils/blob/main/README.md) for philosophy and design choices.

AI Policy: I don't consider AI tool usage any different from IDE usage. This also means that *you* are responsible for the code you write and *you* should inspect every line of code written by an LLM.
