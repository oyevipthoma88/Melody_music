# Runs at dyno boot AFTER the apt buildpack's 000_apt.sh (alphabetical order).
#
# ROOT CAUSE this fixes:
#   ffmpeg: error while loading shared libraries: libpulsecommon-17.0.so
# Debian's ffmpeg links libpulse, whose private libs live in the
# .apt/usr/lib/x86_64-linux-gnu/pulseaudio/ SUBDIRECTORY that the apt
# buildpack does not put on LD_LIBRARY_PATH. Without this, every ffmpeg and
# ffprobe call died instantly and no song ever played in the voice chat.
for _d in "$HOME"/.apt/usr/lib/x86_64-linux-gnu/pulseaudio \
          "$HOME"/.apt/usr/lib/x86_64-linux-gnu \
          "$HOME"/.apt/usr/lib; do
  [ -d "$_d" ] && export LD_LIBRARY_PATH="$_d:${LD_LIBRARY_PATH}"
done
unset _d

# Prefer the fully static ffmpeg vendored by bin/post_compile (no libpulse
# dependency at all) whenever it is present.
if [ -x "$HOME/vendor/ffmpeg/bin/ffmpeg" ]; then
  export PATH="$HOME/vendor/ffmpeg/bin:$PATH"
fi
