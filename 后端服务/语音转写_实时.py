# ============================================================
# 文件名：语音转写_实时.py
# 功能：基于火山引擎豆包大模型的实时语音识别引擎
# 模式：双向流式（bigmodel），边说边出字
# 参考：火山引擎文档 https://www.volcengine.com/docs/6561/1354869
#
# 架构：
#   asyncio 事件循环运行在独立线程中
#   录音回调通过线程安全队列喂入音频块
#   WebSocket 每收到一包识别结果就更新累积文本
#   前端通过 JS API 轮询 get_realtime_text() 获取最新文本
# ============================================================

import os
import sys
import json
import struct
import uuid
import time
import queue
import logging
import asyncio
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Callable

# 复用流式 ASR 的协议常量和工具函数
_src_dir = os.path.dirname(os.path.abspath(__file__))
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from 语音转写_流式 import (
    build_message,
    parse_response,
    MSG_FULL_CLIENT_REQUEST,
    MSG_AUDIO_ONLY_REQUEST,
    FLAG_NO_SEQUENCE,
    FLAG_LAST_NO_SEQ,
    SERIALIZATION_JSON,
    SERIALIZATION_NONE,
    COMPRESSION_NONE,
    _ssl_context,
)

logger = logging.getLogger("realtime_asr")

# #region debug-point H3-H5:reporter
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
# 实时 ASR 配置
# ============================================================
BIGMODEL_WS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
DIARIZATION_INTERVAL = 5.0  # 声纹标注间隔（秒），学术研究表明 1-4s 步长对准确率影响极小
LABEL_DELAY = 5.0           # 标注延迟（秒），最新 N 秒保持原文不标注


