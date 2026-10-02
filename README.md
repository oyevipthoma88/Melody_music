<div align="center">

# Melody Music

### Telegram voice-chat music, video, and group utilities — engineered for resilient playback

<sub><strong>README visual refresh · live local assets · main branch</strong></sub>

<a href="https://github.com/oyevipthoma88/Melody_music">
  <img src="https://img.shields.io/badge/Melody_Music-live_project-ff4d8d?style=for-the-badge&logo=github&logoColor=white" alt="Melody Music project">
</a>
<a href="https://www.python.org/">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776ab?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11 or newer">
</a>
<a href="https://github.com/Kurigram/Kurigram">
  <img src="https://img.shields.io/badge/Kurigram-2.2%2B-f28c28?style=for-the-badge" alt="Kurigram">
</a>
<a href="https://github.com/pytgcalls/pytgcalls">
  <img src="https://img.shields.io/badge/Py--TgCalls-2.3%2B-22c55e?style=for-the-badge" alt="Py-TgCalls">
</a>

<br>
<br>

<a href="https://heroku.com/deploy?template=https://github.com/oyevipthoma88/Melody_music">
  <img src="https://www.herokucdn.com/deploy/button.svg" alt="Deploy Melody Music to Heroku">
</a>
<a href="#docker">
  <img src="https://img.shields.io/badge/Run_with-Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Run with Docker">
</a>
<a href="#quick-start">
  <img src="https://img.shields.io/badge/Quick_Start-README-ff4d8d?style=for-the-badge&logo=readme&logoColor=white" alt="Quick start">
</a>

<br>
<br>

![Melody Music live voice-chat playback animation](assets/melody-live.svg)

<br>

![Melody Music project artwork featuring Modi and Meloni with headphones](assets/melody-hero.jpg)

<br>
<br>

