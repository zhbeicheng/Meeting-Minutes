# ============================================================
# 文件名：语音转写_流式.py
# 功能：基于火山引擎豆包语音识别大模型的流式 ASR 引擎
# 参考：火山引擎文档 https://www.volcengine.com/docs/6561/1354869
#
# 技术原理：
#   使用 WebSocket 二进制协议连接火山引擎语音识别服务
#   支持流式输入模式（bigmodel_nostream），准确率更高
#   与本地 faster-whisper 引擎互为补充，前端可切换
#
# 认证方式（旧版控制台）：
#   Header: X-Api-App-Key + X-Api-Access-Key + X-Api-Resource-Id
# 认证方式（新版控制台）：
#   Header: X-Api-Key + X-Api-Resource-Id
#
# 协议说明：
#   - 4 字节二进制头 + 4 字节 payload 长度 + payload
#   - 大端字节序
#   - 支持 JSON 序列化 + Gzip 压缩
# ============================================================

import os
import json
import struct
import time
import uuid
import logging
import asyncio
import shutil
import tempfile
import wave
from typing import Optional
import ssl as _ssl

# 创建 SSL 上下文（兼容企业网络环境）
_ssl_context = _ssl.create_default_context()
_ssl_context.check_hostname = False
_ssl_context.verify_mode = _ssl.CERT_NONE

# ============================================================
# 配置日志
# ============================================================
logger = logging.getLogger("cloud_asr")

# 比赛演示通常是 1~3 分钟录音，45 秒能保留较完整语义上下文。
# 如果某个分片被云端断开，再自动降级为更小分片兜底。
DEFAULT_CHUNK_SECONDS = 45.0
MIN_FALLBACK_CHUNK_SECONDS = 20.0

# ============================================================
# WebSocket 二进制协议常量
# ============================================================
# 协议版本和头大小
PROTOCOL_VERSION = 0b0001  # 版本 1
HEADER_SIZE = 0b0001       # header = 4 字节 (1 x 4)

# 消息类型
MSG_FULL_CLIENT_REQUEST = 0b0001   # 客户端发送：包含请求参数的完整请求
MSG_AUDIO_ONLY_REQUEST = 0b0010    # 客户端发送：仅包含音频数据
MSG_FULL_SERVER_RESPONSE = 0b1001  # 服务端返回：包含识别结果
MSG_ERROR = 0b1111                 # 服务端返回：错误信息

# 消息类型特定标志
FLAG_NO_SEQUENCE = 0b0000          # header 后无序列号
FLAG_POS_SEQUENCE = 0b0001         # header 后有正序列号
FLAG_NEG_SEQUENCE = 0b0011         # header 后有负序列号（最后一包）
FLAG_LAST_NO_SEQ = 0b0010          # 最后一包，无序列号

# 序列化方法
SERIALIZATION_NONE = 0b0000
SERIALIZATION_JSON = 0b0001

# 压缩方法
COMPRESSION_NONE = 0b0000
COMPRESSION_GZIP = 0b0001


def build_header(
    message_type: int,
    flags: int = FLAG_NO_SEQUENCE,
    serialization: int = SERIALIZATION_JSON,
    compression: int = COMPRESSION_NONE
) -> bytes:
    """
    构建 WebSocket 二进制协议的 4 字节 header
    
    字节布局（大端）：
    Byte 0: [Protocol(4bit) | HeaderSize(4bit)]
    Byte 1: [MessageType(4bit) | Flags(4bit)]
    Byte 2: [Serialization(4bit) | Compression(4bit)]
    Byte 3: [Reserved(8bit)]
    
    参数：
        message_type：消息类型（1=full_request, 2=audio_only, 9=response, 15=error）
        flags：消息类型特定标志
        serialization：序列化方式（0=none, 1=JSON）
        compression：压缩方式（0=none, 1=Gzip）
    
    返回：
        4 字节的二进制 header
    """
    byte0 = (PROTOCOL_VERSION << 4) | HEADER_SIZE
    byte1 = (message_type << 4) | flags
    byte2 = (serialization << 4) | compression
    byte3 = 0x00  # Reserved

    return struct.pack(">BBBB", byte0, byte1, byte2, byte3)


