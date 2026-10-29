# fedtail-sync protocol 2

What a client (fedtail, or anything else) and a fedtail-sync server say to each other.
The server is a dumb store of opaque blobs per journal; everything about what a blob means
belongs to the clients. This page is the contract between the two: either side may change
anything else, but not this without a new protocol number.

Protocol 1 (fedtail 0.21) filed journals by fediverse accounts, checked with a
`read:accounts` token; protocol 2 (0.22) files them by their key, made with an invite code.
A server of protocol 2 answers `GET /v1` too — with `"fedtail-sync": 2`, so a client of 1
learns there's another protocol here and goes no further; the paths of 1 are gone.

## Transport

HTTPS (the server itself speaks plain HTTP on localhost behind a proxy that does TLS).
JSON answers, `Content-Type: application/json`, `Cache-Control: no-store`. One request per
connection (HTTP/1.0 answers). Every error is `{"error": "words for a person"}` with the
status below; a client goes by the status, never by the words.

## Whose journal

A journal is its key's: the devices that share the key derive from it

    journal id = HMAC-SHA256(key, b"fedtail-journal-id"), the first 32 hex digits
    secret     = HMAC-SHA256(key, b"fedtail-journal-secret"), 64 hex digits

and the key itself never comes to the server. Every call but `GET /v2` and
`GET /v2/escrow` carries:

| header | what |
|---|---|
| `X-Fedtail-Journal: <id>` | the journal, 32 lowercase hex digits |
| `Authorization: Bearer <secret>` | its secret, 64 lowercase hex digits |
| `X-Fedtail-Invite: <code>` | only while the journal isn't there yet: an invite code the server's admin gave (case, spaces and dashes don't count) |

A journal the server doesn't know is made by the first call that names it with a good
invite code (at most `new_journals_per_minute_per_ip`); without one — `403`. Then the
secret is what it checks (a hash of it is kept). An invite taken back by the admin takes
the journals made with it away: their calls get `403` again.

## Calls

### `GET /v2`

`200 {"fedtail-sync": 2, "compact": 1, "escrow": 1, "media_kb": 4096}` — the protocol
number; `compact` — it takes `DELETE /v2/devices/<device>`; `escrow` — it keeps the key under
a password; `media_kb` when it keeps the pictures of drafts (the most one may be, in KB; no
key — it doesn't). No journal named. A client that finds another number goes no further;
one that finds no `media_kb` sends no pictures, one that finds no `compact` never packs its
journal.

### `POST /v2/blobs`

The body is one blob, raw bytes, with an exact `Content-Length` (no chunked bodies). Header
`X-Fedtail-Device`: the sending device's id, `[A-Za-z0-9_-]{0,32}` (fedtail: 16 random hex
digits).

`200 {"seq": n}` — its place in the journal: a number that only grows, shared by all the
journal's devices.

### `GET /v2/blobs?after=N&limit=M&exclude=DEVICE`

The journal's blobs with `seq > N` (default 0), oldest first, at most `M` (default 200,
1…500), leaving out those `DEVICE` sent (same pattern as the header; empty — leave none).
`N` and `M` are decimal, at most 19 digits.

```json
{"blobs": [{"seq": 7, "device": "3f2a…", "data": "<base64>"}],
 "last": 9, "more": false}
```

`last` — the cursor for the next call: the last `seq` looked at, the left-out ones
included (so a device never asks for its own again); `N` itself when nothing came. `more`
— there are more past `last`.

### `DELETE /v2/blobs`

The whole journal's blobs gone, its pictures and its key under a password with them.
`200 {"deleted": n}`.

### `DELETE /v2/devices/<device>?before=N`

That device's blobs with `seq < N` gone — the others' stay. A device calls it right after it
has sent a snapshot of everything it knows (whose first blob got `seq` N), so its older
blobs only take up the quota. `200 {"deleted": n}`; no `before` — nothing goes. `<device>` —
the pattern of `X-Fedtail-Device`, not empty.

### `PUT /v2/escrow` · `GET /v2/escrow`

