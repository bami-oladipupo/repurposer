#!/bin/zsh
for job in com.bami.repurposer.worker com.bami.repurposer.web com.bami.repurposer.digest; do
  launchctl bootout "gui/$(id -u)/$job" 2>/dev/null && echo "unloaded $job" || echo "$job was not loaded"
  rm -f ~/Library/LaunchAgents/$job.plist
done
