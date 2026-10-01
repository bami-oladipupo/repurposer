#!/bin/zsh
# Snapshots what git ignores but a new Mac needs (database, notes, Claude memory) into the private
# repurposer-private repo and pushes it. Secrets (.env, tokens/) are never included.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${REPURPOSER_STATE_DIR:-$HOME/repurposer-private}"
MEMORY="$HOME/.claude/projects/-Users-bami-repurpose-content/memory"

if [ ! -d "$DEST/.git" ]; then
  gh repo clone bami-oladipupo/repurposer-private "$DEST"
fi
mkdir -p "$DEST/data" "$DEST/claude"

# .backup takes a consistent copy even while the worker or web app has the database open.
rm -f "$DEST/data/repurposer.db"
sqlite3 "$ROOT/data/repurposer.db" ".backup '$DEST/data/repurposer.db'"
rsync -a --delete --exclude .DS_Store "$ROOT/notes/" "$DEST/notes/"
if [ -d "$MEMORY" ]; then
  rsync -a --delete "$MEMORY/" "$DEST/claude/memory/"
fi
if [ -f "$ROOT/.claude/launch.json" ]; then
  cp "$ROOT/.claude/launch.json" "$DEST/claude/launch.json"
fi
if [ -f "$ROOT/overrides.yaml" ]; then
  cp "$ROOT/overrides.yaml" "$DEST/overrides.yaml"
fi

cd "$DEST"
git add -A
if git diff --cached --quiet; then
  echo "state unchanged, nothing to push"
  exit 0
fi
git commit -qm "State snapshot $(date '+%Y-%m-%d %H:%M')"
git push -q origin HEAD
echo "pushed state snapshot to bami-oladipupo/repurposer-private"
