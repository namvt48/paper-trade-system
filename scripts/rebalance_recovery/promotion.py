"""Approval guards used before an operator can promote validated candidates."""

from __future__ import annotations

import hmac
import json
import os
import shutil
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import assert_never, cast

import redis

from .merge import DatabaseMergeError, merge_equity_database, merge_main_database
from .storage import JsonValue, database_guard, sha256_file, sqlite_snapshot


class ApprovalMismatchError(Exception):
    """The supplied approval hash does not authorize the staged manifest."""


def verify_approval(expected_hash: str, supplied_hash: str) -> None:
    """Require an exact constant-time manifest hash match before promotion."""
    if not hmac.compare_digest(expected_hash, supplied_hash):
        raise ApprovalMismatchError(
            "approval hash does not match the validated recovery manifest"
        )


class PromotionError(Exception):
    """A promotion guard failed before or during the guarded merge."""


def promote_validated_workspace(
    workspace: Path,
    production_main: Path,
    production_equity: Path,
    redis_url: str,
    approval_hash: str,
    services_stopped: bool,
) -> None:
    """Merge validated candidates and Redis state with rollback backups."""
    if not services_stopped:
        raise PromotionError(
            "promotion requires an explicit services-stopped acknowledgement"
        )
    manifest_path = workspace / "manifest.json"
    verify_approval(sha256_file(manifest_path), approval_hash)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not bool(manifest.get("validation_passed")):
        raise PromotionError("validation report did not pass")
    candidate_main = workspace / "candidate-paper-trade.db"
    candidate_equity = workspace / "candidate-equity-snapshots.db"
    redis_state_path = workspace / "candidate-redis-state.json"
    _verify_artifact(manifest, "candidate_main", candidate_main)
    _verify_artifact(manifest, "candidate_equity", candidate_equity)
    _verify_artifact(manifest, "redis_state", redis_state_path)
    if list(database_guard(production_main)) != [
        tuple(item) for item in manifest["source_guards"]["main"]
    ]:
        raise PromotionError("production main DB drifted after staging")
    if list(database_guard(production_equity)) != [
        tuple(item) for item in manifest["source_guards"]["equity"]
    ]:
        raise PromotionError("production equity DB drifted after staging")
    backup_main = workspace / "promotion-backup-paper-trade.db"
    backup_equity = workspace / "promotion-backup-equity-snapshots.db"
    sqlite_snapshot(production_main, backup_main)
    sqlite_snapshot(production_equity, backup_equity)
    redis_state = _parse_redis_state(
        json.loads(redis_state_path.read_text(encoding="utf-8"))
    )
    affected_alphas = tuple(
        sorted(key.removeprefix("runner:positions:") for key in redis_state)
    )
    client = redis.Redis.from_url(
        redis_url,
        socket_timeout=10.0,
        socket_connect_timeout=5.0,
        decode_responses=False,
    )
    with client:
        previous: dict[str, bytes | None] = {}
        for key in redis_state:
            raw = cast(bytes | str | None, cast(object, client.get(key)))
            match raw:
                case None:
                    previous[key] = None
                case bytes():
                    previous[key] = raw
                case str():
                    previous[key] = raw.encode("utf-8")
                case unreachable:
                    assert_never(unreachable)
        try:
            merge_main_database(
                production_main,
                workspace / "baseline-paper-trade.db",
                candidate_main,
                affected_alphas,
            )
            merge_equity_database(
                production_equity,
                workspace / "baseline-equity-snapshots.db",
                candidate_equity,
            )
            for key, positions in redis_state.items():
                if positions:
                    client.set(
                        key,
                        json.dumps(positions, separators=(",", ":"), sort_keys=True),
                    )
                else:
                    client.delete(key)
        except (OSError, sqlite3.Error, redis.RedisError, DatabaseMergeError) as exc:
            _atomic_copy(backup_main, production_main)
            _atomic_copy(backup_equity, production_equity)
            for key, value in previous.items():
                if value is None:
                    client.delete(key)
                else:
                    client.set(key, value)
            raise PromotionError(f"promotion rolled back: {exc}") from exc


def _verify_artifact(manifest: Mapping[str, JsonValue], name: str, path: Path) -> None:
    """Reject any candidate modified after the validation report was produced."""
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, dict):
        raise PromotionError("manifest artifacts section is invalid")
    expected = artifacts[name]
    if not isinstance(expected, str):
        raise PromotionError(f"manifest artifact hash is invalid: {name}")
    actual = sha256_file(path)
    if not hmac.compare_digest(expected, actual):
        raise PromotionError(f"artifact hash mismatch: {name}")


def _parse_redis_state(value: JsonValue) -> dict[str, dict[str, JsonValue]]:
    """Accept only runner position keys and object payloads for promotion."""
    if not isinstance(value, dict):
        raise PromotionError("candidate Redis state must be an object")
    state: dict[str, dict[str, JsonValue]] = {}
    for key, positions in value.items():
        if not key.startswith("runner:positions:") or not isinstance(positions, dict):
            raise PromotionError(f"invalid candidate Redis state entry: {key}")
        state[key] = positions
    return state


def _atomic_copy(source: Path, destination: Path) -> None:
    """Fsync a same-directory temporary copy before atomically replacing a DB."""
    temporary = destination.with_suffix(destination.suffix + ".recovery-tmp")
    shutil.copy2(source, temporary)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    Path(f"{destination}-wal").unlink(missing_ok=True)
    Path(f"{destination}-shm").unlink(missing_ok=True)
    os.replace(temporary, destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