class RealtimeASREngine:
    """
    实时语音识别引擎（双向流式模式）

    使用 WebSocket 双向流式协议，每发送一包音频立即返回一包识别结果
    实现"边说边出字"的实时转写体验

    使用示例：
        engine = RealtimeASREngine()
        engine.start()
        engine.feed_audio(audio_chunk_1)
        engine.feed_audio(audio_chunk_2)
        ...
        final_text = engine.stop()
    """

    _instance: Optional["RealtimeASREngine"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized") and self._initialized:
            return
        self._initialized = True

        self._load_credentials()

        self._ws = None
        self._loop = None
        self._thread = None
        self._running = False
        self._audio_queue = queue.Queue()
        self._accumulated_text = ""
        self._segments = []
        self._lock = threading.Lock()
        self._log_id = None
        self._error = None

        # 延迟声纹标注
        self._audio_buffer = []           # 累积的 PCM 音频块
        self._labeled_text = ""           # 已标注说话人的文本（稳定区）
        self._raw_tail = ""               # 未标注的最新文本（原文区）
        self._last_diarization_time = 0.0 # 上次标注时间
        self._diarization_lock = threading.Lock()
        self._diarization_engine = None   # 延迟加载
        self._diarization_executor = ThreadPoolExecutor(max_workers=1)  # 专用线程池
        self._diarization_future = None
        self._debug_feed_count = 0
        self._debug_recv_count = 0

    def _load_credentials(self):
        """从密钥文件加载认证信息"""
        try:
            _config_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "配置与模板"
            )
            if _config_dir not in sys.path:
                sys.path.insert(0, _config_dir)

            from 火山密钥_流式 import (
                API_KEY, APP_KEY, ACCESS_KEY, RESOURCE_ID
            )
            self.api_key = API_KEY
            self.app_key = APP_KEY
            self.access_key = ACCESS_KEY
            self.resource_id = RESOURCE_ID
        except ImportError:
            self.api_key = os.getenv("ASR_API_KEY", "")
            self.app_key = os.getenv("ASR_APP_KEY", "")
            self.access_key = os.getenv("ASR_ACCESS_KEY", "")
            self.resource_id = os.getenv("ASR_RESOURCE_ID", "volc.bigasr.sauc.duration")

        if self.api_key:
            self.auth_mode = "new"
        elif self.app_key and self.access_key:
            self.auth_mode = "old"
        else:
            self.auth_mode = None

        self.configured = self.auth_mode is not None

    def get_status(self) -> dict:
        """获取引擎状态"""
        return {
            "configured": self.configured,
            "running": self._running,
            "provider": "火山引擎-豆包双向流式语音识别大模型"
        }

    # ============================================================
    # 公开接口
    # ============================================================

    def start(self) -> bool:
        """
        启动实时识别引擎

        建立 WebSocket 连接并发送 full client request
        启动后台 asyncio 事件循环线程
        """
        if not self.configured:
            logger.error("实时 ASR 未配置")
            return False

        if self._running:
            logger.warning("实时 ASR 已在运行中")
            return False

        self._running = True
        self._accumulated_text = ""
        self._segments = []
        self._error = None
        self._audio_queue = queue.Queue()
        self._audio_buffer = []
        self._labeled_text = ""
        self._raw_tail = ""
        self._last_diarization_time = 0.0
        self._diarization_future = None
        self._debug_feed_count = 0
        self._debug_recv_count = 0

        # #region debug-point H3:asr-start
        _debug_report(
            "H3",
            "后端服务/语音转写_实时.py:start",
            "realtime ASR start",
            {
                "configured": self.configured,
                "auth_mode": self.auth_mode,
                "queue_size": self._audio_queue.qsize(),
            },
        )
        # #endregion

        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()

        logger.info("实时 ASR 引擎已启动")
        return True

    def feed_audio(self, audio_chunk: bytes):
        """
        喂入音频数据块

        参数：
            audio_chunk：PCM 音频数据（int16, 16kHz, 单声道）
        """
        if not self._running:
            return
        self._audio_queue.put(audio_chunk)
        self._debug_feed_count += 1
        # #region debug-point H3:feed-audio
        if self._debug_feed_count % 50 == 0:
            _debug_report(
                "H3",
                "后端服务/语音转写_实时.py:feed_audio",
                "audio fed to ASR queue",
                {
                    "feed_count": self._debug_feed_count,
                    "chunk_bytes": len(audio_chunk),
                    "queue_size": self._audio_queue.qsize(),
                    "running": self._running,
                },
            )
        # #endregion

    def stop(self) -> dict:
        """
        停止实时识别

        发送结束包，等待最终结果返回

        返回：
            dict：{"text": "完整转写文本", "segments": [...], "log_id": "..."}
        """
        if not self._running:
            return {"text": self._accumulated_text, "segments": self._segments, "log_id": self._log_id}

        self._running = False
        self._audio_queue.put(None)  # 发送停止信号

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10.0)

        with self._lock:
            result = {
                "text": self._accumulated_text,
                "segments": list(self._segments),
                "log_id": self._log_id,
                "engine": "cloud_realtime"
            }

        logger.info(f"实时 ASR 已停止：文本 {len(result['text'])} 字符")
        # #region debug-point H3-H5:asr-stop
        _debug_report(
            "H3-H5",
            "后端服务/语音转写_实时.py:stop",
            "realtime ASR stopped",
            {
                "text_len": len(result["text"]),
                "segment_count": len(result["segments"]),
                "feed_count": self._debug_feed_count,
                "recv_count": self._debug_recv_count,
                "error": self._error,
            },
        )
        # #endregion
        return result

    def get_realtime_text(self) -> dict:
        """
        获取当前累积的实时转写文本（线程安全）

        返回：
            dict：{"text": "当前累积文本", "segments": [...], "is_final": bool}
        """
        with self._lock:
            return {
                "text": self._accumulated_text,
                "segments": list(self._segments),
                "is_final": not self._running,
                "log_id": self._log_id,
                "error": self._error
            }

    def get_speaker_labeled_text(self) -> dict:
        """
        获取带说话人标签的文本（延迟标注模式）

        返回：
            dict：{
                "labeled": "已标注说话人的稳定区文本",
                "raw": "最新未标注的原文区文本",
                "has_labeled": bool
            }
        """
        with self._diarization_lock:
            return {
                "labeled": self._labeled_text,
                "raw": self._raw_tail,
                "has_labeled": bool(self._labeled_text)
            }

    # ============================================================
    # 后台 asyncio 事件循环
    # ============================================================

    def _run_event_loop(self):
        """在后台线程中运行 asyncio 事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._websocket_task())
        except Exception as e:
            logger.error(f"实时 ASR 事件循环异常：{e}")
            with self._lock:
                self._error = str(e)
        finally:
            self._loop.close()

    async def _websocket_task(self):
        """WebSocket 连接和音频发送主任务"""
        import websockets

        headers = {
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        if self.auth_mode == "new":
            headers["X-Api-Key"] = self.api_key
        else:
            headers["X-Api-App-Key"] = self.app_key
            headers["X-Api-Access-Key"] = self.access_key

        try:
            async with websockets.connect(
                BIGMODEL_WS_URL,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
                ssl=_ssl_context
            ) as ws:
                self._ws = ws
                logger.info("实时 ASR WebSocket 连接成功")

                # 发送 full client request
                await self._send_full_request(ws)

                # 接收首响应确认
                try:
                    first_resp = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    first_result = parse_response(first_resp)
                    if first_result["type"] == "error":
                        raise RuntimeError(f"服务端拒绝：{first_result['payload']}")
                    # 提取 log_id
                    payload = first_result["payload"]
                    self._log_id = (
                        payload.get("result", {})
                        .get("additions", {})
                        .get("log_id", "")
                    )
                except asyncio.TimeoutError:
                    pass

                # 启动接收任务
                recv_task = asyncio.create_task(self._receive_results(ws))

                # 发送音频数据
                seq = 0
                self._last_diarization_time = time.time()
                while self._running or not self._audio_queue.empty():
                    try:
                        chunk = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: self._audio_queue.get(timeout=0.1)
                        )
                    except queue.Empty:
                        continue

                    if chunk is None:
                        break

                    seq += 1
                    audio_msg = build_message(
                        chunk,
                        message_type=MSG_AUDIO_ONLY_REQUEST,
                        flags=FLAG_NO_SEQUENCE,
                        serialization=SERIALIZATION_NONE,
                        compression=COMPRESSION_NONE
                    )
                    await ws.send(audio_msg)

                    # 累积音频用于延迟声纹标注
                    self._audio_buffer.append(chunk)

                    # 周期性声纹标注（专用线程池，不阻塞 ASR）
                    await self._maybe_run_diarization()

                    # #region debug-point H3:send-loop
                    if seq % 50 == 0:
                        _debug_report(
                            "H3",
                            "后端服务/语音转写_实时.py:_websocket_main",
                            "audio packet sent",
                            {
                                "sent_seq": seq,
                                "chunk_bytes": len(chunk),
                                "queue_size": self._audio_queue.qsize(),
                                "audio_buffer_chunks": len(self._audio_buffer),
                            },
                        )
                    # #endregion

                    # 录音回调本身已经按 100ms 产出音频块，这里不能再额外 sleep，
                    # 否则处理开销会让队列逐步积压，造成实时转写体感延迟。
                    await asyncio.sleep(0)

                # 发送最后一包
                last_msg = build_message(
                    b"",
                    message_type=MSG_AUDIO_ONLY_REQUEST,
                    flags=FLAG_LAST_NO_SEQ,
                    serialization=SERIALIZATION_NONE,
                    compression=COMPRESSION_NONE
                )
                await ws.send(last_msg)
                logger.info(f"实时 ASR 音频发送完成，共 {seq} 包")

                # 等待接收任务完成
                await asyncio.wait_for(recv_task, timeout=15.0)

        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"实时 ASR WebSocket 关闭：code={e.code}, reason={e.reason}")
            # #region debug-point H3:connection-closed
            _debug_report(
                "H3",
                "后端服务/语音转写_实时.py:_websocket_main",
                "ASR websocket closed",
                {
                    "code": e.code,
                    "reason": e.reason,
                    "feed_count": self._debug_feed_count,
                    "recv_count": self._debug_recv_count,
                    "text_len": len(self._accumulated_text),
                },
            )
            # #endregion
        except Exception as e:
            logger.error(f"实时 ASR WebSocket 异常：{e}")
            with self._lock:
                self._error = str(e)
            # #region debug-point H3:connection-error
            _debug_report(
                "H3",
                "后端服务/语音转写_实时.py:_websocket_main",
                "ASR websocket exception",
                {"error": str(e), "feed_count": self._debug_feed_count, "recv_count": self._debug_recv_count},
            )
            # #endregion

    async def _send_full_request(self, ws):
        """发送 full client request"""
        request_params = {
            "user": {"uid": "huake-energy-storage"},
            "audio": {
                "format": "pcm",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
                "language": "zh-CN"
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": True,
                "show_utterances": True
            }
        }
        request_json = json.dumps(request_params, ensure_ascii=False)
        request_msg = build_message(
            request_json.encode("utf-8"),
            message_type=MSG_FULL_CLIENT_REQUEST,
            flags=FLAG_NO_SEQUENCE,
            serialization=SERIALIZATION_JSON
        )
        await ws.send(request_msg)
        logger.info("实时 ASR 已发送 full client request")

    async def _receive_results(self, ws):
        """接收识别结果（后台任务）"""
        while True:
            try:
                response_data = await asyncio.wait_for(ws.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                continue

            result = parse_response(response_data)

            if result["type"] == "response":
                self._debug_recv_count += 1
                payload = result["payload"]
                utterances = payload.get("result", [])
                if isinstance(utterances, dict):
                    utterances = [utterances]

                for utterance in utterances:
                    if isinstance(utterance, dict):
                        text = utterance.get("text", "").strip()
                        is_definite = utterance.get("definite", False)
                        if text:
                            with self._lock:
                                # 双向流式模式：definite=true 的分句是最终结果
                                # 累积所有 definite 分句 + 当前非 definite 分句
                                if is_definite:
                                    # 检查是否已存在
                                    existing_texts = {s["text"] for s in self._segments}
                                    if text not in existing_texts:
                                        self._segments.append({
                                            "text": text,
                                            "definite": True
                                        })
                                # 重建累积文本
                                definite_texts = [s["text"] for s in self._segments]
                                current_text = text if not is_definite else ""
                                self._accumulated_text = "".join(definite_texts) + current_text
                                # #region debug-point H5:receive-result
                                if self._debug_recv_count <= 5 or self._debug_recv_count % 10 == 0:
                                    _debug_report(
                                        "H5",
                                        "后端服务/语音转写_实时.py:_receive_results",
                                        "ASR result received",
                                        {
                                            "recv_count": self._debug_recv_count,
                                            "text_len": len(text),
                                            "is_definite": is_definite,
                                            "segment_count": len(self._segments),
                                            "accumulated_len": len(self._accumulated_text),
                                            "sample_text": text[:40],
                                        },
                                    )
                                # #endregion

            elif result["type"] == "error":
                error_msg = result["payload"].get("message", "未知错误")
                logger.error(f"实时 ASR 服务端错误：{error_msg}")
                with self._lock:
                    self._error = error_msg
                break

    # ============================================================
    # 延迟声纹标注
    # ============================================================

    async def _maybe_run_diarization(self):
        """录音中不做声纹标注，确保长录音实时 ASR 优先稳定"""
        return

    def _run_diarization(self):
        """执行声纹标注（在独立线程中运行）"""
        try:
            debug_start = time.time()
            import io
            import wave
            import re

            # 延迟加载声纹引擎
            if self._diarization_engine is None:
                from 说话人分离 import get_speaker_diarization_engine
                self._diarization_engine = get_speaker_diarization_engine()

            # 将累积的 PCM 数据写入内存 WAV
            pcm_data = b"".join(self._audio_buffer)
            if len(pcm_data) < 16000 * 2:  # 至少 1 秒
                return

            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm_data)
            wav_buffer.seek(0)

            # 运行说话人分离
            segments = self._diarization_engine.diarize_from_buffer(wav_buffer)
            if not segments:
                return

            # 获取当前累积文本
            with self._lock:
                full_text = self._accumulated_text

            if not full_text:
                return

            # 合并：全量文本 + 全量声纹分段 → 带标签文本
            labeled = self._diarization_engine.merge_with_transcript(
                segments, full_text
            )

            # 提取原文区：最后 2~3 句（约 5 秒的语音量）
            sentences = re.split(r'[。！？；\n]+', full_text)
            sentences = [s.strip() for s in sentences if s.strip()]
            tail_count = min(3, max(1, len(sentences) // 3))
            raw_tail = "。".join(sentences[-tail_count:]) if len(sentences) > tail_count else ""

            # 去重：如果 raw_tail 已经包含在 labeled 末尾，清空
            if raw_tail:
                # 去掉标签后比较
                clean_labeled = re.sub(r'\[发言人 [A-Z]\]\s*', '', labeled)
                if raw_tail in clean_labeled[-len(raw_tail):]:
                    raw_tail = ""

            with self._diarization_lock:
                self._labeled_text = labeled
                self._raw_tail = raw_tail

            logger.debug(
                f"声纹标注完成：labeled={len(labeled)}字符, "
                f"raw={len(raw_tail)}字符"
            )
            # #region debug-point H3-H4:diarization-done
            _debug_report(
                "H3-H4",
                "后端服务/语音转写_实时.py:_run_diarization",
                "diarization finished",
                {
                    "duration_ms": round((time.time() - debug_start) * 1000, 1),
                    "pcm_bytes": len(pcm_data),
                    "segments": len(segments),
                    "full_text_len": len(full_text),
                    "labeled_len": len(labeled),
                    "raw_tail_len": len(raw_tail),
                },
            )
            # #endregion

        except Exception as e:
            logger.warning(f"延迟声纹标注失败：{e}")
            # #region debug-point H3-H4:diarization-error
            _debug_report(
                "H3-H4",
                "后端服务/语音转写_实时.py:_run_diarization",
                "diarization failed",
                {"error": str(e)},
            )
            # #endregion


def get_realtime_asr_engine() -> RealtimeASREngine:
    """获取全局唯一的实时 ASR 引擎实例"""
    return RealtimeASREngine()
