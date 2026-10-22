'use strict';
const $ = (id) => document.getElementById(id);
let pending = false;
let remoteBusy = false;
let phase = 'idle';
let submittedAt = 0;
let pollRunning = false;
let retryTimer;
let recordingSeconds = 5;
const phases = {starting:'명령을 준비하고 있습니다', recording:'녹음 중 · 파이의 마이크로 말해 주세요', transcribing:'음성을 문자로 옮기고 있습니다', loading:'사물 인식 모델을 준비하고 있습니다', interpreting:'질문의 뜻을 이해하고 있습니다', detecting:'현재 화면의 사물을 분석하고 있습니다'};
const regions = {all:'전체 화면', left:'왼쪽', center:'가운데', right:'오른쪽'};
const actions = {count:'개수 세기', find:'위치 찾기', describe:'사물 살펴보기', unsupported:'지원하지 않는 질문'};

function controls() {
  const busy = pending || remoteBusy;
  for (const button of document.querySelectorAll('#listen, #submit, [data-prompt]')) button.disabled = busy;
  $('command').disabled = busy;
  $('progressDot').classList.toggle('active', busy);
  $('listen').classList.toggle('recording', phase === 'recording');
  $('listenLabel').textContent = phase === 'recording' ? '녹음 중입니다…' : '5초 동안 녹음하기';
  if (busy) $('progressText').textContent = phases[phase] || '명령을 처리하고 있습니다';
}

function showError(message) { $('error').textContent = message; $('error').hidden = false; }
function connectCamera() {
  clearTimeout(retryTimer);
  $('camera').classList.remove('ready');
  $('cameraPlaceholder').hidden = false;
  $('cameraMessage').textContent = '잠시만 기다려 주세요.';
  $('cameraLabel').textContent = '연결 중';
  $('camera').src = '/video_feed?t=' + Date.now();
}
$('camera').addEventListener('load', () => {
  $('camera').classList.add('ready');
  $('cameraPlaceholder').hidden = true;
});
$('camera').addEventListener('error', () => {
  $('camera').classList.remove('ready');
  $('cameraPlaceholder').hidden = false;
  $('cameraMessage').textContent = '영상 연결을 확인하고 있습니다. 다시 연결 버튼으로 재시도할 수 있습니다.';
  clearTimeout(retryTimer);
  retryTimer = setTimeout(connectCamera, 10000);
});
$('reconnect').addEventListener('click', connectCamera);

async function poll() {
  if (pollRunning) return;
  pollRunning = true;
  try {
    const response = await fetch('/status', {cache:'no-store', signal:AbortSignal.timeout(5000)});
    if (!response.ok) throw new Error('상태 조회 실패');
    const status = await response.json();
    $('serverBadge').textContent = '● 파이 서버 연결됨';
    $('serverBadge').classList.add('connected');
    remoteBusy = status.busy;
    phase = status.job.phase;
    if (status.busy && status.job.started_at) submittedAt = status.job.started_at * 1000;
    controls();
    if (status.camera.ready) {
      $('cameraLabel').textContent = '● LIVE';
      // Some browsers do not emit load until an MJPEG stream ends.
      if ($('camera').naturalWidth > 0) {
        $('camera').classList.add('ready');
        $('cameraPlaceholder').hidden = true;
      }
    } else {
      $('cameraLabel').textContent = '카메라 연결 대기';
      $('cameraPlaceholder').hidden = false;
      $('cameraMessage').textContent = status.camera.error || '카메라를 준비하고 있습니다.';
    }
  } catch (error) {
    $('serverBadge').textContent = '● 서버 연결 확인 필요';
    $('serverBadge').classList.remove('connected');
    if (!pending) {
      remoteBusy = false;
      phase = 'idle';
      controls();
    }
  } finally { pollRunning = false; }
}

function tag(text) {
  const span = document.createElement('span');
  span.textContent = text;
  return span;
}

function render(data) {
  $('empty').hidden = true;
  $('result').hidden = false;
  $('transcript').textContent = data.transcript;
  $('answer').textContent = data.answer;
  $('intent').replaceChildren(tag(actions[data.intent.action] || data.intent.action), tag(data.intent.target), tag(regions[data.intent.region] || data.intent.region));
  $('objects').replaceChildren();
  const detections = data.detections || [];
  for (const item of detections) {
    const li = document.createElement('li');
    li.textContent = item.label + ' · ' + regions[item.region];
    const confidence = document.createElement('span');
    confidence.textContent = Math.round(item.confidence * 100) + '%';
    li.append(confidence);
    $('objects').append(li);
  }
  $('noObjects').hidden = detections.length > 0 || data.intent.action === 'unsupported';
  $('snapshotWrap').hidden = !data.snapshot_url;
  if (data.snapshot_url) $('snapshot').src = data.snapshot_url;
  else $('snapshot').removeAttribute('src');
  $('timings').replaceChildren();
  const timingNames = {record_seconds:'녹음', asr_seconds:'음성 인식', intent_seconds:'지시 해석', capture_detect_seconds:'사물 분석'};
  for (const [key, label] of Object.entries(timingNames)) {
    if (data.timings[key] === undefined) continue;
    const span = document.createElement('span');
    span.textContent = label;
    const value = document.createElement('b');
    value.textContent = Number(data.timings[key]).toFixed(1) + '초';
    span.append(value);
    $('timings').append(span);
  }
}

async function send(path, payload) {
  if (pending || remoteBusy) return;
  pending = true;
  phase = 'starting';
  submittedAt = Date.now();
  $('error').hidden = true;
  $('snapshotWrap').hidden = true;
  $('result').hidden = true;
  $('empty').hidden = false;
  controls();
  const started = performance.now();
  try {
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload), signal:AbortSignal.timeout(420000)});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
    render(data);
    $('progressText').textContent = '분석을 마쳤습니다';
    $('elapsed').textContent = ((performance.now() - started) / 1000).toFixed(1) + '초';
  } catch (error) {
    showError(error.name === 'TimeoutError' ? '응답 대기 시간이 지났습니다. 파이에서 작업이 계속되고 있을 수 있습니다.' : error.message);
    $('progressText').textContent = '명령을 완료하지 못했습니다';
  } finally {
    pending = false;
    remoteBusy = false;
    phase = 'idle';
    controls();
    poll();
  }
}

$('listen').addEventListener('click', () => send('/listen', {seconds:recordingSeconds}));
$('commandForm').addEventListener('submit', (event) => {
  event.preventDefault();
  const text = $('command').value.trim();
  if (text) send('/command', {text});
});
for (const button of document.querySelectorAll('[data-prompt]')) button.addEventListener('click', () => {
  $('command').value = button.dataset.prompt;
  $('command').focus();
});
setInterval(() => {
  if (pending || remoteBusy) $('elapsed').textContent = Math.max(0, Math.floor((Date.now() - submittedAt) / 1000)) + '초';
}, 500);
connectCamera();
poll();
setInterval(poll, 1500);
