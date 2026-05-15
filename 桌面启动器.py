# ============================================================
# 文件名：桌面启动器.py
# 功能：华科储能智能会议助手 — 原生桌面 GUI 启动入口
# 说明：使用 pywebview 将前端界面嵌入原生桌面窗口
#       后端 FastAPI 服务在后台线程运行，窗口关闭时自动停止
#       通过 JS API 桥接提供原生录音能力（替代浏览器 MediaRecorder）
# 使用：python3 桌面启动器.py
# ============================================================

import os
import sys
import time
import json
import base64
import signal
import threading
import subprocess
import webview

# ============================================================
# 配置
# ============================================================
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

WINDOW_TITLE = "华科储能智能会议助手"
WINDOW_WIDTH = 1400
WINDOW_HEIGHT = 900
WINDOW_MIN_WIDTH = 1000
WINDOW_MIN_HEIGHT = 600

# ============================================================
# 后端服务管理
# ============================================================
_server_process = None


def start_backend():
    """在后台启动 FastAPI 后端服务"""
    global _server_process

    _server_process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "后端服务.主程序:app",
            "--host", SERVER_HOST,
            "--port", str(SERVER_PORT),
            "--log-level", "warning"
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=os.path.dirname(__file__)
    )

    import urllib.request
    max_retries = 30
    for i in range(max_retries):
        try:
            urllib.request.urlopen(f"{SERVER_URL}/api/status", timeout=1)
            return True
        except Exception:
            time.sleep(0.3)

    return False


def stop_backend():
    """停止后端服务"""
    global _server_process
    if _server_process:
        _server_process.terminate()
        try:
            _server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_process.kill()
        _server_process = None


# ============================================================
# JS API 桥接类：暴露给前端调用的原生能力
# ============================================================
class NativeAPI:
    """
    原生能力 API 桥接

    前端通过 window.pywebview.api.方法名() 调用
    所有方法返回可 JSON 序列化的数据
    """

    def __init__(self):
        self._recorder = None
        self._realtime_asr = None
        self._diarization = None

    def _get_recorder(self):
        """延迟加载录音器"""
        if self._recorder is None:
            from 后端服务.原生录音 import get_native_recorder
            self._recorder = get_native_recorder()
        return self._recorder

    def _get_realtime_asr(self):
        """延迟加载实时 ASR 引擎"""
        if self._realtime_asr is None:
            from 后端服务.语音转写_实时 import get_realtime_asr_engine
            self._realtime_asr = get_realtime_asr_engine()
        return self._realtime_asr

    def _get_diarization(self):
        """延迟加载说话人分离引擎"""
        if self._diarization is None:
            from 后端服务.说话人分离 import get_speaker_diarization_engine
            self._diarization = get_speaker_diarization_engine()
        return self._diarization

    def _on_audio_chunk(self, pcm_bytes: bytes):
        """录音回调：将音频块喂给实时 ASR 引擎"""
        asr = self._get_realtime_asr()
        asr.feed_audio(pcm_bytes)

    def start_recording(self):
        """开始录音（同时启动实时转写引擎）"""
        recorder = self._get_recorder()

        # 启动实时 ASR 引擎
        asr = self._get_realtime_asr()
        if asr.configured:
            asr.start()

        # 开始录音，音频块回调喂给 ASR
        result = recorder.start(on_audio_chunk=self._on_audio_chunk)
        return result

    def pause_recording(self):
        """暂停录音"""
        recorder = self._get_recorder()
        result = recorder.pause()
        return result

    def resume_recording(self):
        """恢复录音"""
        recorder = self._get_recorder()
        result = recorder.resume()
        return result

    def stop_recording(self):
        """停止录音，返回文件路径、实时转写和说话人分离结果"""
        recorder = self._get_recorder()
        result = recorder.stop()

        # 停止实时 ASR 并获取最终结果
        asr = self._get_realtime_asr()
        realtime_result = asr.stop()

        if result.get("success") and result.get("filepath"):
            filepath = result["filepath"]
            filename = result["filename"]
            with open(filepath, "rb") as f:
                audio_bytes = f.read()
            audio_b64 = base64.b64encode(audio_bytes).decode("ascii")

            return {
                "success": True,
                "filename": filename,
                "duration": result["duration"],
                "size": result["size"],
                "sample_rate": result["sample_rate"],
                "audio_base64": audio_b64,
                "realtime_text": realtime_result.get("text", ""),
                "realtime_segments": realtime_result.get("segments", []),
                "realtime_log_id": realtime_result.get("log_id", ""),
                "speaker_text": "",
                "diarization_segments": []
            }

        return result

    def get_recording_status(self):
        """获取录音状态"""
        recorder = self._get_recorder()
        return recorder.get_status()

    def get_audio_level(self):
        """获取当前音频电平（0.0 ~ 1.0）"""
        recorder = self._get_recorder()
        return recorder.get_audio_level()

    def get_waveform_data(self):
        """获取波形采样点数据（256 个点，-1.0 ~ 1.0）"""
        recorder = self._get_recorder()
        return recorder.get_waveform_data(256)

    def get_realtime_text(self):
        """获取实时转写累积文本（前端轮询用）"""
        asr = self._get_realtime_asr()
        return asr.get_realtime_text()

    def get_speaker_labeled_text(self):
        """获取带说话人标签的延迟标注文本"""
        asr = self._get_realtime_asr()
        return asr.get_speaker_labeled_text()

    def get_server_url(self):
        """获取后端服务地址"""
        return SERVER_URL

    def ping(self):
        """诊断用：测试 JS API 桥接是否正常"""
        return {"pong": True, "message": "pywebview JS API 桥接正常"}


# ============================================================
# GUI 窗口配置
# ============================================================
class DesktopApp:
    """桌面应用主类"""

    def __init__(self):
        self.window = None
        self.api = NativeAPI()

    def on_closing(self):
        """窗口关闭时的回调"""
        stop_backend()

    def run(self):
        """启动桌面应用"""
        print("=" * 60)
        print("  🏭 华科储能智能会议助手")
        print("  版本：V1.0")
        print("  原生桌面应用模式")
        print("=" * 60)
        print()

        # 启动后端
        print("🔧 正在启动后端服务...")
        if not start_backend():
            print("❌ 后端服务启动失败！")
            sys.exit(1)
        print(f"   ✅ 后端服务已就绪：{SERVER_URL}")
        print()

        # 创建原生窗口（注册 JS API 桥接）
        print("🖥️  正在启动桌面窗口...")
        self.window = webview.create_window(
            title=WINDOW_TITLE,
            url=SERVER_URL,
            js_api=self.api,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT),
            resizable=True,
            fullscreen=False,
            easy_drag=False,
            text_select=True,
            confirm_close=False,
            background_color="#0f172a"
        )

        # 窗口关闭时清理
        self.window.events.closing += self.on_closing

        # 启动 GUI 事件循环
        webview.start(debug=False, http_server=False)

        print()
        print("✅ 应用已关闭")


# ============================================================
# 主入口
# ============================================================
if __name__ == "__main__":
    app = DesktopApp()
    app.run()
