#!/usr/bin/env python3
"""Azure/MS token health check — reads local az CLI cache + live az probes.

Checks both the global ~/.azure cache and any per-project isolated contexts
under ~/.azure-contexts/<projekt> (see dernerl/claude-config
docs/azure-token-isolation.md for the AZURE_CONFIG_DIR pattern).

Never reads token secrets themselves, only account/tenant metadata and
whether a live `az` call resolves. Writes state.json (machine-readable)
and reports/latest.md (human-readable).
"""
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

AZURE_DIR = Path.home() / ".azure"
CONTEXTS_DIR = Path.home() / ".azure-contexts"
EXTENSION_DIR = AZURE_DIR / "cliextensions"
PROJECT_SEARCH_ROOTS = [Path.home() / "projects", Path.home() / "Desktop", Path.home() / "Desktop" / "YOLO-WORKBENCH"]
HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"
REPORT_PATH = HERE / "reports" / "latest.md"


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
            return None, (proc.stderr or "").strip().splitlines()[-1:] or ["az failed"]
        return json.loads(proc.stdout or "null"), None
    except FileNotFoundError:
        return None, ["az CLI nicht gefunden"]
    except subprocess.TimeoutExpired:
        return None, ["az CLI Timeout"]
    except json.JSONDecodeError:
        return None, ["az CLI lieferte kein gültiges JSON"]


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


def severity_for(no_default_set, default_dead, drift, identities, concurrent=None):
    if default_dead or any(i["status"] == "dead" for i in identities):
        return "red"
    if no_default_set or drift > 0 or concurrent or any(i["status"] == "partial" for i in identities):
        return "orange"
    return "green"


def resolve_context(config_dir: Path | None):
    """Same shape of health check, scoped to config_dir (None = global ~/.azure)."""
    base = config_dir if config_dir is not None else AZURE_DIR
    profile = load_json(base / "azureProfile.json") or {}
    cached_subs = profile.get("subscriptions", [])
    default_sub = next((s for s in cached_subs if s.get("isDefault")), None)

    live_subs, live_list_err = run_az(["account", "list"], config_dir=config_dir)
    live_subs = live_subs or []
    live_ids = {s.get("id") for s in live_subs}

    _, live_show_err = run_az(["account", "show"], config_dir=config_dir)

    no_default_set = default_sub is None
    default_dead = False
    if default_sub is not None:
        default_dead = (default_sub["id"] not in live_ids) or bool(live_show_err)

    drift = len(cached_subs) - len(live_subs)

    # Per-user rollup: only users with cached subscriptions can be scored
    # (an MSAL account with zero subscriptions is normal for Graph-only
    # logins and cannot be assessed without a side-effecting live call).
    by_user = {}
    for s in cached_subs:
        user = s.get("user", {}).get("name", "?")
        by_user.setdefault(user, {"cached": 0, "live": 0})
        by_user[user]["cached"] += 1
        if s.get("id") in live_ids:
            by_user[user]["live"] += 1

    identities = []
    for username in cached_usernames(base):
        counts = by_user.get(username)
        if counts is None:
            identities.append({"username": username, "status": "no-subscription-context"})
        elif counts["live"] == 0:
            identities.append({"username": username, "status": "dead", "cached": counts["cached"]})
        elif counts["live"] < counts["cached"]:
            identities.append({"username": username, "status": "partial", "cached": counts["cached"], "live": counts["live"]})
        else:
            identities.append({"username": username, "status": "ok", "cached": counts["cached"]})

    return {
        "no_default_set": no_default_set,
        "default_subscription": (
            {
                "name": default_sub.get("name"),
                "user": default_sub.get("user", {}).get("name"),
                "tenant": default_sub.get("tenantId"),
                "dead": default_dead,
            }
            if default_sub else None
        ),
        "identities": identities,
        "cached_subscription_count": len(cached_subs),
        "live_subscription_count": len(live_subs),
        "drift": drift,
        "live_list_error": live_list_err,
        "live_show_error": live_show_err,
    }


def discover_contexts():
    if not CONTEXTS_DIR.is_dir():
        return []
    return sorted(p for p in CONTEXTS_DIR.iterdir() if p.is_dir())


def find_project_root(context_dir: Path):
    target = str(context_dir)
    for root in PROJECT_SEARCH_ROOTS:
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
        global_result["drift"], global_result["identities"], concurrent,
    )

    contexts = []
    worst_severity = global_severity
    for ctx_dir in discover_contexts():
        result = resolve_context(ctx_dir)
        ctx_severity = severity_for(
            result["no_default_set"], result["default_subscription"] and result["default_subscription"]["dead"],
            result["drift"], result["identities"],
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
    "dead": "gecachte Subscriptions lösen live nicht mehr auf",
    "partial": "manche gecachten Subscriptions lösen live nicht mehr auf",
    "ok": "alle gecachten Subscriptions lösen live auf",
    "no-subscription-context": "kein Subscription-Kontext gecacht (z. B. Graph-only Login)",
}
SEVERITY_MARK = {"red": "🔴", "orange": "🟠", "green": "🟢"}


def render_report(state: dict) -> str:
    lines = [f"# Azure/MS Token Watchdog — {state['generated']}", ""]
    ds = state["default_subscription"]
    if state["no_default_set"]:
        lines.append("**Default Subscription (global ~/.azure):** 🟠 keine gesetzt — `az account set --subscription <name>`")
    elif ds:
        status = "🔴 TOT (isDefault, aber löst live nicht auf)" if ds["dead"] else "🟢 lebt"
        lines.append(f"**Default Subscription (global ~/.azure):** {ds['name']} ({ds['user']}) — {status}")
    lines.append("")
    lines.append(f"**Cache-Drift (global):** {state['cached_subscription_count']} gecacht vs. {state['live_subscription_count']} live "
                 f"({'Δ ' + str(state['drift']) if state['drift'] else 'keine Differenz'})")
    lines.append("")
    if state["concurrent_az_processes"]:
        lines.append("**⚠️ Laufende az/azd-Prozesse gerade jetzt:**")
        for p in state["concurrent_az_processes"]:
            lines.append(f"- {p}")
        lines.append("")
    lines.append("## Identitäten im globalen Cache")
    for ident in state["identities"]:
        mark = STATUS_MARK[ident["status"]]
        label = STATUS_LABEL[ident["status"]]
        lines.append(f"- {mark} {ident['username']} — {label}")
    lines.append("")
    lines.append("## Projekt-Kontexte (isolierte AZURE_CONFIG_DIR)")
    if not state["contexts"]:
        lines.append("- (keine gefunden unter `~/.azure-contexts/`)")
    for ctx in state["contexts"]:
        mark = SEVERITY_MARK.get(ctx["severity"], "⚪")
        label = ctx["project_root"] or ctx["name"]
        cds = ctx["default_subscription"]
        if ctx["no_default_set"]:
            default_info = "keine Default Subscription gesetzt"
        elif cds:
            default_info = f"Default: {cds['name']} ({'tot' if cds['dead'] else 'lebt'})"
        else:
            default_info = "keine Subscriptions gecacht"
        lines.append(f"- {mark} {label} — {default_info}")
    return "\n".join(lines) + "\n"


def main():
    state = check()
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(render_report(state), encoding="utf-8")
    print(f"severity={state['severity']} drift={state['drift']} contexts={len(state['contexts'])}")


if __name__ == "__main__":
    main()
