#!/usr/bin/env bash
# Build a demo commit with a temporary index; never checkout, reset or force-push.
set -euo pipefail
fault="${1:?usage: scripts/demo_push.sh CLEAN|RESET|<fault>}"
repo="$(git rev-parse --show-toplevel)"
cd "$repo"
git fetch origin main >&2
base="$(git rev-parse FETCH_HEAD)"
git fetch codecommit main >&2
parent="$(git rev-parse FETCH_HEAD)"

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
export GIT_INDEX_FILE="$work/index"
git read-tree "$base"

case "$fault" in
  RESET) message="Reset to origin/main" ;;
  CLEAN)
    mkdir -p "$work/tree/docs"
    # Keep the base log if one is already on GitHub main.
    git show "$base:docs/demo-log.md" > "$work/tree/docs/demo-log.md" 2>/dev/null || true
    printf '%s CLEAN redeploy\n' "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" >> "$work/tree/docs/demo-log.md"
    blob="$(git hash-object -w "$work/tree/docs/demo-log.md")"
    git update-index --add --cacheinfo 100644 "$blob" docs/demo-log.md
    message="Clean redeploy"
    ;;
  *)
    mkdir -p "$work/tree"
    git archive "$base" | tar -x -C "$work/tree"
    (
      cd "$work/tree"
      unset PYTHONPATH
      uv run --no-sync --project "$repo" python -m faults.inject "$fault" --out "$work/bundles" >&2
    )
    bundle="$work/bundles/$fault"
    message="$(uv run --no-sync --project "$repo" python -c \
      'import json, sys; print(json.load(open(sys.argv[1]))["description"])' "$bundle/bundle.json")"
    while IFS= read -r -d '' file; do
      path="${file#"$bundle/"}"
      blob="$(git hash-object -w "$file")"
      git update-index --add --cacheinfo 100644 "$blob" "$path"
    done < <(find "$bundle/serving" -type f -print0)
    ;;
esac

tree="$(git write-tree)"
commit="$(git commit-tree "$tree" -p "$parent" -m "demo: $message")"
# The new commit has the fetched CodeCommit tip as its parent. Concurrent pushes fail safely.
git push codecommit "$commit:refs/heads/main" >&2
printf '%s\n' "$commit"
