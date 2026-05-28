import redis
import uuid
from datetime import datetime, timezone


_r = None
_stream = None


def init(redis_url: str, stream: str):
    global _r, _stream
    _r = redis.from_url(redis_url, decode_responses=True)
    _stream = stream


def push_signal(signal_type: str, alpha_id: str, **kwargs) -> None:
    data = {
        "type": signal_type,
        "alpha_id": alpha_id,
        "signal_id": kwargs.pop("signal_id", str(uuid.uuid4())),
        "timestamp": kwargs.pop("timestamp", datetime.now(timezone.utc).isoformat()),
    }
    for k, v in kwargs.items():
        if v is None:
            continue
        data[k] = str(v) if not isinstance(v, str) else v

    _r.xadd(_stream, data)
