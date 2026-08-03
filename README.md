# azure-token-watchdog

Checks whether the local `az` CLI token cache still matches reality — before a
script trips over an expired token halfway through a run.

The problem: `~/.azure` caches subscriptions and accounts. When a refresh token
expires or a subscription is revoked, the cache stays put anyway. `az account list`
keeps returning entries that no longer resolve live. Anyone juggling several
identities at once (customer tenant, test tenant, personal) only notices when a
command runs against the wrong or a dead subscription.

The watchdog compares **cache against live response** and reports the difference.

## What gets checked

- **Default subscription** — is one set? And does it still resolve live at all?
- **Cache drift** — number of cached vs. live-resolving subscriptions
- **Per identity** — do all, some, or none of its subscriptions still resolve
- **Isolated project contexts** — every `~/.azure-contexts/<project>` (the
  `AZURE_CONFIG_DIR` pattern for separating tenants) is checked individually and
  mapped to the project that uses it
- **Concurrent `az`/`azd` processes** — if any are running, there is a race risk
  on the shared token cache

That rolls up into a traffic light: `green` / `orange` (drift, no default set,
partially dead) / `red` (an identity or the default subscription no longer resolves).

## How token material is handled

The watchdog reads `msal_token_cache.json`, but takes nothing from it except the
`username` field (`cached_usernames()`). Tokens, refresh tokens and secrets are
never read, never logged, never written.

The live check uses read-only commands (`az account list`, `az account show`) with
a 5s timeout. Nothing is modified — no `az login`, no `az account set`.

**The generated artifacts are sensitive regardless:** `state.json` and
`reports/latest.md` contain real usernames, tenant domains, possibly subscription
names along with `tenantId`, and local paths. Both are excluded via `.gitignore` —
please keep it that way.

## Usage

```sh
python3 token_watchdog.py
# severity=orange drift=0 contexts=1
```

Writes two files next to the script:

- `state.json` — machine-readable, for further processing
- `reports/latest.md` — human-readable report

No dependencies beyond Python 3.10+ (`X | None` syntax) and the Azure CLI.

### Configuration

To map an isolated context to the project using it, the watchdog searches the
immediate subdirectories of `~/projects` for a `.claude/settings.local.json` whose
`AZURE_CONFIG_DIR` matches. If your projects live elsewhere, use
`AZURE_WATCHDOG_PROJECT_ROOTS`:

```sh
AZURE_WATCHDOG_PROJECT_ROOTS="$HOME/work:$HOME/Desktop/WORKBENCH" python3 token_watchdog.py
```

Purely cosmetic — without a match the context is simply reported under its
directory name. Context discovery itself does not depend on it.

## Menu bar

The SwiftBar display deliberately lives in its own repo:
[dernerl/swiftbar-plugins](https://github.com/dernerl/swiftbar-plugins). It reads
`state.json` and is entirely optional — this tool runs on its own.

## License

MIT — see [LICENSE](LICENSE).
