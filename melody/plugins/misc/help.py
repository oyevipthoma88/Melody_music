"""
📋 /help — Melody help menu (Modi–Meloni theme).

BUG FIX — "[400 ENTITY_TEXT_INVALID] in help_cb":
  `cb.message.edit_text()` was called directly, so the globally-injected
  premium-emoji entities were re-applied on top of a message that ALREADY
  contained custom-emoji entities. When the same glyph got wrapped twice the
  resulting entity pointed at an empty text range and Telegram rejected the
  whole edit with ENTITY_TEXT_INVALID — the menu simply froze on every
  button tap. Every edit now goes through `safe_html_action()`, which
  verifies entities and degrades (entities → no entities → plain text)
  instead of throwing, and photo-backed menus are edited via caption.

BUTTON CLEANUP — "repeat wale hata dena":
  The old grid had Admin+Auth both pointing at `help_admin` and Play+Song
  both pointing at `help_play`. Every button now maps to a UNIQUE page, and
  the new group-management / protection / channel / tools pages are wired
  in.
"""
from utils.buttons import ikb, STYLE_PRIMARY, STYLE_SUCCESS, STYLE_DANGER, STYLE_WARNING  # premium-emoji + styled buttons (safe on every fork)
from pyrogram import Client, enums, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)

from melody import bot
from melody.config import Config
from strings.themes import fancy
from utils.decorators import error_handler
from utils.telegram_html import safe_html_action
from utils.melody_theme import headline

# ─── Help pages ──────────────────────────────────────────────────────────────

