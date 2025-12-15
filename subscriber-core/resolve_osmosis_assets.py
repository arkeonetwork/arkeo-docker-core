"""
resolve_osmosis_assets.py

Helper to:
1) Query an Osmosis address balance via osmosisd RPC
2) Resolve ibc/<hash> denoms to denom traces (path + base_denom)
3) Optionally pull denom metadata for base denoms to get display symbol + decimals
4) Return a normalized list of assets suitable for UI + tx building

Requirements:
- osmosisd available in PATH
- RPC URL reachable (e.g., http://host:26657 or your custom port)
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class AssetInfo:
    denom: str
    amount: int
    is_ibc: bool
    base_denom: Optional[str] = None
    path: Optional[str] = None
    symbol: Optional[str] = None
    decimals: Optional[int] = None
    display_amount: Optional[float] = None
    label: Optional[str] = None


def _run(cmd: List[str], timeout: int = 20) -> str:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}\nSTDERR:\n{p.stderr.strip()}")
    return p.stdout


def _query_bank_balances(osmo_addr: str, rpc_url: str) -> Dict[str, Any]:
    out = _run(["osmosisd", "query", "bank", "balances", osmo_addr, "--node", rpc_url, "-o", "json"])
    return json.loads(out)


def _query_denom_trace(ibc_hash: str, rpc_url: str) -> Dict[str, Any]:
    out = _run(["osmosisd", "query", "ibc-transfer", "denom-trace", ibc_hash, "--node", rpc_url, "-o", "json"])
    return json.loads(out)


def _query_all_denom_metadata(rpc_url: str) -> List[Dict[str, Any]]:
    out = _run(["osmosisd", "query", "bank", "denom-metadata", "--node", rpc_url, "-o", "json"])
    data = json.loads(out)
    return data.get("metadatas", [])


def _build_metadata_index(metadatas: List[Dict[str, Any]]) -> Dict[str, Tuple[str, int]]:
    """
    base_denom -> (SYMBOL, DECIMALS)
    Uses denom_units where denom == display to find the display exponent.
    """
    idx: Dict[str, Tuple[str, int]] = {}
    for md in metadatas:
        base = md.get("base")
        display = md.get("display")
        denom_units = md.get("denom_units", [])
        decimals: Optional[int] = None

        for du in denom_units:
            if du.get("denom") == display:
                decimals = int(du.get("exponent", 0))
                break

        if decimals is None and denom_units:
            decimals = max(int(du.get("exponent", 0)) for du in denom_units)

        if base and display and decimals is not None:
            idx[base] = (str(display).upper(), int(decimals))
    return idx


def _heuristic_symbol_and_decimals(base_denom: Optional[str], denom: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Fallback only when denom-metadata doesn't help.
    Customize for your known tokens.
    """
    if base_denom:
        b = base_denom.lower()
        if b == "uosmo":
            return "OSMO", 6
        if b in ("uusdc", "usdc"):
            return "USDC", 6
        if b in ("uarkeo", "arkeo"):
            return "ARKEO", 8
        if b.startswith("u") and len(b) > 1:
            return b[1:].upper(), 6
        return b.upper(), None

    if denom.lower() == "uosmo":
        return "OSMO", 6
    return None, None


def resolve_osmosis_assets(
    osmo_addr: str,
    rpc_url: str,
    cache_path: str = "./osmo_denom_cache.json",
    refresh_cache: bool = False,
) -> List[AssetInfo]:
    """
    Returns AssetInfo list for all balances found on the Osmosis address.

    Caching:
    - Stores denom-trace results for ibc hashes so you do not re-query constantly.
    """
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)

    cache: Dict[str, Any] = {}
    if os.path.exists(cache_path) and not refresh_cache:
        try:
            with open(cache_path, "r") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    balances = _query_bank_balances(osmo_addr, rpc_url).get("balances", [])

    try:
        md_idx = _build_metadata_index(_query_all_denom_metadata(rpc_url))
    except Exception:
        md_idx = {}

    assets: List[AssetInfo] = []
    cache_updated = False

    for b in balances:
        denom = b["denom"]
        amount = int(b["amount"])
        is_ibc = denom.startswith("ibc/")

        base_denom = None
        path = None

        if is_ibc:
            ibc_hash = denom.split("/", 1)[1]
            trace = cache.get(ibc_hash)
            if trace is None:
                trace = _query_denom_trace(ibc_hash, rpc_url).get("denom_trace", {})
                cache[ibc_hash] = trace
                cache_updated = True
            base_denom = trace.get("base_denom")
            path = trace.get("path")

        symbol = None
        decimals = None

        if base_denom and base_denom in md_idx:
            symbol, decimals = md_idx[base_denom]
        elif denom in md_idx:
            symbol, decimals = md_idx[denom]
        else:
            symbol, decimals = _heuristic_symbol_and_decimals(base_denom, denom)

        # Override known ARKEO to 8 decimals regardless of metadata quirks
        if (base_denom or "").lower() in ("uarkeo", "arkeo") or denom.lower() == "uarkeo":
            symbol = "ARKEO"
            decimals = 8

        display_amount = None
        if decimals is not None:
            display_amount = amount / (10 ** decimals)

        label = symbol
        if is_ibc and symbol:
            label = f"{symbol} (IBC)"

        assets.append(
            AssetInfo(
                denom=denom,
                amount=amount,
                is_ibc=is_ibc,
                base_denom=base_denom,
                path=path,
                symbol=symbol,
                decimals=decimals,
                display_amount=display_amount,
                label=label,
            )
        )

    if cache_updated:
        with open(cache_path, "w") as f:
            json.dump(cache, f, indent=2, sort_keys=True)

    return assets


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--addr", required=True, help="Osmosis bech32 address (osmo1...)")
    parser.add_argument("--rpc", required=True, help="Osmosis Tendermint RPC URL, e.g. http://host:26657")
    parser.add_argument("--cache", default="./osmo_denom_cache.json")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    assets = resolve_osmosis_assets(args.addr, args.rpc, args.cache, args.refresh)

    for a in assets:
        da = f"{a.display_amount:.6f}" if a.display_amount is not None else str(a.amount)
        print(f"{a.label or a.denom}: {da}  (denom={a.denom}, base={a.base_denom}, path={a.path})")
