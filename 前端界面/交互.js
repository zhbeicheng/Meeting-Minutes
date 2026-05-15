/* ============================================================
 * 文件名：交互.js
 * 功能：华科储能智能会议助手 — 前端交互逻辑
 * 包含：录音控制、波形可视化、API 调用、Markdown 渲染、状态管理
 * 参考：addpipe/simple-recorderjs-demo（GitHub 414 Stars）
 * ============================================================ */

// ============================================================
// 全局状态管理
// ============================================================
const STATE = {
    // 录音状态
    isRecording: false,
    isPaused: false,
    mediaRecorder: null,
    audioChunks: [],
    audioBlob: null,
    recordingStartTime: 0,
    recordingElapsed: 0,
    timerInterval: null,

    // 波形可视化
    audioContext: null,
    analyser: null,
    waveformAnimationId: null,
    waveformTimerType: null,

    // 转写状态
    sttTaskId: null,
    sttPollingInterval: null,

    // 实时转写
    realtimePollingInterval: null,
    speakerPollingInterval: null,
    realtimeText: '',
    labeledText: '',
    rawTail: '',
    speakerAliases: {},

    // 纪要状态
    currentMinutes: '',
    currentRecordId: '',
    isGenerating: false,

    // 系统状态
    engineStatus: {},
    debugWaveformTicks: 0,
    debugRealtimeTicks: 0,
    debugSpeakerTicks: 0,
};

// #region debug-point H1-H4:reporter
function debugReport(hypothesisId, location, msg, data) {
    fetch('http://127.0.0.1:7777/event', {
        method: 'POST',
        body: JSON.stringify({
            sessionId: 'realtime-transcript-waveform',
            runId: 'pre-fix',
            hypothesisId,
            location,
            msg: '[DEBUG] ' + msg,
            data: data || {},
            ts: Date.now()
        })
    }).catch(() => {});
}
// #endregion

// ============================================================
// DOM 元素缓存
// ============================================================
const $ = (id) => document.getElementById(id);

const DOM = {
    // 顶部
    statusStt: $('status-stt'),
    statusLlm: $('status-llm'),
    btnHistory: $('btn-history'),
    clock: $('clock'),

    // 左侧面板
    engineSelect: $('engine-select'),
    btnRecord: $('btn-record'),
    btnPause: $('btn-pause'),
    btnStop: $('btn-stop'),
    recordingTimer: $('recording-timer'),
    recordingStatus: $('recording-status'),
    waveform: $('waveform'),
    fileUpload: $('file-upload'),
    uploadFilename: $('upload-filename'),
    sttProgress: $('stt-progress'),
    realtimeText: $('realtime-text'),
    transcriptText: $('transcript-text'),
    transcriptChars: $('transcript-chars'),
    speakerRenamePanel: $('speaker-rename-panel'),
    speakerRenameList: $('speaker-rename-list'),
    btnCopyTranscript: $('btn-copy-transcript'),
    btnClearTranscript: $('btn-clear-transcript'),

    // 右侧面板
    btnGenerate: $('btn-generate'),
    generateStatus: $('generate-status'),
    minutesContent: $('minutes-content'),
    minutesChars: $('minutes-chars'),
    minutesActions: $('minutes-actions'),
    btnCopyMinutes: $('btn-copy-minutes'),
    btnExportDocx: $('btn-export-docx'),
    btnTranslate: $('btn-translate'),
    translateSection: $('translate-section'),
    translateContent: $('translate-content'),
    btnCloseTranslate: $('btn-close-translate'),

    // 底部
    statusText: $('status-text'),
    engineInfo: $('engine-info'),
    modelInfo: $('model-info'),

    // Toast
    toast: $('toast'),
    historyDrawer: $('history-drawer'),
    btnCloseHistory: $('btn-close-history'),
    historyList: $('history-list'),
};

// ============================================================
// 初始化
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    initClock();
    checkSystemStatus();
    bindEvents();
    initWaveformCanvas();
    diagnoseEnvironment();
    setInterval(checkSystemStatus, 30000); // 每 30 秒检查一次状态
});

// ============================================================
// 时钟
// ============================================================
function initClock() {
    const update = () => {
        const now = new Date();
        DOM.clock.textContent = now.toLocaleTimeString('zh-CN', { hour12: false });
    };
    update();
    setInterval(update, 1000);
}

// ============================================================
// 系统状态检查
// ============================================================
async function checkSystemStatus() {
    try {
        const res = await fetch('/api/status');
        const data = await res.json();
        STATE.engineStatus = data;

        // 更新状态指示灯
        const sttOnline = data.cloud_streaming_configured || data.cloud_file_configured;
        DOM.statusStt.className = 'status-dot' + (sttOnline ? ' online' : '');
        DOM.statusStt.textContent = sttOnline ? '🟢' : '🔴';
        DOM.statusStt.title = sttOnline ? '语音转写引擎：就绪' : '语音转写引擎：未配置';

        DOM.statusLlm.className = 'status-dot' + (data.minutes_configured ? ' online' : '');
        DOM.statusLlm.textContent = data.minutes_configured ? '🟢' : '🔴';
        DOM.statusLlm.title = data.minutes_configured ? '纪要生成引擎：就绪' : '纪要生成引擎：未配置';

        // 更新底部信息
        DOM.modelInfo.textContent = `模型：${data.minutes_model || '--'}`;

        // 更新引擎选择器可用状态
        updateEngineOptions(data);

        setStatus('就绪');
    } catch (e) {
        DOM.statusStt.textContent = '🔴';
        DOM.statusLlm.textContent = '🔴';
        setStatus('服务连接失败');
    }
}

