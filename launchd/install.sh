#!/bin/zsh
# Installs both launchd jobs for the current user. Re-run after editing the plists.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/Library/LaunchAgents
for job in com.bami.repurposer.worker com.bami.repurposer.web com.bami.repurposer.digest; do
  launchctl bootout "gui/$(id -u)/$job" 2>/dev/null || true
  cp "$DIR/$job.plist" ~/Library/LaunchAgents/
  launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/$job.plist
  echo "loaded $job"
done
launchctl list | grep com.bami.repurposer || true
