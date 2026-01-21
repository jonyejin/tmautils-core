- [tmautils](#tmautils)
    - [Installation](#installation)
    - [Quick Start](#quick-start)
        - [What's Included](#whats-included)
            - [`tmautils.bgp`](#tmautilsbgp)
            - [`tmautils.dns`](#tmautilsdns)
            - [`tmautils.enrich_ip`](#tmautilsenrich_ip)
            - [`tmautils.pki`](#tmautilspki)
            - [`tmautils.web`](#tmautilsweb)
            - [`tmautils.db`](#tmautilsdb)
            - [`tmautils.common`](#tmautilscommon)
        - [Writing Your First Program](#writing-your-first-program)
    - [The boring stuff](#the-boring-stuff)
        - [Why does this library exist?](#why-does-this-library-exist)
        - [Philosophy](#philosophy)
        - [Design Choices](#design-choices)
            - [Self-Contained Utilities](#self-contained-utilities)
            - [Async-First Approach](#async-first-approach)
            - [DuckDB as the Middle Layer for Database Stuff](#duckdb-as-the-middle-layer-for-database-stuff)
    - [License](#license)
    - [Contributing](#contributing)

# tmautils
A collection of Python utilities for Internet measurement research.

`tma` could stand for *Traffic Measurement and Analysis*, like the [academic conference](https://tma.ifip.org/), or *Too Much Analysis*, depending on how frustrated you are with your research. (You get to choose.)

## Installation

```bash
pip install path/to/tmautils
```

## Quick Start
`tmautils` provides two types of APIs:
- Python classes (*Utilities*) that use their own storage and logging, and
- Python functions that do specific things, without touching storage.

As a basic example, you can use `get_cert()` (a function) to download the TLS certificate presented by a server, and `RevocationChecker` (a utility class) to check whether said certificate has been revoked:

```python
from tmautils.pki import get_cert, RevocationChecker
from pathlib import Path

cert = await get_cert("www.example.com") # using get_cert function

checker = RevocationChecker( # instantiating RevocationChecker
    working_root=Path("/tmp"),
)
result = await checker.check_cert_chain(cert) # using RevocationChecker's API
```

### What's Included
`tmautils` is divided into submodules roughly based on the functionality provided by the utilities / functions they contain. Submodules and their user-facing utilities and functions are listed below.

#### `tmautils.bgp`
| Utility / Function                                         | What it does                                    |
| ---------------------------------------------------------- | ----------------------------------------------- |
| [`PyasnUtil`](./tmautils/bgp/pyasn.py#L17)                 | IP to ASN lookups (wraps `pyasn`)               |
| [`ASdbCategoryUtil`](./tmautils/bgp/asdb.py#L13)           | AS categorization using Stanford ASdb           |
| [`CaidaAsOrgInfoUtil`](./tmautils/bgp/caida_as_org.py#L12) | AS to Organization mapping using CAIDA's AS2Org |

#### `tmautils.dns`
| Utility / Function                                           | What it does                             |
| ------------------------------------------------------------ | ---------------------------------------- |
| [`AsyncDnsPythonUtil`](./tmautils/dns/dnspython.py#L23)      | Async DNS resolution (wraps `dnspython`) |
| [`CzdsDownloadUtil`](./tmautils/dns/czds.py#L19)             | Download ICANN CZDS zone files           |
| [`OpenIntelZoneStreamUtil`](./tmautils/dns/openintel.py#L99) | Subscribe to OpenINTEL ZoneStream        |
| [`dns_msg_semantic_hash()`](./tmautils/dns/utils.py#L11)     | Compute semantic hash of DNS messages    |

#### `tmautils.enrich_ip`
| Utility / Function                                                 | What it does                                            |
| ------------------------------------------------------------------ | ------------------------------------------------------- |
| [`IPApiBatchUtil`](./tmautils/enrich_ip/ipapi.py#L49)              | Interact with `ip-api.com`'s batch API.                 |
| [`IPInfoLiteUtil`](./tmautils/enrich_ip/ipinfo.py#L12)             | Interact with the IPinfo's Lite dataset.                |
| [`IpInfoPrivacyUtil`](./tmautils/enrich_ip/vpn.py#L303)            | Privacy detection using IPinfo's database               |
| [`IpInfoCarrierUtil`](./tmautils/enrich_ip/carrier.py#L13)         | Mobile carrier lookup using IPinfo's database           |
| [`ChromePrefetchUtil`](./tmautils/enrich_ip/chromeprefetch.py#L12) | Check if an IP address belongs to Chrome Prefetch Proxy |

#### `tmautils.pki`
| Utility / Function                                      | What it does                               |
| ------------------------------------------------------- | ------------------------------------------ |
| [`RevocationChecker`](./tmautils/pki/revocation.py#L36) | Check certificate revocation (OCSP/CRL)    |
| [`get_cert()`](./tmautils/pki/cert.py#L211)             | Fetch TLS certificate from a server        |
| [`get_cert_chain()`](./tmautils/pki/cert.py#L265)       | Fetch full certificate chain from a server |
| [`fetch_issuer_cert()`](./tmautils/pki/cert.py#L372)    | Fetch issuer certificate via AIA extension |
| [`fetch_issuer_chain()`](./tmautils/pki/cert.py#L508)   | Build certificate chain from leaf to root  |

#### `tmautils.web`
| Utility / Function                                        | What it does                             |
| --------------------------------------------------------- | ---------------------------------------- |
| [`TrancoTopListUtil`](./tmautils/web/tranco.py#L17)       | Download and query Tranco top sites list |
| [`PeriodicTrancoCrawlUtil`](./tmautils/web/tranco.py#L63) | Periodic crawling of Tranco-listed sites |
| [`OpenWpmCrawlUtil`](./tmautils/web/openwpm.py#L20)       | Web crawling using OpenWPM               |
| [`request_with_retry()`](./tmautils/web/http.py#L66)      | HTTP requests with retry and backoff     |

#### `tmautils.db`
`tmautils.db` primarily contains database-interfacing used by other submodules, but some exports may be useful for writing custom user code.

| Utility / Function                                          | What it does                            |
| ----------------------------------------------------------- | --------------------------------------- |
| [`BufferedWriter`](./tmautils/db/bufferedwriter.py#L25)     | Batched writes to storage backends      |
| [`DuckDbBackend`](./tmautils/db/duckdb.py#L311)             | DuckDB backend for BufferedWriter       |
| [`DuckLakeBackend`](./tmautils/db/ducklake.py#L728)         | DuckLake backend for BufferedWriter     |
| [`ParquetBackend`](./tmautils/db/parquet.py#L17)            | Parquet backend for BufferedWriter      |
| [`DuckDbStore`](./tmautils/db/duckdb.py#L20)                | DuckDB storage interface                |
| [`DuckLakeStore`](./tmautils/db/ducklake.py#L26)            | DuckLake storage interface              |
| [`DuckDbInetLpmIndex`](./tmautils/db/duckdb_helpers.py#L13) | In-memory LPM index for fast IP lookups |
| [`pydantic_to_arrow()`](./tmautils/db/base.py#L69)          | Convert Pydantic models to Arrow tables |

#### `tmautils.common`
`tmautils.common` primarily contains core code used by other submodules, but some exports may be useful for writing custom user code.

| Utility / Function                                                                                             | What it does                                                |
| -------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| [`IOHelper`](./tmautils/common/io.py#L258)                                                                     | Directory structure and logging for utilities               |
| [`LogHelper`](./tmautils/common/log.py#L174)                                                                   | Logging configuration                                       |
| [`get_logger_from_helper()`](./tmautils/common/log.py#L405)                                                    | Get the configured or no-op logger from LogHelper           |
| [`run_coro_sync()`](./tmautils/common/asyncutils.py#L14)                                                       | Run async code from sync context                            |
| [`import_module_attr()`](./tmautils/common/io.py#L74)                                                          | Dynamically import an attribute from a module               |
| [`gzip_file()`](./tmautils/common/io.py#L109), [`gunzip_file()`](./tmautils/common/io.py#L186)                 | Compress/decompress a file with gzip                        |
| [`maybe_apply()`](./tmautils/common/utils.py#L11)                                                              | Decorator to apply a function on an input without failing   |
| [`try_convert_ip()`](./tmautils/common/utils.py#L26)                                                           | Try to convert an IP address string to an IP address object |
| [`is_ipv4()`](./tmautils/common/utils.py#L55), [`is_ipv6()`](./tmautils/common/utils.py#L63)                   | Check if string is valid IPv4/IPv6 address                  |
| [`is_internal_flow()`](./tmautils/common/utils.py#L71), [`is_external_flow()`](./tmautils/common/utils.py#L75) | Check if IP flow is internal/external                       |
| [`is_internal_flow_or_same_v6_upper_64()`](./tmautils/common/utils.py#L79)                                     | Check if flow is internal or same /64 prefix                |

### Writing Your First Program
As you've probably noticed by now, there is no "one way" to use `tmautils`: what utility/function you use will be driven by your use case, and the options supported vary by individual utilities. However, I have tried to include useful documentation with each utility/function.

That said, there are two "patterns" you will see across the API:
- All utilities support modification of storage/logging behavior through constructor kwargs. See [here](#self-contained-utilities) for details.
- I/O-heavy APIs are written with an `async`-first approach; but in most cases a sync wrapper is provided. See [here](#async-first-approach) for details.

## The boring stuff
### Why does this library exist?
This library grew out of code written by me (Sulyab) for my PhD research. At some point, it made sense to pull out the reusable bits into a common place, and establish some directory structure for data to keep track of what goes where. Days of debugging led to addition of logging features, days of fighting with the GIL led to the addition of multiprocessing helpers, and so on.

### Philosophy
There are three main tenets that shaped the design choices of this library:

1. *Do NOT reinvent the wheel (when sufficiently good wheels exist.)* There are several great Python packages out there that can help with specific Internet measurement analysis tasks, like [pyasn](https://github.com/hadiasghari/pyasn) for IP to ASN lookups. In such cases, it is preferable to write thin wrappers around such packages (such as `PyasnUtil` for `pyasn`.)

2. *However, sometimes reinventing the wheel makes more sense.* Usually this happens when the "best" Python package available for a use case is not *sufficiently* good according to certain criteria. For example, it may lack an `async` API or aggressive caching, two reasons why `RevocationChecker` exists instead of relying on [pki-tools](https://github.com/fulder/pki-tools) for certificate revocation lookups.

3. *Solve the problem at hand first, the "perfect" solution can come later.* This is an important one, and the one that may affect you, the user, the most. Like [mentioned](#why-does-this-library-exist), each utility in this library was written to address specific needs at the time. As such, some utilities may support feature X but not feature Y, and some "pieces" may be more or less polished than others. **However, I am always looking to improve the code and add more features, so please consider contributing!**

### Design Choices
While this library is a grab bag of utilities, I have tried to keep some design choices consistent throughout:

#### Self-Contained Utilities
Each utility in this library is designed to be "self-contained". Typically, you pass a `working_root` parameter when you instantiate a utility, say `AbcUtil`. This action will create a directory named `AbcUtil` in `working_root`, with subdirectories such as `logs`, `raw` and `cache`. (There will be another level of subdirectories if there are multiple instances of the same utility.) The following is an example:

```python
from tmautils.dns import CzdsDownloadUtil
from pathlib import Path
czds = CzdsDownloadUtil(working_root=Path("/data"))
# Creates: /data/CzdsDownloadUtil/raw/, /data/CzdsDownloadUtil/logs/, etc.
```

Under the hood, most of this work is done by `IOHelper`, which takes care of the directory structure and instantiates `LogHelper` to take care of logging.

You can pass additional arguments to `IOHelper` to modify some default behavior, such as making subdirectories symlinks, and turning off file logging. Example:

```python
czds = CzdsDownloadUtil(
    working_root=Path("/data"),
    # The following arguments are passed to IOHelper
    # make /data/CzdsDownloadUtil/raw a symlink to ~/downloads/czds
    raw_dir_symlink_to=Path("~/downloads/czds/"),
    # IOHelper in turn passes the following argument to LogHelper
    logging_kwargs={
        "file_level": None, # No file logging
    }
)
```

If you wish, you can delegate the storage/logging management of your custom program by instantiating IOHelper:

```python
from tmautils.common import IOHelper
io = IOHelper(
    "MyProgram",
    working_root=Path("/data"),
    # by default, IOHelper will configure the subdirectories:
    # raw/, logs/, processed/, results/
)
# Access storage
out_path = io.raw / "hello.txt" # Returns a pathlib.Path object
out_path.write_text("Hello, World!")
# Use logger
io.logger.warning("I have no clue what I am doing.")
```

#### Async-First Approach
Many utilities in this library do I/O-heavy work (e.g., downloading zone files, making HTTP requests, checking certificate revocation status). For better concurrency, I/O-heavy utilities are written with an `async`-first approach. In such cases, the API also provides a sync version in case you do not want to bother with `asyncio`:

```python
result = await util.fetch_data("param")    # Async
result = util.fetch_data_sync("param")     # Sync wrapper
```

`async` methods use the base name (`fetch_data()`), and sync wrappers append `_sync` (`fetch_data_sync()`). The sync wrappers simply call `run_coro_sync` on the `async` method.

#### DuckDB as the Middle Layer for Database Stuff
After experimenting with different database backends, the current direction is to use [DuckDB](https://duckdb.org/) as a unified intermediate layer. DuckDB supports several popular backends (CSV, Parquet, SQLite, even query remote sources) and allows executing SQL queries on those backends. This allows us to write code at the level of DuckDB connections rather than add support for different backends.

New code uses the following flow: Pydantic models -> Arrow table -> DuckDB insert. There is a fair amount of instrumentation around this which you can find in `tmautils.db`. For a good example of how to write a utility using this pattern, see `OpenIntelZoneStreamUtil`.

Some useful tools:
- Writing rows one-by-one to a database is slow. `BufferedWriter` accumulates rows in memory and flushes them in batches, working with multiple backends (thanks to DuckDB).
- For Longest Prefix Matching (LPM) lookups, we use `DuckDbInetLpmIndex` which builds an in-memory LPM index to speed up lookups. Once built, the index can be used in Python code or DuckDB SQL (via [UDFs](https://duckdb.org/docs/stable/clients/python/function).)

In ye olde times, SQLite was used for database stuff. There is an ongoing effort to port utilities to DuckDB instead.

## License
This project is licensed under [MPL-2.0](LICENSE) (Mozilla Public License 2.0).

What this means in practice:
- If you modify an existing file, your modifications must remain MPL-2.0.
- You can license new files however you want. (But I won't merge them into `tmautils` unless they are MPL-2.0.)
- You can use this code alongside code under other licenses.

## Contributing
Contributions to `tmautils` are highly welcome! After all, there is much to do in Internet measurement research.

Before writing code, please familiarize yourself with the [philosophy](#philosophy) and [design choices](#design-choices), and try to follow them, or talk to me about how they are stupid and we should do things differently. I want this library to be the best version of itself.

AI Policy: I don't consider AI tool usage any different from IDE usage. This also means that *you* are responsible for the code you write and you should inspect every line of code written by an LLM. This policy is currently (slightly) relaxed for tests.