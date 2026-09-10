# Melody Music — Audit Status

## ✅ This pass — every printed glyph is premium (Modi–Meloni theme deepened)

- **Root cause of "premium emoji, unicode abhi bhi normal aate hai" found in
  the RUNTIME pipeline, not the map.** The map audit said 0 unmatched, but
  the pipeline still degraded glyphs at two points:
  1. `emoji_patch.MAX_EMOJI_ENTITIES` was a flat **48**, so on long cards
     (help, panel, start, vault) every emoji after the 48th was unwrapped to
     plain unicode — the "aadha card premium, aadha normal" look. The cap is
     now a per-message **entity budget**: Telegram's real limit is 100
     entities including `<b>/<i>/<a>/<code>/<blockquote>`, so
     `emoji_entity_budget()` counts the other tags and spends the rest (up to
     90) on premium emoji.
  2. `send_media_group` and `edit_message_media` were **never patched** — the
     caption lives on an `InputMedia*` object, so every album and every edited
     media card went out with plain unicode. Both are wrapped now, with the
     same verify → quarantine → retry → strip fallback chain.
- **Dingbats that can never be premium are gone.** `▸` (56 uses, the bullet in
  every card) and `➜` were plain unicode by definition — Telegram rejects
  them as custom-emoji entities. Replaced everywhere with `🔸` and `➡️`, both
  of which have verified ids.
- **Theme deepened (`utils/melody_theme.py`).** Tricolour ribbon keeps
  🧡🤍💚, headline now leads with 🎼, the divider is the real tricolour
  `🟧━━━━━⬜━━━━━🟩`, and new premium accents ship as theme tokens:
  `BULLET 🔸`, `SUB_BULLET 🔹`, `ARROW ➡️`, `SPARK ✨`, `CROWN 👑`, `OK ✅`,
  `BAD ❌`, `LIVE 🔊`, plus `accent()` for one-line rows. One edit still
  restyles every card in the bot.
- **New end-to-end audit `tools/verify_premium_emoji.py`** replays the real
  pipeline (`auto_premium_emoji` → `entity_text` → `cap_emoji_entities`) over
  every sendable string literal plus the theme frame, and fails on any glyph
  that would reach the user as plain unicode, any impossible id, and any card
  clipped by the entity budget. Regex character-class literals and
  button-only decorations are excluded (buttons can never carry entities).
  Current: **2421 scanned, 2391 premium entities, 0 plain, 0 bad ids, 0
  clipped.**
- **Every id verified.** All 1333 ids in `EMOJI_ID_MAP` trace back to the
  verified `PREMIUM_EMOJI_ID_POOL` / harvested `ORDER_EMOJI_MAP` — asserted by
  a test now, so a hand-typed id can never sneak in again.
- Tests: new `tests/test_premium_emoji_pipeline.py` (theme fully premium, no
  dingbats, budget maths, cap behaviour, id provenance, media paths patched,
  audit script green). `pytest tests` → **57 passed**, `compileall` clean,
  `verify_card_emojis` 0 unmatched, `verify_button_emojis` 0 mismatched.

## ✅ This pass — every card one theme + PERFECT (accurate) premium emoji ids

- **One theme for every card.** New `utils/melody_theme.py` is the single
  source of truth for the Modi–Meloni tricolour frame (🧡🤍💚 ribbon +
  `fancy()` headline + quoted footer). `melody/core/vc_theme.py` and
  `utils/admin_tools.card()` are now thin re-exports of it, and the 58
  hand-written `<blockquote>emoji <b>Title</b></blockquote>` headers in
  `help`, `start`, `panel`, `cmd_vault` and `source_code` were converted to
  `headline(...)` — so admin, owner, misc, music and VC cards finally look
  identical and one edit restyles all of them.
