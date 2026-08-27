# Verified downloads: every artifact is fetched to a temp file, size-capped,
# checked against the sha256 in app/bin/_artifacts.json, then moved into
# place atomically. Any mismatch removes the file and fails the install.
# shellcheck shell=bash

artifact_field() {  # <section> <key> <field>
  python3 - "$MANIFEST" "$1" "$2" "$3" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))[sys.argv[2]][sys.argv[3]][sys.argv[4]])
PY
}

fetch_verified() {  # <url> <dest> <sha256> <size_bytes>
  local url=$1 dest=$2 sha=$3 size=$4 tmp
  if [[ -f $dest ]] && echo "$sha  $dest" | sha256sum -c --quiet 2>/dev/null; then
    return 0
  fi
  tmp=$(mktemp "$dest.XXXXXX.part")
  # ceiling: expected size + 1 MiB slack. Abort if the transfer stalls
  # (<1 KiB/s for 60 s) rather than hanging forever on a dead connection.
  if ! curl -fSL --progress-bar --connect-timeout 30 --speed-limit 1024 --speed-time 60 \
       --max-filesize $((size + 1048576)) -o "$tmp" "$url"; then
    rm -f "$tmp"; echo "download failed: $url" >&2; return 1
  fi
  if ! echo "$sha  $tmp" | sha256sum -c --quiet; then
    rm -f "$tmp"; echo "sha256 mismatch, refusing to install: $url" >&2; return 1
  fi
  mv -f "$tmp" "$dest"
}
