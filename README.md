# Raspberry Pi 5 See & Hear

라즈베리 파이 5에서 실행하는 로컬 음성·사물 인식 서버입니다.

Pi 5 RAM 8GB, Raspberry Pi OS **64비트**, USB 마이크와 USB/CSI 카메라용입니다.
Whisper base 다국어 → Qwen3-1.7B Q4_K_M → YOLO11n NCNN을 순차 실행합니다.
모델 설치에는 인터넷이 필요하며, 설치 후 추론은 로컬에서 실행합니다.

사용자가 Pi에서 C270 웹캠·마이크로 음성 인식, 명령 해석, 사물 검출 실행을 확인했습니다.
웹 화면과 공유 카메라 스트리밍은 추가 구현했으며 실제 Pi 브라우저에서는 아직 검증하지 않았습니다.
개발 환경의 네트워크 제한으로 Pi에 직접 접속할 수 없습니다.

## 1. 파이에서 설치

이 디렉터리를 파이에 복사하고 **파이 터미널**에서 실행합니다.
여유 저장공간은 빌드·패키지·모델을 포함해 10GB 이상을 권장합니다.
그보다 작으면 설치 중 `df -h .`로 여유 공간을 확인하세요. 실제 설치 용량은 아직 측정하지 않았습니다.
전원과 냉각 상태는 장시간 추론 속도에 영향을 줍니다.

```bash
sudo apt update
sudo apt install -y python3-venv python3-dev build-essential cmake git curl ffmpeg alsa-utils libgl1 libglib2.0-0
bash scripts/setup.sh
```