function updateEngineOptions(data) {
    const options = DOM.engineSelect.options;
    for (let i = 0; i < options.length; i++) {
        const opt = options[i];
        if (opt.value === 'cloud_streaming') {
            opt.disabled = !data.cloud_streaming_configured;
            if (!data.cloud_streaming_configured) opt.text += '（未配置）';
        }
        if (opt.value === 'cloud_file') {
            opt.disabled = !data.cloud_file_configured;
            if (!data.cloud_file_configured) opt.text += '（未配置）';
        }
    }
}

// ============================================================
// 事件绑定
// ============================================================
function bindEvents() {
    // 录音按钮
    DOM.btnRecord.addEventListener('click', toggleRecording);
    DOM.btnPause.addEventListener('click', togglePause);
    DOM.btnStop.addEventListener('click', stopRecording);

    // 文件上传
    DOM.fileUpload.addEventListener('change', handleFileUpload);

    // 引擎切换
    DOM.engineSelect.addEventListener('change', () => {
        DOM.engineInfo.textContent = `引擎：${DOM.engineSelect.selectedOptions[0].text}`;
    });

    // 转写文本操作
    DOM.btnCopyTranscript.addEventListener('click', () => copyText(DOM.transcriptText.value, '转写文本'));
    DOM.btnClearTranscript.addEventListener('click', () => {
        DOM.transcriptText.value = '';
        STATE.speakerAliases = {};
        renderSpeakerRenamePanel();
        updateTranscriptChars();
    });
    DOM.transcriptText.addEventListener('input', () => {
        updateTranscriptChars();
        renderSpeakerRenamePanel();
    });

    // 纪要生成
    DOM.btnGenerate.addEventListener('click', generateMinutes);

    // 纪要操作
    DOM.btnCopyMinutes.addEventListener('click', () => copyText(STATE.currentMinutes, '会议纪要'));
    DOM.btnExportDocx.addEventListener('click', exportDocx);
    DOM.btnTranslate.addEventListener('click', toggleTranslate);
    DOM.btnCloseTranslate.addEventListener('click', () => {
        DOM.translateSection.classList.add('hidden');
    });

    // 历史记录
    DOM.btnHistory.addEventListener('click', openHistoryDrawer);
    DOM.btnCloseHistory.addEventListener('click', () => DOM.historyDrawer.classList.add('hidden'));
}

// ============================================================
// 检测运行环境
// ============================================================
function isNativeApp() {
    const hasPywebview = typeof window.pywebview !== 'undefined' && window.pywebview.api;
    console.log('[诊断] pywebview 检测：', {
        hasWindowPywebview: typeof window.pywebview !== 'undefined',
        hasApi: typeof window.pywebview !== 'undefined' && !!window.pywebview.api,
        isNative: hasPywebview
    });
    return hasPywebview;
}

async function diagnoseEnvironment() {
    console.log('[诊断] 开始环境诊断...');
    console.log('[诊断] navigator.userAgent:', navigator.userAgent);

    if (isNativeApp()) {
        console.log('[诊断] ✅ 检测到 pywebview 原生环境');
        try {
            const pingResult = await window.pywebview.api.ping();
            console.log('[诊断] ping 结果：', pingResult);
            setStatus('原生桌面模式（录音就绪）');
        } catch (e) {
            console.error('[诊断] ❌ ping 失败：', e);
            setStatus('原生桌面模式（API 桥接异常）');
        }
    } else {
        console.log('[诊断] ⚠️ 未检测到 pywebview，使用浏览器模式');
        setStatus('浏览器模式（录音可能受限）');
    }
}

// ============================================================
// 录音功能（原生桌面模式：pywebview 桥接 / 浏览器模式：MediaRecorder）
// ============================================================
async function toggleRecording() {
    console.log('[诊断] toggleRecording 被调用，isNativeApp=', isNativeApp());

    if (STATE.isRecording) {
        return;
    }

    if (isNativeApp()) {
        console.log('[诊断] 使用原生录音模式');
        await nativeStartRecording();
    } else {
        console.log('[诊断] 使用浏览器录音模式');
        await browserStartRecording();
    }
}

async function nativeStartRecording() {
    try {
        const result = await window.pywebview.api.start_recording();
        if (!result.success) {
            showToast('录音启动失败：' + (result.error || '未知错误'), 'error');
            return;
        }

        STATE.isRecording = true;
        STATE.isPaused = false;
        STATE.recordingStartTime = Date.now();
        STATE.recordingElapsed = 0;
        STATE.realtimeText = '';
        if (DOM.realtimeText) DOM.realtimeText.value = '';
        DOM.transcriptText.value = '';
        updateTranscriptChars();

        // #region debug-point H3-H4:native-start
        debugReport('H3-H4', '前端界面/交互.js:nativeStartRecording', 'native recording UI started', {
            startResult: result,
            hasPywebview: !!(window.pywebview && window.pywebview.api)
        });
        // #endregion

        startWaveform(null);
        updateRecordingUI('recording');
        startTimer();
        startRealtimePolling();
        setStatus('正在录音（实时转写中）...');
    } catch (err) {
        console.error('原生录音失败：', err);
        showToast('录音启动失败：' + err.message, 'error');
    }
}