HELP_PAGES = {
    # ── MUSIC ───────────────────────────────────────────────────────────────
    "play": (
        f"{headline('Play')}\n\n"
        "<code>/play [song/url]</code> — 🎵 Audio stream\n"
        "<code>/vplay [song/url]</code> — 🎬 Video stream\n"
        "<code>/playforce</code> — ⚡ Turant bajao (queue skip)\n"
        "<code>/vplayforce</code> — ⚡ Video turant bajao\n"
        "<code>/playlist [url]</code> — 📃 Poori playlist queue karo\n"
        "<code>/search [query]</code> — 🔍 Search karke chuno\n"
        "<code>/dl [song/url]</code> — ⬇️ File ke roop me download (audio/video)\n"
        "<code>/autoplay on|off</code> — 🤖 Queue khatam hone pe khud gaana\n"
        "<code>/live [url]</code> <code>/m3u8</code> <code>/stream</code> — 📡 Direct/HLS link stream\n"
        "<code>/radio [station]</code> — 📻 Ready-made radio stations\n"
        "<code>/replay</code> — 🔁 Current gaana shuru se\n"
        "<code>/playmode</code> — ⚙️ Kaun play kar sakta hai + search style\n"
        "<code>/addplaylist</code> — 💾 Current/searched gaana apni playlist me save\n"
        "<code>/myplaylist</code> <code>/mypl</code> — 📃 Apni saved playlist dekho\n"
        "<code>/playplaylist</code> <code>/pl</code> — ▶️ Apni saved playlist bajao\n"
        "<code>/delplaylist [n|all]</code> — 🗑 Saved playlist se hatao\n\n"
        "<i>Tip: kisi audio/video file pe reply karke /play bhejo.</i>"
    ),
    "queue": (
        f"{headline('Queue')}\n\n"
        "<code>/queue</code> <code>/q</code> — 📋 Queue dekho\n"
        "<code>/skip</code> <code>/s</code> <code>/next</code> — ⏭ Agla gaana\n"
        "<code>/skip [pos]</code> — ⏭ Seedha queue position pe jao\n"
        "<code>/remove [pos]</code> — ❌ Ek item hatao\n"
        "<code>/clearqueue</code> — 🗑 Poori queue clear\n"
        "<code>/shuffle</code> — 🔀 Queue shuffle\n"
        "<code>/np</code> <code>/playing</code> — 🎶 Abhi kya baj raha hai\n"
        "<code>/queuepos</code> <code>/qpos</code> — 📍 Kitna bacha hai (pending + time)"
    ),
    "controls": (
        f"{headline('Playback Controls')}\n\n"
        "<code>/pause</code> — ⏸ Pause\n"
        "<code>/resume</code> — ▶️ Resume\n"
        "<code>/stop</code> <code>/end</code> — ⏹ Stop + queue clear\n"
        "<code>/userbotjoin</code> <code>/joinvc</code> — 🛰 Assistant ko VC me bulao\n"
        "<code>/userbotleave</code> <code>/leavevc</code> — 👋 Assistant ko VC se nikalo\n"
        "<code>/autoend on|off</code> — ⏳ VC khali hote hi stream auto band\n"
        "<code>/vcnotify on|off</code> — 🎙 VC join / left alert cards toggle\n\n"
        "<i>Ye sirf VC me stream chalu hone par kaam karte hain.</i>"
    ),
    "loop": (
        f"{headline('Loop')}\n\n"
        "<code>/loop</code> <code>/loop enable|disable</code> — 🔂 Current song repeat\n"
        "<code>/loopall</code> — 🔁 Poori queue repeat\n"
        "<code>/noloop</code> — ➡️ Loop band"
    ),
    "seek": (
        f"{headline('Seek')}\n\n"
        "<code>/seek [sec]</code> — ⏩ Aage jao\n"
        "<code>/seekback [sec]</code> — ⏪ Peeche jao\n"
        "<code>/rewind [sec]</code> — ⏪ /seekback ka alias\n\n"
        "<i>Example: /seek 30 · /seekback 15</i>"
    ),
    "volume": (
        f"{headline('Volume &amp; Speed')}\n\n"
        "<code>/volume [1-200]</code> — 🔊 Stream volume\n"
        "<code>/vcmute</code> — 🔇 Stream mute\n"
        "<code>/vcunmute</code> — 🔈 Stream unmute\n"
        "<code>/speed [0.5-2.0]</code> — ⏱ Playback speed"
    ),
    "lyrics": (
        f"{headline('Lyrics')}\n\n"
        "<code>/lyrics [song name]</code> — 🎤 Genius se lyrics\n\n"
        "<i>Bina naam ke bhejo to current song ke lyrics milenge.</i>"
    ),
    "cplay": (
        f"{headline('Channel Play')}\n\n"
        "<b>Group se channel control :</b>\n"
        "<code>/channelplay @username</code> — 🔗 Channel link karo\n"
        "<code>/channelplay off|status</code> — 📴 Unlink / info\n"
        "<code>/cplaylink</code> — 🔎 Linked channel dekho\n"
        "<code>/cplay [song]</code> — 🎧 Channel me audio\n"
        "<code>/cvplay [song]</code> — 🎬 Channel me video\n\n"
        "<b>Channel controls :</b>\n"
        "<code>/cpause</code> <code>/cresume</code> <code>/cskip</code> <code>/cs</code> "
        "<code>/cstop</code> <code>/cend</code>\n\n"
        "<i>Bot + assistant dono channel me admin hone chahiye.</i>"
    ),

    # ── VOICE CHAT ──────────────────────────────────────────────────────────
    "vc": (
        f"{headline('Voice Chat Tools')}\n\n"
        "<code>/vcchatlog on|off</code> — 🧾 VC activity log toggle\n"
        "<code>/activevc</code> <code>/ac</code> — 🎧 Saare active VC (owner)\n\n"
        "<b>Cards :</b> #join / #left cards 3 sec me auto-delete; "
        "invite · start · end cards rehte hain.\n"
        "<i>Assistant khud VC join karta hai — manual invite ki zarurat nahi.</i>"
    ),

    # ── GROUP MANAGEMENT ────────────────────────────────────────────────────
    "admin": (
        f"{headline('Moderation')}\n\n"
        "<code>/ban</code> <code>/unban</code> <code>/banuser</code> <code>/unbanuser</code> — 🔨 Ban\n"
        "<code>/gcban</code> <code>/gcunban</code> — 🔨 Group ban alias\n"
        "<code>/kick</code> <code>/punch</code> <code>/dkick</code> — 🚪 Kick\n"
        "<code>/mute</code> <code>/unmute</code> <code>/gcmute</code> <code>/gcunmute</code> — 🔇 Mute\n"
        "<code>/tmute [time]</code> — ⏳ Temporary mute\n"
        "<code>/promote</code> <code>/fpromote</code> <code>/fullpromote</code> <code>/dpromote</code> — ⬆️ Promote\n"
        "<code>/demote</code> — ⬇️ Demote\n"
        "<code>/title [text]</code> — 🏷 Admin title\n"
        "<code>/pin</code> <code>/unpin</code> <code>/unpinall</code> — 📌 Pins\n"
        "<code>/lock</code> <code>/unlock</code> — 🔒 Chat lock\n"
        "<code>/invitelink</code> <code>/link</code> — 🔗 Group link\n"
        "<code>/admins</code> <code>/adminlist</code> <code>/admin</code> <code>/staff</code> — 👥 Admin list\n"
        "<code>/info</code> <code>/whois</code> — 👤 User info"
    ),
    "purge": (
        f"{headline('Cleanup')}\n\n"
        "<code>/purge</code> — 🧽 Reply se yaha tak delete\n"
        "<code>/spurge</code> — 🤫 Silent purge\n"
        "<code>/purgefrom</code> <code>/purgeto</code> — 🎯 Range purge\n"
        "<code>/del</code> <code>/delete</code> — ❌ Ek message delete\n"
        "<code>/purgeme [n]</code> — 🧹 Apne messages\n"
        "<code>/clean [count]</code> <code>/cleanchat</code> — 🧹 Junk clean (bots + commands + service msgs)\n"
        "<code>/clean bots|cmd|service|all [count]</code> — 🧹 Sirf chuni hui cheez clean\n"
        "<code>/cleanall</code> <code>/clearchat</code> <code>/nuke</code> — 🧨 Poori chat clear\n"
        "<code>/zombies [clean]</code> — 🧟 Deleted accounts\n"
        "<code>/kickme</code> — 🚪 Khud group chhodo"
    ),
    "mass": (
        f"{headline('Mass Actions &amp; Tagging')}\n\n"
        "<code>/banall</code> <code>/unbanall</code> — 🔨 Sab ko ban/unban\n"
        "<code>/muteall</code> <code>/unmuteall</code> — 🔇 Sab mute/unmute\n"
        "<code>/kickall</code> — 🚪 Sab non-admins kick\n"
        "<code>/tagall [msg]</code> <code>/all</code> <code>/mention</code> <code>/utag</code> — 📣 Sab ko tag\n"
        "<code>/cancel</code> <code>/canceltag</code> <code>/stoptag</code> — 🛑 Tagging roko\n\n"
        "<i>Sirf group owner / sudo ke liye.</i>"
    ),
    "warns": (
        f"{headline('Warns')}\n\n"
        "<code>/warn [reason]</code> — ⚠️ User ko warn karo\n"
        "<code>/warns</code> — 📋 Warn count\n"
        "<code>/unwarn</code> <code>/resetwarns</code> <code>/rmwarns</code> — ♻️ Warns clear"
    ),
    "guard": (
        f"{headline('Protection &amp; Settings')}\n\n"
        "<code>/protection</code> <code>/guard</code> <code>/antispam</code> — ⚙️ Guard panel\n"
        "<code>/protectinfo</code> <code>/guardinfo</code> — ℹ️ Current settings\n"
        "<code>/safemode</code> <code>/safe</code> — 🧯 Naye members ke liye captcha\n"
        "<code>/settings</code> <code>/toggles</code> <code>/options</code> — 🎛 Group toggles\n"
        "<code>/vcnotify on|off</code> <code>/vcalerts</code> <code>/vcjoinleft</code> — 🎙 VC join/left cards on-off\n"
        "<code>/reload</code> <code>/admincache</code> <code>/refresh</code> — 🔄 Admin + auth cache turant refresh\n"
        "<code>/trigger on|off</code> <code>/tagger</code> — 🤖 Dusre bots ke commands (<code>/play@OtherBot</code>) "
        "par Melody reply kare ya nahi <i>(default: ON)</i>\n"
        "<code>/report</code> <code>/reportuser</code> — 🚨 Admins ko bulao\n\n"
        "<i>Anti-link / anti-abuse / anti-edit sab guard panel ke buttons se on-off hote hain.</i>"
    ),
    "approve": (
        f"{headline('Approvals &amp; Auth')}\n\n"
        "<code>/approve</code> <code>/unapprove</code> <code>/unapprov</code> — ✅ Ek user\n"
        "<code>/approveall</code> <code>/approvall</code> — 👥 Sab approve\n"
        "<code>/unapproveall</code> <code>/unapprovall</code> — 🚫 Sab unapprove\n"
        "<code>/approved</code> <code>/approvelist</code> — 📋 Approved list\n"
        "<code>/auth</code> <code>/unauth</code> — 🔑 Play permission\n"
        "<code>/authlist</code> — 📋 Auth users"
    ),
    "joinreq": (
        f"{headline('Join Requests')}\n\n"
        "<code>/joinrequests</code> <code>/rpending</code> <code>/pendingrequests</code> — 📋 Pending list\n"
        "<code>/approverequest</code> / <code>/declinerequest</code> — ᴇᴋ request handle\n"
        "<code>/approveallrequests</code> <code>/rapproveall</code> <code>/joinapproveall</code> "
        "<code>/requestapproveall</code> — ✅ Sab approve\n"
        "<code>/declineallrequests</code> <code>/rdeclineall</code> <code>/rrejectall</code> "
        "<code>/joindeclineall</code> <code>/requestdeclineall</code> — ❌ Sab decline\n\n"
        "<i>Bot ko \"Add Members / Invite Users\" right chahiye.</i>"
    ),
    "greet": (
        f"{headline('Welcome &amp; Goodbye')}\n\n"
        "<code>/welcome on|off</code> <code>/greetings</code> <code>/greeting</code> — 🎉 Welcome toggle\n"
        "<code>/setwelcome [text]</code> — ✍️ Custom welcome\n"
        "<code>/resetwelcome</code> <code>/clearwelcome</code> — ♻️ Default\n"
        "<code>/welcomecard on|off</code> <code>/welcomethumb</code> — 🖼 Card mode\n"
        "<code>/setwelcomeimage</code> <code>/setwelcomepicgc</code> — 🏞 Group welcome image\n"
        "<code>/delwelcomeimage</code> <code>/rmwelcomeimage</code> — 🗑 Image hatao\n"
        "<code>/welcometest</code> <code>/testwelcome</code> — 👀 Preview\n"
        "<code>/cleanwelcome on|off</code> — 🧹 Purana welcome delete\n\n"
        "<code>/goodbye on|off</code> <code>/left</code> <code>/leftmsg</code> — 👋 Goodbye toggle\n"
        "<code>/setgoodbye [text]</code> — ✍️ Custom goodbye\n"
        "<code>/resetgoodbye</code> <code>/cleargoodbye</code> — ♻️ Default\n"
        "<code>/goodbyecard on|off</code> <code>/goodbyethumb</code> — 🖼 Card mode\n"
        "<code>/goodbyetest</code> <code>/testgoodbye</code> — 👀 Preview\n"
        "<code>/cleangoodbye on|off</code> — 🧹 Purana goodbye delete\n"
        "<code>/cleanservice on|off</code> <code>/cleanservicemsg</code> — 🧽 Service msg hatao\n"
        "<code>/resetgreetings</code> <code>/resetgreeting</code> — ♻️ Dono reset\n\n"
        "<b>Fillers :</b> <code>{mention}</code> <code>{name}</code> <code>{chat}</code> "
        "<code>{count}</code> <code>{username}</code> <code>{id}</code>"
    ),
    "assistant": (
        f"{headline('Owner Assistant (Group Owner Shield)')}\n\n"
        "<code>/assistant</code> <code>/oa</code> <code>/ownerassistant</code> — 📊 Status &amp; panel\n"
        "<code>/oaset</code> <code>/assistantset</code> — ⚙️ Protected owner set karo\n"
        "<code>/oareset</code> <code>/assistantreset</code> — ♻️ Reset\n\n"
        "<i>Group owner ko koi ban / mute / demote kare to bot action revert karke culprit ko handle karta hai.</i>"
    ),

    # ── BOT OWNER PANEL ─────────────────────────────────────────────────────
    "owner": (
        f"{headline('Owner Panel')}\n\n"
        "<code>/panel</code> — 🎛 Full owner control panel\n"
        "<code>/ocmds</code> <code>/ownercmds</code> — 📚 Owner command vault\n"
        "<code>/logs</code> — 🧾 Live logs\n"
        "<code>/chatlist</code> — 📃 Bot ke saare chats\n"
        "<code>/broadcast</code> — 📢 Sab chats me message\n"
        "<code>/maintenance</code> — 🧰 Maintenance mode\n"
        "<code>/promoglobal</code> — 📣 Global auto-promo\n"
        "<code>/speedtest</code> — 📡 Server speed\n"
        "<code>/sysinfo</code> — 🖥 CPU / RAM / disk / uptime\n"
        "<code>/logger on|off</code> — 🧾 Activity logging toggle"
    ),
    "owner_sudo": (
        f"{headline('Sudo &amp; Global Ban')}\n\n"
        "<code>/addsudo</code> <code>/sudo</code> — ➕ Sudo banao\n"
        "<code>/rmsudo</code> <code>/delsudo</code> <code>/unsudo</code> — ➖ Sudo hatao\n"
        "<code>/sudolist</code> <code>/listsudo</code> <code>/sudoers</code> — 📋 Sudo list\n"
        "<code>/gban</code> <code>/ungban</code> — 🌐 Global ban / unban\n"
        "<code>/botban</code> <code>/botunban</code> — 🚫 Bot access block\n"
        "<code>/gbannedusers</code> <code>/gbanlist</code> — 🌍 Gban list\n"
        "<code>/blockedusers</code> <code>/blocklist</code> — 📋 Bot-banned list\n"
        "<code>/blacklistchat</code> <code>/whitelistchat</code> — 🚷 Chat block / unblock\n"
        "<code>/blacklistedchats</code> — 📃 Blocked chats"
    ),
    "owner_deploy": (
        f"{headline('Deploy &amp; Debug')}\n\n"
        "<code>/update</code> — ⬇️ GitHub se update\n"
        "<code>/restart</code> <code>/reboot</code> — 🔄 Bot restart\n"
        "<code>/reload</code> <code>/admincache</code> — ♻️ Admin cache refresh\n"
        "<code>/eval</code> <code>/py</code> — 💻 Python evaluate\n"
        "<code>/shell</code> <code>/sh</code> <code>/bash</code> <code>/exec</code> — 🖥 Shell run\n"
        "<code>/emojiid</code> — 🎞 Premium emoji ID nikalo\n"
        "<code>/setsource</code> <code>/setsourceurl</code> — 🔗 Source URL set"
    ),
    "pics": (
        f"{headline('Pictures')}\n\n"
        "<code>/setpic [key]</code> — 🖼 Card picture set\n"
        "<code>/delpic</code> <code>/delpics</code> — 🗑 Picture hatao\n"
        "<code>/piclist</code> — 📋 Saved pictures\n"
        "<code>/setwelcomepic</code> <code>/delwelcomepic</code> — 👋 Global welcome card"
    ),

    # ── EXTRAS ──────────────────────────────────────────────────────────────
    "economy": (
        f"{headline('Melody Economy')}\n\n"
        "<code>/balance</code> <code>/bal</code> <code>/coins</code> — 🪙 Wallet, bank, level\n"
        "<code>/daily</code> <code>/claim</code> — 🎁 Daily reward\n"
        "<code>/work</code> <code>/job</code> — 💼 Earn coins\n"
        "<code>/deposit [amount]</code> / <code>/withdraw [amount]</code> — 🏦 Bank\n"
        "<code>/pay @user [amount]</code> — 💸 Transfer coins\n"
        "<code>/leaderboard</code> <code>/rich</code> — 🏆 Top players\n\n"
        "<i>Non-gambling group game — coins have no real-money value.</i>"
    ),
    "social": (
        f"{headline('Social GIFs')}\n\n"
        "Reply to someone or use a username with:\n"
        "<code>/couple</code> <code>/couples</code> <code>/kiss</code> <code>/hug</code> <code>/cuddle</code>\n"
        "<code>/love</code> <code>/highfive</code> <code>/ship</code> <code>/slap</code>\n"
        "<code>/punch</code> <code>/bonk</code> <code>/pat</code> <code>/poke</code> <code>/wink</code>\n"
        "<code>/dance</code> <code>/laugh</code> <code>/cry</code> <code>/angry</code>\n"
        "<code>/cheers</code> <code>/brofist</code> <code>/clap</code> <code>/wave</code>\n\n"
        "<i>Example: reply karke /slap ya /hug @username — sender aur target dono clickable mention ke saath tag honge.</i>"
    ),
    "whisper": (
        f"{headline('Whisper — Secret Message')}\n\n"
        "<b>Inline (kisi bhi chat me) :</b>\n"
        "<code>@bot @username your secret text</code>\n"
        "<code>@bot 123456789 your secret text</code>\n\n"
        "<b>Command se :</b>\n"
        "<code>/whisper @username text</code> · <code>/secret @username text</code>\n"
        "<code>/whisper text</code> — reply karke\n\n"
        "🔸 Sirf receiver (aur sender) hi khol sakta hai.\n"
        "🔸 Sender kabhi bhi 🗑 se destroy kar sakta hai · 48h me auto-expire."
    ),
    "ping": (
        f"{headline('Info &amp; Status')}\n\n"
        "<code>/start</code> — 🚀 Welcome card\n"
        "<code>/help</code> — 📖 Ye menu\n"
        "<code>/ping</code> — 🏓 Latency card\n"
        "<code>/alive</code> <code>/uptime</code> — 💚 Uptime card\n"
        "<code>/stats</code> — 📊 Bot statistics\n"
        "<code>/about</code> — ℹ️ About Melody"
    ),
    "tutorial": (
        f"{headline('Melody — Full Tutorial')}\n\n"
        "<b>1️⃣ Setup</b>\n"
        "🔸 Bot ko group me add karke <b>admin</b> banao (delete, ban, invite, pin).\n"
        "🔸 Voice chat start karo, phir <code>/play song name</code>.\n"
        "🔸 Assistant khud join karega.\n\n"
        "<b>2️⃣ Music</b>\n"
        "🔸 <code>/play</code> · <code>/vplay</code> · <code>/playforce</code>\n"
        "🔸 <code>/queue</code> · <code>/skip</code> · <code>/pause</code> · <code>/resume</code> · <code>/stop</code>\n"
        "🔸 <code>/loop</code> · <code>/seek 30</code> · <code>/volume 120</code> · <code>/speed 1.5</code>\n\n"
        "<b>3️⃣ Channel me music</b>\n"
        "🔸 <code>/channelplay @channel</code> → <code>/cplay song</code>\n\n"
        "<b>4️⃣ Group manage</b>\n"
        "🔸 <code>/ban</code> <code>/mute</code> <code>/warn</code> <code>/purge</code> <code>/lock</code>\n"
        "🔸 <code>/settings</code> se toggles, <code>/protection</code> se anti-spam, <code>/safemode</code> se captcha.\n\n"
        "<b>5️⃣ Extra</b>\n"
        "🔸 <code>/autoplay on</code> · <code>/whisper</code>\n\n"
        "<i>Niche wale buttons se har category ki poori list dekho.</i>"
    ),
    "az1": (
        f"{headline('All Commands — A to L')}\n\n"
        "<i>List load ho rahi hai…</i>"
    ),
    "az2": (
        f"{headline('All Commands — M to Z')}\n\n"
        "<i>List load ho rahi hai…</i>"
    ),
}