- **No more wrong-artwork premium emoji.** `assign_pool_id()` used to hand any
  unmapped glyph a RANDOM id from the pool, so 🧾 / ⚖ / 🚪 animated as
  something completely unrelated. The accuracy gate now resolves in order:
  verified map → owner's harvested pack → `GLYPH_ALIASES` (glyph borrows the
  id of the closest glyph the packs really draw, e.g. 🧾→📄, ⏯→▶, 🕵→🔍,
  🎛→🎚, 🙋→👋, 🕒→🕐, ♛→👑) → skin-tone base (🙋🏻→🙋) → PLAIN unicode.
  A mismatched animation is never sent again; unmatched glyphs are recorded in
  `UNMATCHED_GLYPHS` and logged once for mapping.
- **35-entry alias table** replaces the old 4-entry ad-hoc loop and is
  re-applied after the startup Telegram resolution (`apply_aliases()`), so
  aliases survive the map rebuild and inherit the full 12-deep fallback chain.
- **New audit:** `tools/verify_card_emojis.py` walks every emoji printed by the
  bot (2460 occurrences) and fails when one would render with a non-matching
  id. Currently: 0 unmatched. `tools/verify_button_emojis.py` still 0
  unmatched / 0 mismatched.
- Tests: new `tests/test_card_theme_and_emoji.py` (shared frame, alias sources
  exist, alias id == source id, unknown glyph stays plain, audit passes);
  `pytest tests` → 50 passed, `compileall` + `pyflakes` clean, and the new
  f-strings avoid nested same-quotes so Python 3.10 (Dockerfile/Heroku) parses
  them.

## ✅ This pass — VC suite cleanup (chat card, duplicates, mentions)

- **VC chat card = sirf chat + kisne bheji.** Roster listing, "in VC" meter,
  scope/mode lines hata diye; har line ab clickable mention + id ke saath
  quote me aati hai.
- **Sirf 2 buttons:** 🟢/🔴 VC chat ON/OFF aur ⚙ More Settings (auto-delete
  presets + close usi ke andar). `vclog:mode` button/command poora hata diya.
- **GC chat log system removed.** `feed_group_message()` aur mirror mode delete —
  ab sirf real in-call chat (`updateGroupCallMessage`) log hoti hai. Group
  traffic sirf VC watcher ko trigger karta hai, log nahi hota.
- **Duplicate cards fixed.** `vc_events._fresh()` dedupes started/ended/invited
  (service message re-delivery), aur py-tgcalls ka INVITED / CLOSED ChatUpdate
  ab chup rehta hai — ek event = ek card.
- **Mentions everywhere.** Start/end cards batate hain kisne VC start/end ki
  (mention · @username · id), invite card har invited user ko mention karta hai
  aur kabhi delete nahi hota, join/left cards bhi ab clickable mention + id.
- Tests: `tests/test_vc_chat_scope.py` (mirror-only) removed, naye guards
  added; `pytest tests` → 31 passed.

## ✅ This pass — VC chat log = VC people only, roster root fix, flood fix

- **Only voice-chat people are logged (group chat ignored).**
  `vc_chat_log.feed_group_message()` now mirrors a line **only** when the
  sender is present in the live call roster. If the roster cannot be read the
  log stays silent instead of falling back to whole-group traffic
  (REQUESTED: "group ki chat nahi, vc ki chat ka log kr — gc ki chat ignore kr").
- **ROOT FIX for "join/left kaam nahi karta" and "2 users ki chat show nahi
  hoti".** `refresh_roster()` asked py-tgcalls first, which only knows the
  participants of its own stream — so with no music the roster came back as
  1 entry (or empty) and everyone looked "not in VC" (chat dropped, bogus LEFT
  cards). Telegram's `phone.GetGroupParticipants` (works from **outside** the
  call) is now the primary source; py-tgcalls is only a fallback. Roster trust
  window 10s → 25s so it survives between 10s polls without one RPC per
  message.
- **FLOOD fix that also made commands/playback feel dead.** `vc_listener.note()`
  probed *every* chat on *every* message with `channels.GetFullChannel`;
  Telegram answered FLOOD_WAIT and pyrogram slept the whole session. Chats with
  a live VC are still polled every 20s, chats without one only every 5 min, and
  the "is a VC live" cache went 12s → 30s.