async function browserStartRecording() {
    try {
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: {
                sampleRate: 16000,
                channelCount: 1,
                echoCancellation: true,
                noiseSuppression: true,
            }
        });

        const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
            ? 'audio/webm;codecs=opus'
            : 'audio/webm';

        STATE.mediaRecorder = new MediaRecorder(stream, { mimeType });
        STATE.audioChunks = [];
        STATE.audioBlob = null;
        STATE.isRecording = true;
        STATE.isPaused = false;
        STATE.recordingStartTime = Date.now();
        STATE.recordingElapsed = 0;

        STATE.mediaRecorder.ondataavailable = (e) => {
            if (e.data.size > 0) {
                STATE.audioChunks.push(e.data);
            }
        };

        STATE.mediaRecorder.onstop = () => {
            STATE.audioBlob = new Blob(STATE.audioChunks, { type: mimeType });
            stream.getTracks().forEach(track => track.stop());
            if (STATE.audioBlob.size > 0) {
                uploadAndTranscribe(STATE.audioBlob);
            }
        };

        STATE.mediaRecorder.start(1000);
        startWaveform(stream);
        updateRecordingUI('recording');
        startTimer();
        setStatus('正在录音...');
    } catch (err) {
        console.error('录音失败：', err);
        if (err.name === 'NotAllowedError') {
            showToast('麦克风权限被拒绝，请在浏览器设置中允许访问麦克风', 'error');
        } else {
            showToast('录音启动失败：' + err.message, 'error');
        }
    }
}

async function togglePause() {
    if (!STATE.isRecording) return;

    if (isNativeApp()) {
        if (STATE.isPaused) {
            await window.pywebview.api.resume_recording();
            STATE.isPaused = false;
            STATE.recordingStartTime = Date.now() - STATE.recordingElapsed;
            updateRecordingUI('recording');
            startTimer();
            setStatus('继续录音...');
        } else {
            await window.pywebview.api.pause_recording();
            STATE.isPaused = true;
            STATE.recordingElapsed = Date.now() - STATE.recordingStartTime;
            updateRecordingUI('paused');
            stopTimer();
            setStatus('录音已暂停');
        }
    } else {
        if (STATE.isPaused) {
            STATE.mediaRecorder.resume();
            STATE.isPaused = false;
            STATE.recordingStartTime = Date.now() - STATE.recordingElapsed;
            updateRecordingUI('recording');
            startTimer();
            setStatus('继续录音...');
        } else {
            STATE.mediaRecorder.pause();
            STATE.isPaused = true;
            STATE.recordingElapsed = Date.now() - STATE.recordingStartTime;
            updateRecordingUI('paused');
            stopTimer();
            setStatus('录音已暂停');
        }
    }
}

async function stopRecording() {
    if (!STATE.isRecording) return;

    if (isNativeApp()) {
        await nativeStopRecording();
    } else {
        STATE.mediaRecorder.stop();
        STATE.isRecording = false;
        STATE.isPaused = false;
        stopTimer();
        stopWaveform();
        updateRecordingUI('stopped');
        setStatus('录音已停止，正在转写...');
    }
}

async function nativeStopRecording() {
    try {
        stopRealtimePolling();

        const result = await window.pywebview.api.stop_recording();

        STATE.isRecording = false;
        STATE.isPaused = false;
        stopTimer();
        stopWaveform();
        updateRecordingUI('stopped');

        if (result.success && result.audio_base64) {
            const realtimeText = result.realtime_text || STATE.realtimeText || '';

            if (realtimeText && DOM.realtimeText) {
                DOM.realtimeText.value = realtimeText;
                DOM.realtimeText.scrollTop = DOM.realtimeText.scrollHeight;
            }
            setStatus('录音已停止，正在基于完整音频生成最终文本...');

            // 始终上传完整音频做最终转写，避免实时 ASR 长录音中途断开导致最终文本丢失
            const binaryStr = atob(result.audio_base64);
            const bytes = new Uint8Array(binaryStr.length);
            for (let i = 0; i < binaryStr.length; i++) {
                bytes[i] = binaryStr.charCodeAt(i);
            }
            const audioBlob = new Blob([bytes], { type: 'audio/wav' });
            audioBlob.name = result.filename || `recording_${Date.now()}.wav`;

            uploadAndTranscribe(audioBlob, { preserveRealtime: true, finalPass: true });
        } else {
            showToast('录音失败：' + (result.error || '无音频数据'), 'error');
            setStatus('录音失败');
        }
    } catch (err) {
        console.error('停止原生录音失败：', err);
        showToast('停止录音失败：' + err.message, 'error');
        setStatus('录音失败');
    }
}

