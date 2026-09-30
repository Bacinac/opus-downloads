# OPUS · Downloads

**One interface over every source of acquisition.**

OPUS is a household collection of music, films, series, web video and
photographs in three modules. [Library](https://github.com/Bacinac/opus-library)
decides what is worth having, what counts as having it and what a file is
called; Downloads acquires it from every source behind one interface;
[Player](https://github.com/Bacinac/opus-player) plays it on whatever it runs
on: a television, a phone or a DAC. Each module is an application of its own,
with its own address and its own release; they share the sign-in, the look and
the words.

## What it does

Library and Player ask, and Downloads acquires. It searches indexers,
downloads through Usenet, torrent and Soulseek, fetches web video with yt-dlp,
follows the job to its end and hands the file over in the landing zone with its
path. It sets up and wires its engines itself, and any of them can
be swapped for an external instance.

## What sets it apart

Each module once drove the same workers on its own, each with its own
addresses, keys and categories. Downloads is one mechanism for all of them:
search, inspect, grab, report and cancel, whatever the protocol. The judgement
stays with whoever asks: which release is worth taking, whether it passes the
check and what the file will be called. The job of Downloads ends at "the file
is in the landing zone; here is the path".

## How it works

Every engine implements one interface, and the API never switches on
protocol: a release's `grab_ref` names the engine it came from. Prowlarr,
SABnzbd, qBittorrent and slskd search an indexer and grab a release, either
bundled (a container Downloads starts and wires itself) or external (an
existing instance set on the Settings page); yt-dlp fetches by address inside
the backend. Each finished file is reported in the landing zone as Downloads
sees it, in the subtree of the caller that asked for it. A VPN can be switched
on per engine, and only an internal controller ever touches the Docker socket.
The system map is in [ARCHITECTURE.md](ARCHITECTURE.md).

## Technology

Python 3.14, FastAPI and SQLAlchemy 2.1, Postgres 18, SvelteKit 2 and Svelte 5.
Indexer engines: Prowlarr, SABnzbd, qBittorrent and slskd, with FlareSolverr
beside Prowlarr for indexers behind Cloudflare. Web video: yt-dlp. Everything
runs in containers.

## Install

OPUS needs Docker with Compose v2, `python3` and `curl`.

Downloads is installed as part of the OPUS suite, on the storage server beside
Library; the suite installer connects the two. See the
[suite guide](https://github.com/Bacinac/opus-library/blob/main/suite/README.md).

On its own, from a clone:

```bash
git clone --recurse-submodules https://github.com/Bacinac/opus-downloads.git
cd opus-downloads
./install.sh
```

The installer expects Library's address (`OPUS_AUTH_URL`), the token Library
issued to Downloads (`OPUS_AUTH_TOKEN`) and the shared session key
(`OPUS_SESSION_KEY`) in the environment. The interface is on port `5282`, and the bundled engines' own
interfaces are on `8099`.

## Upgrade

```bash
git pull --recurse-submodules
./install.sh
```

The installer is idempotent: it rebuilds and restarts, and keeps every secret,
the database and the downloads.

## Development

```bash
docker compose up -d
```

| Service | Port |
|---|---|
| backend | 8097 |
| interface | 5282 |
| engine interfaces | 8099 |
| Postgres 18 | compose network only |

With `OPUS_UI_TARGET=dev` and `OPUS_DEV_RELOAD=1` in `.env`, code changes are
live without a restart. Migrations are applied by the backend on start; a new
one is

```bash
docker compose run --rm backend alembic revision --autogenerate -m "..."
```

`./check.sh` runs svelte-check, the words check, pyflakes and the backend's
tests against a throwaway database.

## License

OPUS · Downloads is licensed under
[PolyForm Noncommercial 1.0.0](LICENSE.md): free for personal and
non-commercial use. Commercial use requires a separate license; write to
[ivo.boskovic.zg@gmail.com](mailto:ivo.boskovic.zg@gmail.com). External
contributions (pull requests) are not accepted.

Required Notice: Copyright (c) 2026 Ivo Bošković
