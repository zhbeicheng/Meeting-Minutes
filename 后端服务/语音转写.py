# ============================================================
# 文件名：语音转写.py
# 功能：基于 faster-whisper 的语音转文字引擎
# 参考：SYSTRAN/faster-whisper（GitHub 18k+ Stars）
#       https://github.com/SYSTRAN/faster-whisper
#
# 技术原理：
#   faster-whisper 是 OpenAI Whisper 的 CTranslate2 重实现版本
#   - 比原版 Whisper 快 4 倍，内存占用减少 50%
#   - 支持 int8 量化，在 CPU 上也能高效运行
#   - 支持 99 种语言，中文识别准确率 ≥ 95%
#
# 模型选择：
#   tiny   → ~150MB，速度最快，精度最低（适合实时场景）
#   small  → ~2GB，速度与精度平衡（本项目选用）
#   medium → ~5GB，精度更高，速度较慢
#   large  → ~6GB，最高精度，需要 GPU
# ============================================================

import os
import time
import logging
from pathlib import Path
from typing import Optional

# ============================================================
# 配置日志
# ============================================================
logger = logging.getLogger("stt_engine")

# ============================================================
# STTEngine — 语音转写引擎（单例模式）
# ============================================================
# 设计思路：
#   模型文件约 2GB，加载一次需要 5-10 秒
#   使用单例模式确保整个应用生命周期内只加载一次
#   所有转写请求复用同一个模型实例