// ============================================================
// 实时转写轮询（录音中：纯原文 + 每 5 秒延迟声纹标注）
// ============================================================
function startRealtimePolling() {
    stopRealtimePolling();
    STATE.realtimeText = '';
    STATE.labeledText = '';
    STATE.rawTail = '';
    if (DOM.realtimeText) DOM.realtimeText.value = '';

    // 轮询实时原文（300ms 间隔）
    STATE.realtimePollingInterval = setInterval(async () => {
        if (!STATE.isRecording) return;

        try {
            const result = await window.pywebview.api.get_realtime_text();
            STATE.debugRealtimeTicks += 1;
            // #region debug-point H4:realtime-poll
            if (STATE.debugRealtimeTicks <= 5 || STATE.debugRealtimeTicks % 10 === 0) {
                debugReport('H4', '前端界面/交互.js:startRealtimePolling', 'frontend realtime poll', {
                    tick: STATE.debugRealtimeTicks,
                    resultTextLen: result && result.text ? result.text.length : 0,
                    stateTextLen: STATE.realtimeText.length,
                    isFinal: result ? result.is_final : null,
                    error: result ? result.error : null
                });
            }
            // #endregion
            if (result && result.text && result.text !== STATE.realtimeText) {
                STATE.realtimeText = result.text;
                updateTranscriptDisplay();
            }
        } catch (e) {
            // 忽略轮询错误
        }
    }, 300);

    // 长录音稳定策略：录音中不做声纹标注，停止后基于完整音频统一整理。
}

function stopRealtimePolling() {
    if (STATE.realtimePollingInterval) {
        clearInterval(STATE.realtimePollingInterval);
        STATE.realtimePollingInterval = null;
    }
    if (STATE.speakerPollingInterval) {
        clearInterval(STATE.speakerPollingInterval);
        STATE.speakerPollingInterval = null;
    }
}

function updateTranscriptDisplay() {
    const fullRaw = STATE.realtimeText || '';

    // #region debug-point H4:display
    debugReport('H4', '前端界面/交互.js:updateTranscriptDisplay', 'transcript display update', {
        fullRawLen: fullRaw.length,
        mode: 'raw'
    });
    // #endregion

    if (DOM.realtimeText) {
        DOM.realtimeText.value = fullRaw;
        DOM.realtimeText.scrollTop = DOM.realtimeText.scrollHeight;
    }
}

function updateRecordingUI(state) {
    switch (state) {
        case 'recording':
            DOM.btnRecord.classList.add('recording');
            DOM.btnRecord.querySelector('.btn-icon').textContent = '🔴';
            DOM.btnRecord.querySelector('.btn-text').textContent = '录音中...';
            DOM.btnPause.classList.remove('hidden');
            DOM.btnStop.classList.remove('hidden');
            DOM.btnPause.querySelector('.btn-icon').textContent = '⏸️';
            DOM.recordingStatus.textContent = '● 录音中';
            DOM.recordingStatus.style.color = '#fca5a5';
            break;
        case 'paused':
            DOM.btnRecord.classList.remove('recording');
            DOM.btnPause.querySelector('.btn-icon').textContent = '▶️';
            DOM.recordingStatus.textContent = '⏸ 已暂停';
            DOM.recordingStatus.style.color = '#fcd34d';
            break;
        case 'stopped':
            DOM.btnRecord.classList.remove('recording');
            DOM.btnRecord.querySelector('.btn-icon').textContent = '🔴';
            DOM.btnRecord.querySelector('.btn-text').textContent = '开始录音';
            DOM.btnPause.classList.add('hidden');
            DOM.btnStop.classList.add('hidden');
            DOM.recordingStatus.textContent = '';
            DOM.recordingTimer.textContent = '00:00';
            break;
    }
}

// ============================================================
// 计时器
// ============================================================
function startTimer() {
    stopTimer();
    STATE.timerInterval = setInterval(() => {
        const elapsed = STATE.isPaused
            ? STATE.recordingElapsed
            : Date.now() - STATE.recordingStartTime;
        DOM.recordingTimer.textContent = formatTime(elapsed);
    }, 200);
}

function stopTimer() {
    if (STATE.timerInterval) {
        clearInterval(STATE.timerInterval);
        STATE.timerInterval = null;
    }
}