def build_message(
    payload: bytes,
    message_type: int = MSG_FULL_CLIENT_REQUEST,
    flags: int = FLAG_NO_SEQUENCE,
    serialization: int = SERIALIZATION_JSON,
    compression: int = COMPRESSION_NONE
) -> bytes:
    """
    构建完整的 WebSocket 二进制消息
    
    消息格式：
    [4字节 Header] [4字节 PayloadSize(大端)] [Payload]
    
    参数：
        payload：消息负载（JSON 字符串或音频数据）
        message_type：消息类型
        flags：消息类型标志
        serialization：序列化方式
        compression：压缩方式
    
    返回：
        完整的二进制消息帧
    """
    header = build_header(message_type, flags, serialization, compression)
    # Payload 长度使用大端 32 位无符号整数
    payload_size = struct.pack(">I", len(payload))
    return header + payload_size + payload


def parse_response(data: bytes) -> dict:
    """
    解析服务端返回的二进制消息
    
    服务端返回格式（文档规定）：
    Header(4) + Sequence(4) + PayloadSize(4) + Payload
    
    返回格式：
    {
        "type": "response" | "error",
        "payload": {...}  # JSON 解析后的字典
    }
    """
    if len(data) < 12:
        return {"type": "error", "payload": {"message": f"消息太短（{len(data)} 字节）"}}

    # 解析 header
    header = data[:4]
    byte0, byte1, byte2, byte3 = struct.unpack(">BBBB", header)

    message_type = (byte1 >> 4) & 0x0F
    flags = byte1 & 0x0F
    serialization = (byte2 >> 4) & 0x0F
    compression = byte2 & 0x0F

    # 解析 sequence number（4 字节，大端有符号整数）
    sequence = struct.unpack(">i", data[4:8])[0]

    # 解析 payload 长度（4 字节，大端无符号整数）
    payload_size = struct.unpack(">I", data[8:12])[0]
    payload = data[12:12 + payload_size]

    # 根据消息类型处理
    if message_type == MSG_FULL_SERVER_RESPONSE:
        if serialization == SERIALIZATION_JSON:
            try:
                result = json.loads(payload.decode("utf-8"))
                result["_sequence"] = sequence
                return {"type": "response", "payload": result}
            except json.JSONDecodeError:
                return {"type": "error", "payload": {"message": f"JSON 解析失败: {payload[:200]}"}}
        else:
            return {"type": "response", "payload": {"raw": payload, "_sequence": sequence}}

    elif message_type == MSG_ERROR:
        return {"type": "error", "payload": {"message": payload.decode("utf-8", errors="replace"), "_sequence": sequence}}

    else:
        return {"type": "unknown", "payload": {"message_type": message_type, "_sequence": sequence}}


# ============================================================
# CloudASREngine — 云端语音识别引擎
# ============================================================