- **Chat-log card UI**: shows who is currently in the call, a `Sᴄᴏᴘᴇ` line
  stating group chat is ignored, and a clear warning when the roster is not
  readable (assistant not in the group). Buttons unchanged: 🟢/🔴 toggle (OFF
  auto-returns after 30 min), ⏱ auto-delete presets (5s default · 10 · 30 · 1m ·
  5m · never, plus `/vcchatlog time 12`), ✵ CLOSE ✵.
- Routing unchanged: group card + permanent copy in `VC_CHAT_LOG_CHANNEL_ID`
  (-1004445716740); never in `LOG_GROUP_ID`. Join/left stay 2s and separate;
  VC invite cards are never deleted and tag every invited user.
- Tests: new `tests/test_vc_chat_scope.py` (VC-member logged, group chat
  ignored, unreadable roster silent, no-VC silent, roster source order);
  `pytest tests` → 33 passed, `compileall` clean.

## ✅ Latest pass — assistant never joins a VC + FLOOD_WAIT fix

- **Silent VC presence removed.** `call.join_as_listener()` is now a disabled
  no-op and `melody/core/vc_listener.py` was rewritten as a pure *outside the
  call* watcher: it polls `phone.GetGroupParticipants` with the assistant
  account and diffs the roster to produce JOIN / LEFT cards. The assistant
  enters a voice chat only when it is actually streaming music.
- **VC chat still shows without the bot in the VC.** New
  `vc_chat_log.feed_group_message()` mirrors what people currently inside the
  live voice chat type in the group (raw in-call `UpdateGroupCallMessage` is
  still handled as a bonus while music plays). Same themed card, same
  permanent copy in `VC_CHAT_LOG_CHANNEL_ID`, never in `LOG_GROUP_ID`.
- **`FLOOD_WAIT_X ... phone.JoinGroupCall` crash fixed.** `_flood_seconds()`
  digs the FloodWait out of the nested exception chain; short cooldowns
  (≤45s) auto-retry once with a friendly notice, longer ones report the exact
  wait in plain words instead of a traceback. The endless silent joins that
  caused the flood in the first place are gone.
- **VC UI polish**: tricolour divider + timestamp rows on join/left cards,
  softer presence meter, reader status now says whether the VC is watched
  from outside, and the chat-log card states that the assistant never sits in
  the VC.
- Tests: two new policy tests (no silent join, flood-wait handling);
  `pytest tests` → 24 passed.

## ✅ Latest pass — VC suite (chat log · join/left · invite · theme)

- **VC chat log no longer needs the music bot in the call.** Telegram delivers
  in-call chat only to accounts inside the call, so the assistant is kept in
  every live VC as a silent muted listener. New `vc_listener.note()` re-arms
  that listener from ordinary group traffic (group=28 hook in
  `melody/plugins/misc/vc_chat_log.py`), so a VC that was already running
  before the bot started — or one with just two chatting users and no music —
  is picked up too. Watchdog poll 45s → 20s, join cooldown 60s → 30s.
- **Listener is wanted for join/left as well** (`vc_listener._wanted`): the
  `vc_activity` flag alone is now enough to keep the reader inside the call, so
  turning the chat log off no longer kills join/left cards.
- **Join and left are two clearly different cards**, 2s auto-delete, presence
  meter in the footer.
- **VC invite messages are never deleted** (`INVITE_AUTO_DELETE = 0` in both
  `vc_notify.py` and `vc_events.py`) and every invited user is tagged with a
  real clickable `tg://user` mention.
- **Chat-log card UI**: toggle (OFF auto-returns after 30 min), auto-delete
  preset picker (5s default · 10 · 30 · 1m · 5m · never, plus custom
  `/vcchatlog time 12`) and CLOSE.
- **Routing unchanged and verified**: group card + permanent copy to
  `VC_CHAT_LOG_CHANNEL_ID` (-1004445716740). In-call chat is never sent to
  `LOG_GROUP_ID`.
- **New `melody/core/vc_theme.py`** — Modi–Meloni tricolour theme (🧡🤍💚)
  shared by every VC surface: started, ended, join, left, invite, chat log.
- Tests: `tests/test_vc_notification_policy.py` rewritten for the new policy;
  one stale screen-share assertion in `tests/test_regressions.py` fixed.
  `python -m compileall` clean, `pytest tests` → 22 passed.