function formatTime(ms) {
    const totalSeconds = Math.floor(ms / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

// ============================================================
// 波形可视化（原生模式：pywebview 音频电平 / 浏览器模式：Web Audio API）
// ============================================================
function initWaveformCanvas() {
    const canvas = DOM.waveform;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.06)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    const midY = canvas.height / 2;
    ctx.moveTo(0, midY);
    ctx.lineTo(canvas.width, midY);
    ctx.stroke();
}

function startWaveform(stream) {
    if (isNativeApp()) {
        startNativeWaveform();
    } else if (stream) {
        startBrowserWaveform(stream);
    }
}

function startNativeWaveform() {
    const canvas = DOM.waveform;
    const ctx = canvas.getContext('2d');
    const WIDTH = canvas.width;
    const HEIGHT = canvas.height;
    const midY = HEIGHT / 2;

    // 用 setInterval 替代 requestAnimationFrame（pywebview 中更稳定）
    STATE.waveformTimerType = 'interval';
    STATE.waveformAnimationId = setInterval(async () => {
        let points = [];
        try {
            points = await window.pywebview.api.get_waveform_data();
        } catch (e) {
            points = new Array(256).fill(0);
        }

        if (!points || points.length === 0) {
            points = new Array(256).fill(0);
        }

        // #region debug-point H1:waveform-front
        STATE.debugWaveformTicks += 1;
        if (STATE.debugWaveformTicks <= 5 || STATE.debugWaveformTicks % 20 === 0) {
            const maxAbs = points.reduce((m, v) => Math.max(m, Math.abs(v || 0)), 0);
            const nonzeroCount = points.filter(v => Math.abs(v || 0) > 0.0005).length;
            debugReport('H1', '前端界面/交互.js:startNativeWaveform', 'frontend waveform points', {
                tick: STATE.debugWaveformTicks,
                pointsLen: points.length,
                maxAbs,
                nonzeroCount,
                canvasWidth: WIDTH,
                canvasHeight: HEIGHT
            });
        }
        // #endregion

        ctx.clearRect(0, 0, WIDTH, HEIGHT);

        // 背景网格线
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.04)';
        ctx.lineWidth = 1;
        for (let y = 0; y < HEIGHT; y += HEIGHT / 4) {
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(WIDTH, y);
            ctx.stroke();
        }

        // 中间线
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
        ctx.beginPath();
        ctx.moveTo(0, midY);
        ctx.lineTo(WIDTH, midY);
        ctx.stroke();

        const visualGain = 18; // 真实麦克风采样通常只有 0.02~0.04，需要视觉增益
        const toVisualY = (value) => {
            const amplified = Math.max(-1, Math.min(1, (value || 0) * visualGain));
            return midY - amplified * midY * 0.9;
        };

        // 绘制波形填充区域
        ctx.beginPath();
        const stepX = WIDTH / (points.length - 1);
        ctx.moveTo(0, midY);
        for (let i = 0; i < points.length; i++) {
            const x = i * stepX;
            const y = toVisualY(points[i]);
            ctx.lineTo(x, y);
        }
        ctx.lineTo(WIDTH, midY);
        ctx.closePath();

        // 渐变填充
        const gradient = ctx.createLinearGradient(0, 0, 0, HEIGHT);
        gradient.addColorStop(0, 'rgba(26, 86, 219, 0.35)');
        gradient.addColorStop(0.5, 'rgba(59, 130, 246, 0.15)');
        gradient.addColorStop(1, 'rgba(26, 86, 219, 0.35)');
        ctx.fillStyle = gradient;
        ctx.fill();

        // 波形轮廓线
        ctx.beginPath();
        for (let i = 0; i < points.length; i++) {
            const x = i * stepX;
            const y = toVisualY(points[i]);
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = 'rgba(59, 130, 246, 0.8)';
        ctx.lineWidth = 1.5;
        ctx.stroke();

        // 发光效果
        ctx.shadowColor = 'rgba(59, 130, 246, 0.4)';
        ctx.shadowBlur = 6;
        ctx.strokeStyle = 'rgba(59, 130, 246, 0.5)';
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.shadowBlur = 0;
    }, 50); // ~20fps
}

function startBrowserWaveform(stream) {
    try {
        STATE.audioContext = new (window.AudioContext || window.webkitAudioContext)();
        const source = STATE.audioContext.createMediaStreamSource(stream);
        STATE.analyser = STATE.audioContext.createAnalyser();
        STATE.analyser.fftSize = 256;
        source.connect(STATE.analyser);

        drawBrowserWaveform();
    } catch (e) {
        console.warn('波形可视化启动失败：', e);
    }
}

function drawBrowserWaveform() {
    const canvas = DOM.waveform;
    const ctx = canvas.getContext('2d');
    const analyser = STATE.analyser;
    const bufferLength = analyser.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);

    const draw = () => {
        STATE.waveformTimerType = 'animation';
        STATE.waveformAnimationId = requestAnimationFrame(draw);

        analyser.getByteFrequencyData(dataArray);

        ctx.clearRect(0, 0, canvas.width, canvas.height);

        const gradient = ctx.createLinearGradient(0, 0, canvas.width, 0);
        gradient.addColorStop(0, 'rgba(26, 86, 219, 0.3)');
        gradient.addColorStop(0.5, 'rgba(59, 130, 246, 0.5)');
        gradient.addColorStop(1, 'rgba(26, 86, 219, 0.3)');

        const barWidth = (canvas.width / bufferLength) * 2.5;
        const midY = canvas.height / 2;
        let x = 0;

        for (let i = 0; i < bufferLength; i++) {
            const value = dataArray[i] / 255;
            const barHeight = value * canvas.height * 0.8;

            ctx.fillStyle = gradient;
            ctx.fillRect(x, midY - barHeight / 2, barWidth - 1, Math.max(barHeight, 2));
            x += barWidth;
        }
    };

    draw();
}

