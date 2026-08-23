from enum import StrEnum


class ModelStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class CapabilityKind(StrEnum):
    OBSERVE = "observe"
    CONTROL = "control"
    DIAGNOSTIC = "diagnostic"
    CONFIGURATION = "configuration"


class ProfileStatus(StrEnum):
    DRAFT = "draft"
    RELEASED = "released"
    DEPRECATED = "deprecated"


class ProtocolType(StrEnum):
    SIMULATOR = "simulator"
    MODBUS_TCP = "modbus_tcp"
    MODBUS_RTU = "modbus_rtu"
    BACNET_IP = "bacnet_ip"
    MQTT = "mqtt"
    OPCUA = "opcua"


class DataType(StrEnum):
    BOOL = "bool"
    UINT16 = "uint16"
    INT16 = "int16"
    UINT32 = "uint32"
    INT32 = "int32"
    FLOAT32 = "float32"
    STRING = "string"
    UNIX_TS = "unix_ts"


class AccessMode(StrEnum):
    R = "R"
    W = "W"
    RW = "RW"


class PointRole(StrEnum):
    CONTROL = "control"
    STATUS = "status"
    VERIFY = "verify"
    MEASUREMENT = "measurement"
    INPUT = "input"
    OUTPUT = "output"
