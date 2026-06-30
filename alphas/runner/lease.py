from __future__ import annotations


class LeaseManager:
    def __init__(self, redis_client, runner_id: str, ttl_sec: int = 20):
        self.redis_client = redis_client
        self.runner_id = runner_id
        self.ttl_sec = int(ttl_sec)

    def key(self, alpha_id: str) -> str:
        return f"runner:alpha:lease:{alpha_id}"

    def acquire(self, alpha_id: str) -> bool:
        return bool(self.redis_client.set(self.key(alpha_id), self.runner_id, nx=True, ex=self.ttl_sec))

    def renew(self, alpha_id: str) -> bool:
        if hasattr(self.redis_client, "eval"):
            result = self.redis_client.eval(
                """
                if redis.call('GET', KEYS[1]) == ARGV[1] then
                    return redis.call('EXPIRE', KEYS[1], ARGV[2])
                end
                return 0
                """,
                1,
                self.key(alpha_id),
                self.runner_id,
                self.ttl_sec,
            )
            return bool(result)
        if self.redis_client.get(self.key(alpha_id)) != self.runner_id:
            return False
        return bool(self.redis_client.expire(self.key(alpha_id), self.ttl_sec))

    def release(self, alpha_id: str) -> bool:
        if hasattr(self.redis_client, "eval"):
            result = self.redis_client.eval(
                """
                if redis.call('GET', KEYS[1]) == ARGV[1] then
                    return redis.call('DEL', KEYS[1])
                end
                return 0
                """,
                1,
                self.key(alpha_id),
                self.runner_id,
            )
            return bool(result)
        if self.redis_client.get(self.key(alpha_id)) != self.runner_id:
            return False
        return bool(self.redis_client.delete(self.key(alpha_id)))

    def is_valid(self, alpha_id: str) -> bool:
        return self.redis_client.get(self.key(alpha_id)) == self.runner_id
