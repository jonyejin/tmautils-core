from typing import Any, Dict, Optional, Union
from enum import StrEnum
from dataclasses import dataclass, field, asdict
import uuid
import time
import os


class IpcMsgType(StrEnum):
    REQUEST = "request"
    RESPONSE = "response"
    NOTIFY = "notify"
    STATUS = "status"
    LOG = "log"


class IpcStatusCode(StrEnum):
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    STOPPING = "stopping"
    STOPPED = "stopped"


class IpcMethodBase:
    """
    Inherit from this class to define IPC methods.
    This base class provides `get_method` to convert method names back to enum members.

    Example:
    ```
    class MyIpcMethods(IpcMethodBase, StrEnum):
        DO_SOMETHING = "do_something"
        GET_STATUS = "get_status"

    method = MyIpcMethods.get_method("do_something") # returns MyIpcMethods.DO_SOMETHING
    ```
    """

    @classmethod
    def get_method(cls, msg: "IpcMsg"):
        try:
            return cls(msg.method)
        except ValueError:
            return None


@dataclass
class IpcMsg:
    """
    Represents a generic IPC message.

    Instead of using the constructor directly, use the following methods to create messages:
    - `IpcMsg.request(...)`
    - `IpcMsg.response(...)` (or `.respond_with(...)` on a request message)
    - `IpcMsg.notify(...)`
    - `IpcMsg.status(...)`
    - `IpcMsg.log(...)`
    """

    # Common fields
    version: int
    typ: IpcMsgType
    msg_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: int = field(default_factory=time.time_ns)
    sender_pid: int = field(default_factory=os.getpid)

    # RPC metadata
    req_id: Optional[int] = None
    service: Optional[str] = None
    method: Optional[str] = None
    kwargs: Optional[Dict[str, Any]] = None

    # RESPONSE
    ok: Optional[bool] = None
    result: Any = None
    error: Optional[dict[str, str]] = None

    # STATUS
    status_code: Optional[IpcStatusCode] = None

    # LOG
    level: Optional[int] = None
    message: Optional[str] = None

    @staticmethod
    def _encode_method(method: Any):
        if isinstance(method, str):
            return method
        val = getattr(method, "value", None)
        if isinstance(val, str):
            return val
        raise TypeError(
            "method must be a str or an enum with a string 'value' member."
        )

    @staticmethod
    def request(req_id: int, service: str, method: Union[str, IpcMethodBase], **kwargs):
        return IpcMsg(
            version=1, typ=IpcMsgType.REQUEST,
            req_id=req_id, service=service,
            method=IpcMsg._encode_method(method),
            kwargs=kwargs or {}
        )

    @staticmethod
    def response(
        req_id: int, service: str, method: Union[str, IpcMethodBase], ok: bool,
        *,
        result: Any = None, error: Optional[Exception] = None,
    ):
        return IpcMsg(
            version=1, typ=IpcMsgType.RESPONSE,
            req_id=req_id, service=service,
            method=IpcMsg._encode_method(method),
            ok=ok, result=result,
            error=except_to_payload(error) if error else None,
        )

    def respond_with(
        self, ok: bool,
        *,
        result: Any = None, error: Optional[Exception] = None,
    ):
        if self.typ != IpcMsgType.REQUEST or self.req_id is None:
            raise ValueError(
                "Can only respond to a REQUEST message with a req_id"
            )
        return IpcMsg.response(
            req_id=self.req_id,
            service=self.service or "",
            method=self.method or "",
            ok=ok, result=result, error=error,
        )

    @staticmethod
    def notify(service: str, method: Union[str, IpcMethodBase], **kwargs):
        return IpcMsg(
            version=1, typ=IpcMsgType.NOTIFY,
            service=service, method=IpcMsg._encode_method(method),
            kwargs=kwargs or {}
        )

    @staticmethod
    def status(code: IpcStatusCode):
        return IpcMsg(
            version=1, typ=IpcMsgType.STATUS,
            status_code=code,
        )

    @staticmethod
    def log(level: int, message: str):
        return IpcMsg(
            version=1, typ=IpcMsgType.LOG,
            level=level, message=message
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "IpcMsg":
        # Only keep fields that are defined in IpcMsg
        fields = {f.name for f in IpcMsg.__dataclass_fields__.values()}
        payload = {k: v for k, v in d.items() if k in fields}
        return IpcMsg(**payload)

    @property
    def event(self) -> Optional[str]: return self.method

    @property
    def payload(self) -> Optional[Dict[str, Any]]: return self.kwargs

    @property
    def is_request(self) -> bool: return self.typ == IpcMsgType.REQUEST

    @property
    def is_response(self) -> bool: return self.typ == IpcMsgType.RESPONSE

    @property
    def is_notify(self) -> bool: return self.typ == IpcMsgType.NOTIFY

    @property
    def is_status(self) -> bool: return self.typ == IpcMsgType.STATUS

    @property
    def is_log(self) -> bool: return self.typ == IpcMsgType.LOG


def except_to_payload(e: Exception):
    """
    Convert an exception to a serializable dictionary that can be sent over IPC.

    See `raise_from_payload` to convert back to an exception.
    """

    import traceback

    return {
        "exc_module": e.__class__.__module__,
        "exc_type": e.__class__.__name__,
        "message": str(e),
        "traceback": traceback.format_exc(),
    }


def raise_from_payload(payload: dict):
    """
    Reconstruct and raise an exception from a payload dictionary created by `except_to_payload`.
    """

    import importlib

    mod = payload.get("exc_module", "")
    typ = payload.get("exc_type", "RuntimeError")
    msg = payload.get("message", "")
    tb = payload.get("traceback", "")
    try:
        exc_mod = importlib.import_module(mod) if mod else None
        exc_cls = (
            getattr(exc_mod, typ) if exc_mod and hasattr(exc_mod, typ)
            else RuntimeError
        )
    except Exception:
        exc_cls = RuntimeError
    raise exc_cls(f"{msg}\nRemote traceback:\n{tb}")
