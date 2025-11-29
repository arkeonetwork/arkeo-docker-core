#!/usr/bin/env python3
import json
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
import urllib.parse
import yaml
from flask import Flask, jsonify, request

app = Flask(__name__)

def _build_sentinel_uri() -> str:
    port = os.getenv("SENTINEL_PORT") or "3636"
    external = os.getenv("SENTINEL_NODE")
    if external:
        parsed = urllib.parse.urlparse(external)
        scheme = parsed.scheme or "http"
        host = parsed.netloc or parsed.path or external
        if ":" not in host:
            host = f"{host}:{port}"
        return f"{scheme}://{host}/metadata.json"
    # Default to localhost bind for in-container checks
    host = os.getenv("SENTINEL_BIND_HOST") or "127.0.0.1"
    return f"http://{host}:{port}/metadata.json"

ARKEOD_HOME = os.path.expanduser(os.getenv("ARKEOD_HOME", "/root/.arkeod"))
KEY_NAME = os.getenv("KEY_NAME", "provider")
KEYRING = os.getenv("KEY_KEYRING_BACKEND", "test")
def _strip_quotes(val: str | None) -> str:
    if not val:
        return ""
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    return val

ARKEOD_NODE = _strip_quotes(
    os.getenv("ARKEOD_NODE")
    or os.getenv("EXTERNAL_ARKEOD_NODE")
    or "tcp://provider1.innovationtheory.com:26657"
)
CHAIN_ID = _strip_quotes(os.getenv("CHAIN_ID") or os.getenv("ARKEOD_CHAIN_ID") or "")
NODE_ARGS = ["--node", ARKEOD_NODE] if ARKEOD_NODE else []
CHAIN_ARGS = ["--chain-id", CHAIN_ID] if CHAIN_ID else []
# Use the packaged supervisord config unless overridden
SUPERVISOR_CONF = os.getenv("SUPERVISOR_CONF", "/etc/supervisor/conf.d/supervisord.conf")
SUPERVISORCTL = ["supervisorctl", "-c", SUPERVISOR_CONF]
SUPERVISORCTL_FALLBACK = ["supervisorctl"]
SENTINEL_URI_DEFAULT = os.getenv("SENTINEL_URI") or _build_sentinel_uri()
METADATA_NONCE_DEFAULT = os.getenv("METADATA_NONCE") or "1"
BOND_DEFAULT = os.getenv("BOND_AMOUNT") or "1"
FEES_DEFAULT = os.getenv("TX_FEES") or "200uarkeo"
API_PORT = int(os.getenv("ADMIN_API_PORT", "9999"))
SENTINEL_CONFIG_PATH = os.getenv("SENTINEL_CONFIG_PATH", "/app/config/sentinel.yaml")
SENTINEL_ENV_PATH = os.getenv("SENTINEL_ENV_PATH", "/app/config/sentinel.env")


def run(cmd: str) -> tuple[int, str]:
    """Run a shell command and return (exit_code, output)."""
    try:
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT)
        return 0, out.decode("utf-8")
    except subprocess.CalledProcessError as e:
        return e.returncode, e.output.decode("utf-8")


def run_list(cmd: list[str]) -> tuple[int, str]:
    """Run a command without a shell and return (exit_code, output)."""
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        return 0, out.decode("utf-8")
    except subprocess.CalledProcessError as e:
        return e.returncode, e.output.decode("utf-8")


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def derive_pubkeys(user: str, keyring_backend: str) -> tuple[str, str, str | None]:
    """Return (raw_pubkey, bech32_pubkey, error)."""
    pubkey_cmd = [
        "arkeod",
        "--home",
        ARKEOD_HOME,
        "keys",
        "show",
        user,
        "-p",
        "--keyring-backend",
        keyring_backend,
    ]
    code, pubkey_out = run_list(pubkey_cmd)
    if code != 0:
        return "", "", f"failed to fetch raw pubkey: {pubkey_out}"

    try:
        raw_pubkey = json.loads(pubkey_out).get("key", "").strip()
    except json.JSONDecodeError:
        raw_pubkey = ""
    if not raw_pubkey:
        return "", "", f"could not parse raw pubkey: {pubkey_out}"

    bech32_cmd = ["arkeod", "debug", "pubkey-raw", raw_pubkey]
    code, bech32_out = run_list(bech32_cmd)
    if code != 0:
        return raw_pubkey, "", f"failed to convert pubkey: {bech32_out}"

    bech32_pubkey = ""
    for line in bech32_out.splitlines():
        if line.startswith("Bech32 Acc:"):
            bech32_pubkey = line.replace("Bech32 Acc:", "").strip()
            break
    if not bech32_pubkey:
        return raw_pubkey, "", f"Bech32 pubkey not found: {bech32_out}"

    return raw_pubkey, bech32_pubkey, None


