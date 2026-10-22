#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
for file in runtime/llama.cpp/build/bin/llama-server runtime/whisper.cpp/build/bin/whisper-cli models/Qwen3-1.7B-Q4_K_M.gguf models/ggml-base.bin .venv/bin/python; do
    [ -f "$file" ] || { echo "Missing $file; run bash scripts/setup.sh" >&2; exit 1; }
done
export OMP_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=2
export QWEN_URL=http://127.0.0.1:8081
qwen_pid=''
api_pid=''
cleanup() {
    trap - EXIT INT TERM
    if [ -n "$api_pid" ]; then kill "$api_pid" 2>/dev/null || true; fi
    if [ -n "$qwen_pid" ]; then kill "$qwen_pid" 2>/dev/null || true; fi
    wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
runtime/llama.cpp/build/bin/llama-server \
    -m models/Qwen3-1.7B-Q4_K_M.gguf --alias qwen \
    --host 127.0.0.1 --port 8081 -c 2048 -t 3 -tb 3 -np 1 \
    --jinja --chat-template-kwargs '{"enable_thinking":false}' \
    > runtime/qwen.log 2>&1 &
qwen_pid=$!
ready=0
for ((attempt=0; attempt<180; attempt++)); do
    kill -0 "$qwen_pid" 2>/dev/null || { tail -n 30 runtime/qwen.log; exit 1; }
    if curl --noproxy '*' -fsS --max-time 2 http://127.0.0.1:8081/health >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
[ "$ready" = 1 ] || { echo 'Qwen startup timed out; see runtime/qwen.log' >&2; exit 1; }
.venv/bin/python -m uvicorn app:app --host "${HOST:-127.0.0.1}" --port 8000 --workers 1 &
api_pid=$!
wait -n "$qwen_pid" "$api_pid"
