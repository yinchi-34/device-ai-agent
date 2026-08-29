import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReviewField:
    key: str
    label: str
    path: str
    value: Any
    display_value: str
    original_value: Any
    original_display_value: str
    changed: bool
    confidence: float | None
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ReviewItem:
    key: str
    title: str
    fields: tuple[ReviewField, ...]


@dataclass(frozen=True)
class ReviewSection:
    key: str
    label: str
    items: tuple[ReviewItem, ...]
    empty_message: str


_DEVICE_FIELDS = (
    ("existing_device_model_id", "已有型号 ID"),
    ("vendor_name", "厂商"),
    ("model_code", "型号编码"),
    ("model_name", "型号名称"),
    ("description", "型号说明"),
)

_PROFILE_FIELDS = (
    ("version", "版本"),
    ("profile_name", "Profile 名称"),
    ("description", "Profile 说明"),
)

_PROTOCOL_FIELDS = (
    ("candidate_ref", "内部引用"),
    ("code", "协议编码"),
    ("protocol", "协议类型"),
    ("display_name", "显示名称"),
    ("config_schema", "连接配置 Schema"),
)

_POINT_FIELDS = (
    ("candidate_ref", "内部引用"),
    ("protocol_ref", "协议引用"),
    ("code", "点位编码"),
    ("name", "点位名称"),
    ("description", "点位说明"),
    ("address_text", "原始地址"),
    ("object_type", "对象类型"),
    ("data_type", "数据类型"),
    ("access_mode", "访问模式"),
    ("unit", "单位"),
    ("address_spec", "地址配置"),
    ("value_schema", "值 Schema"),
    ("expected_rule", "预期规则"),
)

_CAPABILITY_FIELDS = (
    ("candidate_ref", "内部引用"),
    ("capability_code", "基础能力"),
    ("name_override", "名称覆盖"),
)

_BINDING_FIELDS = (
    ("capability_ref", "能力引用"),
    ("point_ref", "点位引用"),
    ("role", "语义角色"),
    ("alias", "别名"),
    ("is_required", "是否必需"),
)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _display(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if value is True:
        return "是"
    if value is False:
        return "否"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _sourced(value: Any) -> bool:
    return isinstance(value, dict) and (
        "value" in value or "evidence_refs" in value or "confidence" in value
    )


def _field(
    current: dict[str, Any],
    original: dict[str, Any],
    key: str,
    label: str,
    base_path: str,
    *,
    inherited_evidence: dict[str, Any] | None = None,
) -> ReviewField:
    current_raw = current.get(key)
    original_raw = original.get(key)
    is_sourced = _sourced(current_raw) or _sourced(original_raw)
    current_source = _object(current_raw) if is_sourced else {}
    original_source = _object(original_raw) if is_sourced else {}
    value = current_source.get("value") if is_sourced else current_raw
    original_value = (
        original_source.get("value") if is_sourced else original_raw
    )
    evidence_source = current_source if is_sourced else (inherited_evidence or {})
    raw_refs = evidence_source.get("evidence_refs", [])
    refs = tuple(ref for ref in raw_refs if isinstance(ref, str))
    confidence = evidence_source.get("confidence")
    if not isinstance(confidence, (int, float)):
        confidence = None
    path = f"{base_path}/{key}"
    if is_sourced:
        path += "/value"
    return ReviewField(
        key=key,
        label=label,
        path=path,
        value=value,
        display_value=_display(value),
        original_value=original_value,
        original_display_value=_display(original_value),
        changed=current_raw != original_raw,
        confidence=float(confidence) if confidence is not None else None,
        evidence_refs=refs,
    )


def _title(item: dict[str, Any], *keys: str, fallback: str) -> str:
    for key in keys:
        raw = item.get(key)
        value = _object(raw).get("value") if _sourced(raw) else raw
        if value not in (None, ""):
            return str(value)
    return fallback


def _identity(item: dict[str, Any], section: str) -> str:
    if section in {"protocols", "points", "capability_matches"}:
        return str(item.get("candidate_ref", ""))
    if section == "point_bindings":
        return "|".join(
            str(item.get(key, ""))
            for key in ("capability_ref", "point_ref", "role")
        )
    return ""


def _collection_section(
    key: str,
    label: str,
    empty_message: str,
    field_specs: tuple[tuple[str, str], ...],
    original_payload: dict[str, Any],
    reviewed_payload: dict[str, Any],
) -> ReviewSection:
    current_items = _items(reviewed_payload.get(key))
    original_by_identity = {
        _identity(item, key): item for item in _items(original_payload.get(key))
    }
    items = []
    for index, current in enumerate(current_items):
        identity = _identity(current, key)
        original = original_by_identity.get(identity, {})
        if key == "protocols":
            title = _title(current, "code", "candidate_ref", fallback=f"协议 {index + 1}")
        elif key == "points":
            title = _title(current, "name", "code", fallback=f"点位 {index + 1}")
        elif key == "capability_matches":
            title = _title(
                current, "capability_code", "candidate_ref", fallback=f"能力 {index + 1}"
            )
        else:
            title = _title(
                current,
                "role",
                fallback=f"{current.get('capability_ref', '能力')} → {current.get('point_ref', '点位')}",
            )
        evidence = current if key in {"capability_matches", "point_bindings"} else None
        fields = tuple(
            _field(
                current,
                original,
                field_key,
                field_label,
                f"/{key}/{index}",
                inherited_evidence=evidence,
            )
            for field_key, field_label in field_specs
            if field_key in current or field_key in original
        )
        items.append(ReviewItem(key=identity or str(index), title=title, fields=fields))
    return ReviewSection(
        key=key,
        label=label,
        items=tuple(items),
        empty_message=empty_message,
    )


def _single_section(
    key: str,
    label: str,
    field_specs: tuple[tuple[str, str], ...],
    original_payload: dict[str, Any],
    reviewed_payload: dict[str, Any],
) -> ReviewSection:
    current = _object(reviewed_payload.get(key))
    original = _object(original_payload.get(key))
    fields = tuple(
        _field(current, original, field_key, field_label, f"/{key}")
        for field_key, field_label in field_specs
        if field_key in current or field_key in original
    )
    title_keys = ("model_name", "model_code") if key == "device_model" else ("profile_name", "version")
    title = _title(current, *title_keys, fallback=label)
    return ReviewSection(
        key=key,
        label=label,
        items=(ReviewItem(key=key, title=title, fields=fields),),
        empty_message="",
    )


def present_candidate(
    original_payload: dict[str, Any], reviewed_payload: dict[str, Any]
) -> tuple[ReviewSection, ...]:
    original = _object(original_payload)
    reviewed = _object(reviewed_payload)
    return (
        _single_section("device_model", "设备型号", _DEVICE_FIELDS, original, reviewed),
        _single_section("profile", "Profile", _PROFILE_FIELDS, original, reviewed),
        _collection_section(
            "protocols", "协议", "未提取到协议", _PROTOCOL_FIELDS, original, reviewed
        ),
        _collection_section(
            "points", "点位", "未提取到点位", _POINT_FIELDS, original, reviewed
        ),
        _collection_section(
            "capability_matches",
            "能力匹配",
            "未匹配到基础能力",
            _CAPABILITY_FIELDS,
            original,
            reviewed,
        ),
        _collection_section(
            "point_bindings",
            "点位绑定",
            "未生成点位绑定",
            _BINDING_FIELDS,
            original,
            reviewed,
        ),
    )