class StreamingASREngine:
    """
    流式语音识别引擎（火山引擎豆包大模型）
    
    使用 WebSocket 流式协议连接火山引擎 ASR 服务
    支持 WAV/MP3/OGG 格式，16000Hz 采样率
    
    使用示例：
        engine = StreamingASREngine()
        result = await engine.transcribe("会议录音.wav")
        print(result["text"])
    """

    # ============================================================
    # 类变量：单例实例
    # ============================================================
    _instance: Optional["StreamingASREngine"] = None

    def __new__(cls, *args, **kwargs):
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化流式 ASR 引擎，从密钥文件读取配置"""
        if hasattr(self, "_initialized") and self._initialized:
            return

        # ============================================================
        # 从密钥文件读取配置（优先），环境变量作为备选
        # ============================================================
        try:
            import sys
            import os as _os
            # 将 配置与模板 目录加入 Python 路径
            _config_dir = _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                "配置与模板"
            )
            if _config_dir not in sys.path:
                sys.path.insert(0, _config_dir)

            from 火山密钥_流式 import (
                API_KEY, APP_KEY, ACCESS_KEY, RESOURCE_ID, WS_URL
            )
            self.api_key = API_KEY
            self.app_key = APP_KEY
            self.access_key = ACCESS_KEY
            self.resource_id = RESOURCE_ID
            self.ws_url = WS_URL
        except ImportError:
            # 密钥文件不存在，回退到环境变量
            logger.warning("未找到 配置与模板/火山密钥_流式.py，回退到环境变量")
            self.api_key = _os.getenv("ASR_API_KEY", "")
            self.app_key = _os.getenv("ASR_APP_KEY", "")
            self.access_key = _os.getenv("ASR_ACCESS_KEY", "")
            self.resource_id = _os.getenv("ASR_RESOURCE_ID", "volc.bigasr.sauc.duration")
            self.ws_url = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"

        # 判断使用哪种认证方式
        if self.api_key:
            self.auth_mode = "new"  # 新版控制台：只需 X-Api-Key
        elif self.app_key and self.access_key:
            self.auth_mode = "old"  # 旧版控制台：需要 App-Key + Access-Key
        else:
            self.auth_mode = None

        self.configured = self.auth_mode is not None

        if not self.configured:
            logger.warning(
                "流式 ASR 未配置，请在 配置与模板/火山密钥_流式.py 中填入凭证"
            )

        self._initialized = True

    def get_status(self) -> dict:
        """获取引擎状态"""
        return {
            "configured": self.configured,
            "auth_mode": self.auth_mode or "not_configured",
            "resource_id": self.resource_id,
            "provider": "火山引擎-豆包流式语音识别大模型"
        }

    # ============================================================
    # 音频格式转换（与本地引擎共用 pydub）
    # ============================================================
    def _prepare_audio(self, audio_path: str) -> bytes:
        """
        准备音频数据：读取文件并确保格式符合要求

        对于 WAV 文件，剥离 44 字节文件头，返回原始 PCM 数据
        火山引擎流式 ASR 的 bigmodel 引擎要求 PCM 格式

        参数：
            audio_path：音频文件路径

        返回：
            原始 PCM 音频数据（16kHz, 16bit, 单声道）
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"音频文件不存在：{audio_path}")

        file_ext = os.path.splitext(audio_path)[1].lower()

        # WAV 文件：剥离 44 字节文件头，返回原始 PCM
        if file_ext == ".wav":
            with open(audio_path, "rb") as f:
                data = f.read()
            # WAV 文件头通常为 44 字节，但也可能是更大的格式块
            # 查找 "data" 标记来确定 PCM 数据起始位置
            data_marker = data.find(b"data")
            if data_marker != -1:
                # "data" 标记后 4 字节是数据长度，之后是 PCM 数据
                pcm_start = data_marker + 8
                pcm_data = data[pcm_start:]
                logger.info(
                    f"WAV 文件：总大小 {len(data)} 字节，"
                    f"PCM 数据 {len(pcm_data)} 字节"
                )
                return pcm_data
            else:
                # 没找到 data 标记，尝试跳过标准 44 字节头
                logger.warning("WAV 文件未找到 data 标记，使用标准 44 字节偏移")
                return data[44:]

        # MP3/OGG 直接读取
        if file_ext in [".mp3", ".ogg"]:
            with open(audio_path, "rb") as f:
                return f.read()

        # PCM 原始数据直接读取
        if file_ext == ".pcm":
            with open(audio_path, "rb") as f:
                return f.read()

        # 其他格式尝试用 pydub 转换为 PCM
        try:
            from pydub import AudioSegment
            audio = AudioSegment.from_file(audio_path)
            audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
            return audio.raw_data
        except ImportError:
            raise RuntimeError("音频格式转换需要 pydub 库：pip install pydub")

        except Exception as e:
            raise RuntimeError(f"音频格式转换失败：{e}")

    async def transcribe(self, audio_path: str) -> dict:
        """
        使用云端 ASR 引擎转写音频。

        对本地长 WAV 录音自动分片，避免单次 WebSocket 会话承载过长音频。
        """
        if audio_path.lower().endswith(".wav"):
            duration = self._get_wav_duration(audio_path)
            if duration and duration > DEFAULT_CHUNK_SECONDS:
                return await self._transcribe_long_wav(audio_path, duration)
            if duration and duration > MIN_FALLBACK_CHUNK_SECONDS:
                try:
                    return await self._transcribe_single(audio_path)
                except Exception as e:
                    logger.warning(
                        f"短音频直接转写失败，启用兜底分片："
                        f"{duration:.1f}s，原因：{e}"
                    )
                    return await self._transcribe_long_wav(
                        audio_path,
                        duration,
                        chunk_seconds=max(duration / 2, MIN_FALLBACK_CHUNK_SECONDS)
                    )

        return await self._transcribe_single(audio_path)

    def _get_wav_duration(self, audio_path: str) -> float:
        """读取 WAV 时长，失败时返回 0。"""
        try:
            with wave.open(audio_path, "rb") as wav_file:
                frame_rate = wav_file.getframerate()
                if frame_rate <= 0:
                    return 0.0
                return wav_file.getnframes() / frame_rate
        except Exception:
            return 0.0

    def _split_wav_file(
        self,
        audio_path: str,
        chunk_seconds: float = DEFAULT_CHUNK_SECONDS
    ) -> tuple[list[str], str]:
        """把长 WAV 切成多个短 WAV，返回分片路径和临时目录。"""
        temp_dir = tempfile.mkdtemp(prefix="huake_asr_chunks_")
        chunk_paths = []

        with wave.open(audio_path, "rb") as source:
            params = source.getparams()
            frame_rate = source.getframerate()
            total_frames = source.getnframes()
            frames_per_chunk = max(1, int(chunk_seconds * frame_rate))

            start_frame = 0
            chunk_index = 0
            while start_frame < total_frames:
                source.setpos(start_frame)
                frames_to_read = min(frames_per_chunk, total_frames - start_frame)
                frames = source.readframes(frames_to_read)

                chunk_path = os.path.join(temp_dir, f"chunk_{chunk_index:04d}.wav")
                with wave.open(chunk_path, "wb") as target:
                    target.setparams(params)
                    target.writeframes(frames)

                chunk_paths.append(chunk_path)
                start_frame += frames_to_read
                chunk_index += 1

        return chunk_paths, temp_dir

    async def _transcribe_long_wav(
        self,
        audio_path: str,
        duration: float,
        chunk_seconds: float = DEFAULT_CHUNK_SECONDS
    ) -> dict:
        """长 WAV 分片转写后合并文本。"""
        chunk_paths, temp_dir = self._split_wav_file(audio_path, chunk_seconds)
        logger.info(
            f"长音频启用分片转写：{duration:.1f}s，"
            f"{len(chunk_paths)} 个分片，"
            f"每片约 {chunk_seconds:.1f}s"
        )

        text_parts = []
        all_segments = []
        try:
            for index, chunk_path in enumerate(chunk_paths, start=1):
                logger.info(
                    f"正在转写分片 {index}/{len(chunk_paths)}："
                    f"{os.path.basename(chunk_path)}"
                )
                result = await self._transcribe_chunk_with_fallback(
                    chunk_path,
                    chunk_seconds,
                    str(index)
                )
                text = result.get("text", "")
                if text:
                    text_parts.append(text)

                for seg in result.get("segments", []):
                    item = dict(seg)
                    item["chunk_index"] = index - 1
                    all_segments.append(item)

            return {
                "text": "".join(text_parts),
                "segments": all_segments,
                "engine": "cloud_streaming_chunked",
                "duration": round(duration, 1),
                "chunk_count": len(chunk_paths),
                "chunk_seconds": chunk_seconds
            }

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def _transcribe_chunk_with_fallback(
        self,
        chunk_path: str,
        chunk_seconds: float,
        chunk_label: str
    ) -> dict:
        """优先按大块转写，失败时自动拆小块兜底。"""
        try:
            return await self._transcribe_single(chunk_path)
        except Exception as e:
            next_chunk_seconds = chunk_seconds / 2
            duration = self._get_wav_duration(chunk_path)
            if (
                next_chunk_seconds < MIN_FALLBACK_CHUNK_SECONDS
                or duration <= MIN_FALLBACK_CHUNK_SECONDS
            ):
                raise

            logger.warning(
                f"分片 {chunk_label} 转写失败，降级拆分为 "
                f"{next_chunk_seconds:.1f}s 小分片：{e}"
            )

            sub_paths, sub_temp_dir = self._split_wav_file(
                chunk_path,
                next_chunk_seconds
            )
            text_parts = []
            all_segments = []
            try:
                for sub_index, sub_path in enumerate(sub_paths, start=1):
                    sub_result = await self._transcribe_chunk_with_fallback(
                        sub_path,
                        next_chunk_seconds,
                        f"{chunk_label}.{sub_index}"
                    )
                    text = sub_result.get("text", "")
                    if text:
                        text_parts.append(text)
                    all_segments.extend(sub_result.get("segments", []))

                return {
                    "text": "".join(text_parts),
                    "segments": all_segments,
                    "engine": "cloud_streaming_chunked_fallback"
                }
            finally:
                shutil.rmtree(sub_temp_dir, ignore_errors=True)

    # ============================================================
    # 核心转写方法
    # ============================================================
    async def _transcribe_single(self, audio_path: str) -> dict:
        """
        使用云端 ASR 引擎转写音频
        
        流程：
        1. 检查配置是否完整
        2. 读取并准备音频数据
        3. 建立 WebSocket 连接
        4. 发送 full client request（配置参数）
        5. 分片发送音频数据
        6. 发送最后一包（负包）
        7. 接收并合并识别结果
        8. 返回结构化结果
        
        参数：
            audio_path：音频文件路径
        
        返回：
            dict：{
                "text": "转写后的完整文本",
                "segments": [...],
                "engine": "cloud_volcengine"
            }
        """
        if not self.configured:
            raise RuntimeError(
                "云端 ASR 引擎未配置。请在 配置与模板/环境配置.env 中设置：\n"
                "  ASR_APP_KEY=你的APP ID\n"
                "  ASR_ACCESS_KEY=你的Access Token"
            )

        # ============================================================
        # 第 1 步：准备音频数据
        # ============================================================
        logger.info(f"云端 ASR 开始转写：{os.path.basename(audio_path)}")

        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"音频文件不存在：{audio_path}")

        file_ext = os.path.splitext(audio_path)[1].lower().lstrip(".")

        # WAV 文件：剥离文件头后发送裸 PCM。
        # 火山流式接口按音频块持续接收数据，分片发送完整 WAV 文件容易让服务端
        # 在中途解析到非连续容器数据而异常断开。
        if file_ext == "wav":
            audio_data = self._prepare_audio(audio_path)
            audio_format = "pcm"
            logger.info(
                f"WAV 文件转为 PCM 发送：{len(audio_data)} 字节"
            )
        else:
            audio_data = self._prepare_audio(audio_path)
            audio_format = file_ext

        # ============================================================
        # 第 2 步：建立 WebSocket 连接
        # ============================================================
        # 使用流式输入模式（bigmodel_nostream），准确率更高
        ws_url = self.ws_url

        # 构建鉴权头（支持新旧两版控制台）
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
            import websockets
        except ImportError:
            raise RuntimeError(
                "云端 ASR 需要 websockets 库：pip install websockets"
            )

        full_text_parts = []
        segment_list = []
        log_id = None

        try:
            async with websockets.connect(
                ws_url,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
                ssl=_ssl_context
            ) as ws:
                logger.info("WebSocket 连接成功")

                # ============================================================
                # 第 3 步：发送 full client request
                # ============================================================
                request_params = {
                    "user": {
                        "uid": "huake-energy-storage"
                    },
                    "audio": {
                        "format": audio_format,
                        "rate": 16000,
                        "bits": 16,
                        "channel": 1,
                        "language": "zh-CN"
                    },
                    "request": {
                        "model_name": "bigmodel",
                        "enable_itn": True,       # 文本规范化（数字、日期等）
                        "enable_punc": True,      # 启用标点
                        "enable_ddc": True,       # 启用顺滑（去除语气词）
                        "show_utterances": True   # 输出分句信息
                    }
                }

                # 序列化为 JSON 并构建消息
                request_json = json.dumps(request_params, ensure_ascii=False)
                request_msg = build_message(
                    request_json.encode("utf-8"),
                    message_type=MSG_FULL_CLIENT_REQUEST,
                    flags=FLAG_NO_SEQUENCE,
                    serialization=SERIALIZATION_JSON
                )
                await ws.send(request_msg)
                logger.info("已发送 full client request")

                # 等待服务端对 full client request 的响应（nostream 模式首响应为确认包）
                try:
                    first_response = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    first_result = parse_response(first_response)
                    if first_result["type"] == "error":
                        raise RuntimeError(f"服务端拒绝请求: {first_result['payload']}")
                except asyncio.TimeoutError:
                    logger.info("服务端未对 full client request 单独响应（正常）")

                # ============================================================
                # 第 4 步：分片发送音频数据
                # ============================================================
                # 每包约 200ms 的音频数据
                # 16000Hz × 16bit × 1ch = 32000 字节/秒
                # 200ms = 6400 字节/包
                chunk_size = 6400
                total_chunks = (len(audio_data) + chunk_size - 1) // chunk_size

                for i in range(0, len(audio_data), chunk_size):
                    chunk = audio_data[i:i + chunk_size]
                    seq = (i // chunk_size) + 1

                    # 构建 audio only request
                    # 格式：Header(4) + PayloadSize(4) + Payload
                    audio_msg = build_message(
                        chunk,
                        message_type=MSG_AUDIO_ONLY_REQUEST,
                        flags=FLAG_NO_SEQUENCE,
                        serialization=SERIALIZATION_NONE,
                        compression=COMPRESSION_NONE
                    )

                    await ws.send(audio_msg)

                    # 文件最终转写不是实时麦克风流，不能按实时速度慢发。
                    # 长录音若上传时间过长，服务端可能在完整音频发完前关闭连接。
                    await asyncio.sleep(0.01)

                logger.info(f"音频数据发送完成，共 {total_chunks} 包")

                # ============================================================
                # 第 5 步：发送最后一包（标志位指示结束）
                # ============================================================
                last_msg = build_message(
                    b"",
                    message_type=MSG_AUDIO_ONLY_REQUEST,
                    flags=FLAG_LAST_NO_SEQ,
                    serialization=SERIALIZATION_NONE,
                    compression=COMPRESSION_NONE
                )
                await ws.send(last_msg)
                logger.info("已发送最后一包（结束标志）")

                # 等待服务端处理并返回结果（nostream 模式需要处理时间）
                await asyncio.sleep(0.5)

                # ============================================================
                # 第 6 步：接收识别结果
                # ============================================================
                while True:
                    try:
                        response_data = await asyncio.wait_for(
                            ws.recv(), timeout=30.0
                        )
                    except asyncio.TimeoutError:
                        logger.warning("等待识别结果超时")
                        break

                    result = parse_response(response_data)

                    if result["type"] == "response":
                        payload = result["payload"]

                        # 记录 log_id 用于排查问题
                        if log_id is None:
                            log_id = (
                                payload.get("result", {})
                                .get("additions", {})
                                .get("log_id", "")
                            )

                        # 提取识别文本。
                        # bigmodel_nostream 会多次返回“当前完整结果”的中间版本，
                        # 这里用最新一版覆盖，避免把中间结果反复追加造成重复文本。
                        utterances = payload.get("result", [])
                        if isinstance(utterances, dict):
                            utterances = [utterances]
                        current_text_parts = []
                        current_segment_list = []
                        for utterance in utterances:
                            if isinstance(utterance, dict):
                                text = utterance.get("text", "").strip()
                                if text:
                                    current_text_parts.append(text)
                                    current_segment_list.append({
                                        "text": text,
                                        "definite": utterance.get("definite", False)
                                    })
                        if current_text_parts:
                            full_text_parts = current_text_parts
                            segment_list = current_segment_list

                        # 检查是否所有分句都已返回（definite=true）
                        all_definite = all(
                            u.get("definite", False) for u in utterances
                        ) if utterances else False

                        if all_definite and utterances:
                            logger.info("所有分句已返回，识别完成")
                            break

                    elif result["type"] == "error":
                        error_msg = result["payload"].get("message", "未知错误")
                        logger.error(f"服务端返回错误：{error_msg}")
                        raise RuntimeError(f"云端 ASR 识别失败：{error_msg}")

        except websockets.exceptions.ConnectionClosed as e:
            logger.info(
                f"WebSocket 连接关闭：code={e.code}, reason={e.reason}"
            )
            # code 1000 是正常关闭（服务端处理完成），不是错误
            if e.code == 1000:
                if full_text_parts:
                    logger.info("连接正常关闭，已收到识别结果")
                else:
                    logger.warning("连接正常关闭，但未收到识别结果（可能音频无有效语音内容）")
            else:
                raise RuntimeError(
                    f"云端语音识别连接异常关闭（code={e.code}）："
                    f"{e.reason or '未知原因'}"
                )
        except Exception as e:
            logger.error(f"云端 ASR 转写失败：{e}")
            raise RuntimeError(f"云端语音识别失败：{e}")

        # ============================================================
        # 第 7 步：合并结果
        # ============================================================
        full_text = "".join(full_text_parts)

        logger.info(
            f"云端 ASR 转写完成："
            f"文本 {len(full_text)} 字符，"
            f"分句 {len(segment_list)} 个"
        )

        return {
            "text": full_text,
            "segments": segment_list,
            "engine": "cloud_streaming",
            "log_id": log_id
        }


# ============================================================
# 便捷函数
# ============================================================
def get_streaming_asr_engine() -> StreamingASREngine:
    """获取全局唯一的流式 ASR 引擎实例"""
    return StreamingASREngine()