def provider_pubkeys_response(user: str, keyring_backend: str):
    """Helper to return pubkey info even if derivation fails."""
    raw_pubkey, bech32_pubkey, pubkey_err = derive_pubkeys(user, keyring_backend)
    resp = {
        "user": user,
        "keyring_backend": keyring_backend,
        "pubkey": {"raw": raw_pubkey, "bech32": bech32_pubkey},
    }
    if pubkey_err:
        resp["pubkey_error"] = pubkey_err
    return resp


@app.get("/api/ping")
def ping():
    return jsonify({"status": "ok"})

@app.get("/api/version")
def version():
    code, out = run("arkeod version")
    if code != 0:
        return jsonify({"error": "failed to get arkeod version", "detail": out}), 500
    ver = out.strip()
    if not ver:
        ver = "unknown"
    return jsonify({"arkeod_version": ver})

@app.get("/api/block-height")
def block_height():
    """Return the latest block height from the configured node."""
    cmd = ["arkeod", "--home", ARKEOD_HOME]
    if ARKEOD_NODE:
        cmd.extend(["--node", ARKEOD_NODE])
    cmd.append("status")
    code, out = run_list(cmd)
    if code != 0:
        return jsonify({"error": "failed to fetch status", "detail": out, "cmd": cmd}), 500
    try:
        data = json.loads(out)
        # handle common casing
        sync_info = data.get("SyncInfo") or data.get("sync_info") or {}
        height = sync_info.get("latest_block_height") or sync_info.get("latest_block")
        return jsonify({"height": str(height) if height is not None else None, "status": data})
    except json.JSONDecodeError:
        return jsonify({"error": "invalid JSON from status", "detail": out, "cmd": cmd}), 500


@app.get("/api/key")
def get_key():
    cmd = (
        f"arkeod --home {ARKEOD_HOME} "
        f"--keyring-backend {KEYRING} "
        f"keys show {KEY_NAME} -a"
    )
    code, out = run(cmd)
    if code != 0:
        return jsonify({"error": "failed to get key address", "detail": out}), 500

    address = out.strip()
    return jsonify({"address": address})


@app.get("/api/balance")
def get_balance():
    # first get address
    addr_cmd = (
        f"arkeod --home {ARKEOD_HOME} "
        f"--keyring-backend {KEYRING} "
        f"keys show {KEY_NAME} -a"
    )
    code, addr_out = run(addr_cmd)
    if code != 0:
        return jsonify({"error": "failed to get key address", "detail": addr_out}), 500

    address = addr_out.strip()

    # then query balances in JSON form
    bal_cmd = (
        f"arkeod query bank balances {address} "
        f"--node {ARKEOD_NODE} "
        f"-o json"
    )
    code, bal_out = run(bal_cmd)

    if code != 0:
        return jsonify(
            {
                "address": address,
                "error": "failed to query balance",
                "detail": bal_out,
            }
        ), 500

    try:
        data = json.loads(bal_out)
    except json.JSONDecodeError:
        data = {"raw": bal_out}

    return jsonify({"address": address, "balance": data})


