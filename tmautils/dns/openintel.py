from typing import Optional, Callable, ClassVar
from pathlib import Path
from websocket import WebSocketApp
from threading import Thread, Event
from collections import deque
import atexit
import time
import json
from concurrent.futures import ThreadPoolExecutor
import tenacity
from pydantic import BaseModel
import pyarrow as pa

from tmautils.common import IOHelper
from tmautils.db import (
    DuckDbStore, DuckDbBackend, BufferedWriter,
    TableConfig, WriteMode,
)


class NewlyRegisteredFqdn(BaseModel):
    msg_timestamp: float
    fqdn: str
    cert_index: int
    ct_name: str
    timestamp: int

    ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
        pa.field("msg_timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("fqdn", pa.string()),
        pa.field("cert_index", pa.int64()),
        pa.field("ct_name", pa.string()),
        pa.field("timestamp", pa.int64()),
    ])

    SQL_TABLE_CONSTRAINTS: ClassVar[list[str]] = [
        "PRIMARY KEY (msg_timestamp, fqdn)"
    ]

    SQL_TABLE_INDICES: ClassVar[list[list[str]]] = [
        ["fqdn"],
        ["fqdn", "msg_timestamp"]
    ]


class NewlyRegisteredDomain(BaseModel):
    msg_timestamp: float
    domain: str
    cert_index: int
    ct_name: str
    timestamp: int

    ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
        pa.field("msg_timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("domain", pa.string()),
        pa.field("cert_index", pa.int64()),
        pa.field("ct_name", pa.string()),
        pa.field("timestamp", pa.int64()),
    ])

    SQL_TABLE_CONSTRAINTS: ClassVar[list[str]] = [
        "PRIMARY KEY (msg_timestamp, domain)"
    ]

    SQL_TABLE_INDICES: ClassVar[list[list[str]]] = [
        ["domain"]
    ]