[Deploy](#deploy) · [Features](#features) · [Quick start](#quick-start) · [Configuration](#configuration) · [Commands](#commands) · [Architecture](#architecture) · [Operations](#operations)

</div>

> **Melody Music** is an independently developed Telegram bot focused on voice-chat playback, queue reliability, group utilities, and operational safety. It uses external libraries for Telegram, voice transport, media extraction, and persistence; the application orchestration and reliability layers are maintained in this repository.

## Live project snapshot

| Status | Current behavior |
|---|---|
| Playback | Direct stream first, bounded fallback path, queue-aware recovery, and explicit audio/video intent. |
| Startup safety | Strict source-start budget with controlled timeout instead of an unbounded minute-long hang. |
| Large media | Direct/range-oriented paths are preferred; unsafe multi-gigabyte local fallback is blocked for long video. |
| Persistence | MongoDB-backed settings and validated playback snapshots with restart-aware state handling. |
| Operations | Disk guard, memory guard, task cleanup, VC watchdog, structured logs, and regression coverage. |
| Verification | Repository regression suite is run before production changes are published. |

## Deploy

### One-click Heroku

<a href="https://heroku.com/deploy?template=https://github.com/oyevipthoma88/Melody_music"><img src="https://www.herokucdn.com/deploy/button.svg" alt="Deploy Melody Music to Heroku"></a>

The button uses the repository's `app.json`, `Procfile`, Apt buildpack, Python buildpack, FFmpeg dependency, and worker formation. Before launching a public bot, fill every required Telegram, MongoDB, assistant-session, and logging value in the Heroku form. For YouTube reliability, configure permitted fresh `YT_COOKIES` as well.

> **Private repository note:** Heroku must be able to access the repository. If the deploy button cannot read this private repository in your Heroku account, use the Docker or manual deployment paths below, or make a deliberate private-repository deployment connection first. Never put tokens in the README URL.

### Docker

<a id="docker"></a>

```bash
docker build -t melody-music .
docker run --env-file .env --restart unless-stopped melody-music
```

The included `Dockerfile` installs FFmpeg and starts the worker through [`start`](start). Keep `.env` outside version control.

### Manual worker

Use the [Quick start](#quick-start) path when you need full control over Python, FFmpeg, MongoDB, cookies, worker limits, and process supervision.

## Features

### Music and voice chat

- `/play` audio playback from search terms, supported links, direct streams, and Telegram media.
- `/vplay` video playback where the assistant account, codecs, and voice-chat mode support it.
- Per-chat queue, now-playing state, shuffle, loop modes, remove-by-position, seek, rewind, pause, resume, speed, volume, mute, and replay.
- Autoplay with related-track selection, prefetching, mode preservation, generation-safe toggles, and duplicate prevention.
- Assistant-based voice-chat join, leave, recovery, activity notifications, and optional VC chat-log backup.
- Lyrics lookup, playlists, live/radio sources, saved playlists, and inline search utilities.

### Group management

- Moderation: ban, kick, mute, unmute, purge, clean, warnings, promotions, demotions, and admin inspection.
- Authorization: admin-controlled `/auth`, `/unauth`, and protected `/authlist` workflows.
- Protection controls for links, floods, abuse, NSFW content, and configurable group safety policies.
- Join-request inspection plus individual and bulk approve/decline flows.
- Welcome cards, goodbye messages, onboarding, support links, and feature guides.

### Reliability and operations

- Strict startup deadline for the direct-source and fallback race.
- Safe early audio handoff without treating growing files as completed cache files.
- Orphaned staging-directory cleanup after crashes and restarts.
- Bounded resolver/provider attempts, task cancellation, download priorities, and owner-scoped cancellation.
- Playback-state validation, stale-track protection, autoplay race protection, and VC roster watchdog.
- Private logging, health/status commands, owner maintenance tools, and deployment-oriented configuration.

## Quick start

### 1. Prepare Telegram credentials

Create an application at [my.telegram.org](https://my.telegram.org), create a bot with [@BotFather](https://t.me/BotFather), and prepare a Pyrogram/Kurigram assistant session. The assistant account must be able to join the target voice chats and speak in them.

### 2. Prepare MongoDB

Create a MongoDB database and keep its connection string private. Melody reads `MONGO_DB_URI`; never commit it to Git or paste it into public logs or issues.

### 3. Install system dependencies

The runtime needs Python 3.11+, FFmpeg, Git, and a stable network connection.

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg git python3 python3-venv
```

### 4. Install and configure

```bash
git clone https://github.com/oyevipthoma88/Melody_music.git
cd Melody_music
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` with the required values, then start the worker:

```bash
python -m melody
```

For production, use a process supervisor or a platform worker configured to restart on failure. Do not expose owner-only shell/eval functionality to untrusted users.

## Configuration

The full configuration template is in [`.env.example`](.env.example). The following values are the most important:

| Variable | Required | Purpose |
|---|:---:|---|
| `API_ID` / `API_HASH` | Yes | Telegram application credentials. |
| `BOT_TOKEN` | Yes | BotFather token. |
| `MONGO_DB_URI` | Yes | MongoDB connection string. |
| `STRING_SESSION` | Yes | Assistant/userbot session. Treat it as a password. |
| `OWNER_ID` | Yes | Numeric owner Telegram ID. |
| `LOG_GROUP_ID` | Yes | Private error/activity log group. |
| `BOT_USERNAME` | Recommended | Bot username used in links and onboarding. |
| `YT_COOKIES` | Strongly recommended for YouTube | Permitted YouTube cookie material in the supported configuration format. Keep it secret and rotate it. |
| `YT_COOKIES_2` … `YT_COOKIES_10` | Optional | Cookies of additional YouTube accounts. A flagged account (152-18 / bot-check) is rested for `YT_COOKIE_COOLDOWN` seconds and the next one is used automatically. |
| `BGUTIL_STARTUP_WARMUP` | Recommended | Warms the PO-token provider before first playback. |
| `MAX_CONCURRENT_DOWNLOADS` | Recommended | Keep conservative on small hosts; `1` is a safe starting point. |
| `PLAY_START_BUDGET` | Recommended | Strict playback source-start budget (default 4s, capped at 10s) so playback starts within ~5 seconds. |
| `MUSIC_ARCHIVE_CHANNEL_ID` | Optional | Explicit private archive channel; blank disables archival. |
| `VC_CHAT_LOG_CHANNEL_ID` | Optional | Private VC chat-log backup channel. |
| `PLAYBACK_RECOVERY` | Optional | Restart snapshot recovery; enable deliberately after testing. |
| `SOURCE_CODE_URL` / `SUPPORT_URL` | Optional | Links displayed by onboarding and source buttons. |

### Safe production baseline

```dotenv
MAX_CONCURRENT_DOWNLOADS=1
BGUTIL_STARTUP_WARMUP=true
STARTUP_WARMUPS=false
PLAY_START_BUDGET=4.0
MUSIC_ARCHIVE_CHANNEL_ID=
```

A strict startup budget prevents an unbounded wait; it cannot override an unavailable source, YouTube anti-bot response, Telegram permission error, expired cookies, or a network outage. Configure valid cookies and verify deployment logs before opening the bot to a large audience.

## Commands

### Playback

| Command | Purpose |
|---|---|
| `/play <song or URL>` | Play audio in the group voice chat. |
| `/vplay <song or URL>` | Play video where supported. |
| `/queue`, `/q` | Show the current queue and autoplay state. |
| `/np`, `/playing` | Show the current track. |
| `/skip`, `/s`, `/next` | Advance to the next track. |
| `/pause`, `/resume` | Pause or resume. |
| `/stop`, `/end` | Stop playback and clear the queue. |
| `/seek <seconds>` | Seek to a position. |
| `/seekback <seconds>`, `/rewind` | Seek backward. |
| `/speed <value>` | Change playback speed. |
| `/volume <0-200>` | Set volume. |
| `/mute`, `/unmute` | Toggle mute. |
| `/loop`, `/loopall`, `/noloop` | Configure repeat behavior. |
| `/shuffle`, `/remove <position>`, `/clearqueue` | Manage the queue. |
| `/autoplay on/off` | Toggle related-track autoplay. |
| `/lyrics <song>` | Request lyrics when configured. |
| `/playlist`, `/myplaylist`, `/addplaylist` | Manage saved playlists. |

Channel playback uses the corresponding `c*` command family, such as `/cplay`, `/cvplay`, `/cqueue`, `/cnp`, `/cskip`, `/cstop`, `/cpause`, `/cresume`, `/cseek`, `/cspeed`, and `/cvolume`.

### Administration and owner operations

Group administration includes `/ban`, `/unban`, `/kick`, `/mute`, `/unmute`, `/purge`, `/clean`, `/cleanall`, `/warn`, `/promote`, `/demote`, `/admins`, `/auth`, `/unauth`, `/authlist`, `/protection`, `/settings`, `/welcome`, `/goodbye`, `/rpending`, `/approverequest`, `/declinerequest`, `/rapproveall`, `/rdeclineall`, `/tagall`, and `/cancel`.

Owner operations include `/panel`, `/logs`, `/chatlist`, `/maintenance`, `/restart`, `/reboot`, `/reload`, `/broadcast`, `/gban`, `/ungban`, `/setpic`, `/delpic`, `/setsource`, `/shell`, `/eval`, `/sysinfo`, `/speedtest`, `/logger`, `/emojiid`, and `/ocmds`.

## Architecture

```text
Telegram bot updates ──┐
                       ├── handlers ── decorators ── group/admin policy
Assistant updates ─────┘                         │
                                                 ▼
                                      playback state + queue engine
                                                 │
                              ┌──────────────────┼──────────────────┐
                              ▼                  ▼                  ▼
                         resolver             cache          VC transport
                    InnerTube / yt-dlp    diskguard +       Py-TgCalls
                    provider fallbacks    early handoff     lifecycle
                              │                  │                  │
                              └──────────────────┼──────────────────┘
                                                 ▼
                                      Mongo snapshots + logs
```

| Directory | Responsibility |
|---|---|
| `melody/core/` | Playback, queues, resolver/download pipeline, VC lifecycle, autoplay, and state recovery. |
| `melody/plugins/music/` | Play commands, controls, playlists, channel playback, and media-facing handlers. |
| `melody/plugins/admin/` | Moderation, protection, greetings, authorization, and join requests. |
| `melody/plugins/misc/` | Onboarding, help, health, utility, and public command surface. |
| `melody/plugins/owner/` | Maintenance, logs, operations, assets, and owner controls. |
| `utils/` | Database access, caching, task lifecycle, disk/memory guards, media proxy, formatting, and Telegram helpers. |
| `tests/` | Regression coverage for playback, Telegram handlers, permissions, queue state, and reliability fixes. |

## Operations and troubleshooting

### Playback is slow or does not start

1. Check the private log group for `#stream`, `#download`, resolver, and Py-TgCalls messages.
2. Verify `ffmpeg` and `ffprobe` are installed and executable.
3. Verify the assistant session is valid and can join/speak in the voice chat.
4. Verify `YT_COOKIES` is fresh and in the supported format; cloud IPs are frequently bot-checked without it.
5. Keep `MAX_CONCURRENT_DOWNLOADS=1` on small dynos and check memory/disk pressure.
6. Confirm the bot has the necessary group and voice-chat permissions.

The strict startup budget prevents a silent one-minute wait, but no implementation can guarantee playback when the source rejects the request, the media is unavailable, Telegram applies flood control, or the assistant cannot access the call.

### Large media

Direct/range-oriented playback is preferred for long media. The ordinary Telegram Bot API has hard file-size and transport constraints, so a three-hour multi-gigabyte object is not equivalent to a normal `sendAudio` or `sendVideo` message. Do not increase concurrency or disable disk guards without measuring host memory, disk, bandwidth, and Telegram limits.

### Security checklist

- Never commit `.env`, bot tokens, assistant sessions, MongoDB URIs, GitHub tokens, or YouTube cookies.
- Keep `LOG_GROUP_ID`, archive channels, and VC log channels private.
- Set `MUSIC_ARCHIVE_CHANNEL_ID` only when archival is deliberate and the bot has access to that channel.
- Restrict owner commands to the configured owner identity.
- Rotate credentials after accidental exposure and review deployment logs for secrets.

## Development and verification

```bash
python3 -m compileall -q melody utils tests tools
pytest -q --disable-warnings
python3 tools/verify_command_surface.py
pip check
git diff --check
```

Before opening a pull request, add regression coverage for every changed command or state transition. Avoid unbounded background tasks, synchronous network calls inside handlers, unsafe cache assumptions, and configuration claims that are not implemented in code.

## License and references

Read [`LICENSE`](LICENSE) before redistribution. Relevant upstream documentation:

- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Kurigram](https://github.com/Kurigram/Kurigram)
- [Py-TgCalls](https://github.com/pytgcalls/pytgcalls)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [MongoDB](https://www.mongodb.com/docs/)

<div align="center">

### Built for groups that want the music to keep moving.

<a href="https://github.com/oyevipthoma88/Melody_music">View source</a> · <a href="https://github.com/oyevipthoma88/Melody_music/issues">Report an issue</a>

</div>