HELP_MAIN_TEXT = (
    f"<blockquote>📖 <b>{fancy('MELODY')} — Help Menu</b></blockquote>\n\n"
    "<i>Naya ho? pehle</i> 🎓 <b>Tutorial</b> <i>kholo.</i>\n"
    "<i>Ek section chuno — uske andar us category ke saare commands hain.</i>\n\n"
    "🎵 <b>Music</b> · 🎙 <b>Voice Chat</b> · 🛡 <b>Group Manage</b>\n"
    "♛ <b>Owner Panel</b> · ⚙️ <b>Extras</b> · 🔤 <b>All Commands</b>"
)

# ─── Sections (hubs) ─────────────────────────────────────────────────────────
# Har page sirf EK hub me hai — koi repeat nahi. Music = saare music commands,
# Voice Chat = VC tools, Group Manage = group ke saare commands,
# Owner Panel = sirf bot owner ke personal commands.
HUBS = {
    "hub_music": (
        f"{headline('Music')}\n\n"
        "Gaane bajana, queue, playback control, loop, seek, volume aur channel play.",
        [
            [("▶ Play", "play"), ("📋 Queue", "queue"), ("🎛 Controls", "controls")],
            [("🔁 Loop", "loop"), ("⏩ Seek", "seek"), ("🔊 Volume", "volume")],
            [("🎤 Lyrics", "lyrics"), ("📺 Channel Play", "cplay")],
        ],
    ),
    "hub_vc": (
        f"{headline('Voice Chat')}\n\n"
        "VC activity log, join/left cards aur active voice chats.",
        [
            [("🎙 VC Tools", "vc")],
        ],
    ),
    "hub_group": (
        f"{headline('Group Management')}\n\n"
        "Moderation, cleanup, mass actions, warns, protection, approvals, join requests aur greetings.",
        [
            [("🛡 Moderation", "admin"), ("🧹 Cleanup", "purge")],
            [("👥 Mass & Tag", "mass"), ("⚠ Warns", "warns")],
            [("🧯 Protection", "guard"), ("✅ Approve & Auth", "approve")],
            [("🚪 Join Requests", "joinreq"), ("👋 Greetings", "greet")],
            [("🤖 Owner Assistant", "assistant")],
        ],
    ),
    "hub_owner": (
        f"{headline('Bot Owner Panel')}\n\n"
        "Sirf bot owner / sudo ke liye — panel, sudo, global ban, deploy aur pictures.",
        [
            [("♛ Owner Panel", "owner"), ("🔑 Sudo & Gban", "owner_sudo")],
            [("🛠 Deploy & Debug", "owner_deploy"), ("🖼 Pictures", "pics")],
        ],
    ),
    "hub_extra": (
        f"{headline('Extras')}\n\n"
        "Economy, social GIFs, whisper aur info cards.",
        [
            [("🪙 Economy", "economy")],
            [("💞 Social GIFs", "social"), ("🤫 Whisper", "whisper")],
            [("ℹ Info & Status", "ping")],
        ],
    ),
    "hub_all": (
        f"{headline('All Commands (A → Z)')}\n\n"
        "Live handler table se banti poori list — do page me.",
        [
            [("🔤 A - L", "az1"), ("🔤 M - Z", "az2")],
            [("🎓 Tutorial", "tutorial")],
        ],
    ),
}

