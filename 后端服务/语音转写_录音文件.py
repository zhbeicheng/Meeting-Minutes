# ============================================================
# 文件名：语音转写_录音文件.py
# 功能：基于火山引擎豆包语音识别大模型的录音文件识别引擎（HTTP）
# 参考：火山引擎文档 https://www.volcengine.com/docs/6561/1354868
#
# 技术原理：
#   使用 HTTP POST 提交音频链接 → 轮询查询识别结果
#   相比流式 WebSocket 方案更简单，适合长音频（最长 6 小时）
#   两步流程：提交任务 → 轮询结果
#
# 认证方式（旧版控制台）：
#   Header: X-Api-App-Key + X-Api-Access-Key + X-Api-Resource-Id
# 认证方式（新版控制台）：
#   Header: X-Api-Key + X-Api-Resource-Id
#
# 接口地址：
#   提交任务：POST https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit
#   查询结果：POST https://openspeech.bytedance.com/api/v3/auc/bigmodel/query
# ============================================================

import os
import json
import time
import uuid
import logging
import asyncio
from typing import Optional

# ============================================================
# 配置日志
# ============================================================
logger = logging.getLogger("file_asr")


# ============================================================
# FileASREngine — 录音文件识别引擎（HTTP 提交+轮询）
# ============================================================

