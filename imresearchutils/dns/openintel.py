from websocket import WebSocketApp
import pandas as pd
from threading import Thread, Event, Lock
import rel
import atexit
import multiprocessing as mp
from multiprocessing.queues import Queue
import logging
import time

from imresearchutils.common import *


class OpenIntelZoneStreamUtil:

    WS_URL = "wss://zonestream.openintel.nl/ws/{topic}"
    WRITE_INTERVAL_SEC = 10
    RECONNECT_INTERVAL_SEC = 5
    TOPIC_TO_SCHEMA = {
        "newly_registered_fqdn": {
            "msg_timestamp":    pd.Timestamp,
            "fqdn":             str,
            "cert_index":       int,
            "ct_name":          str,
            "timestamp":        int,
        },
        "newly_registered_domain": {
            "msg_timestamp":    pd.Timestamp,
            "domain":           str,
            "cert_index":       int,
            "ct_name":          str,
            "timestamp":        int,
        },
        "confirmed_newly_registered_domain": {
            "msg_timestamp":    pd.Timestamp,
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
        self._lock = Lock()
        self._stop_event = Event()
        self._receiver_thread: Optional[Thread] = None
        self._writer_thread: Optional[Thread] = None
        self._atexit_registered = False

        # IPC/process
        self._ctx = mp.get_context("spawn")
        self._cmd_queue: Optional[Queue] = None   # parent -> child
        self._data_queue: Optional[Queue] = None  # child -> parent
        self._proc: Optional[mp.Process] = None

        # Parent-side buffers
        self._running = False
        self.message_cache: dict[str, list[dict]] = {
            t: [] for t in self.topics
        }

        # Database
        self.db_path = self.io_helper.raw / "openintel_zone_stream.sqlite"
        self.db: Optional[SqliteDatabase] = None
        self.topic_tables: Optional[dict[str, SqliteTable]] = None

    @staticmethod
    def _websocket_listener(
        topics: list[str],
        ws_url: str,
        reconnect_interval_sec: int,
        data_queue: Queue,
        cmd_queue: Queue,
    ):
        from collections import deque
        from queue import Empty
        import time
        import json
        import pandas as pd
        import logging
        import traceback

        from imresearchutils.common import IpcMsgType, IpcCommand

        ws_map = {}
        send_q = deque()
        running = True

        BATCH_MAX = 1000    # max msgs per IPC batch
        SENDER_IDLE_SEC = 0.1   # sleep when queue empty
        PUT_TIMEOUT_SEC = 0.1   # queue put timeout

        def _safe_put(obj) -> bool:
            try:
                data_queue.put(obj, timeout=PUT_TIMEOUT_SEC)
                return True
            except Exception:
                return False

        def _log(level, text):   _safe_put((IpcMsgType.LOG, level, text))
        def _status(st):         _safe_put((IpcMsgType.STATUS, st, None))

        def _sender_loop():
            nonlocal running
            while running or send_q:
                if not send_q:
                    # Nothing to send
                    time.sleep(SENDER_IDLE_SEC)
                    continue
                topic_map = {}
                total = 0
                while send_q and total < BATCH_MAX:
                    topic, msg = send_q.popleft()
                    topic_map.setdefault(topic, []).append(msg)
                    total += 1
                if not _safe_put((IpcMsgType.DATA, topic_map)):
                    # Something went wrong, wait and retry
                    time.sleep(SENDER_IDLE_SEC)

        def _check_cmd():
            nonlocal running
            try:
                while True:
                    cmd = cmd_queue.get_nowait()
                    if isinstance(cmd, tuple) and (
                        cmd[0] == IpcMsgType.COMMAND and cmd[1] == IpcCommand.STOP
                    ):
                        running = False
                        for ws in list(ws_map.values()):
                            try:
                                ws.close()
                            except Exception:
                                pass
                        try:
                            rel.abort()
                        except Exception:
                            pass
                        return False  # Do not reschedule as we are stopping
                    _log(logging.WARNING, f"Unknown command: {cmd}")
            except Empty:
                # Nothing in queue
                return True
            except (EOFError, BrokenPipeError):
                # Parent process gone, exit
                running = False
                try:
                    rel.abort()
                except Exception:
                    pass
                return False

        def _on_open_factory(topic):
            def _on_open(ws):
                _log(logging.INFO, f"[{topic}] WebSocket opened: {ws.url}")
            return _on_open

        def _on_message_factory(topic):
            def _on_message(ws, message: str):
                try:
                    msg = json.loads(message)
                except json.JSONDecodeError as e:
                    _log(logging.ERROR, f"[{topic}] JSON decode error: {e}")
                    return
                msg["msg_timestamp"] = pd.Timestamp.now(tz="UTC")
                send_q.append((topic, msg))
            return _on_message

        def _on_error_factory(topic):
            def _on_error(ws, error: Exception):
                _log(logging.ERROR, f"[{topic}] WebSocket error: {error}")
            return _on_error

        def _on_close_factory(topic):
            def _on_close(ws, code, msg):
                _log(
                    logging.INFO,
                    f"[{topic}] WebSocket closed, code={code}, msg={msg}"
                )
            return _on_close

        sender = None
        try:
            for topic in topics:
                ws = WebSocketApp(
                    ws_url.format(topic=topic),
                    on_open=_on_open_factory(topic),
                    on_message=_on_message_factory(topic),
                    on_error=_on_error_factory(topic),
                    on_close=_on_close_factory(topic),
                )
                ws_map[topic] = ws
                ws.run_forever(
                    dispatcher=rel,
                    reconnect=reconnect_interval_sec
                )

            # Sender thread
            sender = Thread(
                target=_sender_loop,
                name="zone-stream-child-sender",
                daemon=True,
            )
            sender.start()

            # Stop checker
            rel.timeout(1, _check_cmd)

            _status(IpcStatus.READY)
            rel.dispatch()
        except Exception as e:
            _log(
                logging.ERROR,
                f"Child rel.dispatch crashed: {e}\n{traceback.format_exc()}"
            )
        finally:
            try:
                running = False
                if sender and sender.is_alive():
                    sender.join(timeout=3.0)
            except Exception:
                pass
            _status(IpcStatus.STOPPED)

    def start(self):
        if self._running:
            self.io_helper.logger.warning("Already running; start() ignored.")
            return

        self.io_helper.logger.info("Starting Zone Stream listener")
        self._stop_event.clear()

        # Queues
        self._data_queue = self._ctx.Queue()
        self._cmd_queue = self._ctx.Queue()

        # Child process
        self.io_helper.logger.info("Starting child process")
        self._proc = self._ctx.Process(
            target=OpenIntelZoneStreamUtil._websocket_listener,
            name=f"{OpenIntelZoneStreamUtil.__name__}-child",
            args=(
                self.topics,
                self.WS_URL,
                self.RECONNECT_INTERVAL_SEC,
                self._data_queue,
                self._cmd_queue,
            ),
            daemon=True,
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

        # Writer: writes buffers to DB every WRITE_INTERVAL_SEC
        self.io_helper.logger.info("Starting writer thread")
        self._writer_thread = Thread(
            target=self._writer_loop,
            name=f"{OpenIntelZoneStreamUtil.__name__}-writer",
            daemon=True,
        )
        self._writer_thread.start()

        if not self._atexit_registered:
            atexit.register(self._atexit_cleanup)
            self._atexit_registered = True

        self._running = True
        self.io_helper.logger.info(
            f"Started Zone Stream listener for topics: {self.topics}"
        )

    def stop(self):
        if not self._running:
            return
        self._running = False

        self.io_helper.logger.info("Stopping Zone Stream listener")

        # Ask child to stop
        try:
            if self._cmd_queue:
                self._cmd_queue.put(
                    (IpcMsgType.COMMAND, IpcCommand.STOP),
                    timeout=0.5
                )
        except Exception:
            pass

        # Join receiver thread
        self.io_helper.logger.info("Stopping receiver thread")
        if self._receiver_thread and self._receiver_thread.is_alive():
            self._receiver_thread.join(timeout=5.0)

        # Signal writer thread to stop
        self._stop_event.set()

        # Join writer thread
        self.io_helper.logger.info("Stopping writer thread")
        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=self.WRITE_INTERVAL_SEC + 5.0)

        # Join child
        self.io_helper.logger.info("Waiting for child process to stop")
        if self._proc is not None:
            self._proc.join(timeout=10.0)
            if self._proc.is_alive():
                self.io_helper.logger.warning(
                    "Child unresponsive; terminating."
                )
                self._proc.terminate()
                self._proc.join(timeout=5.0)

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

    def _recv_loop(self):
        if self._data_queue is None:
            return

        while not self._stop_event.is_set():
            try:
                msg = self._data_queue.get(timeout=1.0)
            except Exception:
                continue

            if not isinstance(msg, tuple) or not msg:
                continue

            typ = msg[0]
            if typ == IpcMsgType.DATA:
                _, topic_map = msg
                with self._lock:
                    for topic, batch in topic_map.items():
                        if topic in self.message_cache and batch:
                            self.message_cache[topic].extend(batch)
            elif typ == IpcMsgType.LOG:
                _, level, text = msg
                level = level or logging.INFO
                self.io_helper.logger.log(level, text)
            elif typ == IpcMsgType.STATUS:
                _, st, _ = msg
                if not isinstance(st, IpcStatus):
                    self.io_helper.logger.warning(f"Unknown status: {st}")
                    continue
                self.io_helper.logger.info(f"Child status: {st.name}")
                if st == IpcStatus.STOPPED:
                    break  # Stop since child stopped

    def _writer_loop(self):
        self.db = SqliteDatabase(self.db_path, logger=self.io_helper.logger)
        self.topic_tables = {
            topic: self.db.register_table(
                topic,
                schema=self.TOPIC_TO_SCHEMA[topic],
                table_constraints=self.TOPIC_TO_CONSTRAINTS[topic],
                indices=self.TOPIC_TO_INDICES[topic],
            ) for topic in self.topics
        }

        try:
            while not self._stop_event.is_set():
                time.sleep(self.WRITE_INTERVAL_SEC)
                self._flush_all()

            # Final flush on shutdown
            self._flush_all()
        finally:
            self.db.close()

    def _write_to_db(self, topic: str):
        with self._lock:
            cache = self.message_cache.get(topic, [])
            if not cache:
                return

            df = pd.DataFrame(cache)
            # Ensure all schema columns exist; missing -> NA
            for col in self.TOPIC_TO_SCHEMA[topic].keys():
                if col not in df.columns:
                    df[col] = pd.NA
            # Reorder
            df = df[list(self.TOPIC_TO_SCHEMA[topic].keys())]

            self.topic_tables[topic].insert_df(df, if_exists="append")
            self.message_cache[topic] = []

    def _flush_all(self):
        for t in list(self.topics):
            try:
                self._write_to_db(t)
            except Exception as e:
                self.io_helper.logger.error(f"[{t}] flush failed: {e}")
