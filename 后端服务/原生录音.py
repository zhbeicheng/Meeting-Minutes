# ============================================================
# 文件名：原生录音.py
# 功能：基于 sounddevice 的原生音频录制模块
# 说明：替代浏览器 MediaRecorder，在桌面 GUI 中直接调用系统麦克风
#       支持 16kHz 单声道 16bit PCM 录音，输出 WAV 格式
# ============================================================

import os
import wave
import time
import threading
import logging
import json
import urllib.request
import numpy as np
import sounddevice as sd

logger = logging.getLogger("native_recorder")

# #region debug-point H1-H2:reporter
def _debug_report(hypothesis_id, location, msg, data=None):
    try:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_path = os.path.join(project_root, ".dbg", "realtime-transcript-waveform.env")
        url = "http://127.0.0.1:7777/event"
        session_id = "realtime-transcript-waveform"
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("DEBUG_SERVER_URL="):
                        url = line.strip().split("=", 1)[1]
                    elif line.startswith("DEBUG_SESSION_ID="):
                        session_id = line.strip().split("=", 1)[1]
        payload = {
            "sessionId": session_id,
            "runId": "pre-fix",
            "hypothesisId": hypothesis_id,
            "location": location,
            "msg": "[DEBUG] " + msg,
            "data": data or {},
            "ts": int(time.time() * 1000),
        }
        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            ),
            timeout=0.2,
        )
    except Exception:
        pass
# #endregion

# ============================================================
# 录音配置
# ============================================================
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
BLOCK_SIZE = 1600  # 每 100ms 的数据块大小（16000 * 0.1）


