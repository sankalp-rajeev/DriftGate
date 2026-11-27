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

# Restore the baseline for this instance type, never the candidate's reference.
# No bucket means a local run: the caller is responsible for its local reference.
ensure_reference() {
  local bucket="${DRIFTGATE_BUCKET:-${DRIFTGATE_BUNDLE_BUCKET:-}}"
  local fields ref_key ref_dir
  [[ -n "$bucket" ]] || return 0
  fields="$("$PY" -m gate reference --print-key | "$PY" -c \
    'import json, shlex, sys; d = json.load(sys.stdin); print("ref_key=%s ref_dir=%s" % (shlex.quote(d["key"]), shlex.quote(d["local_dir"])))')" || return 1
  eval "$fields"
  if [[ -f "$ref_dir/measurement.json" && -f "$ref_dir/d2_base.kld" ]]; then
    if [[ "${1:-}" == d7 && ! -f "$ref_dir/d7_embeddings.json" ]]; then
      aws s3 cp --only-show-errors --recursive --exclude '*' --include d7_embeddings.json \
        "s3://$bucket/$ref_key" "$ref_dir" || return 1
    fi
    return 0
  fi
  mkdir -p "$ref_dir" || return 1
  aws s3 cp --only-show-errors --recursive "s3://$bucket/$ref_key" "$ref_dir" || return 1
  if [[ ! -f "$ref_dir/measurement.json" || ! -f "$ref_dir/d2_base.kld" ]]; then
    echo "Incomplete baseline reference at $ref_dir (measurement.json and d2_base.kld required)" >&2
    return 1
  fi
  echo "Baseline reference fetched from $ref_key to $ref_dir" >&2
}

# llama.cpp prints an abbreviated Git commit in --version.
llama_cpp_matches() {
  local bin_dir="$1" commit="$2" target version built
  shift 2
  for target in "$@"; do
    [[ -x "$bin_dir/$target" ]] || return 1
    version="$(LD_LIBRARY_PATH="$bin_dir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
      "$bin_dir/$target" --version 2>&1)" || return 1
    built="$(sed -n 's/.*commit \([0-9a-f]\+\).*/\1/p' <<<"$version")"
    [[ ${#built} -ge 7 && "$commit" == "$built"* ]] || return 1
  done
}

# Staging is on the same filesystem as the destination. Keep the old directory until the swap.
install_cached_llama_cpp() {
  local work="$1" previous="$1/previous-bin"
  if [[ -e "$LLAMA_CPP_BIN" || -L "$LLAMA_CPP_BIN" ]]; then
    mv -T -- "$LLAMA_CPP_BIN" "$previous" || return 1
  fi
  if mv -T -- "$work/build/bin" "$LLAMA_CPP_BIN"; then
    return 0
  fi
  if [[ -e "$previous" || -L "$previous" ]]; then
    mv -T -- "$previous" "$LLAMA_CPP_BIN" || return 1
  fi
  return 1
}

# Use the Lab's shared CPU build when available. Always build both tools on a miss.
ensure_llama_cpp() (
  local lock_commit repo cache_bucket cache_key work archive
  local -a targets=(llama-server llama-perplexity) cmake_options=()
  lock_commit="$("$PY" -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["commit"])' serving/llama_cpp.lock)"
  if llama_cpp_matches "$LLAMA_CPP_BIN" "$lock_commit" "$@"; then
    return 0
  fi

  cache_bucket="${DRIFTGATE_BUCKET:-${DRIFTGATE_BUNDLE_BUCKET:-}}"
  if [[ -n "$cache_bucket" ]]; then
    cache_key="bin/llama.cpp/$lock_commit/al2023-x86_64-avx512.tar.gz"
    mkdir -p "$(dirname "$LLAMA_CPP_BIN")"
    work="$(mktemp -d "$(dirname "$LLAMA_CPP_BIN")/.llama-cache.XXXXXX")"
    trap 'rm -rf -- "$work"' EXIT
    archive="$work/al2023-x86_64-avx512.tar.gz"
    if aws s3 cp --only-show-errors "s3://$cache_bucket/$cache_key" "$archive" &&
      "$PY" -c 'import sys, tarfile
with tarfile.open(sys.argv[1], "r:gz") as archive:
    archive.extractall(sys.argv[2], filter="data")' "$archive" "$work" &&
      llama_cpp_matches "$work/build/bin" "$lock_commit" "$@" &&
      install_cached_llama_cpp "$work"; then
      echo "Loaded llama.cpp from the S3 cache: $cache_key" >&2
      return 0
    fi
    echo "No usable llama.cpp cache at $cache_key; building locally" >&2
    # t3 Skylake-SP and Cascade Lake share base AVX-512, but not VNNI or later extensions.
    # Keep the cache key in sync with this instruction set. Shared libs must relocate.
    cmake_options=(
      -DGGML_NATIVE=OFF -DGGML_AVX512=ON
      -DGGML_AVX512_VBMI=OFF -DGGML_AVX512_VNNI=OFF -DGGML_AVX512_BF16=OFF
      -DCMAKE_BUILD_RPATH_USE_ORIGIN=ON
    )
  fi

  echo "Building llama.cpp $lock_commit (${targets[*]})" >&2
  repo="$("$PY" -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["repo"])' serving/llama_cpp.lock)"
  # A previous cache hit can leave build/bin in place without a Git checkout.
  if [[ ! -d "$DRIFTGATE_LLAMA_CPP_DIR/.git" ]]; then
    mkdir -p "$DRIFTGATE_LLAMA_CPP_DIR"
    git -C "$DRIFTGATE_LLAMA_CPP_DIR" init --quiet
  fi
  if ! git -C "$DRIFTGATE_LLAMA_CPP_DIR" remote get-url origin >/dev/null 2>&1; then
    git -C "$DRIFTGATE_LLAMA_CPP_DIR" remote add origin "$repo"
  fi
  git -C "$DRIFTGATE_LLAMA_CPP_DIR" fetch --quiet origin
  git -C "$DRIFTGATE_LLAMA_CPP_DIR" checkout --quiet --detach "$lock_commit"
  cmake -S "$DRIFTGATE_LLAMA_CPP_DIR" -B "$DRIFTGATE_LLAMA_CPP_DIR/build" \
    -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF "${cmake_options[@]}"
  cmake --build "$DRIFTGATE_LLAMA_CPP_DIR/build" --clean-first -j "$(nproc)" --target "${targets[@]}"
  if ! llama_cpp_matches "$LLAMA_CPP_BIN" "$lock_commit" "${targets[@]}"; then
    echo "Built llama.cpp binaries do not match $lock_commit" >&2
    return 1
  fi

  if [[ -n "$cache_bucket" ]]; then
    if ! tar -czf "$archive" -C "$DRIFTGATE_LLAMA_CPP_DIR" build/bin ||
      ! aws s3 cp --only-show-errors --sse AES256 "$archive" "s3://$cache_bucket/$cache_key"; then
      echo "Could not upload the llama.cpp cache: $cache_key" >&2
    fi
  fi
)
