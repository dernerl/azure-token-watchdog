#!/usr/bin/env python3
"""Azure/MS token health check — reads local az CLI cache + live token probes.

Checks both the global ~/.azure cache and any per-project isolated contexts
under ~/.azure-contexts/<project> — the AZURE_CONFIG_DIR pattern that gives
each project its own token cache instead of sharing the global one.

Liveness means: can `az account get-access-token` still acquire a token
(ARM and Graph) for each cached identity/tenant? Only `expiresOn` is
queried, so token secrets never reach this process' output. Writes
state.json (machine-readable) and reports/latest.md (human-readable).
"""
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

AZURE_DIR = Path.home() / ".azure"
CONTEXTS_DIR = Path.home() / ".azure-contexts"
EXTENSION_DIR = AZURE_DIR / "cliextensions"
DEFAULT_PROJECT_SEARCH_ROOTS = [Path.home() / "projects"]
HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"
REPORT_PATH = HERE / "reports" / "latest.md"
# A probe may trigger a refresh-token round trip, so allow more than the
# local-only default.
TOKEN_PROBE_TIMEOUT = 20
TOKEN_RESOURCES = {"arm": [], "graph": ["--resource-type", "ms-graph"]}
AADSTS_RE = re.compile(r"AADSTS\d+")


def run_az(args, timeout=5, config_dir: Path | None = None):
    env = os.environ.copy()
    if config_dir is not None:
        env["AZURE_CONFIG_DIR"] = str(config_dir)
        env.setdefault("AZURE_EXTENSION_DIR", str(EXTENSION_DIR))
    else:
        env.pop("AZURE_CONFIG_DIR", None)
    try:
        proc = subprocess.run(
            ["az", *args, "-o", "json"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            # The AADSTS code is the useful part; az often ends with a
            # generic hint or footer instead, so the last line is useless.
            code = AADSTS_RE.search(stderr)
            if code:
                return None, [code.group(0)]
            errors = [l for l in stderr.splitlines() if l.startswith("ERROR:")]
            return None, errors[:1] or stderr.splitlines()[-1:] or ["az failed"]
        return json.loads(proc.stdout or "null"), None
    except FileNotFoundError:
        return None, ["az CLI not found"]
    except subprocess.TimeoutExpired:
        return None, ["az CLI timed out"]
    except json.JSONDecodeError:
        return None, ["az CLI returned invalid JSON"]


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def cached_usernames(config_dir: Path):
    cache = load_json(config_dir / "msal_token_cache.json") or {}
    return sorted({e.get("username", "?") for e in cache.get("Account", {}).values()})


def concurrent_az_process():
    try:
        proc = subprocess.run(["ps", "-axo", "pid,command"], capture_output=True, text=True, timeout=3)
    except Exception:
        return False
    lines = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid, cmd = parts
        if "az " in cmd or cmd.rstrip().endswith("/az") or "azd " in cmd:
            if "ps -axo" in cmd or "token_watchdog" in cmd:
                continue
            lines.append(f"{pid}: {cmd[:80]}")
    return lines


def severity_for(no_default_set, default_dead, identities, concurrent=None):
    if default_dead or any(i["status"] == "dead" for i in identities):
        return "red"
    if no_default_set or concurrent or any(i["status"] == "partial" for i in identities):
        return "orange"
    return "green"


def probe_tokens(subscription_id: str, config_dir: Path | None):
    """Try to acquire ARM and Graph tokens as the subscription's identity.

    `--query expiresOn` keeps the token itself out of stdout. Any failure
    counts as dead — no allowlist of AADSTS codes, the next new one would
    slip through again.
    """
    probes = {}
    for resource, extra_args in TOKEN_RESOURCES.items():
        expires_on, err = run_az(
            ["account", "get-access-token", "--subscription", subscription_id,
             *extra_args, "--query", "expiresOn"],
            timeout=TOKEN_PROBE_TIMEOUT, config_dir=config_dir,
        )
        probes[resource] = {"ok": err is None, "expires_on": expires_on, "error": err}
    return probes


def resolve_context(config_dir: Path | None):
    """Same shape of health check, scoped to config_dir (None = global ~/.azure)."""
    base = config_dir if config_dir is not None else AZURE_DIR
    profile = load_json(base / "azureProfile.json") or {}
    cached_subs = profile.get("subscriptions", [])
    default_sub = next((s for s in cached_subs if s.get("isDefault")), None)

    # One probe per (user, tenant): tokens are issued per identity and
    # tenant, not per subscription. Prefer the default subscription so its
    # pair is probed through it.
    representatives = {}
    for s in sorted(cached_subs, key=lambda s: not s.get("isDefault")):
        key = (s.get("user", {}).get("name", "?"), s.get("tenantId"))
        representatives.setdefault(key, s["id"])
    probes = {key: probe_tokens(sub_id, config_dir) for key, sub_id in representatives.items()}

    no_default_set = default_sub is None
    default_probe = None
    if default_sub is not None:
        default_probe = probes[(default_sub.get("user", {}).get("name", "?"), default_sub.get("tenantId"))]["arm"]

    # Per-user rollup: only users with cached subscriptions can be scored
    # (an MSAL account with zero subscriptions is normal for Graph-only
    # logins; `get-access-token --tenant` would probe the active account,
    # not necessarily this one).
    identities = []
    for username in cached_usernames(base):
        tenants = [
            {"tenant": tenant, **tenant_probes}
            for (user, tenant), tenant_probes in probes.items() if user == username
        ]
        if not tenants:
            identities.append({"username": username, "status": "no-subscription-context"})
            continue
        if not any(t["arm"]["ok"] for t in tenants):
            status = "dead"
        elif all(t[r]["ok"] for t in tenants for r in TOKEN_RESOURCES):
            status = "ok"
        else:
            status = "partial"
        identities.append({"username": username, "status": status, "tenants": tenants})

    return {
        "no_default_set": no_default_set,
        "default_subscription": (
            {
                "name": default_sub.get("name"),
                "user": default_sub.get("user", {}).get("name"),
                "tenant": default_sub.get("tenantId"),
                "dead": not default_probe["ok"],
                "expires_on": default_probe["expires_on"],
                "error": default_probe["error"],
            }
            if default_sub else None
        ),
        "identities": identities,
    }


def discover_contexts():
    if not CONTEXTS_DIR.is_dir():
        return []
    return sorted(p for p in CONTEXTS_DIR.iterdir() if p.is_dir())


def project_search_roots():
    """Directories whose immediate subdirectories are projects.

    Override via AZURE_WATCHDOG_PROJECT_ROOTS, a list separated by the
    platform path separator (e.g. "~/projects:~/work"). Purely cosmetic —
    it lets a context be labelled with the project that owns it; context
    discovery itself does not depend on it.
    """
    raw = os.environ.get("AZURE_WATCHDOG_PROJECT_ROOTS", "").strip()
    if not raw:
        return DEFAULT_PROJECT_SEARCH_ROOTS
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


def find_project_root(context_dir: Path):
    target = str(context_dir)
    for root in project_search_roots():
        if not root.is_dir():
            continue
        for settings_file in root.glob("*/.claude/settings.local.json"):
            data = load_json(settings_file) or {}
            if data.get("env", {}).get("AZURE_CONFIG_DIR") == target:
                return str(settings_file.parent.parent)
    return None


def check():
    global_result = resolve_context(None)
    concurrent = concurrent_az_process()
    global_severity = severity_for(
        global_result["no_default_set"], global_result["default_subscription"] and global_result["default_subscription"]["dead"],
        global_result["identities"], concurrent,
    )

    contexts = []
    worst_severity = global_severity
    for ctx_dir in discover_contexts():
        result = resolve_context(ctx_dir)
        ctx_severity = severity_for(
            result["no_default_set"], result["default_subscription"] and result["default_subscription"]["dead"],
            result["identities"],
        )
        contexts.append({
            "name": ctx_dir.name,
            "path": str(ctx_dir),
            "project_root": find_project_root(ctx_dir),
            "severity": ctx_severity,
            **result,
        })
        if {"red": 3, "orange": 2, "green": 1}[ctx_severity] > {"red": 3, "orange": 2, "green": 1}[worst_severity]:
            worst_severity = ctx_severity

    return {
        "generated": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "severity": worst_severity,
        "concurrent_az_processes": concurrent,
        "contexts": contexts,
        **global_result,
    }


STATUS_MARK = {
    "dead": "🔴",
    "partial": "🟠",
    "ok": "🟢",
    "no-subscription-context": "⚪",
}
STATUS_LABEL = {
    "dead": "no ARM token can be acquired in any tenant",
    "partial": "some ARM/Graph tokens can no longer be acquired",
    "ok": "ARM and Graph tokens acquired in all tenants",
    "no-subscription-context": "no subscription context cached (e.g. Graph-only login)",
}
SEVERITY_MARK = {"red": "🔴", "orange": "🟠", "green": "🟢"}


def probe_failures(ident: dict) -> list[str]:
    return [
        f"  - {t['tenant']} {resource.upper()}: {', '.join(t[resource]['error'])}"
        for t in ident.get("tenants", []) for resource in TOKEN_RESOURCES
        if not t[resource]["ok"]
    ]


def render_report(state: dict) -> str:
    lines = [f"# Azure/MS Token Watchdog — {state['generated']}", ""]
    ds = state["default_subscription"]
    if state["no_default_set"]:
        lines.append("**Default subscription (global ~/.azure):** 🟠 none set — `az account set --subscription <name>`")
    elif ds:
        status = (f"🔴 DEAD (no ARM token: {', '.join(ds['error'])})" if ds["dead"]
                  else f"🟢 alive (ARM token until {ds['expires_on']})")
        lines.append(f"**Default subscription (global ~/.azure):** {ds['name']} ({ds['user']}) — {status}")
    lines.append("")
    if state["concurrent_az_processes"]:
        lines.append("**⚠️ az/azd processes running right now:**")
        for p in state["concurrent_az_processes"]:
            lines.append(f"- {p}")
        lines.append("")
    lines.append("## Identities in the global cache")
    for ident in state["identities"]:
        mark = STATUS_MARK[ident["status"]]
        label = STATUS_LABEL[ident["status"]]
        lines.append(f"- {mark} {ident['username']} — {label}")
        lines.extend(probe_failures(ident))
    lines.append("")
    lines.append("## Project contexts (isolated AZURE_CONFIG_DIR)")
    if not state["contexts"]:
        lines.append("- (none found under `~/.azure-contexts/`)")
    for ctx in state["contexts"]:
        mark = SEVERITY_MARK.get(ctx["severity"], "⚪")
        label = ctx["project_root"] or ctx["name"]
        cds = ctx["default_subscription"]
        if ctx["no_default_set"]:
            default_info = "no default subscription set"
        elif cds:
            default_info = f"default: {cds['name']} ({'dead: ' + ', '.join(cds['error']) if cds['dead'] else 'alive'})"
        else:
            default_info = "no subscriptions cached"
        lines.append(f"- {mark} {label} — {default_info}")
    return "\n".join(lines) + "\n"


def main():
    state = check()
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(render_report(state), encoding="utf-8")
    print(f"severity={state['severity']} contexts={len(state['contexts'])}")


if __name__ == "__main__":
    main()