class ConfirmedNewlyRegisteredDomain(BaseModel):
    msg_timestamp: float
    domain: str
    cert_index: int
    ct_name: str
    timestamp: int
    confidence: int

    ARROW_SCHEMA: ClassVar[pa.Schema] = pa.schema([
        pa.field("msg_timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("domain", pa.string()),
        pa.field("cert_index", pa.int64()),
        pa.field("ct_name", pa.string()),
        pa.field("timestamp", pa.int64()),
        pa.field("confidence", pa.int64()),
    ])

    SQL_TABLE_CONSTRAINTS: ClassVar[list[str]] = [
        "PRIMARY KEY (msg_timestamp, domain)"
    ]

    SQL_TABLE_INDICES: ClassVar[list[list[str]]] = [
        ["domain"]
    ]


class OpenIntelZoneStreamUtil:
    """
    Utility to connect to OpenIntel ZoneStream using WebSockets,
    act on received messages through an optional callback,
    and store the messages in a local DuckDB database.

    Args:
        topics (list[str]): List of topics to subscribe to. Valid topics are:
            - "newly_registered_fqdn"
            - "newly_registered_domain"
            - "confirmed_newly_registered_domain"

        callback (Callable[[str, list[dict]], None] | None):
            Optional callback function to be called with each batch of messages
            received from the stream.
            The function should accept two arguments: the topic (str),
            and a list of message dictionaries (list[dict]).
            The callback will be called in a separate thread.

        working_root (Path | None):
            Base directory where the namespace directory will be created.
            If None, the current working directory will be used.

        data_dir (Path | None):
            Deprecated alias for `working_root`.

        **kwargs:
            Additional keyword arguments for IOHelper.
            See IOHelper documentation for more details.
    """

    SERVICE = "openintel_zonestream"
    WS_URL = "wss://zonestream.openintel.nl/ws/{topic}"
    BATCH_MAX = 1000
    MAX_CALLBACK_WORKERS = 4
    # WebSocket config
    WS_RECONNECT_INTERVAL_SEC = 5
    # Retry config
    RETRY_MIN_SEC = 5.0
    RETRY_MAX_SEC = 120.0
    RETRY_MULTIPLIER = 1.5

    # Topic to model mapping
    TOPIC_TO_MODEL = {
        "newly_registered_fqdn": NewlyRegisteredFqdn,
        "newly_registered_domain": NewlyRegisteredDomain,
        "confirmed_newly_registered_domain": ConfirmedNewlyRegisteredDomain,
    }

    def __init__(
        self,
        topics: list[str],
        *,
        callback: Optional[Callable[[str, list[dict]], None]] = None,
        working_root: Path | None = None,
        data_dir: Path | None = None,
        **kwargs
    ):
        # Initialize IOHelper
        working_root = IOHelper.handle_working_root_data_dir(
            working_root, data_dir
        )
        self.io_helper = IOHelper(
            self.__class__.__name__,
            working_root=working_root,
            **kwargs,
        )

        # Sanitize topics
        if not topics:
            raise ValueError("At least one topic must be specified")
        invalid_topics = set(topics) - set(self.TOPIC_TO_MODEL.keys())
        if invalid_topics:
            raise ValueError(f"Invalid topics: {invalid_topics}")
        self.topics = topics

        # Callback
        self._callback = callback
        self._cb_pool: Optional[ThreadPoolExecutor] = None

        # Batching
        self.send_q = deque(maxlen=10 * self.BATCH_MAX)
        self.batcher_wake = Event()
        self.running = Event()

        # WebSockets
        self.ws_map: dict[str, WebSocketApp] = {}

        # Threads
        self.batcher_thr: Optional[Thread] = None
        self.ws_threads: dict[str, Thread] = {}
        self._atexit_registered = False

        # Database setup
        db_path = self.io_helper.raw / "openintel_zs.duckdb"
        self.db = DuckDbStore(
            db_path=str(db_path),
            log_helper=self.io_helper.log_helper,
        )
        self.table_configs = {
            topic: TableConfig(
                model=self.TOPIC_TO_MODEL[topic],
                mode=WriteMode.APPEND,
            )
            for topic in self.topics
        }
        self.writer: Optional[BufferedWriter] = None

    def _on_message_factory(self, topic):
        def _on_message(ws, message: str):
            try:
                msg = json.loads(message)
            except Exception as e:
                self.io_helper.logger.error(
                    f"[{topic}] JSON decode error: {e}"
                )
                return

            msg["msg_timestamp"] = time.time()

            # Add to batch queue and wake batcher
            self.send_q.append((topic, msg))
            self.batcher_wake.set()

        return _on_message

    def _on_error_factory(self, topic):
        def _on_error(ws, error):
            self.io_helper.logger.error(f"[{topic}] WebSocket error: {error}")
        return _on_error

    def _on_close_factory(self, topic):
        def _on_close(ws, close_status_code, close_msg):
            self.io_helper.logger.info(
                f"[{topic}] WebSocket closed: status={close_status_code}, msg={close_msg}"
            )
        return _on_close

    def _on_open_factory(self, topic):
        def _on_open(ws):
            self.io_helper.logger.info(f"[{topic}] WebSocket connected")
        return _on_open

    def _pre_retry_websocket(
        self,
        retry_state: tenacity.RetryCallState,
        topic: str
    ):
        attempt = retry_state.attempt_number
        exception = retry_state.outcome.exception()

        self.io_helper.logger.warning(
            f"[{topic}] WebSocket failed (attempt {attempt}): {exception}"
        )

        # Clean up this websocket
        if topic in self.ws_map:
            try:
                self.ws_map[topic].close()
            except Exception:
                pass
            del self.ws_map[topic]

        wait_time = retry_state.next_action.sleep
        self.io_helper.logger.info(
            f"[{topic}] Retrying in {wait_time:.1f} seconds..."
        )

    def _websocket_worker(self, topic: str):
        retry_decorator = tenacity.retry(
            # Retry forever if still running
            retry=tenacity.retry_if_exception(
                lambda e: self.running.is_set()
            ),
            # Exponential backoff
            wait=tenacity.wait_exponential(
                multiplier=self.RETRY_MULTIPLIER,
                min=self.RETRY_MIN_SEC,
                max=self.RETRY_MAX_SEC,
            ),
            # Log and clean up before retrying
            before_sleep=lambda rs: self._pre_retry_websocket(rs, topic),
        )

        @retry_decorator
        def _inner():
            # Create websocket
            ws = WebSocketApp(
                self.WS_URL.format(topic=topic),
                on_message=self._on_message_factory(topic),
                on_error=self._on_error_factory(topic),
                on_close=self._on_close_factory(topic),
                on_open=self._on_open_factory(topic),
            )

            # Store in map for cleanup
            self.ws_map[topic] = ws

            self.io_helper.logger.info(f"[{topic}] Starting WebSocket...")

            # Run until closed or error
            ws.run_forever(reconnect=self.WS_RECONNECT_INTERVAL_SEC)

            # If run_forever exits cleanly while still running, something is wrong
            if self.running.is_set():
                raise RuntimeError(
                    f"WebSocket for {topic} exited unexpectedly"
                )

        _inner()

    def _batcher_loop(self):
        while self.running.is_set() or self.send_q:
            if not self.send_q:
                # Wait for messages
                self.batcher_wake.wait(timeout=0.5)
                self.batcher_wake.clear()
                continue

            # Batch messages by topic
            topic_map = {}
            total = 0
            while self.send_q and total < self.BATCH_MAX:
                topic, msg = self.send_q.popleft()
                topic_map.setdefault(topic, []).append(msg)
                total += 1

            # Buffer for writing and dispatch callback
            for topic, batch in topic_map.items():
                try:
                    self.writer.add_rows(topic, batch)
                except Exception as e:
                    self.io_helper.logger.error(
                        f"Failed to add rows for {topic}: {e}"
                    )

                # Dispatch callback
                if self._callback:
                    self._dispatch_cb(topic, batch)

    def _dispatch_cb(self, topic: str, batch: list[dict]) -> None:
        if not batch:
            return

        def _run():
            try:
                self._callback(topic, batch)
            except Exception as e:
                self.io_helper.logger.error(f"Callback error: {e}")

        self._cb_pool.submit(_run)

    def start(self):
        """Start the WebSocket listeners in individual threads"""

        if self.running.is_set():
            self.io_helper.logger.warning("Already running; start() ignored.")
            return
        self.running.set()

        self.send_q.clear()
        self.batcher_wake.clear()

        if self.writer is None:
            self.writer = BufferedWriter(
                backend=DuckDbBackend(store=self.db),
                table_configs=self.table_configs,
                time_thresh_sec=300.0,
                log_helper=self.io_helper.log_helper,
            )

        if self._callback is not None and self._cb_pool is None:
            self._cb_pool = ThreadPoolExecutor(
                max_workers=self.MAX_CALLBACK_WORKERS,
                thread_name_prefix="zonestream-cb"
            )

        # Start batcher thread
        self.batcher_thr = Thread(
            target=self._batcher_loop,
            name="zonestream-batcher",
            daemon=True,
        )
        self.batcher_thr.start()

        # Start websocket threads
        for topic in self.topics:
            ws_thread = Thread(
                target=self._websocket_worker,
                args=(topic,),
                name=f"zonestream-ws-{topic}",
                daemon=True,
            )
            ws_thread.start()
            self.ws_threads[topic] = ws_thread

        # Register atexit handler
        if not self._atexit_registered:
            atexit.register(self._atexit_cleanup)
            self._atexit_registered = True

        self.io_helper.logger.info(
            f"Started Zone Stream listener for topics: {self.topics}"
        )

    def stop(self):
        """Stop WebSockets, flush data, and clean up"""

        if not self.running.is_set():
            return

        self.io_helper.logger.info("Stopping Zone Stream listener")

        # Signal threads to exit
        self.running.clear()
        self.batcher_wake.set()

        # Close all websockets (causes run_forever() to exit)
        self.io_helper.logger.info("Closing WebSockets...")
        for topic, ws in list(self.ws_map.items()):
            try:
                ws.close()
            except Exception as e:
                self.io_helper.logger.warning(
                    f"[{topic}] Error closing websocket: {e}"
                )
        self.ws_map.clear()

        # Join websocket threads
        for topic, ws_thread in list(self.ws_threads.items()):
            if ws_thread.is_alive():
                ws_thread.join(timeout=5.0)
                if ws_thread.is_alive():
                    self.io_helper.logger.warning(
                        f"[{topic}] WebSocket thread did not stop in time"
                    )
        self.ws_threads.clear()

        # Join batcher thread
        if self.batcher_thr and self.batcher_thr.is_alive():
            self.batcher_thr.join(timeout=5.0)
            if self.batcher_thr.is_alive():
                self.io_helper.logger.warning(
                    "Batcher thread did not stop in time"
                )
        self.batcher_thr = None

        # Flush and close writer
        if self.writer is not None:
            try:
                self.io_helper.logger.info("Flushing BufferedWriter...")
                self.writer.close()
            except Exception as e:
                self.io_helper.logger.error(
                    f"Error closing BufferedWriter: {e}")
            self.writer = None

        # We keep DuckDb open for user queries.
        # Use close() method for explicit cleanup if needed.

        # Shutdown callback pool
        if self._cb_pool is not None:
            try:
                self.io_helper.logger.info(
                    "Shutting down callback thread pool"
                )
                self._cb_pool.shutdown(wait=True, cancel_futures=True)
            except Exception as e:
                self.io_helper.logger.error(
                    f"Error shutting down callback pool: {e}"
                )
            self._cb_pool = None

        self.io_helper.logger.info("OpenIntelZoneStreamUtil stopped")

    def close(self):
        """Explicitly close all resources including database"""

        # Stop websockets if running
        self.stop()

        # Close database connection
        if self.db is not None:
            try:
                self.db.close()
            except Exception as e:
                self.io_helper.logger.error(f"Error closing DuckDbStore: {e}")
            self.db = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _atexit_cleanup(self):
        try:
            self.close()
        except Exception:
            pass
