# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from typing import Any
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import asyncio
import json
import os
import jwt

import aiohttp

from tmautils.common import IOHelper, run_coro_sync, AsyncRateLimiter
from tmautils.web import request_with_retry


class CzdsDownloadUtil:
    """
    Utility for downloading zone files from ICANN CZDS.

    Zone files are stored in a daily directory structure:
        `downloads/<date>/<tld>.txt.gz`

    Metadata about downloads is stored alongside zone files:
        `downloads/<date>/_metadata.json`

    Re-downloads within 24 hours are prevented.

    Args:
        username:
            CZDS username (email). If None, reads from `CZDS_USERNAME` env var.

        password:
            CZDS password. If None, reads from `CZDS_PASSWORD` env var.

        working_root:
            Base directory where the namespace directory will be created.
            If None, the current working directory will be used.

        max_concurrent_downloads:
            Maximum number of parallel zone file downloads.
            Default is 5.

        user_agent:
            User-Agent header for HTTP requests.
            CZDS requires a valid User-Agent.
            Default is `tmautils-czds/1.0`.

        **kwargs:
            Additional arguments passed to IOHelper.

    Example:
        ```python
        czds = CzdsDownloadUtil(
            username="user@example.com",
            password="secret",
        )

        # Download all authorized zones
        paths = await czds.download_all_zones()

        # Download specific zone
        path = await czds.download_zone("com")
        ```
    """

    # CZDS API endpoints
    _AUTH_URL = "https://account-api.icann.org/api/authenticate"
    _BASE_URL = "https://czds-api.icann.org"
    _LINKS_ENDPOINT = "/czds/downloads/links"

    # Environment variable names for credentials
    _ENV_USERNAME = "CZDS_USERNAME"
    _ENV_PASSWORD = "CZDS_PASSWORD"

    # Metadata filename
    _METADATA_FILENAME = "_metadata.json"

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        working_root: Path | None = None,
        max_concurrent_downloads: int = 5,
        user_agent: str = "tmautils-czds/1.0",
        **kwargs,
    ):
        # Handle credentials
        self._username = username or os.environ.get(self._ENV_USERNAME)
        self._password = password or os.environ.get(self._ENV_PASSWORD)

        if not self._username or not self._password:
            raise ValueError(
                f"CZDS credentials required. Provide username/password or set "
                f"{self._ENV_USERNAME} and {self._ENV_PASSWORD} environment variables."
            )

        self._io_helper = IOHelper.init_with_dirs(
            self.__class__.__name__,
            dirs={"downloads", "logs"},
            working_root=working_root,
            **kwargs,
        )

        self._rate_limiter = AsyncRateLimiter(
            max_concurrent=max_concurrent_downloads
        )
        self._user_agent = user_agent

        # Token state
        self._access_token: str | None = None
        self._token_expiry: datetime | None = None

        # Lock to protect concurrent metadata writes
        self._metadata_lock = asyncio.Lock()

    def _get_date_dir(self, date_: date, create_dir: bool = True) -> Path:
        day_dir = self._io_helper.downloads / date_.isoformat()
        if create_dir:
            day_dir.mkdir(parents=True, exist_ok=True)
        return day_dir

    def _get_metadata_path(self, date_: date, create_dir: bool = True) -> Path:
        return self._get_date_dir(date_, create_dir=create_dir) / self._METADATA_FILENAME

    def _load_all_metadata(self, date_: date) -> dict[str, Any]:
        metadata_path = self._get_metadata_path(date_, create_dir=False)
        if not metadata_path.exists():
            return {}
        try:
            return json.loads(metadata_path.read_text())
        except Exception as e:
            self._io_helper.logger.warning(
                f"Failed to parse metadata at {metadata_path}: {e}"
            )
            return {}

    async def _update_metadata(self, tld: str, entry: dict[str, Any]):
        async with self._metadata_lock:
            today = datetime.now(timezone.utc).date()
            metadata = self._load_all_metadata(today)
            metadata[tld] = entry

            # Write to temporary file and rename
            metadata_path = self._get_metadata_path(today, create_dir=True)
            tmp_path = metadata_path.with_suffix(f"{metadata_path.suffix}.tmp")
            tmp_path.write_text(json.dumps(metadata, indent=2))
            tmp_path.rename(metadata_path)

    def _extract_tld_from_url(self, url: str) -> str:
        # URL format: https://czds-download-api.icann.org/czds/downloads/<tld>.zone
        filename = url.rsplit("/", 1)[-1]
        if not filename.endswith(".zone"):
            raise ValueError(f"Cannot extract TLD from URL: {url}")
        return filename.removesuffix(".zone")

    def _downloaded_in_last_24h(self, tld: str) -> Path | None:
        now = datetime.now(timezone.utc)
        # Only need today + yesterday to cover a 24h window
        for day in (now.date(), (now - timedelta(days=1)).date()):
            day_dir = self._io_helper.downloads / day.isoformat()
            if not day_dir.exists():
                continue

            metadata = self._load_all_metadata(day)
            entry = metadata.get(tld)
            if not isinstance(entry, dict):
                continue

            # Prefer last_modified (server-side timestamp), fall back to downloaded_at
            timestamp_str = None
            parse_as_http_date = False
            if entry.get("last_modified"):
                timestamp_str = entry["last_modified"]
                parse_as_http_date = True
            elif entry.get("downloaded_at"):
                timestamp_str = entry["downloaded_at"]
                parse_as_http_date = False

            if not timestamp_str:
                continue

            try:
                if parse_as_http_date:
                    dt: datetime = parsedate_to_datetime(timestamp_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = datetime.fromisoformat(timestamp_str)
            except Exception:
                continue

            if now - dt <= timedelta(hours=24):
                existing = day_dir / f"{tld}.txt.gz"
                if existing.exists():
                    return existing
        return None

    def _decode_jwt_exp(self, token: str) -> datetime:
        # OK to skip signature verification for expiry extraction
        claims = jwt.decode(token, options={"verify_signature": False})
        exp = claims.get("exp")
        if exp is None:
            raise ValueError("JWT missing 'exp' claim")
        return datetime.fromtimestamp(exp, tz=timezone.utc)

    def _is_token_valid(self) -> bool:
        if not self._access_token or not self._token_expiry:
            return False
        # Refresh 5 minutes before expiry
        now = datetime.now(timezone.utc)
        buffer = 5 * 60  # 5 minutes in seconds
        return (self._token_expiry.timestamp() - now.timestamp()) > buffer

    async def authenticate(self) -> str:
        """
        Authenticate with CZDS and obtain access token.

        Returns cached token if still valid, otherwise requests new token.

        Returns:
            Access token string.

        Raises:
            aiohttp.ClientError: On HTTP errors.
            ValueError: On authentication failure.
        """
        if self._is_token_valid():
            return self._access_token

        self._io_helper.logger.info("Authenticating with CZDS...")

        async with aiohttp.ClientSession() as session:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": self._user_agent,
            }
            payload = {
                "username": self._username,
                "password": self._password,
            }

            async with request_with_retry(
                session,
                "POST",
                self._AUTH_URL,
                json=payload,
                headers=headers,
                rate_limiter=self._rate_limiter,
                log_helper=self._io_helper.log_helper,
            ) as resp:
                if resp.status == 401:
                    raise ValueError(
                        "CZDS authentication failed: invalid credentials"
                    )
                if resp.status == 429:
                    raise ValueError(
                        "CZDS rate limit exceeded: max 8 auth attempts per 5 minutes"
                    )
                resp.raise_for_status()

                data = await resp.json()
                self._access_token = data["accessToken"]
                self._token_expiry = self._decode_jwt_exp(self._access_token)

                self._io_helper.logger.info(
                    f"Authenticated successfully, token expires at {self._token_expiry}"
                )

        return self._access_token

    def authenticate_sync(self) -> str:
        """Synchronous version of `authenticate()`."""
        return run_coro_sync(self.authenticate())

    async def get_zone_links(self) -> list[str]:
        """
        Get list of authorized zone file download URLs.

        Returns:
            List of download URLs for authorized zones.

        Raises:
            aiohttp.ClientError: On HTTP errors.
        """
        token = await self.authenticate()

        self._io_helper.logger.info("Fetching zone download links...")

        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": self._user_agent,
            }

            url = f"{self._BASE_URL}{self._LINKS_ENDPOINT}"
            async with request_with_retry(
                session,
                "GET",
                url,
                headers=headers,
                rate_limiter=self._rate_limiter,
                log_helper=self._io_helper.log_helper,
            ) as resp:
                if resp.status == 401:
                    # Token may have expired during request, clear and retry
                    self._access_token = None
                    self._token_expiry = None
                    raise ValueError("CZDS token expired or invalid")

                resp.raise_for_status()
                links: list[str] = await resp.json()

                self._io_helper.logger.info(
                    f"Found {len(links)} authorized zones"
                )
                return links

    def get_zone_links_sync(self) -> list[str]:
        """Synchronous version of get_zone_links()."""
        return run_coro_sync(self.get_zone_links())

    async def get_zone_info(self, download_url: str) -> dict[str, Any]:
        """
        Get zone file metadata without downloading (HEAD request).

        Args:
            download_url: Full download URL for the zone file.

        Returns:
            Dictionary with 'content_length', 'last_modified', 'filename' keys.

        Raises:
            aiohttp.ClientError: On HTTP errors.
        """
        token = await self.authenticate()

        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {token}",
                "User-Agent": self._user_agent,
            }

            async with request_with_retry(
                session,
                "HEAD",
                download_url,
                headers=headers,
                rate_limiter=self._rate_limiter,
                log_helper=self._io_helper.log_helper,
            ) as resp:
                resp.raise_for_status()

                # Parse Content-Disposition for filename
                # Format: attachment;filename=example1.txt.gz
                content_disp = resp.headers.get("Content-Disposition", "")
                filename = None
                for part in content_disp.split(";"):
                    part = part.strip()
                    if part.startswith("filename="):
                        filename = part.removeprefix("filename=")
                        break

                return {
                    "content_length": int(resp.headers.get("Content-Length", 0)),
                    "last_modified": resp.headers.get("Last-Modified"),
                    "filename": filename,
                }

    def get_zone_info_sync(self, download_url: str) -> dict[str, Any]:
        """Synchronous version of get_zone_info()."""
        return run_coro_sync(self.get_zone_info(download_url))

    async def download_zone(self, tld_or_url: str) -> Path | None:
        """
        Download a single zone file.

        Files are saved to `downloads/<date>/<tld>.txt.gz`.
        Re-downloads within the same day are skipped.

        Args:
            tld_or_url:
                Either a TLD name (e.g., "com") or a full download URL.

        Returns:
            Path to downloaded file, or None if download was skipped/failed.

        Raises:
            aiohttp.ClientError: On HTTP errors.
        """
        # Determine if input is TLD or URL
        if tld_or_url.startswith("http"):
            download_url = tld_or_url
            tld = self._extract_tld_from_url(download_url)
        else:
            tld = tld_or_url.lower()
            # Need to get full URL from links
            links = await self.get_zone_links()
            download_url = None
            for link in links:
                if self._extract_tld_from_url(link) == tld:
                    download_url = link
                    break

            if not download_url:
                self._io_helper.logger.warning(
                    f"Zone '{tld}' not found in authorized zones"
                )
                return None

        # Check for recent download within 24 hours
        recent_path = self._downloaded_in_last_24h(tld)
        if recent_path:
            self._io_helper.logger.info(
                f"Zone '{tld}' already downloaded in last 24 hours, skipping"
            )
            return recent_path

        # Download the zone file
        token = await self.authenticate()
        output_file = self._get_date_dir(
            datetime.now(timezone.utc).date(),
            create_dir=True
        ) / f"{tld}.txt.gz"
        self._io_helper.logger.info(f"Downloading zone: '{tld}'")

        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {token}",
                "User-Agent": self._user_agent,
            }

            async with request_with_retry(
                session,
                "GET",
                download_url,
                headers=headers,
                attempt_timeout=600.0,  # 10 minutes for large files
                rate_limiter=self._rate_limiter,
                log_helper=self._io_helper.log_helper,
            ) as resp:
                if resp.status == 403:
                    self._io_helper.logger.error(
                        f"Not authorized to download zone '{tld}'"
                    )
                    return None
                if resp.status == 409:
                    self._io_helper.logger.error(
                        "Must accept CZDS Terms & Conditions on the portal"
                    )
                    return None

                resp.raise_for_status()

                # Stream to temporary file, then rename
                tmp_file = output_file.with_suffix(f"{output_file.suffix}.tmp")
                try:
                    content_length = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0

                    with tmp_file.open("wb") as f:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            f.write(chunk)
                            downloaded += len(chunk)
                    tmp_file.rename(output_file)

                    entry = {
                        "downloaded_at": datetime.now(timezone.utc).isoformat(),
                        "content_length": content_length,
                        "last_modified": resp.headers.get("Last-Modified"),
                        "url": download_url,
                    }
                    await self._update_metadata(tld, entry)

                    self._io_helper.logger.info(
                        f"Downloaded zone '{tld}' ({downloaded} bytes)"
                    )
                    return output_file

                except Exception:
                    if tmp_file.exists():
                        tmp_file.unlink()
                    raise

    def download_zone_sync(self, tld_or_url: str) -> Path | None:
        """Synchronous version of download_zone()."""
        return run_coro_sync(self.download_zone(tld_or_url))

    async def download_all_zones(self) -> list[Path]:
        """
        Download all authorized zone files in parallel.

        Files are saved to `downloads/<date>/<tld>.txt.gz`.
        Re-downloads within the same day are skipped.

        Returns:
            List of paths to downloaded files (excluding skipped/failed).
        """
        links = await self.get_zone_links()

        self._io_helper.logger.info(
            f"Downloading {len(links)} zones with rate limiting: "
            f"{self._rate_limiter.config_string}"
        )

        async def download_one(url: str) -> Path | None:
            try:
                return await self.download_zone(url)
            except Exception as e:
                tld = self._extract_tld_from_url(url)
                self._io_helper.logger.error(
                    f"Failed to download '{tld}': {e}"
                )
                return None

        tasks = [download_one(url) for url in links]
        results = await asyncio.gather(*tasks)

        paths = [p for p in results if p is not None]
        self._io_helper.logger.info(
            f"Downloaded {len(paths)}/{len(links)} zones successfully"
        )

        return paths

    def download_all_zones_sync(self) -> list[Path]:
        """Synchronous version of download_all_zones()."""
        return run_coro_sync(self.download_all_zones())
