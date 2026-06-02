try:
    import orjson
except ImportError:  # pragma: no cover - exercised when optional dep is absent
    orjson = None

import json
from typing import Any


def dumps(value: Any) -> str:
    if orjson is not None:
        return orjson.dumps(value).decode("utf-8")
    return json.dumps(value)


def loads(value: str | bytes | bytearray) -> Any:
    if orjson is not None:
        return orjson.loads(value)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value)
