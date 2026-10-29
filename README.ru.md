# fedtail-sync

[In English](README.md)

Место встречи устройств одного аккаунта федиверса для
[fedtail](https://github.com/eugene-vereshchagin/fedtail): каждое устройство присылает сюда
зашифрованный журнал — прочитанное, подписки и заглушенные треды, ленты, черновики — и
забирает журналы остальных. Прочесть их сервер не может: они зашифрованы (AES-256-GCM)
ключом, который есть только у устройств. Он знает лишь, какого они размера и когда пришли.

Чьи: журнал принадлежит ключу. Устройства выводят из ключа id журнала и секрет (сам ключ
туда не уходит): у кого ключ, у того и журнал. Ни аккаунтов, ни паролей, сервер федиверса
ни о чём не спрашивается. Журнал заводится только по **коду приглашения**, который даёшь
ты: по коду на человека, сколько угодно, каждый можно отозвать — и журналы, заведённые по
нему, уйдут вместе с ним.

Один файл, только стандартная библиотека Python (3.11+), SQLite. Версии — те же, что у fedtail (0.22 — первая отдельно),
протокол 2 ([PROTOCOL.md](PROTOCOL.md)); изменения — [CHANGELOG.md](CHANGELOG.md). Лицензия
GPL-3.0-or-later.

```sh
python3 fedtail_sync.py --version
python3 fedtail_sync.py --config /srv/fedtail-sync/config.toml                 # работать
python3 fedtail_sync.py --config /srv/fedtail-sync/config.toml invite add anna   # код
python3 fedtail_sync.py --config /srv/fedtail-sync/config.toml invite list
python3 fedtail_sync.py --config /srv/fedtail-sync/config.toml invite revoke anna
```

## Установка


Это один файл на стандартной библиотеке: [`fedtail_sync.py`](fedtail_sync.py)
(Python 3.11+, SQLite). Скопируй его на сервер — больше ничего от fedtail там не нужно.

Своя папка, например `/srv/fedtail-sync/`, — в ней файл, его конфиг и база; системный
пользователь для него:

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
quota_mb = 20                 # на журнал; год чтения — несколько МБ
max_blob_kb = 512
requests_per_minute = 120     # на журнал
keep_days = 0                 # 0: хранить, пока журнал не удалят
total_mb = 1024               # на все журналы вместе; 0 — без предела
timeout_seconds = 30          # a silent connection is dropped (a whole request: twice that)
max_connections = 64          # served at once; more are closed at the door
requests_per_minute_per_ip = 240
new_journals_per_minute_per_ip = 3    # новых журналов (каждому нужен код приглашения)
escrow_gets_per_minute_per_ip = 5     # запросов ключа, лежащего под паролем (IPv6: на /64)
escrow_gets_per_minute = 60           # …от всех вместе; 0 — без предела
journals_per_invite = 5               # журналов на один код приглашения; 0 — сколько угодно
client_ip_header = "CF-Connecting-IP"  # behind cloudflared/nginx on this machine; "" — none
max_media_kb = 4096           # одна картинка черновика (запечатана, как и блобы)
media_quota_mb = 50           # картинок на журнал; сверх — уходят давно не нужные; 0 — без картинок
media_keep_days = 30          # картинка, не нужная столько дней, удаляется
media_per_minute = 30         # картинок в минуту на журнал
```

Старый конфиг (протокол 1) запустится: про `instances` и `verifications_per_minute*` сервер
скажет, что их больше нет, и пропустит. Журналы протокола 1 (по аккаунтам федиверса) при
первом запуске удаляются — устройства пришлют своё состояние заново.

Юнит systemd, `/etc/systemd/system/fedtail-sync.service`:

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

Говорит он обычным HTTP на localhost; TLS делается перед ним, на его собственном имени
(например, `sync.твой.домен`) — сервер федиверса не трогаем.

**Cloudflare Tunnel** (туннель из дашборда): Zero Trust → Networks → Tunnels → твой
туннель → Published application routes (Public Hostname) → добавить: поддомен `sync`,
твой домен, путь пустой, сервис `http://127.0.0.1:8471`. Именно `http`, а не `https`
(TLS делает Cloudflare), и `127.0.0.1`, а не `localhost` (тот может уйти в IPv6, где
сервер не слушает). DNS-запись появится сама. Если туннель настроен файлом — то же
правилом ingress:

```yaml
  - hostname: sync.your.domain
    service: http://127.0.0.1:8471
```

**nginx** — отдельный `server { … }`:

```nginx
server {
    server_name sync.your.domain;
    # listen / ssl_certificate — как у остальных твоих сайтов
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

Проверка снаружи: `curl -s https://sync.твой.домен/v2` → `{"fedtail-sync": 2, …}`.

## Коды приглашения

```sh
python3 fedtail_sync.py --config … invite add anna        # invite anna: 1A2B3C4D-…
python3 fedtail_sync.py --config … invite list            # anna  made 2026-10-28  journals 1
python3 fedtail_sync.py --config … invite revoke anna     # и её журналы удалены
```

Код показывается один раз (хранится только его хэш); регистр, пробелы и дефисы при вводе
не важны. Человек вводит адрес сервера и код в fedtail (≡ → Синхронизация → Где или
`fedtail sync invite КОД`) на **первом** устройстве — оно заводит журнал. Остальным его
устройствам нужен только ключ (QR-код, текст или пароль), код — нет. Отзыв кода закрывает и
удаляет все журналы, заведённые по нему; остальные не трогаются.

## Как он себя защищает

Он смотрит в интернет, поэтому все ограничения включены по умолчанию (значения выше — и
есть умолчания, кроме `client_ip_header`):

| что можно попробовать | что этому мешает |
|---|---|
| открыть соединения и молчать или слать по байту раз в полминуты | обрыв после `timeout_seconds` тишины; на весь запрос — вдвое больше |
| открыть тысячи соединений | одновременно не больше `max_connections`, остальные закрываются сразу |
| завалить запросами | `requests_per_minute_per_ip` на адрес, `requests_per_minute` на журнал |
| наплодить своих журналов и забить диск | журнал заводится только по коду приглашения (`invite add`), не чаще `new_journals_per_minute_per_ip`, не больше `journals_per_invite` на код; отозванный код забирает свои журналы с собой |
| угадать чужой журнал | id и секрет выводятся из 256-битного ключа — угадывать нечего; каждая попытка считается на адрес и на журнал |
| подобрать пароль к ключу, лежащему под ним | id, под которым он лежит, растянут (scrypt, 128 МБ на попытку) из пароля *и аккаунта в федиверсе*, так что попытка проверяет одного человека, а не всех здесь; он идёт в заголовке, а не в URL, который пишется в лог; спрашивать можно медленно с адреса (IPv6 /64 — один адрес) и всем вместе: `escrow_gets_per_minute_per_ip`, `escrow_gets_per_minute` |
| подложить свой ключ вместо чужого | id, занятый другим журналом, отклоняется (409): журнал заменяет только свой |
| врать в `Content-Length`, не указывать его, слать кусками (chunked) | отказ до чтения (411, 400, 413); тело короче обещанного не держит поток |
| забить диск | каждое устройство время от времени упаковывает свой журнал в снимок и удаляет свои старые блобы; `quota_mb` на журнал и `total_mb` на всех |
| плодить черновики с картинками в цикле | у картинок своя квота (`media_quota_mb`): сверх неё уходят давно не нужные, так что цикл перемалывает только свои же картинки — журнал, другие журналы и диск этого не чувствуют; каждая не больше `max_media_kb`, не чаще `media_per_minute` в минуту, одна и та же хранится один раз, ненужные уходят через `media_keep_days` |
| выведать его внутренности | ошибка внутри отвечает «internal error», подробности — только в его лог |

**За прокси на той же машине** — cloudflared, nginx, Caddy — все запросы приходят с
127.0.0.1, и лимиты на адрес считали бы весь мир одним клиентом. Укажи в `client_ip_header`
заголовок, куда прокси кладёт настоящий адрес: `CF-Connecting-IP` у Cloudflare, `X-Real-IP`
у nginx с `proxy_set_header X-Real-IP $remote_addr;`. Ему верят только от локального
соседа и только если в нём адрес — клиент так лимит не обойдёт. Если слушать не на
127.0.0.1, сервер при старте предупредит: он говорит обычным HTTP.

Тесты на каждое из этого — в [`tests/test_security.py`](tests/test_security.py); CI гоняет их
вместе с bandit на каждый пуш, и без них релиз не выйдет.

Что хранит сервер: для каждого журнала (id, выведенный из ключа, хэш секрета, код, по
которому он заведён) — зашифрованные куски со случайным id устройства и временем. Что пишет
в лог: строку на запрос — адрес, путь, ответ, — никогда не секрет и не код.
`DELETE /v2/blobs` с устройства журнала удаляет его целиком.

Чей это аккаунт федиверса и не одного ли человека два журнала — сервер не знает:
бета-тестер с любого сервера с твоим кодом прописывает в конфиге тот же
`url = "https://sync.твой.домен"`.


## Тесты

```sh
python3 -m unittest discover -s tests -t .      # из этой папки; только стандартная библиотека
python3 tools/coverage.py --fail-under 100      # покрытие строк (Python 3.12+), должно быть 100 %
```

`tests/test_server.py` — журнал, лимиты, коды приглашения, ключ под паролем, старые
журналы, конфиг и сама программа; `tests/test_security.py` — все способы повесить его,
завалить, забить диск или выведать лишнее. Об уязвимости — приватно: [SECURITY](https://github.com/eugene-vereshchagin/fedtail/security/advisories/new).
