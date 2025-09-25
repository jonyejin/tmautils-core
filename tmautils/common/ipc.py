from enum import Enum
from logging import DEBUG, INFO, WARNING, ERROR, CRITICAL

from .types import *


class IpcMsgType(Enum):
    COMMAND = 1
    DATA = 2
    STATUS = 3
    LOG = 4

    @property
    def is_command(self) -> bool:
        return self == IpcMsgType.COMMAND

    @property
    def is_data(self) -> bool:
        return self == IpcMsgType.DATA

    @property
    def is_status(self) -> bool:
        return self == IpcMsgType.STATUS

    @property
    def is_log(self) -> bool:
        return self == IpcMsgType.LOG


class IpcCommand(Enum):
    STOP = 1

    @property
    def is_stop(self) -> bool:
        return self == IpcCommand.STOP


class IpcStatus(Enum):
    STOPPED = 1
    READY = 2

    @property
    def is_stopped(self) -> bool:
        return self == IpcStatus.STOPPED
    
    @property
    def is_ready(self) -> bool:
        return self == IpcStatus.READYi


class IpcMsg:
    def __init__(
        self,
        msg_type: IpcMsgType,
        value: Any,
    ):
        self.msg_type = msg_type
        self.value = value

    def __repr__(self):
        return f"IpcMsg(type={self.msg_type}, value={self.value})"

    @staticmethod
    def command(cmd: IpcCommand):
        if not isinstance(cmd, IpcCommand):
            raise ValueError("Command must be an instance of IpcCommand")
        return IpcMsg(IpcMsgType.COMMAND, cmd)

    @staticmethod
    def data(data: tuple):
        if not isinstance(data, tuple):
            raise ValueError("Data must be a tuple")
        return IpcMsg(IpcMsgType.DATA, data)

    @staticmethod
    def status(status: IpcStatus):
        if not isinstance(status, IpcStatus):
            raise ValueError("Status must be an instance of IpcStatus")
        return IpcMsg(IpcMsgType.STATUS, status)

    @staticmethod
    def log(level: int, message: str):
        if level not in (DEBUG, INFO, WARNING, ERROR, CRITICAL):
            raise ValueError("Invalid log level")
        return IpcMsg(IpcMsgType.LOG, (level, message))

    @property
    def is_command(self) -> bool:
        return self.msg_type.is_command

    def get_command(self) -> IpcCommand:
        if not self.is_command:
            raise ValueError("Message is not a command")
        return self.value

    @property
    def is_data(self) -> bool:
        return self.msg_type.is_data

    def get_data(self) -> tuple[Any, ...]:
        if not self.is_data:
            raise ValueError("Message is not data")
        return self.value

    @property
    def is_status(self) -> bool:
        return self.msg_type.is_status

    def get_status(self) -> IpcStatus:
        if not self.is_status:
            raise ValueError("Message is not a status")
        return self.value

    @property
    def is_log(self) -> bool:
        return self.msg_type.is_log

    def get_log(self) -> tuple[int, str]:
        if not self.is_log:
            raise ValueError("Message is not a log")
        return self.value
