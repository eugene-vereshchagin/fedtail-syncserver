# fedtail-sync

[По-русски](README.ru.md)

The meeting point of one fediverse account's devices for
[fedtail](https://github.com/eugene-vereshchagin/fedtail): every device sends it a sealed
journal — read marks, threads followed or muted, lanes, drafts — and picks up the others'.
It can't read them: they are encrypted (AES-256-GCM) with a key only the devices have. All
it learns is how big they are and when.

Whose: a journal is its key's. The devices derive from the key a journal id and a secret
(the key itself never goes there): whoever has the key has the journal. No accounts, no
passwords, no fediverse server asked anything. A journal is made only with an **invite
code** you give: one per person, as many as you like, each taken back when you want — and
the journals made with it go with it.

One file, the Python standard library only (3.11+), SQLite. Versions are fedtail's own (0.22 the first apart), protocol 2
([PROTOCOL.md](PROTOCOL.md)); changes — [CHANGELOG.md](CHANGELOG.md). License GPL-3.0-or-later.

```sh
python3 fedtail_sync.py --version
python3 fedtail_sync.py --config /srv/fedtail-sync/config.toml                 # serve
python3 fedtail_sync.py --config /srv/fedtail-sync/config.toml invite add anna   # a code
python3 fedtail_sync.py --config /srv/fedtail-sync/config.toml invite list
python3 fedtail_sync.py --config /srv/fedtail-sync/config.toml invite revoke anna
```

## Installing


It is one file of the standard library: [`fedtail_sync.py`](fedtail_sync.py)
(Python 3.11+, SQLite). Copy it to the server, nothing else of fedtail is needed there.

A folder of its own, say `/srv/fedtail-sync/`, with the file, its config and its
database; a system user for it:

```sh
sudo useradd --system --no-create-home --shell /usr/sbin/nologin fedtail-sync
sudo mkdir -p /srv/fedtail-sync
sudo cp fedtail_sync.py /srv/fedtail-sync/
sudo chown -R fedtail-sync: /srv/fedtail-sync
```

`/srv/fedtail-sync/config.toml`:

```toml
listen = "127.0.0.1:8471"
db = "/srv/fedtail-sync/sync.db"
quota_mb = 20                 # per journal; a year of reading is a few MB
max_blob_kb = 512
requests_per_minute = 120     # per journal
keep_days = 0                 # 0: kept until the journal is deleted
total_mb = 1024               # all journals together; 0 — no cap
timeout_seconds = 30          # a silent connection is dropped (a whole request: twice that)
max_connections = 64          # served at once; more are closed at the door
requests_per_minute_per_ip = 240
new_journals_per_minute_per_ip = 3    # journals made (each needs an invite code)
escrow_gets_per_minute_per_ip = 5     # the key under a password asked for
client_ip_header = "CF-Connecting-IP"  # behind cloudflared/nginx on this machine; "" — none
max_media_kb = 4096           # one picture of a draft (sealed, like the blobs)
media_quota_mb = 50           # pictures per journal; past it the least used go; 0 — none here
media_keep_days = 30          # a picture unused this long is gone
media_per_minute = 30         # pictures put per journal
```

An older config (protocol 1) still starts: `instances` and `verifications_per_minute*` are
said to be gone and ignored. The journals of protocol 1 (filed by fediverse accounts) are
dropped at the first start — the devices send their state anew.

A systemd unit, `/etc/systemd/system/fedtail-sync.service`:

```ini
[Unit]
Description=fedtail-sync
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /srv/fedtail-sync/fedtail_sync.py --config /srv/fedtail-sync/config.toml
User=fedtail-sync
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/srv/fedtail-sync
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload && sudo systemctl enable --now fedtail-sync
curl -s http://127.0.0.1:8471/v2        # {"fedtail-sync": 2, …}
```

It speaks plain HTTP on localhost; TLS is done in front of it, on a name of its own
(say `sync.your.domain`) — the fediverse server is left alone.

**Cloudflare Tunnel** (the tunnel run from the dashboard): Zero Trust → Networks →
Tunnels → your tunnel → Published application routes (Public Hostname) → add one:
subdomain `sync`, your domain, path empty, service `http://127.0.0.1:8471`. `http`, not
`https` (Cloudflare does TLS), and `127.0.0.1`, not `localhost` (that may go to IPv6,
where the server doesn't listen). The DNS record is made by itself. With the tunnel in a
config file, the same as an ingress rule:

```yaml
  - hostname: sync.your.domain
    service: http://127.0.0.1:8471
```

**nginx**, a `server { … }` of its own:

```nginx
server {
    server_name sync.your.domain;
    # listen / ssl_certificate as for your other sites
    location / {
        proxy_pass http://127.0.0.1:8471;
        client_max_body_size 1m;
    }
}
```

**Caddy**:

```
sync.your.domain {
    reverse_proxy 127.0.0.1:8471
}
```

Check from outside: `curl -s https://sync.your.domain/v2` → `{"fedtail-sync": 2, …}`.

## Invite codes

```sh
python3 fedtail_sync.py --config … invite add anna        # invite anna: 1A2B3C4D-…
python3 fedtail_sync.py --config … invite list            # anna  made 2026-10-28  journals 1
python3 fedtail_sync.py --config … invite revoke anna     # and her journals gone
```

A code is shown once (only its hash is kept); case, spaces and dashes don't count when it
is typed. The person puts the server's address and the code in fedtail (≡ → Sync → Where,
or `fedtail sync invite CODE`) on their **first** device; it makes the journal. Their other
devices need only the key (a QR code, its text, or a password) — not the code. Taking a
code back closes and deletes every journal made with it; the others aren't touched.

## How it guards itself

It faces the internet, so every limit is on by default (the values above are the
defaults, but for `client_ip_header`):

| what someone might try | what stops it |
|---|---|
| open connections and stay silent, or trickle a byte now and then | dropped after `timeout_seconds` of silence; a whole request gets twice that |
| open thousands of connections | `max_connections` at once; the rest are closed at the door |
| flood it with requests | `requests_per_minute_per_ip` per address, `requests_per_minute` per journal |
| make journals of their own and fill the disk | a journal is made only with an invite code (`invite add`), at most `new_journals_per_minute_per_ip`; a code taken back takes its journals with it |
| guess someone's journal | its id and secret are derived from a 256-bit key: nothing to guess; each try counts against the address and the journal |
| guess a password for the key kept under it | the id it is kept under is stretched from the password (scrypt), and asking is slow: `escrow_gets_per_minute_per_ip` |
| a lying or missing `Content-Length`, a chunked body | refused before reading (411, 400, 413); a body shorter than promised doesn't hold a thread |
| fill the disk | each device packs its own journal into a snapshot now and then, dropping its older blobs; `quota_mb` per journal and `total_mb` for everyone |
| make drafts with pictures in a loop | pictures have a quota of their own (`media_quota_mb`): past it the least used go, so a loop churns only its own pictures — the journal, the other journals and the disk don't feel it; each at most `max_media_kb`, `media_per_minute` a minute, the same picture stored once, unused ones gone after `media_keep_days` |
| make it show its insides | an error inside answers "internal error"; the details go to its log |

**Behind a proxy on the same machine** — cloudflared, nginx, Caddy — every request comes
from 127.0.0.1, and the per-address limits would treat the whole world as one client. Set
`client_ip_header` to the header that proxy puts the real address in: `CF-Connecting-IP`
for Cloudflare, `X-Real-IP` for nginx with `proxy_set_header X-Real-IP $remote_addr;`. It
is believed only from a loopback peer, and only if it holds an address — a client can't
fake its way around the limit. Listening on anything but 127.0.0.1 makes it warn at start:
it speaks plain HTTP.

The tests of each of these are in [`tests/test_security.py`](tests/test_security.py); CI runs
them, with bandit, on every push, and a release can't go out without them.

What the server keeps: per journal (an id derived from its key, a hash of its secret, the
invite it was made with), sealed blobs with the sending device's random id and a time.
What it logs: one line per request — the address, the path, the answer — never a secret
or a code. `DELETE /v2/blobs` from a device of the journal removes it whole.

It doesn't know whose fediverse account a journal belongs to, or whether two journals are
one person's: a beta tester on any server, with your code, puts the same
`url = "https://sync.your.domain"` in their config.


## Tests

```sh
python3 -m unittest discover -s tests -t .      # from this folder; the standard library only
python3 tools/coverage.py --fail-under 100      # line coverage (Python 3.12+), must stay 100 %
```

`tests/test_server.py` — the journal, limits, invites, the key under a password, old
journals, the config and the program; `tests/test_security.py` — every way to hang, flood
or fill it, or make it say what it shouldn't. Report a vulnerability privately: [SECURITY](https://github.com/eugene-vereshchagin/fedtail/security/advisories/new).
