# azure-token-watchdog

Checks whether the local `az` CLI token cache still matches reality — before a
script trips over an expired token halfway through a run.

The problem: `~/.azure` caches subscriptions and accounts. When a refresh token
expires or a subscription is revoked, the cache stays put anyway. `az account list`
keeps returning entries that no longer resolve live. Anyone juggling several
identities at once (customer tenant, test tenant, personal) only notices when a
command runs against the wrong or a dead subscription.

The watchdog asks az for a **real token** for every cached identity and tenant —
for Azure Resource Manager and for Microsoft Graph — and reports what fails.

Reading `azureProfile.json` (or `az account list`/`az account show`, which only read
that file) is not enough: an identity can look perfectly fine there while its refresh
token was revoked hours ago, e.g. by a Conditional Access sign-in frequency policy
(`AADSTS70043`). ARM and Graph can also expire independently.

## What gets checked

- **Default subscription** — is one set? And can an ARM token still be acquired for it?
- **Per identity and tenant** — can ARM and Graph tokens still be acquired? Failures
  are reported with their `AADSTS` code
- **Isolated project contexts** — every `~/.azure-contexts/<project>` (the
  `AZURE_CONFIG_DIR` pattern for separating tenants) is checked individually and
  mapped to the project that uses it
- **Concurrent `az`/`azd` processes** — if any are running, there is a race risk
  on the shared token cache

That rolls up into a traffic light: `green` / `orange` (no default set, or some
ARM/Graph tokens fail) / `red` (no ARM token for an identity or the default
subscription).

## How token material is handled

The watchdog reads `msal_token_cache.json`, but takes nothing from it except the
`username` field (`cached_usernames()`). Tokens, refresh tokens and secrets are
never read, never logged, never written.

The live check runs `az account get-access-token --query expiresOn` (20s timeout),
once per identity/tenant and resource. Only the expiry time reaches the watchdog —
never the token. Acquiring a token may refresh it in az's own cache, exactly like any
other az command would; nothing else is modified — no `az login`, no `az account set`.

**The generated artifacts are sensitive regardless:** `state.json` and
`reports/latest.md` contain real usernames, tenant domains, possibly subscription
names along with `tenantId`, and local paths. Both are excluded via `.gitignore` —
please keep it that way.

## Usage

```sh
python3 token_watchdog.py
# severity=orange contexts=1
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

## Design decisions

Architecture decisions are recorded as ADRs in [`docs/adr/`](docs/adr/).

## Menu bar

The SwiftBar display deliberately lives in its own repo:
[dernerl/swiftbar-plugins](https://github.com/dernerl/swiftbar-plugins). It reads
`state.json` and is entirely optional — this tool runs on its own.

## License

MIT — see [LICENSE](LICENSE).
