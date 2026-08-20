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