# page key → hub it belongs to (drives the BACK button).
_PARENT = {
    page: hub
    for hub, (_, rows) in HUBS.items()
    for row in rows
    for _label, page in row
}
_PARENT["tutorial"] = "main"

_GRID = [
    [("Tutorial", "tutorial")],
    [("Music", "hub_music"), ("Voice Chat", "hub_vc")],
    [("Group Manage", "hub_group"), ("Owner Panel", "hub_owner")],
    [("Extras", "hub_extra"), ("All Commands", "hub_all")],
]

# Extra quick-jump links on the bigger leaf pages.
_PAGE_LINKS = {
    "tutorial": [
        [("🎵 Music", "hub_music"), ("🎙 Voice Chat", "hub_vc")],
        [("🛡 Group Manage", "hub_group"), ("⚙️ Extras", "hub_extra")],
        [("🔤 All Commands", "hub_all")],
    ],
    "play": [[("📋 Queue", "queue"), ("🎛 Controls", "controls")]],
    "queue": [[("▶ Play", "play"), ("🔁 Loop", "loop")]],
    "controls": [[("⏩ Seek", "seek"), ("🔊 Volume", "volume")]],
    "vc": [[("▶ Play", "play"), ("🎓 Tutorial", "tutorial")]],
    "admin": [[("🧹 Cleanup", "purge"), ("⚠ Warns", "warns")]],
    "guard": [[("✅ Approve & Auth", "approve"), ("👋 Greetings", "greet")]],
    "owner": [[("🔑 Sudo & Gban", "owner_sudo"), ("🛠 Deploy", "owner_deploy")]],
    "az1": [[("🔤 M - Z", "az2")]],
    "az2": [[("🔤 A - L", "az1")]],
}