The journal's key sealed by a password (fedtail: `FTK2`, below), so that a new device with
only the password and its fediverse account takes it. The id, in the header
`X-Fedtail-Escrow` (never in the URL, so no request line in a log holds it) — 64 hex digits
the client derives from the password and the account:

    id = scrypt(password, salt = b"fedtail-escrow-id:" + the server's address, lowercase,
                no trailing "/" + b"\n" + the account, "user@host" lowercase,
                n = 2**17, r = 8, p = 1, 32 bytes), hex

The account in it makes each id one person's: a guess of a password tests one account,
not everyone on the server, and two people with the same password don't meet.

`PUT` (a device of the journal; body at most 4096 bytes) → `200 {"stored": true}`; one per
journal — a new one replaces the journal's own; an id held by another journal → `409`.
`GET` names no journal: `200` with the bytes, or `404`; asked at most
`escrow_gets_per_minute_per_ip` (an IPv6 address counts by its /64) and
`escrow_gets_per_minute` by everyone together.

### `PUT /v2/media/<name>`

One picture of a draft: raw sealed bytes, an exact `Content-Length`, at most `media_kb`.
`<name>` — 32 to 64 lowercase hex digits the client makes (fedtail: a keyed hash of the
picture, so the same picture has the same name and the store learns nothing from it).

`200 {"stored": true}` — kept; `{"stored": false}` — it was there already (not counted
twice). The journal's pictures have a quota of their own: past it the server drops the
least used ones (put or fetched longest ago) to make room — a client must expect a picture
to be gone (404) and does without it. Unused for the server's days, a picture goes too.

### `GET /v2/media/<name>`

`200` with the bytes (`application/octet-stream`), or `404`. Fetching it counts as using it.

### `DELETE /v2/media/<name>`

`200 {"deleted": 1}`, or `{"deleted": 0}` — there was none.

## Statuses

| status | when | what a client does |
|---|---|---|
| 200 | done | — |
| 400 | a bad number, device id, picture name, escrow id, query, empty blob, a body shorter than its length | a bug: report it |
| 401 | no journal named, an odd id or secret, not this journal's secret | the key isn't this journal's |
| 403 | a new journal without an invite code, a code not known (or taken back), a code that has made all the journals it may (`journals_per_invite`) | ask for an invite code |
| 404 | no such path; no such picture or key under a password; the server keeps no pictures | not a fedtail-sync 0.22; do without it |
| 409 | a key under a password put under an id another journal holds | another password |
| 411 | no `Content-Length`, or a chunked body | send the length |
| 413 | the blob is over the server's `max_blob_kb`, the picture over `media_kb`, the key over 4 KB | smaller batches; the picture stays on its device |
| 429 | too many requests (per address, per journal), new journals, keys asked for | try again in a minute |
| 501 | another method | — |
| 507 | over the journal's quota, or the server is full | delete the journal, or another server |
| 500 | something broke inside the server (it is in its log) | try again later |

## Blobs

The server never looks inside. fedtail's blobs, for anyone writing another client:

    blob = b"FTS1" | key id (8) | nonce (12) | AES-256-GCM(zlib(json)) — the first 12 bytes as AAD

The key is 32 random bytes the account's first device makes and the others are given by
hand; the key id is 8 bytes of a hash of it (a blob sealed by another key is told at once).
The JSON is `{"v": 1, "d": device, "at": ms, "ops": [...]}`; what the ops are is fedtail's
business (`fedtail/core/sync/engine.py`) and may grow without a new protocol number.

A picture is sealed the same way, its bytes in place of the zlib'd JSON:

    picture = b"FTM1" | key id (8) | nonce (12) | AES-256-GCM(the file's bytes) — the first 12 bytes as AAD

and named by `HMAC-SHA256(key, b"fedtail-media" | the file's bytes)`, the first 48 hex digits.

The key under a password:

    escrow = b"FTK2" | salt (16) | nonce (12) | AES-256-GCM(the key) — the first 20 bytes as AAD

sealed by `scrypt(password, salt, n = 2**17, r = 8, p = 1, 32 bytes)` (`FTK1`, from
fedtail 0.21 in a folder's journal: the same with `n = 2**15` — opened, never made).