_Last updated: this audit pass._

## ✅ Done in this pass

### 1. Gemini / AI chatbot modernised
- New `melody/core/genai.py`: prefix-agnostic Gemini client.
  - Auto-detects credential type — classic API keys go out as `x-goog-api-key`,
    OAuth-style tokens (`AQ...`, `ya29...`) as `Authorization: Bearer`.
  - The old code hard-rejected anything not starting with `AIza`; that
    assumption is gone. No key is hardcoded — everything comes from env.
  - Lazy model discovery: if `CHATBOT_MODEL` (default `gemini-2.5-flash`)
    404s, the client lists available models and picks a working one.
- `melody/plugins/misc/chatbot.py` rewired to the new client; legacy
  `_clean_ai_key()` validation removed.

### 2. VC "bot rejoins after /stop" race — root cause fixed
`_stream_track()` runs fire-and-forget while a download races. If the user
issued `/stop`, `/end` or `/leave` in that window, the finished download
still called `play()` and **re-joined** the voice chat.
- `_is_leaving(chat_id)` guards added to `_stream_track()` and
  `_pre_join_locked()` in `melody/core/call.py`.

### 3. Callback security audit (real bugs found & fixed)
- **Inline player buttons had no permission check at all.** `pause`,
  `resume`, `skip`, `stop` were open to any group member while the identical
  `/pause` `/skip` `/stop` commands were admin-gated.
- **The `controls <action> <chat_id>` router trusted the chat id inside the
  callback data**, so a crafted press could control playback in a chat the
  presser is not an admin of.
- Fix: new `cb_admin_or_auth()` in `utils/decorators.py` — the CallbackQuery
  twin of `admin_or_auth` (owner → gban/ban → chat admin → auth list), with an
  explicit `chat_id` override so the check runs against the chat actually being
  controlled. Applied to the four player buttons, the `controls` router
  (read-only `status` stays open) and the AutoPlay toggle.
- Verified as already correctly gated: owner panel (`owner_*`), owner command
  vault, settings (`set_*`), `/cleanall` (bound to the initiator + expiry),
  safemode captcha / join-request callbacks (self-scoped).

### 4. Dependency pins
- `yt-dlp>=2025.1.15,<2026.0.0` — was unbounded; monthly releases ship
  breaking extractor changes and had already broken playback once.
- `motor>=3.7.1,<4` + `pymongo>=4.9,<5` — matched pair, was unpinned.
- `aiohttp>=3.10,<4`, `aiofiles>=24.1.0`, `onnxruntime>=1.19,<2`.
- Verified current: kurigram 2.2.24, py-tgcalls 2.3.3 (2.1.1 crashes on
  Layer 227 `UpdateGroupCall`).

### 5. Already landed earlier, re-verified here
- Owner-panel promo controls (`owner_promo` / `_on` / `_off`, owner-checked
  server-side); the public `/promo off` command is gone.
- Source Code button for all users + owner Set/Remove/Current URL management
  (`melody/plugins/owner/source_code.py`).
- Centralised premium emoji IDs with Unicode fallback (`utils/formatters.py`).
- Keyless InnerTube fast path for playback latency.

### 6. Verification run
- `python -m compileall melody utils strings` — clean.
- `pytest tests` — 13 passed.
- Secret scan — no tokens, no API keys, no repo owner/link in the source.
  (The two `AIza…` strings in `melody/core/ytdl.py` are YouTube's *public*
  InnerTube client constants, published in the web player — not credentials.)

## 🔜 Remaining / recommended

| # | Item | Why |
|---|------|-----|
| 1 | Measure real `/play` latency on Heroku after this deploy | The keyless InnerTube path should remove most of the ~20 s delay, but it needs a production timing sample per source (YouTube / search / URL). |
| 2 | Prefetch tuning | `prefetch_next()` currently starts on track start; starting it at ~50 % of the current track would smooth queue transitions further. |
| 3 | yt-dlp cookie/PO-token strategy | Heroku IPs get bot-checked by YouTube periodically. A rotating cookie or PO-token provider is the durable fix; today it falls back per-client. |
| 4 | `nudenet`/`onnxruntime` slug size | These two dominate the Heroku slug. Move NSFW vision behind an optional extra if the slug limit is ever hit. |
| 5 | Structured metrics | No counters for join failures / download timeouts, so regressions are only visible in raw logs. |
| 6 | Test coverage | The suite covers regressions and secret hygiene only; the call/queue state machine has no unit tests. |

