#!/usr/bin/env python3
"""Audit every alphas/*/whitelist.txt against Binance's current tradable universe.

Flags symbols that are no longer eligible for live MDS streaming (delisted,
settling, or otherwise off status=TRADING) but still linger in an alpha's
static whitelist.txt -- exactly the gap that let 1d-iamp re-open TONUSDT/IPUSDT
weeks after Binance moved them to SETTLING.

Mirrors the filter in market-data-service/app/adapters/binance/adapter.py::fetch_symbols.
Keep both in sync if that filter changes.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import requests

TRADABLE_CONTRACT_TYPES = {"PERPETUAL", "TRADIFI_PERPETUAL"}

ALPHAS_ROOT = Path(__file__).resolve().parents[1] / "alphas"


def fetch_tradable_symbols(rest_base: str) -> set[str]:
    resp = requests.get(f"{rest_base}/fapi/v1/exchangeInfo", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return {
        item["symbol"]
        for item in data.get("symbols", [])
        if item.get("quoteAsset") == "USDT"
        and item.get("contractType") in TRADABLE_CONTRACT_TYPES
        and item.get("status") == "TRADING"
    }


def load_whitelist(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def audit(alphas_root: Path, tradable: set[str]) -> dict[str, list[str]]:
    stale: dict[str, list[str]] = {}
    for whitelist_path in sorted(alphas_root.glob("*/whitelist.txt")):
        alpha_id = whitelist_path.parent.name
        symbols = load_whitelist(whitelist_path)
        offenders = [s for s in symbols if s.upper() not in tradable]
        if offenders:
            stale[alpha_id] = offenders
    return stale


def fix_whitelist(path: Path, offenders: set[str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [
        line for line in lines
        if not (line.strip() and not line.lstrip().startswith("#") and line.strip().upper() in offenders)
    ]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="Remove stale symbols from whitelist.txt files in place")
    parser.add_argument("--alphas-dir", default=str(ALPHAS_ROOT), help="Path to the alphas/ directory")
    parser.add_argument(
        "--rest-base",
        default=os.getenv("BINANCE_REST_BASE_URL") or "https://fapi.binance.com",
        help="Binance futures REST base URL (env BINANCE_REST_BASE_URL as override, e.g. proxy-router)",
    )
    args = parser.parse_args()

    alphas_root = Path(args.alphas_dir)
    tradable = fetch_tradable_symbols(args.rest_base)
    stale = audit(alphas_root, tradable)

    report = {
        "tradable_symbol_count": len(tradable),
        "alphas_scanned": len(list(alphas_root.glob("*/whitelist.txt"))),
        "stale": stale,
        "fixed": bool(args.fix and stale),
    }

    if args.fix:
        for alpha_id, offenders in stale.items():
            fix_whitelist(alphas_root / alpha_id / "whitelist.txt", {s.upper() for s in offenders})

    print(json.dumps(report, indent=2))
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