def main_help_kb(is_private: bool = False, is_owner: bool = False) -> InlineKeyboardMarkup:
    """Native colored category grid (identical in private chats and groups)."""
    rows = []
    category_icons = {
        "tutorial": "📖", "hub_music": "▶", "hub_vc": "🎙",
        "hub_group": "🛡", "hub_admin": "♛", "hub_extra": "✨",
        "hub_all": "🔤",
    }
    styles = (STYLE_PRIMARY, STYLE_SUCCESS, STYLE_WARNING)
    rows.extend([
        [
            ikb(
                label,
                callback_data=f"help_{key}",
                icon=__import__("utils.emoji_map", fromlist=["EMOJI_ID_MAP"]).EMOJI_ID_MAP.get(category_icons.get(key, "")),
                style=styles[index % len(styles)],
            )
            for index, (label, key) in enumerate(row)
        ]
        for row in _GRID
    ])
    rows.append([ikb(
        "➕ Aᴅᴅ Mᴇ Tᴏ Yᴏᴜʀ Gʀᴏᴜᴘ",
        url=f"https://t.me/{Config.BOT_USERNAME.lstrip('@')}?startgroup=true",
    )])
    from utils.emoji_map import EMOJI_ID_MAP
    rows.append([ikb("CLOSE", callback_data="help_close", icon=EMOJI_ID_MAP.get("❌"), style=STYLE_DANGER)])
    return InlineKeyboardMarkup(rows)


