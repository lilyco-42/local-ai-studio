#!/usr/bin/env bash
# Safe, unprivileged, pinned A7A installer. No kernel changes or on-device compilation.
set -Eeuo pipefail

if [ "$(id -un)" != "radxa" ] || [ "$(uname -m)" != "aarch64" ]; then
  echo "Run as user radxa on the ARM64 A7A." >&2
  exit 2
fi

errors=/sys/fs/ext4/mmcblk1p3/errors_count
if [ ! -r "$errors" ] || [ "$(cat "$errors")" != 0 ]; then
  echo "Refusing SD writes: expected clean A7A ext4 root (error count 0)." >&2
  exit 3
fi

root="$HOME/a7a-ai"
here="$(cd -- "$(dirname -- "$0")" && pwd)"
mkdir -p "$root/cache" "$root/models" "$root/run" "$HOME/.config/systemd/user"

download_verified() {
  local url="$1" final="$2" sha="$3" partial="$2.part"
  if [ -f "$final" ]; then
    if printf '%s  %s\n' "$sha" "$final" | sha256sum -c -; then return 0; fi
    echo "Existing file has a mismatched SHA256; refusing overwrite: $final" >&2
    return 1
  fi
  if [ -f "$partial" ] && printf '%s  %s\n' "$sha" "$partial" | sha256sum -c - >/dev/null 2>&1; then
    mv -- "$partial" "$final"
    return 0
  fi
  curl -fsSL --retry 2 --retry-delay 3 --connect-timeout 20 --max-time 1200 \
       --limit-rate 4M -C - -o "$partial" "$url"
  printf '%s  %s\n' "$sha" "$partial" | sha256sum -c -
  mv -- "$partial" "$final"
}

archive="$root/cache/llama-b11804-bin-ubuntu-arm64.tar.gz"
# Use the already-staged package from the Windows admin host if present.
if [ ! -e "$archive" ] && [ -f "$HOME/llama-b11804-bin-ubuntu-arm64.tar.gz" ]; then
  cp "$HOME/llama-b11804-bin-ubuntu-arm64.tar.gz" "$archive"
fi
download_verified \
  "https://github.com/ggml-org/llama.cpp/releases/download/b11804/llama-b11804-bin-ubuntu-arm64.tar.gz" \
  "$archive" \
  "2c2c2daf42db57fceca356e3440213f350dd800f74101d78c1dde04a14dca124"

if [ ! -x "$root/llama-b11804/llama-server" ]; then
  tar -xzf "$archive" -C "$root"
fi
"$root/llama-b11804/llama-server" --version

download_verified \
  "https://hf-mirror.com/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q2_k.gguf" \
  "$root/models/qwen2.5-0.5b-instruct-q2_k.gguf" \
  "9ee36184e616dfc76df4f5dd66f908dbde6979524ae36e6cefb67f532f798cb8"

key="$root/run/api.key"
if [ ! -s "$key" ]; then (umask 077; openssl rand -hex 32 > "$key"); fi
chmod 600 "$key"

unit="$HOME/.config/systemd/user/a7a-llama.service"
if [ -f "$unit" ] && ! cmp -s "$here/a7a-llama.service" "$unit"; then
  echo "Existing unit differs; refusing to overwrite $unit" >&2
  exit 4
fi
if [ ! -f "$unit" ]; then install -m 644 "$here/a7a-llama.service" "$unit"; fi

if [ "$(cat "$errors")" != 0 ]; then
  echo "ext4 error appeared during staging; stopping before enabling service." >&2
  exit 5
fi
systemctl --user daemon-reload
systemctl --user enable --now a7a-llama.service
ready=0
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:8081/health; then ready=1; break; fi
  sleep 1
done
echo
[ "$ready" -eq 1 ] || { echo "API did not become ready" >&2; exit 6; }
echo "A7A engine ready on 127.0.0.1:8081; ext4 errors: $(cat "$errors")"
if ! loginctl show-user radxa -p Linger | grep -qx 'Linger=yes'; then
  echo "For boot persistence, run once: sudo loginctl enable-linger radxa"
fi
