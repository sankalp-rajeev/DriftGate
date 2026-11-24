# Shared by the build host and CodeDeploy hooks.

# Export KEY=VALUE lines from a file, keeping any variable that is already set.
load_env() {
  local line key
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in '' | \#*) continue ;; esac
    key="${line%%=*}"
    if [ -z "${!key+x}" ]; then export "$key=${line#*=}" || return; fi
  done <"$1"
}

# Build the requested CPU binaries only if one is missing or at a different commit.
ensure_llama_cpp() {
  local lock_commit built repo target
  lock_commit="$("$PY" -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["commit"])' serving/llama_cpp.lock)"
  for target in "$@"; do
    built="$("$LLAMA_CPP_BIN/$target" --version 2>&1 | sed -n 's/.*commit \([0-9a-f]*\).*/\1/p' | head -n1 || true)"
    [[ -n "$built" && "$lock_commit" == "$built"* ]] || break
  done
  if [[ -n "$built" && "$lock_commit" == "$built"* ]]; then
    return 0
  fi

  echo "Building llama.cpp $lock_commit ($*)" >&2
  repo="$("$PY" -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["repo"])' serving/llama_cpp.lock)"
  [ -d "$DRIFTGATE_LLAMA_CPP_DIR/.git" ] || git clone --quiet "$repo" "$DRIFTGATE_LLAMA_CPP_DIR"
  git -C "$DRIFTGATE_LLAMA_CPP_DIR" fetch --quiet origin
  git -C "$DRIFTGATE_LLAMA_CPP_DIR" checkout --quiet --detach "$lock_commit"
  cmake -S "$DRIFTGATE_LLAMA_CPP_DIR" -B "$DRIFTGATE_LLAMA_CPP_DIR/build" -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF
  cmake --build "$DRIFTGATE_LLAMA_CPP_DIR/build" -j "$(nproc)" --target "$@"
}