CSI 카메라는 Raspberry Pi OS의 `rpicam-vid`도 필요합니다.
setup은 두 C++ 엔진을 빌드하고 모델을 다운로드합니다.
저장공간을 줄이기 위해 [bartowski의 커뮤니티 Qwen 양자화 파일](https://huggingface.co/bartowski/Qwen_Qwen3-1.7B-GGUF)을
Q4_K_M으로 바로 받습니다. 공식 Qwen 원본 저장소에서 제공하는 파일은 아닙니다.
YOLO는 320 입력으로 NCNN에 내보냅니다.
설치된 엔진 commit과 Python 패키지 버전은 `runtime/`에 기록합니다.
ARM용 패키지/내보내기 호환성 문제는 실제 Pi 설치 시 확인해야 합니다.

PyTorch/torchvision은 공식 CPU 인덱스에서 먼저 설치하고 `cpu-constraints.txt`로
버전을 제한합니다. CUDA 패키지는 필요하지 않습니다. 설치 중 공간 부족이 발생했다면
프로젝트의 실패한 `.venv`와 사용자 pip 캐시를 정리한 후 다시 설치하세요.
RAM 8GB와 저장장치의 여유 공간은 별개입니다. `df -h . /tmp`로 확인할 수 있습니다.

## 2. 장치 확인 및 서버 실행

```bash
arecord -l
arecord -L
ls /dev/video*
```

USB 카메라 예시입니다. `MIC_DEVICE`는 `arecord -l` 결과에 맞게 바꾸세요.
`plughw`를 쓰면 마이크가 16kHz를 직접 지원하지 않아도 ALSA가 변환할 수 있습니다.

```bash
export MIC_DEVICE=plughw:1,0
export CAMERA_KIND=usb
export CAMERA_DEVICE=/dev/video0
bash scripts/start.sh
```

CSI 카메라는 `export CAMERA_KIND=csi`로 변경합니다.
API는 `127.0.0.1:8000`, Qwen은 `127.0.0.1:8081`에서 실행됩니다.
Qwen 로그는 `runtime/qwen.log`이며 Ctrl+C로 두 프로세스를 종료합니다.
다른 PC에서는 `ssh -L 8000:127.0.0.1:8000 사용자@파이주소`로 접근할 수 있습니다.

## 웹 화면과 라이브 카메라

서버 실행 후 브라우저에서 `http://127.0.0.1:8000/`을 열면 라이브 영상,
질문 입력, 5초 녹음 버튼, 인식한 문장, 검출 사물, 단계별 시간과 분석 사진을 볼 수 있습니다.
녹음 버튼은 **파이에 연결된 마이크**를 사용합니다. 브라우저 PC의 마이크가 아닙니다.
영상은 640×480, 최대 약 8fps로 전송하며 YOLO는 명령이 있을 때 실행합니다.
분석 사진의 박스는 해당 사진에서 검출한 전체 사물이고 결과 목록은 질문 조건으로 필터링됩니다.

같은 네트워크의 PC에서 직접 열려면 서버 실행 시 `HOST=0.0.0.0`을 설정합니다.
현재 확인된 C270 설정 예시:

```bash
cd ~/pi-voice-vision
export HOST=0.0.0.0
export MIC_PROFILE=usb
export MIC_DEVICE=plughw:2,0
export CAMERA_KIND=usb
export CAMERA_DEVICE=/dev/video0
bash scripts/start.sh
```

PC 브라우저: `http://192.168.110.143:8000/` (Pi의 현재 IP 기준).
카드 번호는 재부팅·USB 재연결 시 달라질 수 있으므로 `arecord -l`로 확인하세요.
API와 카메라는 단일 worker로 실행하며, 한 캡처 프로세스를 미리보기와 추론이 공유합니다.
카메라는 첫 영상 요청 또는 분석 명령부터 서버 종료까지 열려 있습니다.
`/camera/status`에서 카메라 오류를, `/status`에서 현재 처리 단계를 확인할 수 있습니다.
MJPEG 스트림은 `/video_feed`, 분석 사진은 응답의 `snapshot_url`입니다.

기존 설치에 웹 화면만 업데이트할 때는 서버를 Ctrl+C로 종료하고
`pi-voice-vision-web-update.tar.gz`를 프로젝트 폴더에 풀어 재시작합니다.
모델 다운로드나 `setup.sh` 재실행은 필요하지 않습니다. 새 Python 패키지도 추가하지 않았습니다.

## SPH0645 I2S 마이크

USB 대신 SPH0645를 사용할 때는 [Adafruit 배선·설정 가이드](https://learn.adafruit.com/adafruit-i2s-mems-microphone-breakout/raspberry-pi-wiring-test)를 따릅니다.
배선은 3V→3.3V, GND/SEL→GND, BCLK→GPIO18(12번), LRCL→GPIO19(35번), DOUT→GPIO20(38번)입니다.
전원을 끈 상태에서 배선하세요.
Bookworm의 `/boot/firmware/config.txt`에서 기존 I2S 설정을 먼저 확인하고,
설정이 없다면 `[all]` 구간에 `dtoverlay=googlevoicehat-soundcard`를 추가한 뒤 재부팅합니다.
이미 다른 I2S 사운드카드 overlay가 있으면 중복으로 추가하지 말고 먼저 확인하세요.
`arecord -l`에서 실제 카드 번호를 확인한 후 다음처럼 실행합니다(카드 1 예시).

```bash
export MIC_PROFILE=sph0645
export MIC_DEVICE=plughw:1,0
bash scripts/start.sh
```

이 프로필은 48kHz S32_LE로 녹음하고 Whisper용 16kHz S16_LE로 변환합니다.
실제 Pi의 드라이버 동작과 오디오 품질은 아직 검증하지 않았습니다.

## 3. 테스트

파이의 별도 터미널에서 실행합니다.

```bash
curl -sS http://127.0.0.1:8000/health
curl -sS http://127.0.0.1:8000/command -H 'Content-Type: application/json' -d '{"text":"왼쪽에 컵이 몇 개 있어?"}'
curl -sS http://127.0.0.1:8000/listen -H 'Content-Type: application/json' -d '{"seconds":5}'
```

`/listen`을 호출하면 5초 동안 녹음합니다. 그 안에 “컵이 몇 개 있어?”처럼 말하세요.
결과에는 인식 문장, 해석한 명령, 사물·좌표·신뢰도, 답변, 단계별 시간이 포함됩니다.
`/health`는 API와 모델 파일 존재 여부만 확인합니다. 실제 Qwen·카메라·마이크 상태는
`/command`, `/listen` 성공 여부로 확인합니다. 지연시간은 아직 측정하지 않았습니다.

API 논리 검증:

```bash
python3 -m unittest discover -s tests -v
```

## 첫 버전의 범위

- 고정 시간 녹음으로 테스트합니다. 자동 발화 종료(VAD), 상시 청취, 음성 출력은 아직 없습니다.
- YOLO 검출 클래스의 개수·위치·목록을 처리합니다. 좌우는 화면을 3등분한 기준입니다.
- 색상/크기 조건, 사용자 정의 사물, 장치 제어, 여러 명의 겹친 음성 분리는 지원하지 않습니다.
- Qwen이 명령을 해석한 뒤 공유 카메라의 다음 프레임을 받아 분석합니다.
- 한 번에 명령 하나를 처리하며 다른 요청은 409를 반환합니다.
- 첫 YOLO 로딩·모델 초기화는 추가 시간이 걸립니다. 작은 사물은 320 입력에서 놓칠 수 있습니다.
- 실제 음성 테스트 후 Whisper small 또는 Qwen3-ASR-0.6B와 정확도·지연시간을 비교할 수 있습니다.

## 공식 참고 자료

- [whisper.cpp](https://github.com/ggml-org/whisper.cpp)
- [llama.cpp server](https://github.com/ggml-org/llama.cpp/tree/master/tools/server)
- [Qwen3-1.7B GGUF](https://huggingface.co/Qwen/Qwen3-1.7B-GGUF)
- [Ultralytics NCNN](https://docs.ultralytics.com/integrations/ncnn/)
