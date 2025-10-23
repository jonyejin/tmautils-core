from websocket import WebSocketApp
import pandas as pd
from threading import Thread, Event
import rel
import atexit
import multiprocessing as mp
import logging
from concurrent.futures import ThreadPoolExecutor

from tmautils.common import *


class ZoneStreamMethod(IpcMethodBase, StrEnum):
    STOP = "stop"
    BATCH = "batch"


class OpenIntelZoneStreamWorker:
    SERVICE = "openintel_zonestream"
    WS_URL = "wss://zonestream.openintel.nl/ws/{topic}"
    BATCH_MAX = 1000
    THREAD_JOIN_TIMEOUT_SEC = 3.0
    RECONNECT_INTERVAL_SEC = 5

    def __init__(
        self,
        topics: list[str],
        data_q: mp.Queue,
        cmd_q: mp.Queue,
        *,
        logging_config: Optional[LogConfig] = None,
    ):
        from collections import deque

        self.topics = topics
        self.data_q = data_q
        self.cmd_q = cmd_q

        self.ws_map: dict[str, WebSocketApp] = {}

        self.send_q = deque()
        self.sender_wake = Event()
        self.running = Event()
        self.running.set()

        self.sender_thr: Optional[Thread] = None
        self.cmd_thr: Optional[Thread] = None

        self._async_helper = AsyncHelper()
        self._log_helper = LogHelper(
            logging_config) if logging_config else None
        self.logger = get_logger_from_helper(self._log_helper)

    def _sender_loop(self):
        while self.running.is_set() or self.send_q:
            if not self.send_q:
                # Nothing to send, wait for sender_wake_event
                self.sender_wake.wait()
                self.sender_wake.clear()  # Woke up => clear event
                continue  # Let the next iteration handle sending or exit

            topic_map = {}
            total = 0
            while self.send_q and total < self.BATCH_MAX:
                topic, msg = self.send_q.popleft()
                topic_map.setdefault(topic, []).append(msg)
                total += 1

            self._async_helper.mpq_put_sync(
                self.data_q,
                IpcMsg.notify(
                    self.SERVICE,
                    ZoneStreamMethod.BATCH,
                    topic_map=topic_map
                )
            )

    def _cmd_loop(self):
        while self.running.is_set():
            try:
                msg = self.cmd_q.get(timeout=0.5)
            except Exception:
                continue

            if ((not isinstance(msg, IpcMsg)) or
                (msg.service != self.SERVICE) or
                    (not msg.is_notify)):
                continue

            if ZoneStreamMethod.get_method(msg) == ZoneStreamMethod.STOP:
                self.logger.info("Received STOP notification from parent")
                self.shutdown()
                break

    def _on_open_factory(self, topic):
        def _on_open(ws):
            self.logger.info(f"[{topic}] WebSocket opened: {ws.url}")

        return _on_open

    def _on_message_factory(self, topic):
        import json

        def _on_message(ws, message: str):
            try:
                msg = json.loads(message)
            except Exception as e:
                self.logger.error(f"[{topic}] JSON decode error: {e}")
                return
            msg["msg_timestamp"] = pd.Timestamp.now(tz="UTC").timestamp()
            self.send_q.append((topic, msg))
            self.sender_wake.set()

        return _on_message

    def _on_error_factory(self, topic):
        def _on_error(ws, error: Exception):
            self.logger.error(f"[{topic}] WebSocket error: {error}")

        return _on_error

    def _on_close_factory(self, topic):
        def _on_close(ws, code, msg):
            self.logger.info(
                f"[{topic}] WebSocket closed, code={code}, msg={msg}"
            )

        return _on_close

    def shutdown(self):
        if not self.running.is_set():
            return
        self.running.clear()

        # Close all websockets
        for ws in self.ws_map.values():
            try:
                ws.close()
            except Exception:
                pass

        # Abort rel dispatcher
        try:
            rel.abort()
        except Exception:
            pass

        # Signal sender to exit
        self.sender_wake.set()

    def run(self):
        try:
            # Command thread
            self.cmd_thr = Thread(
                target=self._cmd_loop,
                name=f"{self.__class__.__name__}-cmd",
                daemon=True,
            )
            self.cmd_thr.start()

            self.logger.info(
                f"Starting WebSocket listeners for topics: {self.topics}"
            )

            for topic in self.topics:
                ws = WebSocketApp(
                    self.WS_URL.format(topic=topic),
                    on_open=self._on_open_factory(topic),
                    on_message=self._on_message_factory(topic),
                    on_error=self._on_error_factory(topic),
                    on_close=self._on_close_factory(topic),
                )
                self.ws_map[topic] = ws
                ws.run_forever(
                    dispatcher=rel,  # Use rel to run in background
                    reconnect=self.RECONNECT_INTERVAL_SEC,
                )

            # Sender thread
            self.sender_thr = Thread(
                target=self._sender_loop,
                name=f"{self.__class__.__name__}-sender",
                daemon=True,
            )
            self.sender_thr.start()

            self._async_helper.mpq_put_sync(
                self.data_q, IpcMsg.status(IpcStatusCode.READY)
            )
            rel.dispatch()
        except Exception as e:
            self.logger.exception(
                f"Error starting WebSocket listeners: {e}"
            )
        finally:
            self.shutdown()
            try:
                if self.sender_thr and self.sender_thr.is_alive():
                    self.sender_thr.join(timeout=self.THREAD_JOIN_TIMEOUT_SEC)
                if self.cmd_thr and self.cmd_thr.is_alive():
                    self.cmd_thr.join(timeout=self.THREAD_JOIN_TIMEOUT_SEC)
            except Exception:
                pass
            self._async_helper.mpq_put_sync(
                self.data_q, IpcMsg.status(IpcStatusCode.STOPPED)
            )


