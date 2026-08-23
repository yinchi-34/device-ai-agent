import struct
from decimal import Decimal
from typing import Any

from app.modules.device.codecs import get_register_codec
from app.modules.device.enums import DataType


class DecodeError(ValueError):
    pass


def decode_register_words(
    words: list[int],
    *,
    data_type: str,
    word_order: str = "big",
    byte_order: str = "big",
    scale: float | Decimal = 1,
    offset: float | Decimal = 0,
    expected_rule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Decode exactly as configured; never search alternate interpretations."""
    registered = get_register_codec(data_type)
    if registered is None:
        raise DecodeError(f"unsupported data type: {data_type}")
    normalized_data_type, codec = registered
    required_words, format_code = codec.words, codec.struct_format
    if len(words) != required_words:
        raise DecodeError(
            f"{normalized_data_type} requires {required_words} register words"
        )
    if word_order not in {"big", "little"}:
        raise DecodeError("word_order must be 'big' or 'little'")
    if byte_order not in {"big", "little"}:
        raise DecodeError("byte_order must be 'big' or 'little'")
    if any(not isinstance(word, int) or not 0 <= word <= 0xFFFF for word in words):
        raise DecodeError("register words must be integers between 0 and 65535")

    ordered_words = words if word_order == "big" else list(reversed(words))
    chunks = [word.to_bytes(2, byteorder="big") for word in ordered_words]
    if byte_order == "little":
        chunks = [chunk[::-1] for chunk in chunks]
    payload = b"".join(chunks)
    prefix = ">" if byte_order == "big" else "<"
    raw_value = struct.unpack(prefix + format_code, payload)[0]
    if normalized_data_type == DataType.BOOL.value:
        raw_value = bool(raw_value)
    scale_value = float(scale)
    offset_value = float(offset)
    engineering_value = float(raw_value) * scale_value + offset_value

    rule = expected_rule or {}
    minimum = rule.get("minimum", rule.get("min"))
    maximum = rule.get("maximum", rule.get("max"))
    within_expected_range: bool | None = None
    if minimum is not None or maximum is not None:
        within_expected_range = (
            minimum is None or engineering_value >= float(minimum)
        ) and (maximum is None or engineering_value <= float(maximum))

    return {
        "raw_words": words,
        "raw_value": raw_value,
        "engineering_value": engineering_value,
        "unit": rule.get("unit"),
        "within_expected_range": within_expected_range,
        "applied_config": {
            "data_type": normalized_data_type,
            "word_order": word_order,
            "byte_order": byte_order,
            "scale": scale_value,
            "offset": offset_value,
        },
    }