class STTEngine:
    """
    语音转写引擎
    
    使用 faster-whisper 将音频文件转写为文字
    支持 WAV/MP3/M4A/WebM 等常见音频格式
    
    使用示例：
        engine = STTEngine()
        result = engine.transcribe("会议录音.wav")
        print(result["text"])
    """

    # ============================================================
    # 类变量：单例实例
    # ============================================================
    _instance: Optional["STTEngine"] = None
    _model = None  # faster-whisper 模型实例

    def __new__(cls, *args, **kwargs):
        """
        单例模式：确保全局只有一个引擎实例
        
        为什么用单例？
        - 模型文件 2GB，重复加载浪费内存和时间
        - 多个请求共享同一个模型实例，线程安全
        """
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, model_size: str = None, device: str = "cpu"):
        """
        初始化语音转写引擎
        
        参数：
            model_size：模型大小（tiny/small/medium/large）
                       默认从环境变量 WHISPER_MODEL_SIZE 读取，未设置则用 "small"
            device：推理设备（cpu/cuda）
                    默认 cpu，Mac M 系列芯片也使用 cpu（faster-whisper 对 Apple Silicon 有优化）
        
        注意：
            首次初始化时会自动下载模型文件（约 2GB），请确保网络畅通
            模型下载后缓存在 huggingface 缓存目录，后续启动无需重新下载
        """
        # 避免重复初始化
        if self._initialized:
            return

        # 从环境变量读取模型大小配置
        if model_size is None:
            model_size = os.getenv("WHISPER_MODEL_SIZE", "small")

        self.model_size = model_size
        self.device = device
        self.model_loaded = False
        self.model_load_error = None

        logger.info(f"语音转写引擎初始化：model={model_size}, device={device}")

        # 延迟加载模型（首次调用 transcribe 时才加载）
        # 这样应用启动时不会因为下载模型而阻塞
        self._initialized = True

    # ============================================================
    # 模型加载
    # ============================================================
    def _load_model(self):
        """
        加载 faster-whisper 模型
        
        加载策略：
        - 首次调用时自动下载模型（约 2GB）
        - 下载后缓存在本地，后续启动秒级加载
        - 使用 int8 量化，减少 50% 内存占用
        
        异常处理：
        - 网络问题导致下载失败 → 记录错误，返回友好提示
        - 磁盘空间不足 → 记录错误，提示清理空间
        """
        if self.model_loaded:
            return

        try:
            from faster_whisper import WhisperModel

            logger.info(f"正在加载 Whisper 模型：{self.model_size}...")
            load_start = time.time()

            # ============================================================
            # 创建 WhisperModel 实例
            # ============================================================
            # device="cpu"：使用 CPU 推理
            #   - Mac M 系列芯片上，faster-whisper 会自动使用 Apple Silicon 优化
            #   - Intel Mac 上使用 AVX 指令集加速
            # compute_type="int8"：8-bit 量化
            #   - 模型体积减半（2GB → 1GB 内存占用）
            #   - 推理速度几乎不变
            #   - 精度损失 < 1%，对中文识别影响可忽略
            # num_workers=1：单工作线程
            #   - CPU 推理时设为 1 即可，多线程反而增加调度开销
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type="int8",
                num_workers=1,
                download_root=os.getenv("WHISPER_MODEL_DIR", None)
            )

            load_time = time.time() - load_start
            self.model_loaded = True
            logger.info(f"模型加载完成，耗时 {load_time:.1f} 秒")

        except Exception as e:
            self.model_load_error = str(e)
            logger.error(f"模型加载失败：{e}")
            raise RuntimeError(
                f"Whisper 模型加载失败：{e}\n"
                f"请检查：\n"
                f"  1. 网络连接是否正常（首次需要下载约 2GB 模型文件）\n"
                f"  2. 磁盘空间是否充足（至少需要 3GB 可用空间）\n"
                f"  3. 模型名称是否正确（当前：{self.model_size}）"
            )

    # ============================================================
    # 音频格式转换
    # ============================================================
    def _convert_to_wav(self, audio_path: str) -> str:
        """
        将音频文件转换为 Whisper 所需的 WAV 格式
        
        支持的输入格式：
        - WAV（直接使用，无需转换）
        - MP3、M4A、WebM、OGG（通过 pydub 转换）
        
        转换参数：
        - 采样率：16000 Hz（Whisper 最佳输入采样率）
        - 声道数：1（单声道）
        - 位深度：16 bit
        
        参数：
            audio_path：原始音频文件路径
        
        返回：
            转换后的 WAV 文件路径（存放在 录音缓存/ 目录）
        """
        file_ext = os.path.splitext(audio_path)[1].lower()

        # WAV 文件直接返回，无需转换
        if file_ext == ".wav":
            return audio_path

        # 其他格式需要转换为 WAV
        try:
            from pydub import AudioSegment

            logger.info(f"正在转换音频格式：{file_ext} → WAV")

            # 加载音频文件
            audio = AudioSegment.from_file(audio_path)

            # 转换为单声道 16kHz WAV
            audio = audio.set_channels(1).set_frame_rate(16000)

            # 保存到缓存目录
            cache_dir = os.path.join(
                os.path.dirname(__file__), "..", "录音缓存"
            )
            os.makedirs(cache_dir, exist_ok=True)

            wav_path = os.path.join(
                cache_dir,
                f"converted_{os.path.basename(audio_path)}.wav"
            )
            audio.export(wav_path, format="wav")
            logger.info(f"音频转换完成：{wav_path}")

            return wav_path

        except ImportError:
            raise RuntimeError(
                "音频格式转换需要 pydub 库，请运行：pip install pydub"
            )
        except Exception as e:
            raise RuntimeError(f"音频格式转换失败：{e}")

    # ============================================================
    # 核心转写方法
    # ============================================================
    def transcribe(self, audio_path: str) -> dict:
        """
        将音频文件转写为文字
        
        这是语音转写引擎的核心方法，整个转写流程如下：
        
        1. 加载 Whisper 模型（首次调用时）
        2. 检查音频文件是否存在
        3. 格式转换（非 WAV 格式 → WAV）
        4. 执行转写推理
        5. 合并分段结果
        6. 返回结构化结果
        
        参数：
            audio_path：音频文件路径（支持 WAV/MP3/M4A/WebM）
        
        返回：
            dict：{
                "text": "转写后的完整文本",
                "segments": [
                    {"start": 0.0, "end": 2.5, "text": "大家好"},
                    {"start": 2.5, "end": 5.0, "text": "今天我们讨论..."},
                    ...
                ],
                "language": "zh",
                "duration": 180.5,
                "model_size": "small"
            }
        
        异常：
            FileNotFoundError：音频文件不存在
            RuntimeError：模型未加载或转写失败
        """
        # ============================================================
        # 第 1 步：确保模型已加载
        # ============================================================
        if not self.model_loaded:
            self._load_model()

        # ============================================================
        # 第 2 步：检查音频文件
        # ============================================================
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"音频文件不存在：{audio_path}")

        file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        logger.info(f"开始转写：{os.path.basename(audio_path)} ({file_size_mb:.1f} MB)")

        # ============================================================
        # 第 3 步：格式转换
        # ============================================================
        wav_path = self._convert_to_wav(audio_path)

        # ============================================================
        # 第 4 步：执行转写
        # ============================================================
        transcribe_start = time.time()

        try:
            # ----------------------------------------------------------
            # faster-whisper 转写参数说明：
            #
            # beam_size=5：
            #   束搜索宽度，控制搜索空间大小
            #   值越大越准确，但速度越慢
            #   5 是推荐值，平衡速度与精度
            #
            # language="zh"：
            #   指定语言为中文，避免语言自动检测的误差
            #   如果会议中有英文，可设为 None 让模型自动检测
            #
            # vad_filter=True：
            #   启用语音活动检测（Voice Activity Detection）
            #   自动过滤静音段和非语音段
            #   减少无效推理，提升速度
            #
            # vad_parameters=dict(min_silence_duration_ms=500)：
            #   静音判定阈值：500ms 以上的静音视为段落分隔
            #   可根据会议风格调整（正式会议可设 1000ms）
            # ----------------------------------------------------------
            segments, info = self._model.transcribe(
                wav_path,
                beam_size=5,
                language="zh",
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=500,
                    min_speech_duration_ms=250,
                )
            )

            # ============================================================
            # 第 5 步：收集转写结果
            # ============================================================
            # info 包含音频的元信息：
            #   - language：检测到的语言
            #   - language_probability：语言检测置信度
            #   - duration：音频总时长（秒）
            #   - all_language_probs：所有语言的概率分布

            full_text_parts = []
            segment_list = []

            for segment in segments:
                # segment 对象包含：
                #   - start：起始时间（秒）
                #   - end：结束时间（秒）
                #   - text：转写文本
                #   - words：词级时间戳（可选）
                #   - avg_logprob：平均对数概率（置信度指标）
                #   - no_speech_prob：非语音概率

                text = segment.text.strip()
                if text:  # 过滤空文本
                    full_text_parts.append(text)
                    segment_list.append({
                        "start": round(segment.start, 2),
                        "end": round(segment.end, 2),
                        "text": text
                    })

            full_text = "".join(full_text_parts)

            # ============================================================
            # 第 6 步：记录性能指标
            # ============================================================
            transcribe_time = time.time() - transcribe_start
            audio_duration = info.duration

            # 实时率（Real-Time Factor）= 转写耗时 / 音频时长
            # RTF < 1 表示比实时快，RTF = 0.1 表示 10 分钟音频只需 1 分钟转写
            rtf = transcribe_time / audio_duration if audio_duration > 0 else 0

            logger.info(
                f"转写完成：音频 {audio_duration:.0f}秒，"
                f"转写 {transcribe_time:.1f}秒，"
                f"RTF={rtf:.2f}，"
                f"文本 {len(full_text)} 字符"
            )

            # ============================================================
            # 第 7 步：返回结构化结果
            # ============================================================
            return {
                "text": full_text,
                "segments": segment_list,
                "language": info.language,
                "language_probability": round(info.language_probability, 4),
                "duration": round(audio_duration, 1),
                "model_size": self.model_size,
                "rtf": round(rtf, 3),
                "segment_count": len(segment_list)
            }

        except Exception as e:
            logger.error(f"转写失败：{e}")
            raise RuntimeError(f"语音转写失败：{e}")

    # ============================================================
    # 模型状态查询
    # ============================================================
    def get_status(self) -> dict:
        """
        获取引擎状态
        
        返回：
            dict：{
                "loaded": True/False,
                "model_size": "small",
                "device": "cpu",
                "error": None
            }
        """
        return {
            "loaded": self.model_loaded,
            "model_size": self.model_size,
            "device": self.device,
            "error": self.model_load_error
        }


