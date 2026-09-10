# Social-command GitHub research

## Sources reviewed

1. **Void-Verser/Warborn-Music** — `bot/utils/social_commands.py`, commit `49ba773`. The helper documents and implements a target resolution order of text-mention entity, replied-to user, then `@username`/numeric ID. It uses Pyrogram's `MessageEntityType.TEXT_MENTION` and `user.mention` for clickable HTML mentions. Its media helper attempts direct URL animation send, then downloads/caches the asset and uploads locally with bounded timeouts; bot-first upload preserves the bot as the visible sender, with a userbot fallback.

2. **Awesome-RJ/CutiepiiRobot** — `Cutiepii_Robot/modules/fun.py`, commit `f8633ea`, a large mature fun/social module. It was selected because GitHub code search returned repeated couple/reaction/mention matches and the repository has a substantial social command surface. The implementation is useful as a pattern source for action-specific templates and media-backed replies, but its large monolithic module should not be copied into Melody because this bot already has a dedicated social plugin and stricter responsiveness requirements.

## Adaptation decision

Melody should use the safer Warborn-style target resolver: explicit text mention first, then reply target, then username/user ID argument. Captions should use HTML-safe clickable mentions generated from the Pyrogram user object rather than interpolating raw display names. The existing Melody media path should remain the transport mechanism; the UI can be improved with concise action title, tagged invoker/target, relationship score, and a single inline action button where supported. `/couples` should be a canonical alias for the existing couple-pair flow, while preserving `/couple` and all existing social aliases.

## Scope guardrails

Do not copy untrusted code wholesale, add new external runtime dependencies, expose owner tools, or remove existing handlers. Keep the slash menu under Telegram's per-scope command limit and keep `/help` as the exhaustive command catalog.

URLs:
- https://github.com/Void-Verser/Warborn-Music/blob/49ba773303e676b12f5f7df08dcbd35f06007dea/bot/utils/social_commands.py
- https://github.com/Awesome-RJ/CutiepiiRobot/blob/f8633ea171949ed45f1191c4b62ff0e696929ff6/Cutiepii_Robot/modules/fun.py

3. **NandhaxD/NandhaxBOT** — `nandha/helpers/help_func.py`, commit `4b69913`. GitHub search surfaced this as a recurring social/mention source. It includes a broad anime-GIF action catalog and media helper patterns, but it also carries a restrictive source notice and unrelated heavyweight helpers, so no code was copied. The useful takeaway is to keep action assets and presentation helpers separated from the command handler and to prefer a compact action catalog.

The three-source comparison supports Melody's implementation: use a small maintained GIF catalog, clickable `tg://user?id=...` mentions, concise action-specific cards, and a safe text fallback when media delivery fails.

Third source URL:
- https://github.com/NandhaxD/NandhaxBOT/blob/4b69913151ad18470737eb80c35851aa04b82814/nandha/helpers/help_func.py