class OpenIntelZoneStreamUtil:
    """
    Utility to connect to OpenIntel Zone Stream WebSocket and store messages to
    a local SQLite database.

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

    MAX_CALLBACK_WORKERS = 4
    STOP_TIMEOUT_SEC = 10.0
    TOPIC_TO_SCHEMA = {
        "newly_registered_fqdn": {
            "msg_timestamp":    float,
            "fqdn":             str,
            "cert_index":       int,
            "ct_name":          str,
            "timestamp":        int,
        },
        "newly_registered_domain": {
            "msg_timestamp":    float,
            "domain":           str,
            "cert_index":       int,
            "ct_name":          str,
            "timestamp":        int,
        },
        "confirmed_newly_registered_domain": {
            "msg_timestamp":    float,
            "domain":           str,
            "cert_index":       int,
            "ct_name":          str,
            "timestamp":        int,
            "confidence":       int,
        },
    }
    TOPIC_TO_CONSTRAINTS = {
        "newly_registered_fqdn": ["PRIMARY KEY (msg_timestamp, fqdn)"],
        "newly_registered_domain": ["PRIMARY KEY (msg_timestamp, domain)"],
        "confirmed_newly_registered_domain": [
            "PRIMARY KEY (msg_timestamp, domain)"
        ],
    }
    TOPIC_TO_INDICES = {
        "newly_registered_fqdn": [["fqdn"]],
        "newly_registered_domain": [["domain"]],
        "confirmed_newly_registered_domain": [["domain"]],
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
        invalid_topics = set(topics) - set(self.TOPIC_TO_SCHEMA.keys())
        if invalid_topics:
            raise ValueError(f"Invalid topics: {invalid_topics}")
        self.topics = topics

        # Concurrency
        self._stop_event = Event()
        self._receiver_thread: Optional[Thread] = None
        self._atexit_registered = False
        self._async_helper = AsyncHelper()
        self._child_stopped = Event()

        # IPC/process
        self._ctx = mp.get_context("spawn")
        self._cmd_queue: Optional[mp.Queue] = None   # parent -> child
        self._data_queue: Optional[mp.Queue] = None  # child -> parent
        self._proc: Optional[mp.Process] = None
        self._running = False

        # Callback
        self._callback = callback
        self._cb_pool = ThreadPoolExecutor(
            max_workers=self.MAX_CALLBACK_WORKERS,
            thread_name_prefix=f"{self.__class__.__name__}-cb"
        ) if callback is not None else None

        # Database
        self.db_path = self.io_helper.raw / "openintel_zone_stream.sqlite"
        self.db: Optional[SqliteDatabase] = SqliteDatabase(
            self.db_path,
            log_helper=self.io_helper.log_helper,
            offload_to_worker=True,
            write_buffering=True,
        )
        self.topic_tables: Optional[dict[str, SqliteTable]] = {
            topic: self.db.register_table(
                topic,
                schema=self.TOPIC_TO_SCHEMA[topic],
                table_constraints=self.TOPIC_TO_CONSTRAINTS[topic],
                indices=self.TOPIC_TO_INDICES[topic],
            ) for topic in self.topics
        }

    @staticmethod
    def _child_entry(
        topics: list[str],
        data_q: mp.Queue,
        cmd_q: mp.Queue,
        logging_config: Optional[LogConfig] = None,
    ):
        # Ignore SIGINT in child
        import signal
        signal.signal(signal.SIGINT, signal.SIG_IGN)

        OpenIntelZoneStreamWorker(
            topics,
            data_q,
            cmd_q,
            logging_config=logging_config,
        ).run()

    def start(self):
        """
        Start the Zone Stream listener.
        """

        if self._running:
            self.io_helper.logger.warning("Already running; start() ignored.")
            return

        self.io_helper.logger.info("Starting Zone Stream listener")
        self._stop_event.clear()
        self._child_stopped.clear()

        # Queues
        self._data_queue = self._ctx.Queue()
        self._cmd_queue = self._ctx.Queue()

        # Child process
        self.io_helper.logger.info("Starting child process")
        self._proc = self._ctx.Process(
            target=OpenIntelZoneStreamUtil._child_entry,
            name=f"{OpenIntelZoneStreamUtil.__name__}-child",
            args=(
                self.topics,
                self._data_queue,
                self._cmd_queue,
                self.io_helper.get_worker_logging_config(),
            ),
        )
        self._proc.start()

        # Receiver: drains queue into parent buffers
        self.io_helper.logger.info("Starting receiver thread")
        self._receiver_thread = Thread(
            target=self._recv_loop,
            name=f"{OpenIntelZoneStreamUtil.__name__}-receiver",
            daemon=True,
        )
        self._receiver_thread.start()

        if not self._atexit_registered:
            atexit.register(self._atexit_cleanup)
            self._atexit_registered = True

        self._running = True

        self.io_helper.logger.info(
            f"Started Zone Stream listener for topics: {self.topics}"
        )

    def stop(self):
        """
        Stop the Zone Stream listener and flush all caches to the database.
        """

        if not self._running:
            return
        self._running = False

        self.io_helper.logger.info("Stopping Zone Stream listener")

        # Ask child to stop
        try:
            if self._cmd_queue:
                self._async_helper.mpq_put_sync(
                    self._cmd_queue,
                    IpcMsg.notify(
                        OpenIntelZoneStreamWorker.SERVICE,
                        ZoneStreamMethod.STOP
                    ),
                    timeout=0.5
                )
        except Exception as exc:
            self.io_helper.logger.warning(
                f"Failed to signal child process to stop: {exc}"
            )

        if not self._child_stopped.wait(timeout=self.STOP_TIMEOUT_SEC):
            self.io_helper.logger.warning(
                "Timed out waiting for child process to stop"
            )

        self._stop_event.set()  # Signal receiver to stop

        # Join receiver thread
        self.io_helper.logger.info("Stopping receiver thread")
        if self._receiver_thread and self._receiver_thread.is_alive():
            self._receiver_thread.join(timeout=5.0)
        self._receiver_thread = None

        # Shut down callback pool
        try:
            if self._cb_pool is not None:
                self.io_helper.logger.info(
                    "Shutting down callback thread pool"
                )
                self._cb_pool.shutdown(wait=True)
                self._cb_pool = None
        except Exception:
            pass

        # Join child
        self.io_helper.logger.info("Waiting for child process to stop")
        if self._proc is not None:
            self._proc.join(timeout=self.STOP_TIMEOUT_SEC)
            if self._proc.is_alive():
                self.io_helper.logger.warning(
                    "Child unresponsive; terminating."
                )
                self._proc.terminate()
                self._proc.join(timeout=5.0)
        self._proc = None

        # Close DB
        try:
            if self.db is not None:
                self.io_helper.logger.info("Closing database")
                self.db.close()
        except Exception:
            pass
        self.db = None
        self.topic_tables = None
        self._cmd_queue = None
        self._data_queue = None

        self.io_helper.logger.info(
            "Stopped Zone Stream listener and flushed caches to database"
        )

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()

    def _atexit_cleanup(self):
        try:
            self.stop()
        except Exception:
            pass

    def _dispatch_cb(self, topic: str, batch: list[dict]) -> None:
        if not self._callback or not self._cb_pool or not batch:
            return

        def _run(cb=self._callback, t=topic, b=batch):
            try:
                cb(t, b)
            except Exception as e:
                self.io_helper.logger.error(f"Callback error: {e}")

        self._cb_pool.submit(_run)

    def _recv_loop(self):
        if self._data_queue is None:
            return

        while not self._stop_event.is_set():
            try:
                msg = self._async_helper.mpq_get_sync(
                    self._data_queue,
                    timeout=0.5,
                )
            except Exception:
                continue

            if not isinstance(msg, IpcMsg):
                self.io_helper.logger.warning(f"Invalid IPC message: {msg}")
                continue

            if msg.is_status:
                st = msg.status_code
                self.io_helper.logger.info(
                    f"Child status: {st.value if st else st}"
                )
                if st == IpcStatusCode.STOPPED:
                    self._child_stopped.set()
                    self._stop_event.set()
                    break  # Stop since child stopped
                continue

            if msg.service != OpenIntelZoneStreamWorker.SERVICE:
                self.io_helper.logger.warning(
                    f"Message for unknown service: {msg.service}"
                )
                continue

            if msg.is_notify:
                if ZoneStreamMethod.get_method(msg) == ZoneStreamMethod.BATCH:
                    topic_map = (msg.kwargs or {}).get("topic_map", {})
                    for topic, batch in topic_map.items():
                        if not batch:
                            continue

                        df = pd.DataFrame(batch)

                        # Missing columns -> NA
                        for col in self.TOPIC_TO_SCHEMA[topic].keys():
                            if col not in df.columns:
                                df[col] = pd.NA

                        # Sometimes integers are sent as raw bytes, fix them here
                        # (assume little-endian 64-bit)
                        for col, typ in self.TOPIC_TO_SCHEMA[topic].items():
                            if typ is int and col in df.columns:
                                mask_bytes = df[col].apply(
                                    lambda v: isinstance(v, bytes)
                                )
                                if mask_bytes.any():
                                    df.loc[mask_bytes, col] = df.loc[mask_bytes, col].apply(
                                        lambda b: int.from_bytes(b, "little")
                                    )
                                df[col] = pd.to_numeric(
                                    df[col], errors="coerce"
                                ).astype("Int64")

                        # Reorder
                        df = df[list(self.TOPIC_TO_SCHEMA[topic].keys())]
                        self.topic_tables[topic].insert_df(df)

                        # Dispatch callback if any
                        self._dispatch_cb(topic, batch)
