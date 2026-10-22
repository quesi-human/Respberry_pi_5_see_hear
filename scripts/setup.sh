#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
for tool in python3 cmake g++ git curl ffmpeg arecord; do
    command -v "$tool" >/dev/null || { echo "Missing: $tool (see README.md)" >&2; exit 1; }
done
mkdir -p runtime models
python3 -m venv .venv
export PIP_NO_CACHE_DIR=1
export PIP_CONSTRAINT="$PWD/cpu-constraints.txt"
# Explicit CPU wheels first; constraints also apply to exporter subprocesses.
.venv/bin/python -m pip install 'torch==2.10.0+cpu' 'torchvision==0.25.0+cpu' --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python - <<'PY'
import importlib.metadata as metadata
import torch
assert torch.version.cuda is None, 'Expected CPU-only PyTorch'
unexpected = [d.metadata['Name'] for d in metadata.distributions()
              if (d.metadata['Name'].lower().startswith(('nvidia-', 'cuda-'))
                  or d.metadata['Name'].lower() == 'triton')
              and d.metadata['Name'].lower() != 'nvidia-ml-py']
if unexpected:
    raise SystemExit('Remove partial CUDA install by recreating .venv: ' + ', '.join(unexpected))
print('CPU-only PyTorch:', torch.__version__)
PY
if [ ! -d runtime/whisper.cpp ]; then
    git clone --depth 1 https://github.com/ggml-org/whisper.cpp runtime/whisper.cpp
fi
if [ ! -d runtime/llama.cpp ]; then
    git clone --depth 1 https://github.com/ggml-org/llama.cpp runtime/llama.cpp
fi
# Record exact versions used by this installation; reruns keep the existing checkout.
git -C runtime/whisper.cpp rev-parse HEAD > runtime/whisper-revision.txt
git -C runtime/llama.cpp rev-parse HEAD > runtime/llama-revision.txt
cmake -S runtime/whisper.cpp -B runtime/whisper.cpp/build -DCMAKE_BUILD_TYPE=Release -DGGML_VULKAN=OFF
cmake --build runtime/whisper.cpp/build --target whisper-cli -j 2
cmake -S runtime/llama.cpp -B runtime/llama.cpp/build -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF -DGGML_VULKAN=OFF
cmake --build runtime/llama.cpp/build --target llama-server -j 2
download() {
    if [ ! -s "$2" ]; then
        curl -fL --retry 3 "$1" -o "$2.part"
        mv "$2.part" "$2"
    fi
}
download https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin models/ggml-base.bin
# Community pre-quantized weights avoid retaining Q8 plus Q4 on a small SD card.
download https://huggingface.co/bartowski/Qwen_Qwen3-1.7B-GGUF/resolve/main/Qwen_Qwen3-1.7B-Q4_K_M.gguf models/Qwen3-1.7B-Q4_K_M.gguf
if [ ! -d models/yolo11n_ncnn_model ]; then
    (cd models && ../.venv/bin/python -c 'from ultralytics import YOLO; YOLO("yolo11n.pt").export(format="ncnn", imgsz=320)')
fi
.venv/bin/python -m pip freeze > runtime/installed-packages.txt
echo 'Installation complete. Run: bash scripts/start.sh'