function stopWaveform() {
    if (STATE.waveformAnimationId) {
        if (STATE.waveformTimerType === 'interval') {
            clearInterval(STATE.waveformAnimationId);
        } else {
            cancelAnimationFrame(STATE.waveformAnimationId);
        }
        STATE.waveformAnimationId = null;
        STATE.waveformTimerType = null;
    }
    if (STATE.audioContext && STATE.audioContext.state !== 'closed') {
        STATE.audioContext.close().catch(() => {});
        STATE.audioContext = null;
        STATE.analyser = null;
    }
    initWaveformCanvas();
}

// ============================================================
// 文件上传
// ============================================================
function handleFileUpload(e) {
    const file = e.target.files[0];
    if (!file) return;

    DOM.uploadFilename.textContent = file.name;

    // 检查文件类型
    const validTypes = ['audio/wav', 'audio/mp3', 'audio/mpeg', 'audio/m4a', 'audio/webm', 'audio/x-m4a', 'audio/mp4'];
    const validExts = ['.wav', '.mp3', '.m4a', '.webm', '.mp4'];
    const ext = '.' + file.name.split('.').pop().toLowerCase();

    if (!validTypes.includes(file.type) && !validExts.includes(ext)) {
        showToast('不支持的音频格式，请使用 WAV/MP3/M4A/WebM 格式', 'error');
        return;
    }

    uploadAndTranscribe(file);
}

async function uploadAndTranscribe(audioData, options = {}) {
    // 显示进度条
    DOM.sttProgress.classList.remove('hidden');
    if (!options.preserveFinal) {
        DOM.transcriptText.value = '';
        updateTranscriptChars();
    }
    setStatus(options.finalPass ? '正在基于完整音频生成最终文本...' : '正在上传音频并转写...');

    try {
        // 构建 FormData
        const formData = new FormData();
        const filename = audioData.name || `recording_${Date.now()}.webm`;
        formData.append('file', audioData, filename);

        const engine = options.finalPass ? 'cloud_streaming' : DOM.engineSelect.value;

        // 上传音频，获取 task_id
        const uploadRes = await fetch(`/api/stt?engine=${engine}`, {
            method: 'POST',
            body: formData,
        });

        if (!uploadRes.ok) {
            const err = await uploadRes.json();
            throw new Error(err.detail || '上传失败');
        }

        const { task_id } = await uploadRes.json();
        STATE.sttTaskId = task_id;

        // 开始轮询转写结果
        pollSttResult(task_id, options);

    } catch (err) {
        DOM.sttProgress.classList.add('hidden');
        showToast('转写失败：' + err.message, 'error');
        setStatus('转写失败');
    }
}

// ============================================================
// 转写结果轮询
// ============================================================
function pollSttResult(taskId, options = {}) {
    if (STATE.sttPollingInterval) {
        clearInterval(STATE.sttPollingInterval);
    }

    let pollCount = 0;
    const maxPolls = options.finalPass ? 360 : 120; // 最终长录音最多等待 6 分钟

    STATE.sttPollingInterval = setInterval(async () => {
        pollCount++;

        try {
            const res = await fetch(`/api/stt/${taskId}`);
            const data = await res.json();

            if (data.status === 'completed') {
                // 转写完成
                clearInterval(STATE.sttPollingInterval);
                STATE.sttPollingInterval = null;
                DOM.sttProgress.classList.add('hidden');

                DOM.transcriptText.value = data.speaker_text || data.text || '';
                STATE.speakerAliases = {};
                updateTranscriptChars();
                renderSpeakerRenamePanel();
                DOM.transcriptText.scrollTop = DOM.transcriptText.scrollHeight;
                setStatus(data.speaker_text ? '最终转写与发言人标注完成' : '最终转写完成');
                showToast(data.speaker_text ? '最终文本与发言人标注完成！' : '语音转写完成！', 'success');

            } else if (data.status === 'failed') {
                // 转写失败
                clearInterval(STATE.sttPollingInterval);
                STATE.sttPollingInterval = null;
                DOM.sttProgress.classList.add('hidden');

                showToast('转写失败：' + (data.error || '未知错误'), 'error');
                setStatus('转写失败');

            } else if (pollCount >= maxPolls) {
                // 超时
                clearInterval(STATE.sttPollingInterval);
                STATE.sttPollingInterval = null;
                DOM.sttProgress.classList.add('hidden');

                showToast('转写超时，请重试', 'error');
                setStatus('转写超时');
            }
            // 否则继续轮询（pending / processing）

        } catch (err) {
            clearInterval(STATE.sttPollingInterval);
            STATE.sttPollingInterval = null;
            DOM.sttProgress.classList.add('hidden');
            showToast('查询转写状态失败：' + err.message, 'error');
            setStatus('查询失败');
        }
    }, 1000); // 每秒轮询一次
}