@app.post("/api/bond-provider")
def bond_provider():
    payload = request.get_json(force=True, silent=True) or {}
    user = KEY_NAME
    service = payload.get("service")
    bond = str(payload.get("bond") or BOND_DEFAULT)
    keyring_backend = KEYRING
    fees = FEES_DEFAULT

    if not service:
        return jsonify({"error": "service is required"}), 400

    # Step 1: get raw pubkey for the user
    raw_pubkey, bech32_pubkey, pubkey_err = derive_pubkeys(user, keyring_backend)
    if pubkey_err:
        return jsonify(
            {
                "error": pubkey_err,
                "pubkey_error": pubkey_err,
                "user": user,
                "keyring_backend": keyring_backend,
                "pubkey": {"raw": raw_pubkey, "bech32": bech32_pubkey},
                "inputs": {
                    "service": service,
                    "bond": bond,
                    "keyring_backend": keyring_backend,
                    "fees": fees,
                },
            }
        ), 500

    # Step 3: send the bond-provider tx
    bond_cmd = [
        "arkeod",
        "--home",
        ARKEOD_HOME,
        "tx",
        "arkeo",
        "bond-provider",
        bech32_pubkey,
        service,
        bond,
        *NODE_ARGS,
        "--from",
        user,
        "--fees",
        fees,
        "--keyring-backend",
        keyring_backend,
        "-y",
    ]
    code, bond_out = run_list(bond_cmd)
    if code != 0:
        return jsonify(
            {
                "error": "failed to bond provider",
                "detail": bond_out,
                "inputs": {
                    "service": service,
                    "bond": bond,
                    "keyring_backend": keyring_backend,
                    "fees": fees,
                },
                "pubkey": {"raw": raw_pubkey, "bech32": bech32_pubkey},
                "user": user,
            }
        ), 500

    return jsonify(
        {
            "status": "bond_submitted",
            "tx_output": bond_out,
            "inputs": {
                "service": service,
                "bond": bond,
                "keyring_backend": keyring_backend,
                "fees": fees,
            },
            "pubkey": {"raw": raw_pubkey, "bech32": bech32_pubkey},
            "user": user,
            "pubkey_error": pubkey_err,
        }
    )