def hub_kb(key: str) -> InlineKeyboardMarkup:
    """Section page: its own pages + BACK to the root menu."""
    from utils.emoji_map import EMOJI_ID_MAP

    _text, page_rows = HUBS[key]
    rows = [
        [ikb(label, callback_data=f"help_{target}") for label, target in row]
        for row in page_rows
    ]
    rows.append([
        ikb("BACK", callback_data="help_main", icon=EMOJI_ID_MAP.get("◀"), style=STYLE_PRIMARY),
        ikb("CLOSE", callback_data="help_close", icon=EMOJI_ID_MAP.get("❌"), style=STYLE_DANGER),
    ])
    return InlineKeyboardMarkup(rows)


def back_kb(key: str = "") -> InlineKeyboardMarkup:
    """Quick-jump links + BACK to the page's own section (not the root)."""
    from utils.emoji_map import EMOJI_ID_MAP

    rows = [
        [ikb(label, callback_data=f"help_{target}") for label, target in row]
        for row in _PAGE_LINKS.get(key, [])
    ]
    parent = _PARENT.get(key, "main")
    rows.append([
        ikb("BACK", callback_data=f"help_{parent}", icon=EMOJI_ID_MAP.get("◀"), style=STYLE_PRIMARY),
        ikb("CLOSE", callback_data="help_close", icon=EMOJI_ID_MAP.get("❌"), style=STYLE_DANGER),
    ])
    return InlineKeyboardMarkup(rows)