## 🔒 Notes
- No secrets are committed. All credentials (bot token, Mongo URI, Gemini key,
  session strings) come from environment variables / Heroku config vars.
- The upstream repository owner and URL are intentionally kept out of the
  source; a regression test asserts this.

## ✅ Follow-up pass (this run)

Verified-already-fixed (left untouched): VC join/left/activity feed wiring in
`melody/core/call.py` → `melody/core/vc_notify.py`, VC start/end "who did it"
mentions, join-request notify + external-resolution logging, Gemini
prefix-agnostic client, Source Code button + owner Set/Remove/Current URL,
owner-only menu gating in `/start` and `/help`, Mongo + GitHub pic persistence.

Newly fixed here:
1. **Premium emoji coverage** — full crawl of every message in
   https://t.me/OrderEmoji (not just the first page): `utils/emoji_order_pack.py`
   now ships 267 glyphs / 1897 verified `custom_emoji_id`s (was 262 / 962),
   including 🆒 🤯 ❤️‍🔥 ☃️ 🍾. Extra variants per glyph act as fallbacks when an
   id is quarantined, so glyphs degrade to another premium emoji, not plain text.
   (The channel itself contains ~3k ids total; 10k do not exist there.)
2. **Assistant join requests** — groups with "approve new members" turned the
   assistant's invite-link join into a pending request and playback died.
   `_auto_join_assistant()` now detects that and the bot approves its own
   assistant's request automatically.
3. **Error logs** — every `send_error_log()` card now carries `#error #crash
   #<command>` hashtags, a clickable chat link + user deep link, and tags the
   OWNER at the bottom. `error_handler` feeds the chat username into context.


## 🆕 This pass — VC activity + help panel

### VC text mirroring removed (root cause)
Telegram does not expose a distinct update for text typed from the voice-chat UI.
Treating ordinary group messages from VC participants as "VC chat" duplicated
the group chat and mislabeled it, so the command, passive handler, and toggles
were removed. Only genuine group-call participant events remain.
- `melody/core/vc_notify.py`: `refresh_roster()` now falls back to a raw
  `phone.GetGroupParticipants` lookup through the assistant, so the roster is
  correct for **any** running voice chat (music or not). Results are cached
  (6s TTL, 10s trust window) and de-duplicated with a per-chat lock, and live
  JOIN/LEFT updates refresh the trust stamp.
- `roster_is_known()` added: a "not in VC" answer is only trusted while the
  roster is fresh, otherwise one shared refresh runs first.

### VC event cards
- VC cards auto-delete in exactly **3 seconds** and display plain names without
  mentioning/tagging users.
- Join/left cards carry searchable `#join` / `#left` hashtags.

### Help panel rebuilt
- New pages: Tutorial (full step-by-step), VC Tools, Greetings, Join Requests,
  Owner Assistant, Guards & Safe Mode, and A-L / M-Z full command index.
- Every page has quick-jump buttons to related pages (no more walking back
  through the main menu).
- Fixed a stale reference to a `/promo` command that no longer exists.


## VC chat vs group chat (strict mode)
Telegram has no separate readable "VC chat" stream: in-call text arrives only as
`updateGroupCallMessage` to accounts inside the call, and everything else is a
plain group message with zero VC marker. The old group-mirror heuristic was
therefore logging ordinary group chat. Now:

- `vc_chat_log` has a per-chat source mode, **default `strict`** — only genuine
  in-call messages are logged; group traffic is dropped before any filtering.
- `mirror` mode (old behaviour) is opt-in via `/vcchatlog mode mirror` or the
  new 🎯 Source button, and the card warns that group chat will be included.
- Roster fix: `phone.GetGroupParticipants` rows flagged `left=True` are now
  skipped, so people who hung up no longer count as being in the VC.
