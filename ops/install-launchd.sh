#!/bin/sh
# install-launchd.sh: run `tl serve` as a per-user launchd agent that restarts if it dies.
#
#   ops/install-launchd.sh --print     show the plist and the launchctl commands; change nothing
#   ops/install-launchd.sh --install   write the plist to ~/Library/LaunchAgents and load it
#
# launchd's PATH lacks ~/.local/bin, where `uv tool install` puts `tl`, so the plist
# names `tl` by its absolute path. Run this after `uv tool install --force .` in the
# repo-prod checkout: whatever `tl` resolves to is what the service runs. A `tl` whose
# real path is in a .venv or venv folder or in a git checkout (what `uv run` puts first
# on PATH) is refused; `uv tool`'s own environment lives elsewhere and is accepted.
#
# TL_HOME is passed through when it is set; otherwise the service uses its default home.
# The server writes its own log, $TL_HOME/logs/server.log, rotated at 5 MB with 3 kept.
# launchd's own stdout and stderr go to logs/launchd.log, which holds what is written
# before the server takes over its log (a crash at import, say), and one line for each
# write server.log could not take later (the text itself is lost).
set -eu

LABEL=local.transcript-labeling.server
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

usage() { echo "usage: $0 --print | --install" >&2; exit 2; }
[ $# -eq 1 ] || usage
case "$1" in --print|--install) mode=$1 ;; *) usage ;; esac

tl_bin=$(command -v tl || true)
[ -n "$tl_bin" ] || { echo "REFUSING: tl is not on PATH; run \`uv tool install --force .\` in repo-prod first" >&2; exit 1; }
case "$tl_bin" in /*) ;; *) echo "REFUSING: tl is $tl_bin, not a file (a shell function or alias?)" >&2; exit 1 ;; esac
tl_real=$(/bin/realpath "$tl_bin")
case "$tl_real" in */.venv/*|*/venv/*) echo "REFUSING: $tl_bin is $tl_real, inside a virtual environment; install with \`uv tool install\` and run this outside \`uv run\`" >&2; exit 1 ;; esac
if /usr/bin/git -C "$(dirname "$tl_real")" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "REFUSING: $tl_bin is $tl_real, inside a git checkout; the service must run the installed tl" >&2; exit 1
fi
[ -x /usr/bin/git ] || { echo "REFUSING: /usr/bin/git is missing; the service commits with it" >&2; exit 1; }

home=${TL_HOME:-$HOME/.local/share/transcript-labeling}
case "$home" in /*) ;; *) echo "REFUSING: TL_HOME must be an absolute path, got $home" >&2; exit 1 ;; esac
[ -f "$home/config.json" ] || { echo "REFUSING: $home has no config.json; run \`tl init\` first" >&2; exit 1; }

xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

env_block=""
if [ -n "${TL_HOME:-}" ]; then
  env_block="    <key>TL_HOME</key><string>$(xml "$TL_HOME")</string>
"
fi

plist=$(cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml "$tl_bin")</string>
    <string>serve</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
$env_block    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$(xml "$home/logs/launchd.log")</string>
  <key>StandardErrorPath</key><string>$(xml "$home/logs/launchd.log")</string>
</dict>
</plist>
EOF
)

uid=$(id -u)
if [ "$mode" = --print ]; then
  printf '%s\n' "$plist"
  echo "# --install would write $PLIST, then run:" >&2
  echo "#   launchctl bootout gui/$uid/$LABEL   (only if already loaded)" >&2
  echo "#   launchctl bootstrap gui/$uid $PLIST" >&2
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$home/logs"
trap 'rm -f "$PLIST.tmp"' EXIT
printf '%s\n' "$plist" > "$PLIST.tmp"
plutil -lint "$PLIST.tmp" >/dev/null
mv "$PLIST.tmp" "$PLIST"
if launchctl print "gui/$uid/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$uid/$LABEL"
fi
# A job that was just booted out can take a moment to unload; bootstrap fails until it has.
tries=0
until launchctl bootstrap "gui/$uid" "$PLIST"; do
  tries=$((tries + 1))
  [ $tries -lt 5 ] || { echo "REFUSING: launchctl bootstrap failed 5 times; see launchctl print gui/$uid/$LABEL" >&2; exit 1; }
  sleep 2
done
echo "loaded $LABEL from $PLIST; check it with: tl doctor"