# ── A-Z pages are built from the LIVE handler table ─────────────────────────
# BUG FIX: the A-L / M-Z pages were hand-typed lists that had drifted badly
# out of sync with the code — ~75 real commands (/vcchatlog, /trigger,
# /secret, /admincache, /playing, /next, /staff, /utag, /options, every
# /gc* and greeting command, ...) were never listed, "/reload" appeared
# twice, and a few listed names no longer existed. Reading the registered
# command filters straight off the dispatcher at first use means the list
# can never go stale again.

_AZ_CACHE: dict[str, str] = {}


def _filter_commands(flt, seen: set[int] | None = None) -> set[str]:
    """Collect command names from a Pyrogram filter tree at any depth."""
    if flt is None:
        return set()
    if seen is None:
        seen = set()
    marker = id(flt)
    if marker in seen:
        return set()
    seen.add(marker)

    names = {str(command).lower() for command in (getattr(flt, "commands", None) or ())}
    for child_name in ("base", "other"):
        names.update(_filter_commands(getattr(flt, child_name, None), seen))
    return names


def _registered_commands() -> list[str]:
    """Every command string currently registered on the bot's dispatcher."""
    names: set[str] = set()
    try:
        groups = bot.dispatcher.groups
    except Exception:
        return []
    for handlers in groups.values():
        for handler in handlers:
            names.update(_filter_commands(getattr(handler, "filters", None)))
    return sorted(names)