@app.post("/api/bond-mod-provider")
def bond_and_mod_provider():
    """Bond a provider then mod-provider using supplied parameters."""
    payload = request.get_json(force=True, silent=True) or {}
    user = KEY_NAME
    service = payload.get("service")
    bond = str(payload.get("bond") or BOND_DEFAULT)
    keyring_backend = KEYRING
    fees = FEES_DEFAULT

    sentinel_uri = payload.get("sentinel_uri") or SENTINEL_URI_DEFAULT
    metadata_nonce = str(payload.get("metadata_nonce") or METADATA_NONCE_DEFAULT)
    status = str(payload.get("status") or "1")
    min_contract_dur = str(payload.get("min_contract_dur") or "5")
    max_contract_dur = str(payload.get("max_contract_dur") or "432000")
    subscription_rates = payload.get("subscription_rates") or "200uarkeo"
    pay_as_you_go_rates = payload.get("pay_as_you_go_rates") or "200uarkeo"
    settlement_dur = str(payload.get("settlement_dur") or "1000")

    if not service:
        return jsonify({"error": "service is required"}), 400

    # Resolve numeric service IDs to the service name (CLI expects name)
    resolved_service = service
    lookup_note = ""
    if isinstance(service, str) and service.strip().isdigit():
        svc_id = service.strip()
        def _lookup_service_name_by_id(sid: str) -> str | None:
            cmd = ["arkeod", "--home", ARKEOD_HOME]
            if ARKEOD_NODE:
                cmd.extend(["--node", ARKEOD_NODE])
            cmd.extend(["query", "arkeo", "all-services", "-o", "json"])
            code, out = run_list(cmd)
            if code != 0:
                return None
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                return None
            services = data.get("services") or data.get("service") or data.get("result") or []
            if isinstance(services, dict):
                services = services.get("services") or services.get("service") or []
            for item in services if isinstance(services, list) else []:
                if not isinstance(item, dict):
                    continue
                sid_val = str(item.get("id") or item.get("service_id") or item.get("serviceID") or "")
                if sid_val == sid:
                    return item.get("service") or item.get("name") or sid
            return None
        looked_up = _lookup_service_name_by_id(svc_id)
        if looked_up:
            resolved_service = looked_up
        else:
            lookup_note = f"could not resolve service id {svc_id} to name"

    raw_pubkey, bech32_pubkey, pubkey_err = derive_pubkeys(user, keyring_backend)
    if pubkey_err:
        return jsonify(
            {
                "error": pubkey_err,
                "pubkey_error": pubkey_err,
                "user": user,
                "keyring_backend": keyring_backend,
                "pubkey": {"raw": raw_pubkey, "bech32": bech32_pubkey},
            }
        ), 500

    # If provider already exists, skip rebond and go straight to mod.
    skip_bond = False
    bond_cmd: list[str] | None = None
    bond_code = 0
    bond_out = "skipped: provider already bonded"
    try:
        lookup_cmd = [
            "arkeod",
            "--home",
            ARKEOD_HOME,
            "query",
            "arkeo",
            "provider",
            bech32_pubkey,
            resolved_service,
            "-o",
            "json",
        ]
        if ARKEOD_NODE:
            lookup_cmd.extend(["--node", ARKEOD_NODE])
        code, lookup_out = run_list(lookup_cmd)
        if code == 0:
            skip_bond = True
            bond_out = "skipped: provider already exists"
        else:
            skip_bond = False
    except Exception:
        skip_bond = False

    if not skip_bond:
        bond_cmd = [
            "arkeod",
            "--home",
            ARKEOD_HOME,
            "tx",
            "arkeo",
            "bond-provider",
            bech32_pubkey,
            resolved_service,
            bond,
            *NODE_ARGS,
            *CHAIN_ARGS,
            "--from",
            user,
            "--fees",
            fees,
            "--keyring-backend",
            keyring_backend,
            "-y",
        ]
        bond_code, bond_out = run_list(bond_cmd)
        if bond_code != 0:
            return jsonify(
                {
                    "error": "failed to bond provider",
                    "detail": bond_out,
                    "cmd": bond_cmd,
                    "inputs": {
                        "service": service,
                        "resolved_service": resolved_service,
                        "lookup_note": lookup_note,
                        "bond": bond,
                        "keyring_backend": keyring_backend,
                        "fees": fees,
                    },
                    "pubkey": {"raw": raw_pubkey, "bech32": bech32_pubkey},
                    "user": user,
                }
            ), 500

        # Give the bond a brief moment to settle before mod-provider
        time.sleep(2)

    # Fetch account sequence to avoid mismatch (with retries to catch fresh state)
    sequence_arg: list[str] = []
    for _ in range(3):
        try:
            acct_cmd = ["arkeod", "--home", ARKEOD_HOME, "query", "auth", "account", bech32_pubkey, "-o", "json"]
            if ARKEOD_NODE:
                acct_cmd.extend(["--node", ARKEOD_NODE])
            code, acct_out = run_list(acct_cmd)
            if code == 0:
                acct = json.loads(acct_out)
                seq_val = None
                if isinstance(acct, dict):
                    account_info = acct.get("account") or acct.get("result") or {}
                    if isinstance(account_info, dict):
                        val = account_info.get("value") or account_info
                        if isinstance(val, dict):
                            seq_val = val.get("sequence")
                if seq_val is not None:
                    sequence_arg = ["--sequence", str(seq_val)]
                    break
        except Exception:
            pass
        time.sleep(1)

    mod_cmd_base = [
        "arkeod",
        "--home",
        ARKEOD_HOME,
        "tx",
        "arkeo",
        "mod-provider",
        bech32_pubkey,
        resolved_service,
        sentinel_uri,
        metadata_nonce,
        status,
        min_contract_dur,
        max_contract_dur,
        subscription_rates,
        pay_as_you_go_rates,
        settlement_dur,
        *NODE_ARGS,
        *CHAIN_ARGS,
        "--from",
        user,
        "--fees",
        fees,
        "--keyring-backend",
        keyring_backend,
        "-y",
    ]

    def run_mod_with_sequence(seq_arg: list[str]):
        cmd = mod_cmd_base.copy()
        # insert sequence args just before the --from flag
        try:
            insert_at = cmd.index("--from")
        except ValueError:
            insert_at = len(cmd)
        cmd[insert_at:insert_at] = seq_arg
        return cmd, *run_list(cmd)

    mod_cmd, mod_code, mod_out = run_mod_with_sequence(sequence_arg)

    # Retry once on account-sequence mismatch by refetching or using the expected sequence
    if "account sequence mismatch" in str(mod_out):
        time.sleep(1)
        retry_seq: list[str] = []
        # First, try to parse the expected sequence from the error text
        m = re.search(r"expected\s+(\d+)", str(mod_out))
        if m:
            retry_seq = ["--sequence", m.group(1)]
        # If not found, re-query the account for the latest sequence
        if not retry_seq:
            for _ in range(2):
                try:
                    acct_cmd = ["arkeod", "--home", ARKEOD_HOME, "query", "auth", "account", bech32_pubkey, "-o", "json"]
                    if ARKEOD_NODE:
                        acct_cmd.extend(["--node", ARKEOD_NODE])
                    code, acct_out = run_list(acct_cmd)
                    if code == 0:
                        acct = json.loads(acct_out)
                        seq_val = None
                        if isinstance(acct, dict):
                            account_info = acct.get("account") or acct.get("result") or {}
                            if isinstance(account_info, dict):
                                val = account_info.get("value") or account_info
                                if isinstance(val, dict):
                                    seq_val = val.get("sequence")
                        if seq_val is not None:
                            retry_seq = ["--sequence", str(seq_val)]
                            break
                except Exception:
                    pass
                time.sleep(1)
        mod_cmd, mod_code, mod_out = run_mod_with_sequence(retry_seq)

    if mod_code != 0:
        return jsonify(
            {
                "error": "failed to mod provider",
                "detail": mod_out,
                "cmd": mod_cmd,
                "inputs": {
                    "service": service,
                    "resolved_service": resolved_service,
                    "sentinel_uri": sentinel_uri,
                    "metadata_nonce": metadata_nonce,
                    "status": status,
                    "min_contract_dur": min_contract_dur,
                    "max_contract_dur": max_contract_dur,
                    "subscription_rates": subscription_rates,
                    "pay_as_you_go_rates": pay_as_you_go_rates,
                    "settlement_dur": settlement_dur,
                    "bond": bond,
                    "keyring_backend": keyring_backend,
                    "fees": fees,
                },
                "pubkey": {"raw": raw_pubkey, "bech32": bech32_pubkey},
                "user": user,
                "bond_tx": {"exit_code": bond_code, "output": bond_out},
            }
        ), 500

    return jsonify(
        {
            "status": "bond_and_mod_submitted",
            "user": user,
            "inputs": {
                "service": service,
                "resolved_service": resolved_service,
                "lookup_note": lookup_note,
                "sentinel_uri": sentinel_uri,
                "metadata_nonce": metadata_nonce,
                "status": status,
                "min_contract_dur": min_contract_dur,
                "max_contract_dur": max_contract_dur,
                "subscription_rates": subscription_rates,
                "pay_as_you_go_rates": pay_as_you_go_rates,
                "settlement_dur": settlement_dur,
                "bond": bond,
                "keyring_backend": keyring_backend,
                "fees": fees,
            },
            "pubkey": {"raw": raw_pubkey, "bech32": bech32_pubkey},
            "bond_cmd": bond_cmd,
            "mod_cmd": mod_cmd,
            "bond_tx": {"exit_code": bond_code, "output": bond_out},
            "mod_tx": {"exit_code": mod_code, "output": mod_out},
        }
    )


