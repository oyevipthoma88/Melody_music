"""ffmpeg/ffprobe runtime guard.

ROOT CAUSE THIS FILE FIXES
══════════════════════════
Symptom in production logs (worker.1):

    ffmpeg: error while loading shared libraries: libpulsecommon-17.0.so:
            cannot open shared object file: No such file or directory
    ffprobe check_stream failed (JSONDecodeError: Expecting value: line 1 ...)
    giving up resuming <video_id> in <chat> after 15 premature ends

The previous commit replaced the (dead) static-ffmpeg buildpack with
`heroku-community/apt` + an `Aptfile` containing `ffmpeg`. The apt buildpack
does install ffmpeg, but Debian's ffmpeg links against libpulse, and
`libpulsecommon-<ver>.so` does NOT live in `.apt/usr/lib/x86_64-linux-gnu/` —
it lives in the `pulseaudio/` SUBDIRECTORY, which the buildpack never adds to
`LD_LIBRARY_PATH`. So every single `ffmpeg`/`ffprobe` invocation died instantly
with an empty stdout:

  * ffprobe -> empty stdout -> json.loads("") -> JSONDecodeError -> probe
    "unavailable" (harmless, playback is supposed to continue), then
  * ffmpeg -> same loader failure -> the stream ends 0 ms after it starts ->
    py-tgcalls fires stream_end -> 15 "premature ends" -> track skipped.

Net effect for the user: the bot came online and still reacted to messages,
but NOTHING ever played in the voice chat.

THE FIX (layered, cheapest first)
═════════════════════════════════
1. Repair `LD_LIBRARY_PATH` so the apt-installed ffmpeg can find the pulseaudio
   private libs (covers the exact failure above with zero downloads).
2. If ffmpeg still cannot execute, use the fully static build that
   `bin/post_compile` vendors into `vendor/ffmpeg/bin` (static builds have no
   libpulse dependency at all).
3. If even that is missing (build step failed / bare host), download the static
   build once at startup into a temp dir.

`ensure_ffmpeg_runtime()` is called at the very top of startup, and is safe to
call more than once.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from melody.logging import LOGGER

STATIC_URLS = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz",
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
)

_READY = False


def _prepend_path(env_var: str, value: str) -> None:
    current = os.environ.get(env_var, "")
    parts = [p for p in current.split(os.pathsep) if p]
    if value in parts:
        return
    os.environ[env_var] = os.pathsep.join([value, *parts]) if parts else value


def _binary_works(binary: str) -> bool:
    """True only when the binary can actually EXECUTE (not just exist).

    `shutil.which()` is not enough here: the broken apt ffmpeg was present and
    executable, it just aborted on a missing shared library.
    """
    path = shutil.which(binary)
    if not path:
        return False
    try:
        proc = subprocess.run(
            [path, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
    except Exception:
        return False
    out = (proc.stdout or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        LOGGER.warning("%s is present but unusable: %s", binary, out.strip()[:300])
        return False
    if "error while loading shared libraries" in out:
        LOGGER.warning("%s loader failure: %s", binary, out.strip()[:300])
        return False
    return True


def _both_work() -> bool:
    return _binary_works("ffmpeg") and _binary_works("ffprobe")


def _repair_library_path() -> None:
    """Add the apt slug's private lib dirs (incl. pulseaudio/) to the loader path."""
    roots = [
        os.path.join(os.environ.get("HOME", "/app"), ".apt"),
        "/app/.apt",
    ]
    added = []
    for root in roots:
        base = os.path.join(root, "usr", "lib")
        if not os.path.isdir(base):
            continue
        candidates = [base, os.path.join(base, "x86_64-linux-gnu")]
        # pulseaudio ships libpulsecommon-*.so in a private subdirectory.
        for parent in list(candidates):
            if not os.path.isdir(parent):
                continue
            for entry in sorted(os.listdir(parent)):
                sub = os.path.join(parent, entry)
                if os.path.isdir(sub) and entry in {"pulseaudio", "blas", "lapack"}:
                    candidates.append(sub)
        for candidate in candidates:
            if os.path.isdir(candidate):
                _prepend_path("LD_LIBRARY_PATH", candidate)
                added.append(candidate)
    if added:
        LOGGER.info("ffmpeg: LD_LIBRARY_PATH repaired with %s", ", ".join(added))


def _use_vendored() -> bool:
    for base in (
        os.path.join(os.environ.get("HOME", "/app"), "vendor", "ffmpeg", "bin"),
        "/app/vendor/ffmpeg/bin",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor", "ffmpeg", "bin"),
    ):
        if os.path.isfile(os.path.join(base, "ffmpeg")) and os.path.isfile(
            os.path.join(base, "ffprobe")
        ):
            _prepend_path("PATH", base)
            if _both_work():
                LOGGER.info("ffmpeg: using vendored static build at %s", base)
                return True
    return False


def _download_static() -> bool:
    target = os.path.join(tempfile.gettempdir(), "melody-ffmpeg")
    bin_dir = os.path.join(target, "bin")
    if os.path.isfile(os.path.join(bin_dir, "ffmpeg")):
        _prepend_path("PATH", bin_dir)
        if _both_work():
            return True
    os.makedirs(bin_dir, exist_ok=True)
    for url in STATIC_URLS:
        archive = os.path.join(target, "ffmpeg.tar.xz")
        try:
            LOGGER.info("ffmpeg: downloading static build from %s", url)
            with urllib.request.urlopen(url, timeout=180) as response, open(archive, "wb") as handle:
                shutil.copyfileobj(response, handle)
            # tarfile handles .xz through Python's lzma module, so this works
            # even on hosts without the `xz` CLI installed.
            with tarfile.open(archive, "r:xz") as tar:
                for member in tar.getmembers():
                    name = os.path.basename(member.name)
                    if name not in {"ffmpeg", "ffprobe"} or not member.isfile():
                        continue
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        continue
                    dest = os.path.join(bin_dir, name)
                    with open(dest, "wb") as handle:
                        shutil.copyfileobj(extracted, handle)
                    os.chmod(dest, 0o755)
        except Exception as exc:  # network/mirror hiccup — try the next mirror
            LOGGER.warning("ffmpeg: static download failed (%s): %s", url, exc)
            continue
        finally:
            if os.path.exists(archive):
                try:
                    os.remove(archive)
                except OSError:
                    pass
        _prepend_path("PATH", bin_dir)
        if _both_work():
            LOGGER.info("ffmpeg: static build ready at %s", bin_dir)
            return True
    return False


def ensure_ffmpeg_runtime() -> bool:
    """Guarantee a WORKING ffmpeg + ffprobe on PATH. Returns True on success."""
    global _READY
    if _READY:
        return True

    if _both_work():
        _READY = True
        LOGGER.info("✅ ffmpeg/ffprobe verified: %s", shutil.which("ffmpeg"))
        return True

    LOGGER.warning("ffmpeg/ffprobe not usable — repairing before playback starts")
    _repair_library_path()
    if _both_work():
        _READY = True
        LOGGER.info("✅ ffmpeg/ffprobe fixed via LD_LIBRARY_PATH repair")
        return True

    if _use_vendored() or _download_static():
        _READY = True
        return True

    LOGGER.error(
        "❌ No working ffmpeg/ffprobe available — voice-chat playback will fail. "
        "Check the apt buildpack (Aptfile) and bin/post_compile output."
    )
    return False


if __name__ == "__main__":  # manual check: python -m utils.ffmpeg_runtime
    sys.exit(0 if ensure_ffmpeg_runtime() else 1)
