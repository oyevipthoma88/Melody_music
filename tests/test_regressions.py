import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_python_sources_parse():
    for path in [*ROOT.joinpath("melody").rglob("*.py"), *ROOT.joinpath("utils").rglob("*.py")]:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_safe_defaults_are_centralized():
    defaults = (ROOT / "melody/core/settings_defaults.py").read_text(encoding="utf-8")
    assert "AUTOPLAY_DEFAULT = True" in defaults
    assert '"enabled": False' in defaults


def test_add_button_opens_telegram_group_picker():
    source = (ROOT / "utils/inline.py").read_text(encoding="utf-8")
    assert "?startgroup=true" in source


def test_play_starts_peer_work_in_parallel_and_keeps_single_stream_owner():
    play = (ROOT / "melody/plugins/music/play.py").read_text(encoding="utf-8")
    call = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "spawn(ensure_assistant_peer(chat.id)" in play
    assert "never start a second direct resolver" in play
    assert "_stream_track()` is the single owner" in play
    assert "download_task.cancel()" in call
    assert "_peer_inflight: dict[int, asyncio.Task]" in call
    assert "return bool(await asyncio.shield(existing))" in call


def test_music_only_profile_filters_unrelated_plugins():
    startup = (ROOT / "melody/__main__.py").read_text(encoding="utf-8")
    config = (ROOT / "melody/config.py").read_text(encoding="utf-8")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert 'MUSIC_ONLY_MODE: bool = _env_bool("MUSIC_ONLY_MODE", False)' in config
    assert '"MUSIC_ONLY_MODE": {' in (ROOT / "app.json").read_text(encoding="utf-8")
    assert '"value": "false"' in (ROOT / "app.json").read_text(encoding="utf-8")
    assert 'allowed_prefix = "melody.plugins.music."' in startup
    assert "if music_only and not module_name.startswith(allowed_prefix):" in startup
    assert "if not Config.MUSIC_ONLY_MODE:" in startup
    assert "MUSIC_ONLY_MODE=false" in env
    assert "PLAYBACK_RECOVERY=false" in env
    assert "MONGO_GRIDFS_CACHE=false" in env
    watcher = (ROOT / "melody/plugins/music/vc_session.py").read_text(encoding="utf-8")
    assert "if Config.MUSIC_ONLY_MODE:" in watcher
    assert "_watcher = asyncio.get_running_loop().create_task(_watchdog())" in watcher


def test_restart_and_song_cache_are_wired():
    startup = (ROOT / "melody/__main__.py").read_text(encoding="utf-8")
    ytdl = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "recover_playback" in startup
    assert "restore_archived_file" in ytdl
    archive = (ROOT / "utils/telegram_archive.py").read_text(encoding="utf-8")
    assert "archive_completed_file" in archive
    assert "MONGO" not in archive

def test_download_dedup_does_not_await_while_holding_lock():
    source = Path("melody/core/ytdl.py").read_text()
    registry = source.index("async with _download_futures_lock:")
    release = source.index("if existing is not None:", registry)
    locked_block = source[registry:release]
    assert "await existing" not in locked_block
    assert "return await asyncio.shield(fut)" in source[release:]


def test_stream_url_cache_is_bounded():
    source = Path("melody/core/ytdl.py").read_text()
    assert "_STREAM_URL_CACHE_MAX = 128" in source
    assert "def _prune_stream_url_state" in source
    assert source.count("_prune_stream_url_state()") >= 2


def test_heroku_memory_defaults_are_conservative():
    pools = Path("melody/core/pools.py").read_text()
    player = Path("melody/core/call.py").read_text()
    assert '_DEFAULT_YTDL_WORKERS = 4 if _MEMORY_LIMIT_MB <= 768 else 8' in pools
    assert '_DEFAULT_IO_WORKERS = 2 if _MEMORY_LIMIT_MB <= 768 else 4' in pools
    assert 'YTDL_WORKERS = (' in pools
    assert 'IO_WORKERS = (' in pools
    assert 'os.getenv("SONG_CACHE_MB", "96")' in player


def test_innertube_calls_are_keyless():
    """Legacy `?key=AIza...` on /player and /search 404s for several clients.

    Those dead round-trips were pure latency in front of every /play.
    """
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "youtubei/v1/player?key=" not in source
    assert '_SEARCH_URL}?key=' not in source


def test_ytdlp_progress_bar_is_muted():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert '"noprogress": True' in source


def test_repository_url_is_not_hardcoded():
    """The owner's repository must stay private: no repo URL in the source."""
    for path in [*ROOT.joinpath("melody").rglob("*.py"), *ROOT.joinpath("utils").rglob("*.py")]:
        text = path.read_text(encoding="utf-8")
        assert "thomas82822" not in text, path
    src = (ROOT / "melody/plugins/owner/source_code.py").read_text(encoding="utf-8")
    assert 'os.environ.get("SOURCE_CODE_URL"' in src


def test_join_requests_only_auto_approve_music_assistant():
    source = (ROOT / "melody/plugins/admin/join_requests.py").read_text(encoding="utf-8")
    assert "user.id == assistant_id" in source
    assert source.count("approve_chat_join_request") == 2
    # The second approval is the admin-authorized callback; there must be no
    # unconditional automatic approval for ordinary users.
    assert "cb_admin_or_auth" in source


