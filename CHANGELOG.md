# fedtail-sync changelog

The server's versions are fedtail's: one number for both, released together (a version of
fedtail-sync with nothing new in it is the same server under the new number). The protocol
number (`GET /v1`, [PROTOCOL.md](PROTOCOL.md)) changes only when a client would have to.

## 0.22.0 — in progress

The first version in a repository of its own (until now it came inside fedtail's).

- The pictures of drafts: `PUT` / `GET` / `DELETE /v1/media/<name>` (protocol 1 still —
  `GET /v1` says `media_kb` when the server keeps them). Sealed by the devices like the
  blobs, named by a keyed hash (the same picture stored once). Guarded against a loop of
  drafts: a quota of their own per account where the least used go first
  (`media_quota_mb`, 50), a size (`max_media_kb`, 4096), a pace (`media_per_minute`, 30),
  an age (`media_keep_days`, 30); counted in `total_mb`; gone with the journal.

- Protocol 1: store, fetch and delete an account's sealed blobs; who is who asked of each
  account's own fediverse server with a `read:accounts` token.
- No more `/.well-known/fedtail-sync`: the server is a service of its own, and a client
  takes its address from its config. `public_url` is ignored with a warning.
- Guards itself — it faces the internet: `timeout_seconds` for a silent connection (a whole
  request within twice that), `max_connections`, `requests_per_minute_per_ip`, token checks
  capped per address and in all (`verifications_per_minute_per_ip`,
  `verifications_per_minute`), an honest `Content-Length` required, `total_mb` for the
  disk, no redirects and (with `"*"`) public peers only for the token check, a plain
  "internal error" for anything broken inside. `client_ip_header` for a proxy on the same
  machine (`CF-Connecting-IP`, `X-Real-IP`), believed from loopback only.
- `--version`; its own tests (`tests/`, the standard library only) at 100 % of lines
  (`tools/coverage.py --fail-under 100`), bandit, in a CI job a release waits for.
