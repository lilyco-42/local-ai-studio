#!/usr/bin/env bash
# Read-only A7A inference smoke test; does not alter configuration.
set -Eeuo pipefail
errors=/sys/fs/ext4/mmcblk1p3/errors_count
test -r "$errors"
before="$(cat "$errors")"
test "$before" = 0 || { echo "ext4 error count is $before" >&2; exit 2; }
systemctl --user is-active --quiet a7a-llama.service
curl -fsS --max-time 4 http://127.0.0.1:8081/health
echo

python3 - "$HOME/a7a-ai/run/api.key" <<'PY'
import json
import pathlib
import sys
import urllib.request

token = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").strip()
payload = {
    "model": "qwen2.5-0.5b-instruct-q2_k.gguf",
    "messages": [{"role": "user", "content": "Reply with one short greeting."}],
    "max_tokens": 32,
    "stream": False,
    "temperature": 0,
}
body = json.dumps(payload).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8081/v1/chat/completions",
    body,
    headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    },
)
with urllib.request.urlopen(req, timeout=45) as res:
    assert res.status == 200, res.status
    result = json.load(res)
answer = result["choices"][0]["message"]["content"]
assert answer.strip(), "model returned empty content"
print("OpenAI API: PASS")
print("Answer:", answer[:160])
print("Usage:", result.get("usage"))
print("Timing:", result.get("timings"))
PY

after="$(cat "$errors")"
test "$after" = "$before" || { echo "ext4 error count changed ($before -> $after)" >&2; exit 3; }
echo "SD ext4 error count unchanged: $after"
