from enum import Enum


class IpcMsgType(Enum):
    COMMAND = 1
    DATA = 2
    STATUS = 3
    LOG = 4


class IpcCommand(Enum):
    STOP = 1


class IpcStatus(Enum):
    STOPPED = 1
    READY = 2