// ============================================================
// 纪要生成
// ============================================================
async function generateMinutes() {
    const text = DOM.transcriptText.value.trim();
    if (!text) {
        showToast('请先完成语音转写或粘贴会议文本', 'error');
        return;
    }

    if (STATE.isGenerating) return;

    STATE.isGenerating = true;
    DOM.btnGenerate.disabled = true;
    DOM.btnGenerate.innerHTML = '<span class="btn-icon">⏳</span> 生成中...';
    DOM.generateStatus.textContent = 'AI 正在整理纪要...';
    DOM.generateStatus.className = 'generate-status generating';
    setStatus('正在生成纪要...');

    try {
        const template = '部门例会';

        const res = await fetch('/api/minutes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text, template, task_id: STATE.sttTaskId || '' }),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || '生成失败');
        }

        const data = await res.json();
        STATE.currentMinutes = data.markdown;
        STATE.currentRecordId = data.record_id || '';

        // 渲染 Markdown
        renderMarkdown(data.markdown);

        // 显示操作按钮
        DOM.minutesActions.classList.remove('hidden');
        DOM.minutesChars.textContent = `${data.markdown.length} 字`;

        setStatus('纪要生成完成');
        showToast('会议纪要生成完成！', 'success');

    } catch (err) {
        showToast('纪要生成失败：' + err.message, 'error');
        setStatus('纪要生成失败');
    } finally {
        STATE.isGenerating = false;
        DOM.btnGenerate.disabled = false;
        DOM.btnGenerate.innerHTML = '<span class="btn-icon">✨</span> 生成会议纪要';
        DOM.generateStatus.textContent = '';
        DOM.generateStatus.className = 'generate-status';
    }
}

// ============================================================
// Markdown 渲染（简易版，无需引入额外库）
// ============================================================
function renderMarkdown(md) {
    let html = md;

    // 转义 HTML
    html = html.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    // 表格（必须在其他行级元素之前处理）
    html = html.replace(/\n(\|.+\|)\n\|[-| :]+\|\n((?:\|.+\|\n?)*)/g, (_match, header, body) => {
        const headers = header.split('|').filter(c => c.trim()).map(c => `<th>${c.trim()}</th>`).join('');
        const rows = body.trim().split('\n').map(row => {
            const cells = row.split('|').filter(c => c.trim()).map(c => `<td>${c.trim()}</td>`).join('');
            return `<tr>${cells}</tr>`;
        }).join('');
        return `\n<table><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table>\n`;
    });

    // 标题
    html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');

    // 粗体
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

    // 无序列表
    html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
    html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');

    // 有序列表
    html = html.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');

    // 段落（连续的非空行）
    html = html.replace(/\n\n/g, '</p><p>');
    html = '<p>' + html + '</p>';

    // 清理空段落
    html = html.replace(/<p><\/p>/g, '');
    html = html.replace(/<p>(\s*<[hut])/g, '$1');
    html = html.replace(/(<\/[hut][^>]*>)\s*<\/p>/g, '$1');

    // 换行
    html = html.replace(/\n/g, '<br>');

    DOM.minutesContent.innerHTML = html;
    DOM.minutesContent.classList.add('rendered');
}

// ============================================================
// 发言人重命名/校正
// ============================================================
function extractSpeakerLabels(text) {
    const labels = new Set();
    const pattern = /\[(发言人\s*[A-Z]|SPEAKER_\d+)\]/g;
    let match;

    while ((match = pattern.exec(text || '')) !== null) {
        labels.add(match[1].replace(/\s+/g, ' '));
    }

    return Array.from(labels).sort((a, b) => a.localeCompare(b, 'zh-CN'));
}

function renderSpeakerRenamePanel() {
    if (!DOM.speakerRenamePanel || !DOM.speakerRenameList) return;

    const labels = extractSpeakerLabels(DOM.transcriptText.value);
    if (labels.length === 0) {
        DOM.speakerRenamePanel.classList.add('hidden');
        DOM.speakerRenameList.innerHTML = '';
        return;
    }

    DOM.speakerRenamePanel.classList.remove('hidden');
    DOM.speakerRenameList.innerHTML = labels.map(label => {
        const currentValue = STATE.speakerAliases[label] || '';
        return `
            <div class="speaker-rename-item">
                <span class="speaker-rename-label">${escapeHtml(label)}</span>
                <input
                    class="speaker-rename-input"
                    data-speaker-label="${escapeHtml(label)}"
                    value="${escapeHtml(currentValue)}"
                    placeholder="真实姓名"
                >
                <button class="btn btn-xs" data-rename-speaker="${escapeHtml(label)}">应用</button>
            </div>
        `;
    }).join('');

    DOM.speakerRenameList.querySelectorAll('[data-rename-speaker]').forEach(button => {
        button.addEventListener('click', () => {
            const label = button.getAttribute('data-rename-speaker');
            const input = DOM.speakerRenameList.querySelector(
                `.speaker-rename-input[data-speaker-label="${cssEscape(label)}"]`
            );
            applySpeakerRename(label, input ? input.value : '');
        });
    });
}

function applySpeakerRename(label, rawName) {
    const name = (rawName || '').trim();
    if (!label || !name) {
        showToast('请输入真实姓名后再应用', 'error');
        return;
    }

    const oldTag = `[${label}]`;
    const newTag = `[${name}]`;
    const escapedOldTag = oldTag.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    DOM.transcriptText.value = DOM.transcriptText.value.replace(new RegExp(escapedOldTag, 'g'), newTag);

    STATE.speakerAliases[label] = name;
    updateTranscriptChars();
    renderSpeakerRenamePanel();
    showToast(`${label} 已替换为 ${name}`, 'success');
}