class NativeAudioRecorder:
    """原生音频录制器（单例模式）"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._recording = False
        self._paused = False
        self._frames = []
        self._stream = None
        self._start_time = 0
        self._paused_duration = 0
        self._pause_start = 0
        self._output_dir = ""
        self._on_audio_chunk = None  # 实时音频块回调
        self._waveform_buffer = np.zeros(16000 * 3, dtype=np.float32)  # 3 秒环形缓冲
        self._waveform_pos = 0
        self._debug_chunk_count = 0
        self._debug_waveform_calls = 0

    def start(self, output_dir="录音缓存", on_audio_chunk=None):
        """开始录音

        参数：
            output_dir：录音文件保存目录
            on_audio_chunk：可选回调，每收到一个音频块时调用 on_audio_chunk(pcm_bytes)
        """
        if self._recording:
            return {"success": False, "error": "已在录音中"}

        self._output_dir = os.path.join(
            os.path.dirname(__file__), output_dir
        )
        os.makedirs(self._output_dir, exist_ok=True)

        self._frames = []
        self._recording = True
        self._paused = False
        self._start_time = time.time()
        self._paused_duration = 0
        self._on_audio_chunk = on_audio_chunk
        self._debug_chunk_count = 0
        self._debug_waveform_calls = 0

        # #region debug-point H2:recording-start
        _debug_report(
            "H2",
            "后端服务/原生录音.py:start",
            "native recording started",
            {
                "sample_rate": SAMPLE_RATE,
                "block_size": BLOCK_SIZE,
                "channels": CHANNELS,
                "has_audio_callback": bool(on_audio_chunk),
            },
        )
        # #endregion

        def callback(indata, frames, time_info, status):
            if status:
                logger.warning(f"录音回调状态：{status}")
            if self._recording and not self._paused:
                self._frames.append(indata.copy())
                # 实时转写回调：将 numpy 数组转为 PCM bytes
                if self._on_audio_chunk:
                    try:
                        pcm_bytes = indata.tobytes()
                        self._on_audio_chunk(pcm_bytes)
                    except Exception as e:
                        logger.warning(f"音频块回调异常：{e}")
                # 累积波形数据到环形缓冲
                flat = indata.flatten().astype(np.float32) / 32768.0
                n = len(flat)
                buf_len = len(self._waveform_buffer)
                if self._waveform_pos + n <= buf_len:
                    self._waveform_buffer[self._waveform_pos:self._waveform_pos + n] = flat
                else:
                    part1 = buf_len - self._waveform_pos
                    self._waveform_buffer[self._waveform_pos:] = flat[:part1]
                    self._waveform_buffer[:n - part1] = flat[part1:]
                self._waveform_pos = (self._waveform_pos + n) % buf_len
                self._debug_chunk_count += 1
                # #region debug-point H1-H2:audio-callback
                if self._debug_chunk_count % 20 == 0:
                    _debug_report(
                        "H1-H2",
                        "后端服务/原生录音.py:callback",
                        "audio callback waveform stats",
                        {
                            "chunk_count": self._debug_chunk_count,
                            "frames_total": len(self._frames),
                            "rms_i16": round(float(np.sqrt(np.mean(indata.astype(np.float64) ** 2))), 2),
                            "peak_i16": int(np.max(np.abs(indata.astype(np.int32)))) if len(indata) else 0,
                            "waveform_pos": int(self._waveform_pos),
                        },
                    )
                # #endregion

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=BLOCK_SIZE,
                callback=callback
            )
            self._stream.start()
            logger.info("原生录音已启动")
            return {"success": True}
        except Exception as e:
            self._recording = False
            logger.error(f"启动录音失败：{e}")
            return {"success": False, "error": str(e)}

    def pause(self):
        """暂停录音"""
        if not self._recording:
            return {"success": False, "error": "未在录音中"}
        if self._paused:
            return {"success": False, "error": "已暂停"}

        self._paused = True
        self._pause_start = time.time()
        return {"success": True}

    def resume(self):
        """恢复录音"""
        if not self._recording:
            return {"success": False, "error": "未在录音中"}
        if not self._paused:
            return {"success": False, "error": "未暂停"}

        self._paused = False
        self._paused_duration += time.time() - self._pause_start
        return {"success": True}

    def stop(self):
        """停止录音并保存为 WAV 文件"""
        if not self._recording:
            return {"success": False, "error": "未在录音中"}

        self._recording = False

        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        total_duration = time.time() - self._start_time - self._paused_duration

        if len(self._frames) == 0:
            return {
                "success": False,
                "error": "录音数据为空",
                "duration": 0
            }

        audio_data = np.concatenate(self._frames, axis=0)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"录音_{timestamp}.wav"
        filepath = os.path.join(self._output_dir, filename)

        with wave.open(filepath, "w") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_data.tobytes())

        file_size = os.path.getsize(filepath)
        logger.info(
            f"录音已保存：{filename}，"
            f"时长 {total_duration:.1f} 秒，"
            f"大小 {file_size} 字节"
        )

        return {
            "success": True,
            "filepath": filepath,
            "filename": filename,
            "duration": round(total_duration, 1),
            "size": file_size,
            "sample_rate": SAMPLE_RATE
        }

    def get_status(self):
        """获取当前录音状态"""
        if not self._recording:
            return {"recording": False, "paused": False, "elapsed": 0}

        now = time.time()
        if self._paused:
            elapsed = self._pause_start - self._start_time - self._paused_duration
        else:
            elapsed = now - self._start_time - self._paused_duration

        return {
            "recording": True,
            "paused": self._paused,
            "elapsed": round(elapsed, 1)
        }

    def get_audio_level(self):
        """获取当前音频电平（0.0 ~ 1.0，已做非线性放大便于波形可视化）"""
        if not self._recording or self._paused or len(self._frames) == 0:
            return 0.0

        latest = self._frames[-1]
        if len(latest) == 0:
            return 0.0

        rms = np.sqrt(np.mean(latest.astype(np.float64) ** 2))
        # 16-bit 音频 RMS 通常在 500~15000，除以 3000 使正常语音在 0.3~0.8
        level = min(1.0, rms / 3000.0)
        # 非线性放大：让小声也能看到波动
        level = level ** 0.6
        return round(float(level), 4)

    def get_waveform_data(self, num_points=256):
        """
        获取波形采样点数据（用于前端绘制真实波形线）

        返回：list[float]，长度为 num_points，范围 -1.0 ~ 1.0
        """
        if not self._recording or self._waveform_pos == 0:
            return [0.0] * num_points

        # 从环形缓冲中提取最近的数据
        buf = self._waveform_buffer
        pos = self._waveform_pos
        buf_len = len(buf)

        # 取最近 2 秒的数据
        recent_samples = min(16000 * 2, buf_len)
        if pos >= recent_samples:
            recent = buf[pos - recent_samples:pos]
        else:
            recent = np.concatenate([
                buf[buf_len - (recent_samples - pos):],
                buf[:pos]
            ])

        # 降采样到 num_points
        if len(recent) <= num_points:
            result = recent.tolist()
            # 补齐
            result = result + [0.0] * (num_points - len(result))
        else:
            indices = np.linspace(0, len(recent) - 1, num_points, dtype=int)
            result = recent[indices].tolist()

        # #region debug-point H1-H2:waveform-return
        self._debug_waveform_calls += 1
        if self._debug_waveform_calls % 20 == 0:
            arr = np.array(result, dtype=np.float32)
            _debug_report(
                "H1-H2",
                "后端服务/原生录音.py:get_waveform_data",
                "waveform data returned",
                {
                    "call_count": self._debug_waveform_calls,
                    "num_points": num_points,
                    "max_abs": round(float(np.max(np.abs(arr))), 5) if len(arr) else 0,
                    "mean_abs": round(float(np.mean(np.abs(arr))), 5) if len(arr) else 0,
                    "nonzero_count": int(np.count_nonzero(np.abs(arr) > 0.0005)) if len(arr) else 0,
                    "waveform_pos": int(self._waveform_pos),
                },
            )
        # #endregion

        return [round(float(x), 4) for x in result]


def get_native_recorder():
    """获取全局唯一的原生录音器实例"""
    return NativeAudioRecorder()