@app.get("/api/provider-info")
def provider_info():
    """Return hotwallet provider info including pubkeys and defaults."""
    user = KEY_NAME
    keyring_backend = KEYRING
    fees = FEES_DEFAULT
    bond = BOND_DEFAULT

    base = provider_pubkeys_response(user, keyring_backend)
    base.update(
        {
            "fees": fees,
            "bond": bond,
            "sentinel_uri": SENTINEL_URI_DEFAULT,
            "metadata_nonce": METADATA_NONCE_DEFAULT,
            "arkeod_node": ARKEOD_NODE,
            "provider_metadata": _load_env_file(SENTINEL_ENV_PATH),
        }
    )
    return jsonify(base)


@app.get("/api/services")
def list_services():
    """Return available services from arkeod."""
    cmd = ["arkeod", "--home", ARKEOD_HOME]
    if ARKEOD_NODE:
        cmd.extend(["--node", ARKEOD_NODE])
    cmd.extend(["query", "arkeo", "all-services", "-o", "json"])

    code, out = run_list(cmd)
    if code != 0:
        return jsonify({"error": "failed to list services", "detail": out}), 500

    raw_out = out

    def parse_json(text: str):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            first_brace = text.find("{")
            first_bracket = text.find("[")
            candidates = [i for i in (first_brace, first_bracket) if i >= 0]
            if not candidates:
                return None
            start = min(candidates)
            try:
                return json.loads(text[start:])
            except json.JSONDecodeError:
                return None

    parsed = parse_json(raw_out)
    if parsed is None:
        parsed = raw_out

    services = []
    # Try common shapes; fall back to raw data if not recognized
    candidates = []
    if isinstance(parsed, dict):
        for key in ("services", "result", "data"):
            val = parsed.get(key)
            if isinstance(val, list):
                candidates = val
                break
    if not candidates and isinstance(parsed, list):
        candidates = parsed

    for item in candidates:
        if not isinstance(item, dict):
            continue
        sid = item.get("id") or item.get("service_id") or item.get("serviceID")
        name = item.get("service") or item.get("name") or item.get("label")
        if sid is None and name is None:
            continue
        services.append({"id": sid, "name": name})

    # If parsing failed, try to extract minimal info from text lines
    if not services and isinstance(parsed, str):
        text_pattern = re.compile(
            r"^\s*-\s*(?P<service>[^:]+?)\s*:\s*(?P<id>[0-9]+)\s*\((?P<desc>.*)\)\s*$"
        )
        for line in parsed.splitlines():
            m = text_pattern.match(line)
            if not m:
                continue
            sid = m.group("id").strip()
            svc = m.group("service").strip()
            desc = m.group("desc").strip()
            services.append({"id": sid, "name": svc, "description": desc})

    return jsonify({"services": services, "raw": parsed, "cmd": cmd})