# ============================================================
# 便捷函数：获取全局引擎实例
# ============================================================
def get_stt_engine() -> STTEngine:
    """
    获取全局唯一的语音转写引擎实例
    
    所有 API 路由通过此函数获取引擎实例，
    确保整个应用只加载一次模型
    """
    return STTEngine()


# ============================================================
# 统一转写调度函数：根据引擎类型选择本地或云端引擎
# ============================================================
async def transcribe_audio(audio_path: str, engine_type: str = "local") -> dict:
    """
    统一的音频转写入口，支持三种引擎
    
    引擎类型：
    - "local"：本地 faster-whisper 引擎（默认，完全离线，无需联网）
    - "cloud_streaming"：火山引擎流式 ASR（WebSocket，实时返回，适合短音频）
    - "cloud_file"：火山引擎录音文件 ASR（HTTP 提交+轮询，适合长音频，需音频 URL）
    
    参数：
        audio_path：音频文件路径（本地引擎）或音频 URL（录音文件引擎）
        engine_type：引擎类型
    
    返回：
        dict：{
            "text": "转写文本",
            "segments": [...],
            "engine": "local_whisper" | "cloud_streaming" | "cloud_file",
            ...
        }
    """
    if engine_type == "cloud_streaming":
        # 使用流式 ASR 引擎（WebSocket）
        from 后端服务.语音转写_流式 import get_streaming_asr_engine
        engine = get_streaming_asr_engine()
        result = await engine.transcribe(audio_path)
        result["engine"] = "cloud_streaming"
        return result

    elif engine_type == "cloud_file":
        # 使用录音文件 ASR 引擎（HTTP 提交+轮询）
        from 后端服务.语音转写_录音文件 import get_file_asr_engine
        engine = get_file_asr_engine()
        # 录音文件引擎需要音频 URL，如果传入的是本地路径则报错
        if audio_path.startswith("http://") or audio_path.startswith("https://"):
            result = await engine.transcribe(audio_path)
        else:
            raise ValueError(
                "录音文件识别引擎需要音频的公网 URL，不支持本地文件。\n"
                "请使用 'cloud_streaming'（流式引擎）处理本地文件，"
                "或先将音频上传到可公网访问的存储服务。"
            )
        result["engine"] = "cloud_file"
        return result

    else:
        # 使用本地 Whisper 引擎（默认）
        import asyncio
        engine = get_stt_engine()
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, engine.transcribe, audio_path)
        result["engine"] = "local_whisper"
        return result