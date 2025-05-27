import asyncio
import pandas as pd

from imresearchutils.common import *

IP_API_BATCH_URL = "http://ip-api.com/batch"
MAX_IPS_PER_BATCH = 100
MAX_CONCURRENT_REQUESTS = 8
RANDOM_WAIT_MIN = 15
RANDOM_WAIT_MAX = 75
JITTER_MIN = 2
JITTER_MAX = 8
CACHE_DAYS_FRESH = 7

FIELDS = (
    "query",
    "status",
    "message",
    "continent",
    "continentCode",
    "country",
    "countryCode",
    "region",
    "regionName",
    "city",
    "district",
    "zip",
    "lat",
    "lon",
    "timezone",
    "offset",
    "currency",
    "isp",
    "org",
    "as",
    "asname",
    "mobile",
    "proxy",
    "hosting",
)


class IPApiUtil:
    """
    Utility class for interacting with ip-api.com

    Args:
        cache_days_fresh (int):
            Number of days to consider a cached result fresh.
            Default is 7 days.

        data_dir (Path | None):
            Base directory for data files.
            If None, the current working directory will be used.

        **kwargs (dict):
            Additional arguments for IOHelper.
            See the IOHelper class for more details.
    """

    def __init__(
        self,
        cache_days_fresh: int = CACHE_DAYS_FRESH,
        data_dir: Path | None = None,
        **kwargs,
    ):
        self.io_helper = IOHelper(
            self.__class__.__name__,
            data_dir=data_dir,
            **kwargs,
        )

        self.cache_days_fresh = cache_days_fresh

        self.io_helper.logger.info(
            f"Initialized IPApiUtil with cache directory: {self.io_helper.processed}"
        )

    async def get_batch_api(
        self,
        ips: list[str | IPv4Address | IPv6Address],
        max_retry: int = 8,
        save_cache: bool = True,
    ):
        """
        Query ip-api.com's batch API for a list of IP addresses.

        NOTE: This function instantiates its own `asyncio.Semaphore` internally
        to throttle requests. If you invoke it multiple times concurrently
        (or from separate event loops), each call will create its own semaphore
        and you may exceed your intended global limit. Therefore, only call
        `get_batch_api()` once at a time per loop to guarantee a true cap of
        `max_concurrent_requests`.

        Args:
            ips (list[str | IPv4Address | IPv6Address]):
                List of IP addresses to query.

            max_retry (int):
                Maximum number of retries for each batch.
                Default is 5.

            save_cache (bool):
                Whether to save the results to a cache file.
                Default is True.

        Returns:
            df (pd.DataFrame):
                DataFrame containing the results for the requested IPs.
        """
        import aiohttp
        from random import randint

        # Convert all IPs to strings
        ips = [str(ip) for ip in ips]

        # Split the list of IPs into batches of MAX_IPS_PER_BATCH
        batches = [
            ips[i:i+MAX_IPS_PER_BATCH]
            for i in range(0, len(ips), MAX_IPS_PER_BATCH)
        ]

        ipapi_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        results: dict[str, Any] = {}

        async with aiohttp.ClientSession() as session:
            async def _fetch_one_batch(batch: list[str]):
                for attempt in range(1, max_retry + 1):
                    async with ipapi_semaphore:
                        try:
                            resp = await session.post(
                                f"{IP_API_BATCH_URL}?fields={','.join(FIELDS)}",
                                json=batch
                            )
                        except aiohttp.ClientConnectionError as e:
                            # We do not know how long to wait
                            wait = randint(RANDOM_WAIT_MIN, RANDOM_WAIT_MAX)
                            self.io_helper.logger.warning(
                                f"ConnErr on batch {batch[:3]}... "
                                f"(attempt {attempt}/{max_retry}): {e}. "
                                f"retrying in {wait:.0f}s"
                            )
                            if attempt < max_retry:
                                await asyncio.sleep(wait)
                            continue

                        if resp.status != 200:
                            ttl = int(resp.headers.get("X-Ttl", "0"))
                            rl = int(resp.headers.get("X-Rl", "0"))
                            if ttl == 0 and rl == 0:
                                # We don't know how long to wait
                                ttl = randint(RANDOM_WAIT_MIN, RANDOM_WAIT_MAX)
                            else:
                                ttl += randint(JITTER_MIN, JITTER_MAX)
                            self.io_helper.logger.info(
                                f"Got HTTP {resp.status} on batch {batch[:3]}... "
                                f"(attempt {attempt}/{max_retry}), retrying in {ttl}s"
                            )
                            if attempt < max_retry:
                                await asyncio.sleep(ttl)
                            continue

                        data = await resp.json()
                        for entry in data:
                            results[entry["query"]] = entry

                        self.io_helper.logger.info(
                            f"Batch {batch[:3]}... fetched successfully "
                            f"(attempt {attempt}/{max_retry})"
                        )

                        return

                # If we reach here, we are out of attempts
                self.io_helper.logger.warning(
                    f"Batch {batch[:3]}... failed after {max_retry} attempts"
                )

            tasks = [asyncio.create_task(_fetch_one_batch(b)) for b in batches]
            await asyncio.gather(*tasks)

        # Convert the result into a DataFrame
        results_df = pd.DataFrame.from_dict(results, orient='index')
        results_df.index.name = "query"
        if "query" in results_df.columns:
            results_df = results_df.drop(columns=["query"])
        results_df.reset_index(inplace=True)

        # Ensure all expected columns are present
        for col in FIELDS:
            if col not in results_df.columns:
                results_df[col] = pd.NA

        # Save the results to a cache file
        if save_cache:
            self._cache_results(results_df)

        return results_df

    def _cache_results(
        self,
        results: pd.DataFrame,
    ):
        from datetime import date, timedelta

        date_str = date.today().isoformat()
        results["last_queried"] = date_str

        # Set up snapshot and history directories
        snapshot_dir = self.io_helper.processed / "latest"
        history_dir = self.io_helper.processed / "history" / date_str
        for dir in (snapshot_dir, history_dir):
            dir.mkdir(parents=True, exist_ok=True)

        # Read existing snapshot if it exists
        snapshot_fp = snapshot_dir / "current.csv"
        if snapshot_fp.exists():
            current = pd.read_csv(
                snapshot_fp,
                encoding="utf-8",
                low_memory=False,
            )
            current = current.loc[:, ~current.columns.duplicated()]
        else:
            current = pd.DataFrame(columns=list(FIELDS) + ["last_queried"])
        if "last_queried" not in current.columns:
            current["last_queried"] = pd.NA

        # Convert last_queried to date objects
        current["last_queried"] = (
            pd.to_datetime(current["last_queried"], errors="coerce")
            .dt.date
        )

        # Drop rows that are no longer fresh
        cutoff = date.today() - timedelta(days=self.cache_days_fresh)
        stale = current["last_queried"] < cutoff
        current = current.loc[~stale].reset_index(drop=True)

        def _row_changed(r):
            if pd.isna(r["last_queried_old"]):
                return True
            for f in FIELDS:
                if r[f] != r[f + "_old"]:
                    return True
            return False

        # Identify rows that have changed
        merged = results.merge(
            current, on="query", how="left", suffixes=("", "_old")
        )
        changed = merged[
            merged.apply(_row_changed, axis=1)
        ][list(FIELDS) + ["last_queried"]]

        if not changed.empty:
            date_existing = list(
                history_dir.glob(f"changes_{date_str}_*.csv")
            )
            idx = len(date_existing) + 1
            target = history_dir / f"changes_{date_str}_{idx}.csv"
            changed.to_csv(
                target,
                index=False,
                encoding="utf-8",
            )
            self.io_helper.logger.info(
                f"Saved {len(changed)} changes to {target}"
            )

        # ensure both have exactly the same columns (in the same order)
        data_cols = [c for c in results.columns if c != "query"]
        cur_idx = current.set_index("query").reindex(columns=data_cols)
        new_idx = results.set_index("query").reindex(columns=data_cols)
        new_entries = new_idx.loc[~new_idx.index.isin(cur_idx.index)]

        # Save the current snapshot
        combined = pd.concat([cur_idx, new_entries], sort=False).reset_index()
        combined.to_csv(
            snapshot_fp,
            index=False,
            encoding="utf-8",
        )

    def get_batch(
        self,
        ips: list[str | IPv4Address | IPv6Address],
    ) -> pd.DataFrame:
        """
        Get a batch of IP addresses from the cache or API.

        Args:
            ips (list[str | IPv4Address | IPv6Address]):
                List of IP addresses to query.

        Returns:
            df (pd.DataFrame):
                DataFrame containing the results for the requested IPs.
        """

        from datetime import date, timedelta

        ips = [str(ip) for ip in ips]

        # Load snapshot if it exists
        snapshot_fp = self.io_helper.processed / "latest" / "current.csv"
        if snapshot_fp.exists():
            current = pd.read_csv(
                snapshot_fp,
                encoding="utf-8",
                low_memory=False,
            )
            current = current.loc[:, ~current.columns.duplicated()]
            if "last_queried" not in current.columns:
                current["last_queried"] = pd.NaT
            else:
                current["last_queried"] = pd.to_datetime(
                    current["last_queried"], format="%Y-%m-%d", errors="coerce"
                )
        else:
            current = pd.DataFrame(
                {c: pd.Series(dtype="object") for c in list(FIELDS)}
            )
            current["last_queried"] = pd.Series(dtype="datetime64[ns]")

        # If a result is fresh, use it
        cutoff = date.today() - timedelta(days=self.cache_days_fresh)
        fresh_mask = (
            current["query"].isin(ips)
            & (current["last_queried"].dt.date >= cutoff)
        )
        cached_df = current.loc[fresh_mask, list(FIELDS)]

        # Determine which IPs need querying
        cached_set = set(cached_df["query"])
        to_query = [ip for ip in ips if ip not in cached_set]

        # Query the API for the remaining IPs
        new_df = pd.DataFrame(columns=list(FIELDS))
        if to_query:
            self.io_helper.logger.info(
                f"Cached result not found for {len(to_query)} IPs, querying API"
            )
            fetched = asyncio.run(self.get_batch_api(to_query))
            new_df = fetched[list(FIELDS)]

        # Combine and return
        if new_df.empty:
            return cached_df.reset_index(drop=True)
        cached_df = cached_df.reindex(columns=list(FIELDS))
        new_df = new_df.reindex(columns=list(FIELDS))
        return pd.concat([cached_df, new_df], ignore_index=True)