def test_anonymous_group_owner_is_never_rendered_or_tagged():
    owner_view = (ROOT / "utils/owner_view.py").read_text(encoding="utf-8")
    start = (ROOT / "melody/plugins/misc/start.py").read_text(encoding="utf-8")
    gcmanage = (ROOT / "melody/plugins/admin/gcmanage.py").read_text(encoding="utf-8")
    assert "def is_anonymous_message" in owner_view
    assert "anonymous_adder = is_anonymous_message(message)" in start
    assert "adder = None if anonymous_adder" in start
    assert gcmanage.count("not is_anonymous_member(a)") >= 2


def test_heroku_uses_apt_ffmpeg_not_dead_static_buildpack():
    app = (ROOT / "app.json").read_text(encoding="utf-8")
    aptfile = (ROOT / "Aptfile").read_text(encoding="utf-8")
    assert "heroku-community/apt" in app
    assert "jonathanong/heroku-buildpack-ffmpeg-latest" not in app
    assert aptfile.strip() == "ffmpeg"


def test_ffmpeg_runtime_guard_fixes_libpulse_loader_failure():
    """apt ffmpeg died on libpulsecommon-*.so -> streams ended instantly."""
    guard = (ROOT / "utils/ffmpeg_runtime.py").read_text(encoding="utf-8")
    assert "def ensure_ffmpeg_runtime" in guard
    assert "pulseaudio" in guard
    assert "LD_LIBRARY_PATH" in guard
    startup = (ROOT / "melody/__main__.py").read_text(encoding="utf-8")
    assert "ensure_ffmpeg_runtime" in startup
    boot = (ROOT / ".profile.d/zz-ffmpeg.sh").read_text(encoding="utf-8")
    assert "pulseaudio" in boot
    assert "vendor/ffmpeg/bin" in boot
    post_compile = (ROOT / "bin/post_compile").read_text(encoding="utf-8")
    assert "vendor/ffmpeg/bin" in post_compile


def test_private_start_cannot_hang_after_instant_reaction():
    """Slow Mongo/media calls must fall back instead of leaving only a 👍."""
    start = (ROOT / "melody/plugins/misc/start.py").read_text(encoding="utf-8")
    assert "async def _start_db_call" in start
    assert "async def _send_start_welcome" in start
    assert "asyncio.wait_for(awaitable, timeout=_START_DB_TIMEOUT)" in start
    assert "send_quote(message, WELCOME_DM" in start
    assert "start_pic = await _start_db_call(get_pic(\"start\"), None)" in start


def test_memory_guard_is_started():
    startup = (ROOT / "melody/__main__.py").read_text(encoding="utf-8")
    assert "memory_guard" in startup
    guard = (ROOT / "utils/memguard.py").read_text(encoding="utf-8")
    assert "malloc_trim" in guard