function escapeHtml(value) {
    return String(value || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === 'function') {
        return window.CSS.escape(value);
    }
    return String(value || '').replace(/"/g, '\\"');
}

// ============================================================
// 翻译功能（阶段 5 完整实现，当前占位）
// ============================================================
async function toggleTranslate() {
    if (!STATE.currentMinutes) {
        showToast('请先生成会议纪要', 'error');
        return;
    }

    DOM.translateSection.classList.remove('hidden');
    DOM.translateContent.innerHTML = '<p style="color: var(--text-muted);">⏳ 正在生成英文翻译...</p>';

    try {
        const res = await fetch('/api/translate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: STATE.currentMinutes, direction: 'zh2en' }),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || '翻译失败');
        }

        const data = await res.json();
        DOM.translateContent.textContent = data.translated || '';
        showToast('英文翻译完成', 'success');
    } catch (err) {
        DOM.translateContent.innerHTML = `<p style="color: var(--error);">翻译失败：${escapeHtml(err.message)}</p>`;
        showToast('翻译失败：' + err.message, 'error');
    }
}

// ============================================================
// 导出 Word（阶段 7 完整实现，当前占位）
// ============================================================
async function exportDocx() {
    if (!STATE.currentMinutes) {
        showToast('请先生成会议纪要', 'error');
        return;
    }

    try {
        const title = extractMarkdownTitle(STATE.currentMinutes) || '会议纪要';
        const res = await fetch('/api/export', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                markdown: STATE.currentMinutes,
                title,
                doc_type: 'minutes'
            }),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || '导出失败');
        }

        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `${title}.docx`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
        showToast('Word 文档已导出', 'success');
    } catch (err) {
        showToast('Word 导出失败：' + err.message, 'error');
    }
}

// ============================================================
// 历史记录
// ============================================================
async function openHistoryDrawer() {
    DOM.historyDrawer.classList.remove('hidden');
    DOM.historyList.innerHTML = '<div class="history-card">正在加载历史记录...</div>';

    try {
        const res = await fetch('/api/records');
        const data = await res.json();
        renderHistoryList(data.records || []);
    } catch (err) {
        DOM.historyList.innerHTML = '<div class="history-card">历史记录加载失败</div>';
    }
}

function renderHistoryList(records) {
    if (!records.length) {
        DOM.historyList.innerHTML = '<div class="history-card">暂无历史会议记录。生成纪要后会自动保存。</div>';
        return;
    }

    DOM.historyList.innerHTML = records.map(record => `
        <div class="history-card">
            <div class="history-card-title">${escapeHtml(record.title || '未命名会议')}</div>
            <div class="history-card-meta">
                ${escapeHtml(record.created_at || '')}<br>
                模板：${escapeHtml(record.template || '--')} ｜ 转写 ${record.transcript_len || 0} 字 ｜ 纪要 ${record.minutes_len || 0} 字<br>
                音频：${record.audio_path ? escapeHtml(record.audio_path) : '无'}
            </div>
            <div class="history-card-actions">
                <button class="btn btn-xs" data-open-record="${escapeHtml(record.id)}">打开</button>
            </div>
        </div>
    `).join('');

    DOM.historyList.querySelectorAll('[data-open-record]').forEach(button => {
        button.addEventListener('click', () => openHistoryRecord(button.getAttribute('data-open-record')));
    });
}

async function openHistoryRecord(recordId) {
    try {
        const res = await fetch(`/api/records/${recordId}`);
        if (!res.ok) throw new Error('记录不存在');
        const record = await res.json();

        DOM.transcriptText.value = record.transcript || '';
        STATE.currentMinutes = record.minutes || '';
        STATE.currentRecordId = record.id || '';

        updateTranscriptChars();
        renderSpeakerRenamePanel();
        if (STATE.currentMinutes) {
            renderMarkdown(STATE.currentMinutes);
            DOM.minutesActions.classList.remove('hidden');
            DOM.minutesChars.textContent = `${STATE.currentMinutes.length} 字`;
        }

        DOM.historyDrawer.classList.add('hidden');
        showToast('历史会议已打开', 'success');
    } catch (err) {
        showToast('打开历史记录失败：' + err.message, 'error');
    }
}

function extractMarkdownTitle(markdown) {
    const line = (markdown || '').split('\n').find(item => item.startsWith('# '));
    return line ? line.replace(/^#\s+/, '').trim() : '';
}

// ============================================================
// 工具函数
// ============================================================
function updateTranscriptChars() {
    const len = DOM.transcriptText.value.length;
    DOM.transcriptChars.textContent = `${len} 字`;
}

async function copyText(text, label) {
    if (!text) {
        showToast('没有可复制的内容', 'error');
        return;
    }
    try {
        await navigator.clipboard.writeText(text);
        showToast(`${label}已复制到剪贴板`, 'success');
    } catch (e) {
        // 降级方案
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
        showToast(`${label}已复制到剪贴板`, 'success');
    }
}

function setStatus(text) {
    DOM.statusText.textContent = text;
}

function showToast(message, type = '') {
    const toast = DOM.toast;
    toast.textContent = message;
    toast.className = 'toast show ' + type;

    clearTimeout(toast._timeout);
    toast._timeout = setTimeout(() => {
        toast.className = 'toast hidden';
    }, 3000);
}