@app.get("/api/provider-services")
def provider_services():
    """List services for the current provider (filtered by pubkey)."""
    user = KEY_NAME
    keyring_backend = KEYRING
    raw_pubkey, bech32_pubkey, pubkey_err = derive_pubkeys(user, keyring_backend)
    if pubkey_err:
        return jsonify(
            {
                "error": pubkey_err,
                "user": user,
                "keyring_backend": keyring_backend,
                "pubkey": {"raw": raw_pubkey, "bech32": bech32_pubkey},
            }
        ), 500

    cmd = ["arkeod", "--home", ARKEOD_HOME]
    if ARKEOD_NODE:
        cmd.extend(["--node", ARKEOD_NODE])
    cmd.extend(["query", "arkeo", "list-providers", "--output", "json"])

    code, out = run_list(cmd)
    if code != 0:
        return jsonify(
            {
                "error": "failed to list providers",
                "detail": out,
                "cmd": cmd,
                "pubkey": {"raw": raw_pubkey, "bech32": bech32_pubkey},
            }
        ), 500

    providers = []
    try:
        data = json.loads(out)
        providers = data.get("provider") or data.get("providers") or []
    except json.JSONDecodeError:
        providers = []

    matched = []
    for p in providers:
        if not isinstance(p, dict):
            continue
        pk = p.get("pub_key") or p.get("pubkey") or p.get("pubKey")
        if pk and pk == bech32_pubkey:
            matched.append(p)

    services = []
    def _rate_to_string(rate_val):
        """Normalize rate structures into a compact string."""
        if isinstance(rate_val, list):
            parts = []
            for r in rate_val:
                if not isinstance(r, dict):
                    continue
                amount = r.get("amount")
                denom = r.get("denom") or ""
                if amount is None:
                    continue
                parts.append(f"{amount}{denom}")
            return ",".join(parts) if parts else ""
        if isinstance(rate_val, dict):
            amount = rate_val.get("amount")
            denom = rate_val.get("denom") or ""
            if amount is None:
                return ""
            return f"{amount}{denom}"
        return str(rate_val) if rate_val is not None else ""

    for p in matched:
        svc_list = []
        if isinstance(p.get("services"), list):
            svc_list = p.get("services")
        elif isinstance(p.get("service"), list):
            svc_list = p.get("service")
        # If the provider entry itself looks like a single service entry
        if svc_list:
            for s in svc_list:
                if not isinstance(s, dict):
                    continue
                min_dur = s.get("min_contract_duration") or s.get("min_contract_dur")
                max_dur = s.get("max_contract_duration") or s.get("max_contract_dur")
                sub_rate_raw = s.get("subscription_rate") or s.get("subscription_rates")
                paygo_rate_raw = s.get("pay_as_you_go_rate") or s.get("pay_as_you_go_rates")
                sub_rate = _rate_to_string(sub_rate_raw)
                paygo_rate = _rate_to_string(paygo_rate_raw)
                settle = s.get("settlement_duration") or s.get("settlement_dur")
                status_val = s.get("status")
                # Normalized id/name: if id missing, fall back to service field
                sid = s.get("service_id") or s.get("id") or s.get("service")
                sname = s.get("service") or s.get("name")
                services.append(
                    {
                        "name": sname,
                        "id": sid,
                        "service": s.get("service"),
                        "metadata_uri": s.get("metadata_uri") or s.get("metadataUri"),
                        "metadata_nonce": s.get("metadata_nonce") or s.get("metadataNonce"),
                        "status": status_val,
                        "min_contract_dur": min_dur,
                        "max_contract_dur": max_dur,
                        "subscription_rates": sub_rate,
                        "pay_as_you_go_rates": paygo_rate,
                        "settlement_dur": settle,
                    }
                )
        else:
            min_dur = p.get("min_contract_duration") or p.get("min_contract_dur")
            max_dur = p.get("max_contract_duration") or p.get("max_contract_dur")
            sub_rate_raw = p.get("subscription_rate") or p.get("subscription_rates")
            paygo_rate_raw = p.get("pay_as_you_go_rate") or p.get("pay_as_you_go_rates")
            sub_rate = _rate_to_string(sub_rate_raw)
            paygo_rate = _rate_to_string(paygo_rate_raw)
            settle = p.get("settlement_duration") or p.get("settlement_dur")
            status_val = p.get("status")
            sid = p.get("service_id") or p.get("id") or p.get("service")
            sname = p.get("service") or p.get("name")
            services.append(
                {
                    "name": sname,
                    "id": sid,
                    "service": p.get("service"),
                    "metadata_uri": p.get("metadata_uri") or p.get("metadataUri"),
                    "metadata_nonce": p.get("metadata_nonce") or p.get("metadataNonce"),
                    "status": status_val,
                    "min_contract_dur": min_dur,
                    "max_contract_dur": max_dur,
                    "subscription_rates": sub_rate,
                    "pay_as_you_go_rates": paygo_rate,
                    "settlement_dur": settle,
                }
            )

    return jsonify(
        {
            "services": services,
            "matched_providers": matched,
            "pubkey": {"raw": raw_pubkey, "bech32": bech32_pubkey},
            "cmd": cmd,
        }
    )