class FileASREngine:
    """
    录音文件识别引擎（火山引擎豆包大模型）
    
    使用 HTTP POST 提交音频链接，轮询获取识别结果
    适合长音频场景（最长 6 小时），准确率最高
    
    使用示例：
        engine = FileASREngine()
        result = await engine.transcribe("https://example.com/audio.mp3")
        print(result["text"])
    
    注意：
        此引擎需要音频文件的公网可访问 URL
        如果只有本地文件，请使用流式引擎（语音转写_流式.py）
    """

    # ============================================================
    # 类变量：单例实例
    # ============================================================
    _instance: Optional["FileASREngine"] = None

    def __new__(cls, *args, **kwargs):
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化录音文件识别引擎，从密钥文件读取配置"""
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

            from 火山密钥_录音文件 import (
                API_KEY, APP_KEY, ACCESS_KEY, RESOURCE_ID,
                SUBMIT_URL, QUERY_URL
            )
            self.api_key = API_KEY
            self.app_key = APP_KEY
            self.access_key = ACCESS_KEY
            self.resource_id = RESOURCE_ID
            self.submit_url = SUBMIT_URL
            self.query_url = QUERY_URL
        except ImportError:
            # 密钥文件不存在，回退到环境变量
            logger.warning("未找到 配置与模板/火山密钥_录音文件.py，回退到环境变量")
            self.api_key = _os.getenv("ASR_API_KEY", "")
            self.app_key = _os.getenv("ASR_APP_KEY", "")
            self.access_key = _os.getenv("ASR_ACCESS_KEY", "")
            self.resource_id = _os.getenv("ASR_RESOURCE_ID", "volc.bigasr.auc")
            self.submit_url = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
            self.query_url = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"

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
                "录音文件 ASR 未配置，请在 配置与模板/火山密钥_录音文件.py 中填入凭证"
            )

        self._initialized = True

    def get_status(self) -> dict:
        """获取引擎状态"""
        return {
            "configured": self.configured,
            "auth_mode": self.auth_mode or "not_configured",
            "resource_id": self.resource_id,
            "provider": "火山引擎-豆包录音文件识别大模型"
        }

    # ============================================================
    # 构建认证头
    # ============================================================
    def _build_headers(self, task_id: str) -> dict:
        """
        构建 HTTP 请求头（含认证信息）
        
        参数：
            task_id：任务 ID（UUID）
        
        返回：
            dict：HTTP 请求头
        """
        headers = {
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": task_id,
        }

        if self.auth_mode == "new":
            # 新版控制台：只需 X-Api-Key
            headers["X-Api-Key"] = self.api_key
        else:
            # 旧版控制台：需要 App-Key + Access-Key
            headers["X-Api-App-Key"] = self.app_key
            headers["X-Api-Access-Key"] = self.access_key

        return headers

    # ============================================================
    # 提交识别任务
    # ============================================================
    async def _submit_task(self, audio_url: str, audio_format: str = "mp3") -> str:
        """
        提交录音文件识别任务
        
        参数：
            audio_url：音频文件的公网可访问 URL
            audio_format：音频格式（wav/mp3/ogg/raw）
        
        返回：
            str：任务 ID
        
        异常：
            RuntimeError：提交失败
        """
        import httpx

        task_id = str(uuid.uuid4())
        headers = self._build_headers(task_id)

        # ============================================================
        # 构建请求体
        # ============================================================
        request_body = {
            "user": {
                "uid": "huake-energy-storage"
            },
            "audio": {
                "url": audio_url,
                "format": audio_format,
                "rate": 16000,
                "bits": 16,
                "channel": 1
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,         # 文本规范化
                "enable_punc": True,        # 启用标点
                "enable_ddc": True,         # 启用顺滑
                "show_utterances": True     # 输出分句信息
            }
        }

        logger.info(f"提交录音文件识别任务：{audio_url[:80]}...")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.submit_url,
                    headers=headers,
                    json=request_body
                )

                # 检查响应状态
                status_code = response.headers.get("X-Api-Status-Code", "")
                status_msg = response.headers.get("X-Api-Message", "")

                if status_code == "20000000":
                    logger.info(f"任务提交成功：task_id={task_id}")
                    return task_id
                else:
                    error_msg = f"提交失败：code={status_code}, msg={status_msg}"
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)

        except httpx.HTTPError as e:
            raise RuntimeError(f"HTTP 请求失败：{e}")

    # ============================================================
    # 查询识别结果
    # ============================================================
    async def _query_result(self, task_id: str) -> dict:
        """
        查询录音文件识别结果
        
        参数：
            task_id：提交任务时返回的任务 ID
        
        返回：
            dict：识别结果
        
        异常：
            RuntimeError：查询失败
        """
        import httpx

        headers = self._build_headers(task_id)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.query_url,
                    headers=headers,
                    json={}  # 空 JSON body
                )

                status_code = response.headers.get("X-Api-Status-Code", "")
                status_msg = response.headers.get("X-Api-Message", "")

                if status_code == "20000000":
                    # 识别完成
                    result = response.json()
                    return {"status": "completed", "result": result}

                elif status_code == "20000001":
                    # 正在处理中
                    return {"status": "processing"}

                elif status_code == "20000002":
                    # 任务在队列中
                    return {"status": "queued"}

                elif status_code == "20000003":
                    # 静音音频
                    return {"status": "completed", "result": {"text": ""}}

                else:
                    error_msg = f"查询失败：code={status_code}, msg={status_msg}"
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)

        except httpx.HTTPError as e:
            raise RuntimeError(f"HTTP 请求失败：{e}")

    # ============================================================
    # 核心转写方法
    # ============================================================
    async def transcribe(
        self,
        audio_url: str,
        audio_format: str = None,
        poll_interval: float = 2.0,
        max_wait: float = 300.0
    ) -> dict:
        """
        使用录音文件识别引擎转写音频
        
        流程：
        1. 提交识别任务（POST submit）
        2. 轮询查询结果（POST query）
        3. 解析并返回结构化结果
        
        参数：
            audio_url：音频文件的公网可访问 URL
            audio_format：音频格式（wav/mp3/ogg），默认从 URL 后缀推断
            poll_interval：轮询间隔（秒），默认 2 秒
            max_wait：最大等待时间（秒），默认 300 秒（5 分钟）
        
        返回：
            dict：{
                "text": "转写后的完整文本",
                "segments": [...],
                "engine": "cloud_file"
            }
        """
        if not self.configured:
            raise RuntimeError(
                "录音文件 ASR 引擎未配置。请在 配置与模板/环境配置.env 中设置：\n"
                "  新版控制台：ASR_API_KEY=你的API Key\n"
                "  旧版控制台：ASR_APP_KEY + ASR_ACCESS_KEY"
            )

        # ============================================================
        # 第 1 步：推断音频格式
        # ============================================================
        if audio_format is None:
            # 从 URL 后缀推断格式
            url_lower = audio_url.lower()
            if ".wav" in url_lower:
                audio_format = "wav"
            elif ".mp3" in url_lower:
                audio_format = "mp3"
            elif ".ogg" in url_lower:
                audio_format = "ogg"
            else:
                audio_format = "mp3"  # 默认

        # ============================================================
        # 第 2 步：提交任务
        # ============================================================
        logger.info(f"录音文件 ASR 开始转写：{os.path.basename(audio_url)}")
        task_id = await self._submit_task(audio_url, audio_format)

        # ============================================================
        # 第 3 步：轮询查询结果
        # ============================================================
        start_time = time.time()

        while True:
            elapsed = time.time() - start_time

            if elapsed > max_wait:
                raise RuntimeError(
                    f"识别超时：已等待 {elapsed:.0f} 秒，超过最大等待时间 {max_wait:.0f} 秒"
                )

            query_result = await self._query_result(task_id)

            if query_result["status"] == "completed":
                # 识别完成，解析结果
                result_data = query_result["result"]
                full_text = result_data.get("result", {}).get("text", "")

                # 提取分句信息
                utterances = result_data.get("result", {}).get("utterances", [])
                segment_list = []
                for utterance in utterances:
                    text = utterance.get("text", "").strip()
                    if text:
                        segment_list.append({
                            "text": text,
                            "start_time": utterance.get("start_time", 0),
                            "end_time": utterance.get("end_time", 0),
                            "definite": utterance.get("definite", False)
                        })

                audio_duration = result_data.get("audio_info", {}).get("duration", 0)

                logger.info(
                    f"录音文件 ASR 转写完成："
                    f"文本 {len(full_text)} 字符，"
                    f"分句 {len(segment_list)} 个，"
                    f"耗时 {elapsed:.1f} 秒"
                )

                return {
                    "text": full_text,
                    "segments": segment_list,
                    "engine": "cloud_file",
                    "duration": audio_duration,
                    "task_id": task_id
                }

            elif query_result["status"] in ("processing", "queued"):
                # 仍在处理中，等待后重试
                logger.debug(
                    f"任务 {task_id} 状态：{query_result['status']}，"
                    f"已等待 {elapsed:.0f} 秒"
                )
                await asyncio.sleep(poll_interval)

            else:
                raise RuntimeError(f"未知的查询状态：{query_result['status']}")


# ============================================================
# 便捷函数
# ============================================================
def get_file_asr_engine() -> FileASREngine:
    """获取全局唯一的录音文件 ASR 引擎实例"""
    return FileASREngine()