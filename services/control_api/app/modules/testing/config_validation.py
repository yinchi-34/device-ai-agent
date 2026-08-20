from typing import Any

from jsonschema import Draft202012Validator


def validate_connection_config(
    schema: dict[str, Any], config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Pure JSON Schema structural validation. Never opens sockets."""
    if not schema:
        return []
    validator = Draft202012Validator(schema)
    return [
        {"path": list(error.path), "message": error.message}
        for error in sorted(validator.iter_errors(config), key=lambda e: list(e.path))
    ]