def _az_page(half: str) -> str:
    if half in _AZ_CACHE:
        return _AZ_CACHE[half]
    cmds = _registered_commands()
    if not cmds:
        return HELP_PAGES[half]  # dispatcher not ready — fall back to static text
    if half == "az1":
        title, picked = "A - L", [c for c in cmds if c[0] <= "l"]
    else:
        title, picked = "M - Z", [c for c in cmds if c[0] > "l"]
    body = " ".join("/" + c for c in picked)
    text = (
        f"<blockquote>\U0001f524 <b>All Commands \u2014 {title}</b></blockquote>\n\n"
        f"<code>{body}</code>"
    )
    _AZ_CACHE[half] = text
    return text


def render_help_category(key: str, is_private: bool = False):
    """Shared renderer for the callback flow and the Mini App flow."""
    if key == "main":
        return HELP_MAIN_TEXT, main_help_kb(is_private=is_private)
    if key in HUBS:
        return HUBS[key][0], hub_kb(key)
    if key in ("az1", "az2"):
        return _az_page(key), back_kb(key)
    if key in HELP_PAGES:
        return HELP_PAGES[key], back_kb(key)
    return None, None



# ─── Handlers ────────────────────────────────────────────────────────────────

@bot.on_message(filters.command("help"))
@error_handler
async def help_cmd(client: Client, message: Message):
    is_private = message.chat.type == enums.ChatType.PRIVATE
    await safe_html_action(
        lambda t, **kw: message.reply(t, **kw),
        HELP_MAIN_TEXT,
        client=client,
        reply_markup=main_help_kb(
            is_private=is_private,
            is_owner=bool(message.from_user and message.from_user.id == Config.OWNER_ID),
        ),
    )


@bot.on_callback_query(filters.regex(r"^help_(.+)$"))
@error_handler
async def help_cb(client: Client, cb: CallbackQuery):
    key = cb.data.split("help_", 1)[1]

    if key in {"owner", "pics", "owner_sudo", "owner_deploy", "hub_owner"} and cb.from_user.id != Config.OWNER_ID:
        return await cb.answer("Owner only.", show_alert=True)

    if key == "close":
        try:
            await cb.message.delete()
        except Exception:
            pass
        return await cb.answer()

    is_private = cb.message.chat.type == enums.ChatType.PRIVATE
    text, markup = render_help_category(key, is_private=is_private)
    if not text:
        return await cb.answer("Ye page ab available nahi hai.", show_alert=True)

    # BUG FIX: photo-backed help cards have no `text`, only a caption —
    # edit_text() on them raises MESSAGE_TEXT_EMPTY. Pick the right editor,
    # and route both through safe_html_action so a bad custom-emoji entity
    # can never produce ENTITY_TEXT_INVALID again.
    editor = (
        cb.message.edit_caption
        if (cb.message.photo or cb.message.video or cb.message.animation)
        else cb.message.edit_text
    )
    try:
        await safe_html_action(
            lambda t, **kw: editor(t, **kw),
            text,
            client=client,
            reply_markup=markup,
        )
    except Exception:
        # Editing failed entirely (message too old / deleted) — send fresh.
        await safe_html_action(
            lambda t, **kw: client.send_message(cb.message.chat.id, t, **kw),
            text,
            client=client,
            reply_markup=markup,
        )
    await cb.answer()