@app.get("/api/sentinel-status")
def sentinel_status():
    """Return sentinel process status (supervisor + short logs)."""
    status = ""
    log_tail = ""
    err_tail = ""
    try:
        code, out = run_list([*SUPERVISORCTL, "status", "sentinel"])
        status = out.strip()
    except Exception:
        try:
            code, out = run_list([*SUPERVISORCTL_FALLBACK, "status", "sentinel"])
            status = out.strip()
        except Exception as e:
            status = f"status error: {e}"
    try:
        code, out = run_list(["tail", "-n", "80", "/var/log/provider-sentinel.log"])
        log_tail = out
    except Exception as e:
        log_tail = f"log error: {e}"
    try:
        code, out = run_list(["tail", "-n", "80", "/var/log/provider-sentinel.err.log"])
        err_tail = out
    except Exception as e:
        err_tail = f"errlog error: {e}"
    return jsonify({"status": status, "log": log_tail, "err_log": err_tail})


@app.post("/api/sentinel-control")
def sentinel_control():
    """Start/stop/restart sentinel via supervisorctl."""
    payload = request.get_json(force=True, silent=True) or {}
    action = (payload.get("action") or "").lower()
    if action not in {"start", "stop", "restart"}:
        return jsonify({"error": "action must be one of start, stop, restart"}), 400
    try:
        code, out = run_list([*SUPERVISORCTL, action, "sentinel"])
        return jsonify({"status": "ok", "action": action, "exit_code": code, "output": out})
    except Exception:
        try:
            code, out = run_list([*SUPERVISORCTL_FALLBACK, action, "sentinel"])
            return jsonify({"status": "ok", "action": action, "exit_code": code, "output": out})
        except Exception as e:
            return jsonify({"error": f"failed to {action} sentinel", "detail": str(e)}), 500


@app.get("/api/sentinel-metadata")
def sentinel_metadata():
    """Fetch sentinel metadata.json from the given URL (or default)."""
    url = request.args.get("url") or request.args.get("sentinel_uri") or SENTINEL_URI_DEFAULT
    if not url:
        return jsonify({"error": "sentinel uri not provided"}), 400
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        return jsonify({"error": "failed to fetch sentinel metadata", "detail": str(e), "url": url}), 500
    parsed = None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = None
    if parsed is None:
        return jsonify({"url": url, "raw": body})
    return jsonify({"url": url, "metadata": parsed})


