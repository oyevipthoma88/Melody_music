# Public GitHub plugin/comparison research

## Sources reviewed

| Repository | URL | Verified signal |
|---|---|---|
| AsmSafone/MusicPlayer | https://github.com/AsmSafone/MusicPlayer | Public Pyrogram/Py-TgCalls bot; current page shows a recent 2026 playlist-parsing fix and documented Pyrogram 2 / Py-TgCalls 2 migration. Its source separates core stream and queue logic. |
| callsmusic/remix | https://github.com/callsmusic/remix | Public bot with per-chat queues, simultaneous streams, controls, and streaming while downloading/converting. GitHub marks it archived since 2022, so it is useful as an architectural reference, not a dependency source. |
| CyberPixelPro/AviaxMusic | https://github.com/CyberPixelPro/AviaxMusic | Public Py-TgCalls v2-oriented bot found in the GitHub shortlist; used as a modern comparison target for call lifecycle and dependency layout. |

## Local audit implication

The Melody repository has 70 plugin Python files including package initializers, 14,554 plugin LOC, 337 command aliases, and 347 message handlers. The local plugin tree compiles cleanly; actionable Ruff checks and a 90%-confidence Vulture scan currently report no plugin errors. High-risk review is therefore focused on runtime semantics and edge cases rather than blindly replacing code from older public repositories.

The most important cross-project patterns are one queue per chat, a single authoritative stream owner per chat, explicit admin gating for sensitive controls, and fallback-safe streaming while downloads continue. Melody already contains those broad patterns, so comparison is being used to find gaps in individual plugins such as seek, controls, playlist, download, live, channel controls, moderation, greetings, and owner tools.

## Additional source reviewed

| Repository | URL | Verified signal |
|---|---|---|
| CertifiedCoders/AnnieXMusic | https://github.com/CertifiedCoders/AnnieXMusic | Active-looking Pyrogram + PyTgCalls bot with 955 commits shown on GitHub, async VideosSearch import modernization, explicit `NoAudioSourceFound`/`NoVideoSourceFound` handling, and support for YouTube, Spotify, Apple Music, SoundCloud and Resso. |

The main reusable professional patterns are: asynchronous search APIs instead of synchronous search calls in handlers, explicit no-source exception messages, and keeping optional external download APIs behind configuration rather than making them mandatory for core playback.