def test_custom_emoji_entities_reject_non_emoji_glyphs():
    """'🇩', '❜', '✘', '♥' triggered 400 ENTITY_TEXT_INVALID on Telegram."""
    import importlib.util
    import sys

    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("_emoji_map_t", ROOT / "utils/emoji_map.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for bad in ("🇩", "❜", "✘", "♥", "🕊", "▶"):
        assert module._entity_safe(bad) is False, bad
    for good in ("🎵", "😀", "🪩", "▶️"):
        assert module._entity_safe(good) is True, good


def test_vc_activity_is_default_on_and_owner_help_is_guarded():
    notify = (ROOT / "melody/core/vc_notify.py").read_text(encoding="utf-8")
    toggles = (ROOT / "melody/plugins/admin/toggles.py").read_text(encoding="utf-8")
    help_source = (ROOT / "melody/plugins/misc/help.py").read_text(encoding="utf-8")
    assert 'get_setting_flag(chat_id, "vc_activity", True)' in notify
    # Screen/camera/hand state notices were intentionally dropped; the
    # activity flag now only gates JOIN / LEFT cards.
    assert "#JᴏɪɴᴇᴅVɪᴅᴇᴏCʜᴀᴛ" in notify
    assert "set_t_vcactivity" in toggles
    # The owner-only guard now covers every owner hub/page, not just
    # {"owner", "pics"} — assert on the behaviour, not the old literal.
    assert "cb.from_user.id != Config.OWNER_ID" in help_source
    for owner_key in ("owner", "pics", "owner_sudo", "owner_deploy", "hub_owner"):
        assert f'"{owner_key}"' in help_source


def test_emoji_verification_fails_open():
    """A bot account cannot call messages.GetCustomEmojiDocuments, so an
    unverifiable id must stay premium instead of being unwrapped to plain
    unicode (that made every card render with normal emoji)."""
    resolver = Path("utils/custom_emoji.py").read_text()
    assert "class EmojiResolutionUnavailable" in resolver
    assert "BOT_METHOD_INVALID" in resolver
    assert "raise EmojiResolutionUnavailable" in resolver

    html = Path("utils/telegram_html.py").read_text()
    assert "except EmojiResolutionUnavailable" in html
    assert "cache.get(emoji_id, True)" in html


def test_emoji_map_keeps_static_map_when_unverifiable():
    source = Path("utils/emoji_map.py").read_text()
    assert "except EmojiResolutionUnavailable" in source
    search = Path("utils/emoji_search.py").read_text()
    assert "except EmojiResolutionUnavailable" in search


def test_button_icons_use_sequence_matching_not_random_pool():
    """Inline-button premium icons must MATCH the label glyph.

    Regression: utils/buttons.icon_id_for() walked the label codepoint by
    codepoint and fell back to assign_pool_id(), so flags/keycaps/ZWJ emoji
    never matched and buttons animated an unrelated premium emoji.
    """
    source = (ROOT / "utils/buttons.py").read_text(encoding="utf-8")
    assert "def label_glyphs" in source
    assert "def icon_ids_for" in source
    assert "_EMOJI_SEQ_RE" in source
    assert "assign_pool_id" not in source
    # A quarantined primary id must fall through to the next verified variant
    # for the SAME glyph instead of dropping the icon entirely.
    assert "next((c for c in chain if c not in blocked), None)" in source


def test_every_literal_button_label_has_a_matching_premium_icon():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "tools/verify_button_emojis.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "unmatched glyphs .............. 0" in result.stdout
    assert "MISMATCHED glyph/id pairs ..... 0" in result.stdout


def test_playback_recovery_runs_before_secondary_startup_tasks():
    """On restart the voice chat must resume FIRST, other systems after."""
    startup = (ROOT / "melody/__main__.py").read_text(encoding="utf-8")
    assert "async def warm_recovery_peers" in startup
    recover = startup.index("recovered = await recover_playback()")
    assert startup.index("await start_call_py()") < recover
    # emoji map / slash commands / full peer warm-up are all secondary.
    assert recover < startup.index("resolve_emoji_map(bot)")
    assert recover < startup.rindex("await send_startup_log(")


def test_downloads_are_globally_throttled_for_r14():
    """Heroku R14: mem=1056M(102%) -> mem=1613M(157%) in 18s.

    Every /play used to spawn an unthrottled thread with its own yt-dlp
    working set. A global semaphore is what keeps concurrent downloads (and
    therefore RSS) bounded.
    """
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "MAX_CONCURRENT_DOWNLOADS" in source
    assert "def _download_semaphore" in source
    assert "class _PriorityDownloadGate" in source
    assert "await gate.acquire(priority)" in source
    assert "await gate.release()" in source


def test_playback_only_uses_validated_audio_prefix_handoff():
    """Cloud audio may start from a safe prefix; video/MP4 stays completion-only."""
    ytdl = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    call = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    warm = (ROOT / "melody/plugins/music/play.py").read_text(encoding="utf-8")
    assert '_env_flag("EARLY_HANDOFF", False)' in ytdl
    assert '_EARLY_AUDIO_HANDOFF_ENABLED = _env_flag("EARLY_AUDIO_HANDOFF", True)' in ytdl
    assert 'if not audio_only or not _early_audio_path_is_safe' in ytdl
    assert 'await done_async.wait()' in ytdl
    assert 'allow_early=not video' in call
    assert 'await _persist_completed_song(archived_path, track)' in call
    assert 'completed = await wait_for_download(' in call
    assert 'if not should_try_direct_stream():' in warm


def test_ytdl_caches_are_bounded_and_releasable():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "def drop_caches" in source
    assert "_TG_STATE_MAX = 256" in source
    assert "_bound_dict(TG_MEDIA_SOURCES, _TG_STATE_MAX)" in source



def test_memguard_reacts_before_r14():
    guard = (ROOT / "utils/memguard.py").read_text(encoding="utf-8")
    assert 'int(quota * 0.65)' in guard
    assert '_env_int("MEM_CHECK_SECONDS", 5)' in guard
    assert "quota * 0.80" in guard
    assert "drop_caches" in guard


def test_prefetch_is_memory_adaptive_and_interactive_priority_wins():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert 'os.getenv("PREFETCH_WORKERS", "2")' in source
    assert "memory_mb >= 1024" in source
    assert "priority=20 + (rank * 15)" in source
    assert "asyncio.gather" in source


def test_no_bare_fire_and_forget_tasks():
    """asyncio only weak-refs tasks: a bare `asyncio.create_task(...)`
    statement can be garbage-collected mid-flight. Every background job must
    go through utils.tasks.spawn(), which keeps a strong reference and logs
    failures instead of losing them."""
    import ast

    offenders = []
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts or path.name == "tasks.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "create_task"
                and isinstance(func.value, ast.Name)
                and func.value.id == "asyncio"
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, "use utils.tasks.spawn() instead: " + ", ".join(offenders)



def test_search_result_callbacks_share_play_permission_policy():
    source = (ROOT / "melody/plugins/music/search.py").read_text(encoding="utf-8")
    assert "@error_handler\nasync def play_search_cb" in source
    assert "cb_playmode_gate" in source
    assert "cb_playmode_gate(client, cb, chat.id)" in source


def test_linked_channel_controls_allow_authorized_users():
    source = (ROOT / "melody/plugins/music/channel_controls.py").read_text(encoding="utf-8")
    assert "from utils.decorators import error_handler, is_admin_or_auth" in source
    assert "is_admin_or_auth(" in source
    assert "from utils.admin_tools import is_admin" not in source


def test_vc_backup_destination_is_opt_in():
    config = (ROOT / "melody/config.py").read_text(encoding="utf-8")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert 'VC_CHAT_LOG_CHANNEL_ID: int = _env_int("VC_CHAT_LOG_CHANNEL_ID")' in config
    assert "-1004445716740" not in config
    assert "VC_CHAT_LOG_CHANNEL_ID=" in env


def test_about_cards_do_not_expose_developer_attribution():
    about = (ROOT / "melody/plugins/misc/about.py").read_text(encoding="utf-8")
    start = (ROOT / "melody/plugins/misc/start.py").read_text(encoding="utf-8")
    assert "Sasta Developer" not in about + start
    assert "Made with 💛 for music lovers" in about + start


def test_autoplay_duration_env_is_safe():
    source = (ROOT / "melody/core/autoplay.py").read_text(encoding="utf-8")
    assert "def _autoplay_duration_limit()" in source
    assert "except (TypeError, ValueError)" in source
    assert "AUTOPLAY_MAX_DURATION = _autoplay_duration_limit()" in source


def test_same_song_mode_switch_invalidates_predownload():
    source = (ROOT / "melody/core/queue.py").read_text(encoding="utf-8")
    assert "bool(previous_mode) != bool(video)" in source
    assert "_predownloaded.pop(chat_id, None)" in source


def test_decorator_module_has_no_duplicate_callback_helper():
    source = (ROOT / "utils/decorators.py").read_text(encoding="utf-8")
    assert source.count("async def cb_admin_or_auth(") == 1
    assert source.count("async def is_admin_or_auth(") == 1
    assert "    return status\n" in source


def test_linked_group_cplay_commands_are_permission_gated():
    source = (ROOT / "melody/plugins/music/channelplay.py").read_text(encoding="utf-8")
    assert '@bot.on_message(filters.command("cplay") & filters.group)\n@error_handler\n@admin_or_auth' in source
    assert '@bot.on_message(filters.command("cvplay") & filters.group)\n@error_handler\n@admin_or_auth' in source


def test_help_command_index_walks_nested_filter_trees():
    source = (ROOT / "melody/plugins/misc/help.py").read_text(encoding="utf-8")
    assert "def _filter_commands(flt, seen: set[int] | None = None)" in source
    assert "_filter_commands(getattr(flt, child_name, None), seen)" in source
    assert "names.update(_filter_commands(getattr(handler, \"filters\", None)))" in source


def test_command_patch_preserves_filter_metadata_for_help():
    source = (ROOT / "utils/command_patch.py").read_text(encoding="utf-8")
    assert "commands=lowered" in source
    assert "prefixes=set(prefix_list)" in source
    assert "case_sensitive=case_sensitive" in source


def test_startup_heavy_warmups_are_opt_in():
    config = (ROOT / "melody/config.py").read_text(encoding="utf-8")
    startup = (ROOT / "melody/__main__.py").read_text(encoding="utf-8")
    assert 'MEMORY_LIMIT_MB: int = _env_int("MEMORY_LIMIT_MB", 512)' in config
    assert 'STARTUP_WARMUPS: bool = (' in config
    assert 'BGUTIL_STARTUP_WARMUP: bool = (' in config
    assert 'and not _LOW_MEMORY_PROFILE' in config
    assert "if Config.BGUTIL_STARTUP_WARMUP or Config.STARTUP_WARMUPS:" in startup
    assert "if Config.STARTUP_WARMUPS:" in startup


def test_playback_fallback_is_lazy_and_memory_bounded():
    ytdl = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    init = (ROOT / "melody/__init__.py").read_text(encoding="utf-8")
    pools = (ROOT / "melody/core/pools.py").read_text(encoding="utf-8")
    assert "_DEFAULT_CONCURRENT_DOWNLOADS = 1 if _MEMORY_BUDGET_MB <= 1024 else 2" in ytdl
    assert "1 if _MEMORY_BUDGET_MB <= 1024 else min(2, _requested_downloads)" in ytdl
    assert "ytdlp_future = None" in ytdl
    assert "workers=_worker_count(\"BOT_WORKERS\", 8, 16)" in init
    assert "workers=_worker_count(\"ASSISTANT_WORKERS\", 4, 8)" in init
    assert "threading.stack_size(512 * 1024)" in pools


def test_remote_probe_uses_dedicated_io_pool():
    source = (ROOT / "utils/pytgcalls_patch.py").read_text(encoding="utf-8")
    assert 'REMOTE_CHECK_TIMEOUT", "2.5"' in source
    assert "from melody.core.pools import IO_POOL" in source
    assert "loop.run_in_executor(\n                IO_POOL" in source


def test_bare_youtube_ids_use_the_direct_metadata_route():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert 're.fullmatch(r"[A-Za-z0-9_-]{11}", url_or_query.strip())' in source
    assert "if vid_id:" in source
    assert "_innertube_player_sync(vid_id)" in source


def test_small_dyno_hard_caps_ignore_stale_high_config_vars():
    pools = (ROOT / "melody/core/pools.py").read_text(encoding="utf-8")
    ytdl = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    init = (ROOT / "melody/__init__.py").read_text(encoding="utf-8")
    assert "if _MEMORY_LIMIT_MB <= 768" in pools
    assert "1 if _MEMORY_BUDGET_MB <= 1024 else min(2, _requested_downloads)" in ytdl
    assert "hard_max = default if _CLIENT_MEMORY_LIMIT_MB <= 768 else maximum" in init


def test_slash_menu_contains_canonical_command_families():
    source = (ROOT / "melody/__main__.py").read_text(encoding="utf-8")
    for command in (
        "play", "playforce", "playlist", "queue", "skip", "stop",
        "balance", "daily", "work", "deposit", "withdraw", "pay",
        "leaderboard", "economy", "couple", "couples", "kiss", "hug", "slap",
        "punch", "channelplay", "playmode", "adminlist", "promote",
        "warn", "purge", "protection",
    ):
        assert f'BotCommand("{command}"' in source
    for command in ("cplay", "cpause", "cskip", "cqueue", "cnp", "cvolume"):
        assert f'BotCommand("{command}"' in source
    for command in ("panel", "logs", "chatlist", "restart", "eval", "shell"):
        assert f'BotCommand("{command}"' in source
    assert "BotCommandScopeChat(int(Config.OWNER_ID))" in source


def test_economy_is_persistent_and_non_gambling():
    economy = (ROOT / "melody/plugins/misc/economy.py").read_text(encoding="utf-8")
    db = (ROOT / "utils/economy_db.py").read_text(encoding="utf-8")
    for command in ("balance", "daily", "work", "deposit", "withdraw", "pay", "leaderboard"):
        assert f'"{command}"' in economy
    assert 'economy_users' in db
    assert 'find_one_and_update' in db
    assert "No betting, no real-money value" in economy


def test_social_commands_have_gif_fallbacks():
    source = (ROOT / "melody/plugins/misc/social.py").read_text(encoding="utf-8")
    for command in ("couple", "couples", "kiss", "hug", "cuddle", "love", "highfive", "ship"):
        assert f'"{command}"' in source
    assert "send_animation" in source
    assert "except Exception:" in source


def test_social_commands_tag_users_and_render_premium_pair_cards():
    source = (ROOT / "melody/plugins/misc/social.py").read_text(encoding="utf-8")
    assert "TEXT_MENTION" in source
    assert "return tag(int(user.id)" in source
    assert "Compatibility" in source
    assert "switch_inline_query_current_chat" in source
    assert 'requested == "couples"' in source


def test_thumbnail_uses_professional_renderer():
    source = (ROOT / "utils/thumbnails.py").read_text(encoding="utf-8")
    assert "_render_thumbnail_sync_professional" in source
    assert "NOW PLAYING" in source
    assert "rounded_rectangle" in source
    assert "ImageOps.fit" in source


def test_economy_pay_supports_reply_and_username_amount_positions():
    source = (ROOT / "melody/plugins/misc/economy.py").read_text(encoding="utf-8")
    assert "return message.reply_to_message.from_user, index" in source
    assert "return await client.get_users(ref), index + 1" in source


def test_social_reaction_catalog_is_complete_and_targeted():
    source = (ROOT / "melody/plugins/misc/social.py").read_text(encoding="utf-8")
    for command in (
        "couple", "kiss", "hug", "cuddle", "love", "highfive", "ship",
        "slap", "punch", "bonk", "pat", "poke", "wink", "dance", "laugh",
        "cry", "angry", "cheers", "brofist", "clap", "wave",
    ):
        assert f'"{command}"' in source
    assert "reply_to_message" in source
    assert "get_users" in source
    assert "send_animation" in source


def test_help_lists_expanded_social_actions():
    source = (ROOT / "melody/plugins/misc/help.py").read_text(encoding="utf-8")
    for command in ("/couple", "/couples", "/slap", "/punch", "/bonk", "/pat", "/poke", "/wink", "/dance", "/cheers", "/brofist", "/wave"):
        assert command in source
    assert "clickable mention" in source


def test_playback_state_handles_atlas_quota_without_breaking_playback():
    source = (ROOT / "utils/playback_state.py").read_text(encoding="utf-8")
    assert "_is_storage_quota_error" in source
    assert "_WRITES_DISABLED" in source
    assert "writes are blocked" in source
    assert "playback continues without restart recovery metadata" in source
    assert "if _WRITES_DISABLED:" in source


def test_download_waiter_tasks_consume_supersession_exceptions():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert source.count("future_task.add_done_callback(_consume_download_future)") >= 2


def test_mongo_uri_validation_catches_deploy_button_mistakes():
    source = (ROOT / "melody/config.py").read_text(encoding="utf-8")
    assert "def _mongo_uri(name: str)" in source
    assert "def _validate_mongo_uri(name: str, value: str)" in source
    assert "mongodb+srv://" in source
    assert "not a label, dashboard URL, or placeholder" in source
    assert "contains an empty host or extra comma" in source
    assert 'MONGO_DB_URI: str = _mongo_uri("MONGO_DB_URI")' in source




def test_heroku_music_runtime_has_worker_and_streaming_prerequisites():
    procfile = (ROOT / "Procfile").read_text(encoding="utf-8")
    app_json = (ROOT / "app.json").read_text(encoding="utf-8")
    aptfile = (ROOT / "Aptfile").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "worker:" in procfile and "python -m melody" in procfile
    assert "heroku-community/apt" in app_json and "heroku/python" in app_json
    assert "ffmpeg" in aptfile.lower()
    assert "yt-dlp>=" in requirements
    assert "curl_cffi>=" in requirements
    assert "py-tgcalls>=" in requirements


def test_deployment_template_exposes_only_live_mongo_uri():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "MONGO_DB_URI=" in env_example


def test_play_audio_path_does_not_wait_for_ui_before_stream():
    source = (ROOT / "melody/plugins/music/play.py").read_text(encoding="utf-8")
    stream_pos = source.index("playing_now = await play_stream")
    ui_wait_pos = source.index("processing = await processing_task", stream_pos)
    assert ui_wait_pos > stream_pos
    assert "Telegram UI round-trips are non-audio work" in source


def test_primary_play_uses_real_audio_to_create_the_voice_chat():
    source = (ROOT / "melody/plugins/music/play.py").read_text(encoding="utf-8")
    assert "join_task = asyncio.create_task(pre_join" not in source
    assert "force_play_stream(" in source and "video=video, prejoin=False" in source
    assert "play_stream(" in source and "video=video, prejoin=False" in source


def test_fresh_log_force_play_is_serialized_and_clears_failed_state():
    call_source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    queue_source = (ROOT / "melody/core/queue.py").read_text(encoding="utf-8")
    assert "async with _get_play_lock(chat_id):" in call_source
    assert "remove_matching_track(chat_id, track.video_id)" in call_source
    assert "if result is not True and _resolving.get(chat_id) == gen" in call_source
    assert "def remove_matching_track(chat_id: int, video_id: str)" in queue_source


def test_fresh_log_chat_list_is_bounded_and_acknowledged_first():
    panel = (ROOT / "melody/plugins/owner/panel.py").read_text(encoding="utf-8")
    logs = (ROOT / "melody/plugins/owner/logs.py").read_text(encoding="utf-8")
    database = (ROOT / "utils/database.py").read_text(encoding="utf-8")
    assert 'await cb.answer("Loading chat list…")' in panel
    assert "get_chats_page(limit=page_size, offset=offset)" in panel
    assert "owner_chatlist:{offset + page_size}" in panel
    assert "asyncio.wait_for(" in panel and "get_chats_page(limit=page_size, offset=offset)" in panel
    assert "to_list(length=limit)" in database
    assert "get_chats_page(limit=50)" in logs


def test_picture_setters_ack_after_file_id_and_defer_binary_backup():
    helper = (ROOT / "utils/picture_assets.py").read_text(encoding="utf-8")
    for path in (
        ROOT / "melody/plugins/owner/set_start_pic.py",
        ROOT / "melody/plugins/owner/set_welcome_pic.py",
        ROOT / "melody/plugins/misc/pics.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "save_picture_file_id" in source
        assert "schedule_picture_backup" in source
    assert "await asyncio.wait_for(set_pic(key, file_id), timeout=_PICTURE_DB_TIMEOUT)" in helper
    assert "client.download_media(file_id, file_name=str(tmp))" in helper
    assert "os.replace(source, target)" in helper
    assert "name=f\"picture-backup:{key}\"" in helper
    assets = (ROOT / "utils/github_assets.py").read_text(encoding="utf-8")
    assert "timeout=_ASSET_DB_TIMEOUT" in assets
    gc_db = (ROOT / "utils/gc_db.py").read_text(encoding="utf-8")
    assert "to_list(length=50)" in gc_db


def test_fresh_log_direct_resolver_negative_cache_is_bounded():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "_stream_url_failures: dict = {}" in source
    assert "_STREAM_URL_FAILURE_TTL = 8.0" in source
    assert "direct stream temporarily unavailable (cached failure)" in source
    assert "_stream_url_failures.clear()" in source


def test_download_first_playback_is_default_with_audio_early_handoff():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert 'os.getenv("DIRECT_STREAM", "true")' in source
    assert '_EARLY_AUDIO_HANDOFF_ENABLED = _env_flag("EARLY_AUDIO_HANDOFF", True)' in source
    assert 'allow_early=not video' in (ROOT / "melody/core/call.py").read_text(encoding="utf-8")


def test_fresh_log_ytdlp_plugin_loading_is_single_flight():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "_YTDLP_INIT_LOCK = threading.Lock()" in source
    assert "with _YTDLP_INIT_LOCK:" in source
    assert source.count("with _locked_ytdl(opts) as ydl:") >= 5


def test_cloud_fallback_download_starts_immediately_and_persists_complete_audio():
    call_source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    archive_source = (ROOT / "utils/telegram_archive.py").read_text(encoding="utf-8")
    assert 'float(os.getenv("DOWNLOAD_START_DELAY", "0.0"))' in call_source
    assert "_IS_CLOUD_RUNTIME" in call_source
    assert '_DOWNLOAD_START_DELAY = max(0.0, configured_download_delay)' in call_source
    assert "spawn(_persist_completed_song(filepath, track), name=f\"song-cache:{track.video_id}\")" in call_source
    assert "archive_completed_file" in call_source
    assert "Telegram" in call_source
    assert "async def archive_completed_file" in archive_source
    assert "send_document" in archive_source
    assert "MONGO" not in archive_source


def test_song_cache_gridfs_api_and_restore_filename_are_correct():
    source = (ROOT / "utils/song_cache.py").read_text(encoding="utf-8")
    # Motor returns an AsyncIOMotorGridIn; write/close must be awaited.
    assert "upload = bucket.open_upload_stream(" in source
    assert "await bucket.open_upload_stream(" not in source
    assert "await upload.write(chunk)" in source
    assert "await upload.close()" in source
    # Restored files must match ytdl's completed-file glob on the next request.
    assert 'target = f"/tmp/melody_{video_id}_{tag}.cache"' in source


def test_restore_song_telegram_media_uses_atomic_discoverable_target(monkeypatch):
    import asyncio
    import importlib.util
    import os
    import sys
    import types

    class FakeCollection:
        async def find_one(self, *args, **kwargs):
            return {"file_id": "cached-file-id"}

    class FakeDb:
        def __getitem__(self, _name):
            return FakeCollection()

    fake_database = types.ModuleType("utils.database")
    fake_database.db = FakeDb()
    monkeypatch.setitem(sys.modules, "utils.database", fake_database)
    spec = importlib.util.spec_from_file_location("song_cache_isolated", ROOT / "utils/song_cache.py")
    song_cache = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(song_cache)

    class FakeClient:
        async def download_media(self, file_id, file_name):
            assert file_id == "cached-file-id"
            with open(file_name, "wb") as handle:
                handle.write(b"complete-audio")
            return file_name

    monkeypatch.setattr(song_cache, "song_cache_col", FakeCollection())
    video_id = "restore_test_az"
    target = f"/tmp/melody_{video_id}_a.cache"
    try:
        restored = asyncio.run(song_cache.restore_song(FakeClient(), video_id, False))
        assert restored == target
        assert os.path.isfile(restored)
        assert open(restored, "rb").read() == b"complete-audio"
    finally:
        for path in (target, f"{target}.restore.part"):
            try:
                os.unlink(path)
            except OSError:
                pass


def test_dependency_bounds_target_audited_current_releases():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for expected in (
        "kurigram>=2.2.25,<2.3",
        "pytgcrypto>=1.2.12,<2",
        "py-tgcalls>=2.3.3,<2.4",
        "yt-dlp>=2026.8.19,<2027.0.0",
        "curl_cffi>=0.16.2,<0.17",
        "Pillow>=12.3.0,<13",
        "aiofiles>=25.1.0,<26",
        "motor>=3.7.1,<4",
        "pymongo>=4.17,<5",
        "httpx>=0.28.1,<1",
    ):
        assert expected in requirements



def test_runtime_only_constructs_mongo_client_from_live_uri():
    source = (ROOT / "utils/database.py").read_text(encoding="utf-8")
    assert 'Config.MONGO_DB_URI' in source
    assert "AsyncIOMotorClient" in source


def test_mongo_runtime_logs_only_credential_free_target():
    source = (ROOT / "utils/database.py").read_text(encoding="utf-8")
    assert "def _mongo_target(uri: str)" in source
    assert "urlsplit(uri).hostname" in source
    assert '"Mongo runtime target: %s (database=%s)"' in source


def test_playback_state_repairs_duplicate_chat_snapshots_before_unique_index():
    source = (ROOT / "utils/playback_state.py").read_text(encoding="utf-8")
    assert "async def _deduplicate_chat_snapshots()" in source
    assert 'await state_col.delete_many({"_id": {"$in": duplicate_ids}})' in source
    assert 'await state_col.drop_index("chat_id_1")' in source
    assert "await state_col.create_index(\"chat_id\", unique=True)" in source


def test_early_handoff_uses_staging_file_size_when_progress_bytes_are_missing():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "Native fragment downloads do not consistently populate" in source
    assert "downloaded = max(downloaded, os.path.getsize(fp))" in source
    assert "if not audio_only or not _early_audio_path_is_safe(fp or \"\")" in source


def test_direct_stream_retains_recovery_backup_after_success():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "recovery backup retained after direct stream" in source
    assert "completed local backup" in source
    assert "stale or half-written fallback" in source


def test_direct_stream_supports_more_clients_and_higher_quality_audio():
    ytdl = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert '"android_music", "android"' in ytdl
    assert '"web_safari", "mweb"' in ytdl
    assert "YT_AUDIO_MAX_ABR', 160" in ytdl
    assert 'bestaudio[ext=webm][abr<=192]' in ytdl
    assert 'float(os.getenv("YT_AUDIO_MAX_ABR", "160"))' in ytdl


def test_new_tracks_reset_stale_seek_offset_but_recovery_can_seek():
    call = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "A fresh manual/queued track must never inherit a previous track's seek" in call
    assert "_seek_offset[chat_id] = 0" in call
    assert "Recovery is the only path allowed to pass a nonzero start_at" in call


def test_direct_resolver_has_last_resort_invidious_video_formats():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "def _invidious_streams_sync(video_id: str, want_video: bool)" in source
    assert 'f"{instance}/api/v1/videos/{video_id}"' in source
    assert 'data.get("adaptiveFormats")' in source
    assert "_invidious_streams_sync, vid_only, want_video" in source


def test_download_retry_rungs_align_client_user_agent_and_cookie_policy():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert 'youtube["player_client"] = list(clients)' in source
    assert 'headers["User-Agent"] = ua' in source
    assert 'out.pop("cookiefile", None)' in source
    assert '"android_music", "android", "android_vr"' in source


def test_direct_failure_cache_allows_immediate_warm_playback_retry():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "failure_age = _STREAM_URL_FAILURE_TTL - (failure_until - now)" in source
    assert "failure_age >= 0.75" in source
    assert "background warm resolve" in source


def test_zero_second_direct_stream_end_recovers_from_local_fallback_first():
    call = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "Recover that case FIRST" in call
    assert "prefer_local=True" in call
    assert "cached_file_path(track.video_id, audio_only=not video) if prefer_local else None" in call


def test_zero_second_recovery_disables_growing_file_handoff():
    call = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    recovery_start = call.index("async def seek_stream(")
    recovery_end = call.index("\nasync def change_volume", recovery_start)
    recovery = call[recovery_start:recovery_end]
    assert "prefer_local=True" in call
    assert "allow_early=False" in recovery
    assert "direct CDN stream that ended at 0s" in recovery


def test_stream_end_recovery_runs_before_stale_event_filter():
    call = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    recovery = call.index("if await _resume_if_premature_end(chat_id):")
    stale_filter = call.index("if _is_probably_stale_stream_end(chat_id):")
    assert recovery < stale_filter
    assert "Recover that case FIRST" in call[recovery - 300:recovery + 200]


def test_remote_probe_preserves_stream_identity_headers():
    patch = (ROOT / "utils/pytgcalls_patch.py").read_text(encoding="utf-8")
    assert 'supplied.get("Referer")' in patch
    assert 'supplied_agent' in patch
    assert '"Range": "bytes=0-1"' in patch
    assert 'impersonate="chrome124"' in patch


def test_direct_stream_gets_exclusive_window_before_retained_fallback_backup():
    call = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert 'os.getenv("DOWNLOAD_START_DELAY", "0.0")' in call
    assert "fallback" in call
    assert "recovery backup retained" in call


def test_direct_stream_failure_recovery_starts_clean_completed_download():
    call = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "recovery" in call
    assert "allow_early=False" in call
    assert "stale or half-written fallback" in call


def test_cloud_runtime_does_not_override_direct_stream_grace_delay():
    call = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert 'os.getenv("DOWNLOAD_START_DELAY", "0.0")' in call
    assert "_DOWNLOAD_START_DELAY = max(0.0, configured_download_delay)" in call
    assert "cloud-only grace period" in call


def test_cloud_direct_first_logic_enforces_minimum_delay_over_stale_config():
    call = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "configured_download_delay" in call
    assert "_DOWNLOAD_START_DELAY = max(0.0, configured_download_delay)" in call
    assert "parallel" in call


def test_play_has_single_direct_stream_owner_for_current_track():
    play = (ROOT / "melody/plugins/music/play.py").read_text(encoding="utf-8")
    assert "never start a second direct resolver" in play
    assert "_stream_track()` is the single owner" in play
    assert "direct-warm-" not in play
    assert "warm_task = asyncio.create_task(_warm_sources" not in play


def test_direct_audio_accepts_metadata_light_http_formats():
    ytdl = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "metadata_light = [" in ytdl
    assert "accepted metadata-light direct audio URL" in ytdl
    # The last resort must never hand a VIDEO-ONLY itag to the audio path:
    # ffprobe then raises NoAudioSourceFound and the song pays a full
    # download fallback (see _maybe_has_audio()).
    assert "_maybe_has_audio(f)" in ytdl
    assert "def _maybe_has_audio(" in ytdl


def test_no_audio_cdn_url_is_blacklisted_instead_of_being_re_picked():
    ytdl = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    call = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert "def note_no_audio_url(" in ytdl
    assert "_NO_AUDIO_URLS" in ytdl
    assert "_url_has_no_audio(f[\"url\"])" in ytdl
    assert "no audio source" in call
    assert "note_no_audio_url" in call
    assert "_melody_audio_url" in call
    # Mirror-proof blacklist + alternate-source retry before any re-resolve.
    assert "gv:" in ytdl
    assert "def _audio_stream_candidates(" in ytdl
    assert "_melody_audio_candidates" in call
    assert "alternate direct source recovered" in call


def test_top_level_hls_url_is_preserved_for_direct_playback():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    assert "is_hls = \"m3u8\" in proto" in source
    assert "top_url.split(\"?\", 1)[0].endswith(\".m3u8\")" in source
    assert "no directly streamable HTTP/HLS format found" in source


def test_direct_resolution_has_realistic_budget_before_full_download_fallback():
    source = (ROOT / "melody/core/ytdl.py").read_text(encoding="utf-8")
    call = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert '_RESOLVE_TIMEOUT = float(os.getenv("RESOLVE_TIMEOUT", "4.0"))' in source
    assert 'configured_download_delay = float(os.getenv("DOWNLOAD_START_DELAY", "0.0"))' in call


def test_direct_stream_fallback_logs_empty_timeout_exceptions_with_type():
    source = (ROOT / "melody/core/call.py").read_text(encoding="utf-8")
    assert '(%s: %r) — falling back to download' in source
    assert 'type(exc).__name__' in source
