# 1. Liveness via token acquisition instead of profile comparison

Date: 2026-10-08

## Status

Accepted

## Context

The watchdog is supposed to warn before a script hits an expired `az` token. Until
now it compared the subscriptions cached in `azureProfile.json` with the output of
`az account list` / `az account show`. Both commands only read that same file and
never acquire a token, so the comparison was tautological: the watchdog could not
detect a dead token at all.

On 2026-10-08 this surfaced in practice: a Conditional Access sign-in frequency
policy (9 h) had invalidated the ARM refresh token (`AADSTS70043`) in both the global
and an isolated context. `az containerapp update` failed, the watchdog showed green.
Graph calls kept working, so ARM and Graph tokens can expire independently.

The previous design deliberately avoided live calls with side effects. Acquiring a
token may refresh it in az's MSAL cache — but every regular `az` command does the
same, so this side effect is harmless.

## Decision

Per context, the watchdog probes each (identity, tenant) pair taken from the cached
subscriptions with `az account get-access-token --subscription <id> --query expiresOn`,
once for ARM and once with `--resource-type ms-graph`.

- Any failure counts; there is no allowlist of AADSTS codes. The AADSTS code (or the
  first `ERROR:` line) is reported.
- No ARM token for an identity, or for the default subscription → red; individual
  ARM/Graph failures → orange.
- `--query expiresOn` ensures only the expiry time reaches the watchdog, never the
  token itself.
- The cached-vs-live drift comparison is removed.

## Consequences

- The watchdog now detects expired/revoked tokens, including CA sign-in frequency.
- Expect red regularly (e.g. every morning under a 9 h policy) — that is correct.
- A run takes longer (two `az` calls per identity/tenant, ~10 s observed) and needs
  network access; the probe timeout is 20 s.
- `state.json` no longer contains `drift`, `cached_subscription_count`,
  `live_subscription_count`, `live_list_error`, `live_show_error`. Consumers reading
  them with defaults (the SwiftBar plugins) keep working.
- Identities without any cached subscription (Graph-only logins) remain unassessed:
  `get-access-token --tenant` would probe the active account, not necessarily theirs.