def _load_sentinel_config():
    """Load sentinel YAML config if present."""
    if not SENTINEL_CONFIG_PATH or not os.path.isfile(SENTINEL_CONFIG_PATH):
        return None, None
    try:
        with open(SENTINEL_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = f.read()
        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError:
            parsed = None
        return parsed, raw
    except OSError:
        return None, None


def _load_env_file(path: str) -> dict:
    data: dict[str, str] = {}
    if not path or not os.path.isfile(path):
        return data
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
                    v = v[1:-1]
                data[k.strip()] = v
    except OSError:
        pass
    return data


@app.get("/api/sentinel-config")
def sentinel_config():
    """Return sentinel-related env values and parsed sentinel.yaml if present."""
    env_keys = [
        "PROVIDER_NAME",
        "MONIKER",
        "WEBSITE",
        "DESCRIPTION",
        "LOCATION",
        "PORT",
        "SOURCE_CHAIN",
        "PROVIDER_HUB_URI",
        "ARKEO_REST_API_PORT",
        "EVENT_STREAM_HOST",
        "FREE_RATE_LIMIT",
        "FREE_RATE_LIMIT_DURATION",
        "CLAIM_STORE_LOCATION",
        "CONTRACT_CONFIG_STORE_LOCATION",
        "PROVIDER_CONFIG_STORE_LOCATION",
        "LOG_LEVEL",
        "PROVIDER_PUBKEY",
    ]
    env_data = {k.lower(): os.getenv(k) for k in env_keys}
    env_file = _load_env_file(SENTINEL_ENV_PATH)
    parsed, raw = _load_sentinel_config()
    return jsonify(
        {
            "env": env_data,
            "env_file": env_file,
            "config_path": SENTINEL_CONFIG_PATH,
            "config": parsed,
            "raw": raw,
            "sentinel_uri_default": SENTINEL_URI_DEFAULT,
        }
    )


@app.post("/api/sentinel-config")
def update_sentinel_config():
    """Update sentinel.yaml provider/api fields (services remain untouched)."""
    payload = request.get_json(force=True, silent=True) or {}
    provider_pubkey = payload.get("provider_pubkey")
    provider_name = payload.get("provider_name")
    listen_addr = payload.get("listen_addr")
    moniker = payload.get("moniker")
    website = payload.get("website")
    description = payload.get("description")
    location = payload.get("location")
    free_rate_limit = payload.get("free_rate_limit")
    free_rate_limit_duration = payload.get("free_rate_limit_duration")

    if not os.path.isfile(SENTINEL_CONFIG_PATH):
        return jsonify({"error": f"sentinel config not found at {SENTINEL_CONFIG_PATH}"}), 404

    try:
        with open(SENTINEL_CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except Exception as e:
        return jsonify({"error": "failed to read sentinel config", "detail": str(e)}), 500

    # Update env file for UI-managed fields
    env_file = _load_env_file(SENTINEL_ENV_PATH)
    def _set_env(key, value):
        if value is None:
            return
        env_file[key] = value
    _set_env("MONIKER", moniker)
    _set_env("WEBSITE", website)
    _set_env("DESCRIPTION", description)
    _set_env("LOCATION", location)
    _set_env("FREE_RATE_LIMIT", free_rate_limit)
    _set_env("FREE_RATE_LIMIT_DURATION", free_rate_limit_duration)
    # Keep ARKEO_REST_API_PORT in sync with PROVIDER_HUB_URI if provided
    if payload.get("provider_hub_uri"):
        _set_env("PROVIDER_HUB_URI", payload.get("provider_hub_uri"))
        _set_env("ARKEO_REST_API_PORT", payload.get("provider_hub_uri"))

    # Prefer explicit provider_name, otherwise fall back to moniker for the YAML provider name
    effective_provider_name = provider_name or moniker

    if provider_pubkey:
        config.setdefault("provider", {})
        config["provider"]["pubkey"] = provider_pubkey
    if effective_provider_name:
        config.setdefault("provider", {})
        config["provider"]["name"] = effective_provider_name
    # listen_addr and provider_pubkey are not user-editable in the UI anymore; we keep them unchanged unless provided explicitly

    try:
        with open(SENTINEL_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, sort_keys=False)
    except Exception as e:
        return jsonify({"error": "failed to write sentinel config", "detail": str(e)}), 500

    try:
        with open(SENTINEL_ENV_PATH, "w", encoding="utf-8") as f:
            for k, v in env_file.items():
                # Quote values to keep spaces safe when sourced
                f.write(f"{k}={shlex.quote(str(v))}\n")
    except Exception as e:
        return jsonify({"error": "failed to write sentinel env", "detail": str(e)}), 500

    restart_output = ""
    try:
        app.logger.info("Restarting sentinel via supervisorctl")
        code, out = run_list([*SUPERVISORCTL, "restart", "sentinel"])
        restart_output = out
        app.logger.info("Sentinel restart exited code=%s output=%s", code, out.strip())
    except Exception as e:
        restart_output = f"restart failed: {e}"
        app.logger.warning("Sentinel restart failed: %s", e)

    return jsonify(
        {
            "status": "updated",
            "config_path": SENTINEL_CONFIG_PATH,
            "config": config,
            "restart_output": restart_output,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=API_PORT)
