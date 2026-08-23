from dataclasses import dataclass

from app.modules.device.enums import DataType, ProtocolType


@dataclass(frozen=True)
class RegisterCodec:
    words: int
    struct_format: str


DATA_TYPE_ALIASES: dict[str, str] = {
    "bit": DataType.BOOL.value,
    "bool": DataType.BOOL.value,
    "boolean": DataType.BOOL.value,
    "float": DataType.FLOAT32.value,
    "float32": DataType.FLOAT32.value,
    "long": DataType.INT32.value,
    "int16": DataType.INT16.value,
    "int32": DataType.INT32.value,
    "string": DataType.STRING.value,
    "uint16": DataType.UINT16.value,
    "uint32": DataType.UINT32.value,
    "unix_ts": DataType.UNIX_TS.value,
    "unix_ts_utc": DataType.UNIX_TS.value,
    "unsigned_long": DataType.UINT32.value,
    "unsigned_short": DataType.UINT16.value,
}

REGISTER_CODEC_REGISTRY: dict[DataType, RegisterCodec] = {
    DataType.BOOL: RegisterCodec(1, "H"),
    DataType.UINT16: RegisterCodec(1, "H"),
    DataType.INT16: RegisterCodec(1, "h"),
    DataType.UINT32: RegisterCodec(2, "I"),
    DataType.INT32: RegisterCodec(2, "i"),
    DataType.FLOAT32: RegisterCodec(2, "f"),
    DataType.UNIX_TS: RegisterCodec(2, "I"),
}

REGISTER_PROTOCOLS = {
    ProtocolType.MODBUS_TCP.value,
    ProtocolType.MODBUS_RTU.value,
}


def normalize_data_type(value: object) -> str | None:
    token = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    normalized = DATA_TYPE_ALIASES.get(token, token)
    try:
        return DataType(normalized).value
    except ValueError:
        return None


def get_register_codec(value: object) -> tuple[str, RegisterCodec] | None:
    normalized = normalize_data_type(value)
    if normalized is None:
        return None
    codec = REGISTER_CODEC_REGISTRY.get(DataType(normalized))
    return (normalized, codec) if codec is not None else None


def protocol_supports_data_type(protocol: object, data_type: object) -> bool:
    normalized = normalize_data_type(data_type)
    if normalized is None:
        return False
    if str(protocol) in REGISTER_PROTOCOLS:
        return DataType(normalized) in REGISTER_CODEC_REGISTRY
    return True
