# ============================================================
# 文件名：纪要生成.py
# 功能：基于 LLM 的会议纪要生成引擎
# 参考：ReverendBayes/MinutesAI 的 Prompt 链式调用设计
#
# 技术原理：
#   将转写文本 + 会议模板 Prompt 发送给火山方舟 LLM API
#   LLM 根据 Prompt 指令将口语化转写文本整理为结构化 Markdown 纪要
#   支持 4 套会议模板：部门例会、项目评审、客户沟通、领导汇报
# ============================================================

import os
import sys
import json
import asyncio
import logging
from typing import Optional

# ============================================================
# 配置日志
# ============================================================
logger = logging.getLogger("minutes_generator")

# ============================================================
# 加载 LLM 配置（密钥文件优先，环境变量备选）
# ============================================================
def _load_llm_config():
    """
    加载 LLM API 配置
    
    优先级：
    1. 配置与模板/火山密钥_LLM.py（推荐）
    2. 环境变量（.env 文件）
    """
    try:
        _config_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "配置与模板"
        )
        if _config_dir not in sys.path:
            sys.path.insert(0, _config_dir)

        from 火山密钥_LLM import API_BASE, API_KEY, MODEL
        if API_KEY:
            return {
                "api_base": API_BASE,
                "api_key": API_KEY,
                "model": MODEL
            }
    except ImportError:
        pass

    # 回退到环境变量
    return {
        "api_base": os.getenv("LLM_API_BASE", "https://ark.cn-beijing.volces.com/api/v3"),
        "api_key": os.getenv("LLM_API_KEY", ""),
        "model": os.getenv("LLM_MODEL", "doubao-pro-32k")
    }


# ============================================================
# 加载 Prompt 模板
# ============================================================
def _load_prompt_templates():
    """
    加载会议纪要 Prompt 模板
    
    模板文件位置：配置与模板/Prompt配置.json
    如果文件不存在，使用内置默认模板
    """
    try:
        _config_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "配置与模板"
        )
        prompt_file = os.path.join(_config_dir, "Prompt配置.json")
        if os.path.exists(prompt_file):
            with open(prompt_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"加载 Prompt 模板文件失败：{e}，使用内置默认模板")

    # ============================================================
    # 内置默认模板（4 套）
    # ============================================================
    return {
        "部门例会": {
            "name": "部门例会",
            "system_prompt": """你是一位专业的会议纪要秘书，擅长将会议录音转写文本整理为结构化的会议纪要。

## 输出格式要求
请严格按照以下 Markdown 格式输出：

# {会议主题}

**会议日期**：{日期}
**参会人员**：{从文本中提取}

## 一、会议要点
1. 要点一
2. 要点二

## 二、决议事项
1. 决议一
2. 决议二

## 三、待办任务
| 任务 | 负责人 | 截止时间 |
|------|--------|----------|
| ... | ... | ... |

## 四、下次会议计划
- 时间：
- 议题：

## 注意事项
- 使用正式、专业的语言风格
- 保留原文中的项目名称、技术参数、人名等关键信息
- 去除口语化的语气词和重复内容
- 待办任务必须明确责任人和时间节点
- 如果原文中未提及某项内容，标注"（未提及）"即可，不要编造"""
        },
        "项目评审": {
            "name": "项目评审",
            "system_prompt": """你是一位专业的项目评审会议纪要秘书，擅长整理技术评审会议的要点和决策。

## 输出格式要求
请严格按照以下 Markdown 格式输出：

# {项目名称} — 项目评审会议纪要

**会议日期**：{日期}
**参会人员**：{从文本中提取}
**评审项目**：{从文本中提取}

## 一、项目概况
- 项目背景：
- 技术路线：
- 当前阶段：

## 二、评审要点
1. 技术方案评审意见
2. 进度计划评审意见
3. 资源配置评审意见

## 三、评审结论
- 总体评价：
- 主要风险：
- 改进建议：

## 四、决议事项
1. 决议一
2. 决议二

## 五、待办任务
| 任务 | 负责人 | 截止时间 |
|------|--------|----------|
| ... | ... | ... |

## 注意事项
- 保留技术参数、产品名称、项目编号等关键信息
- 评审意见要客观、具体，避免模糊表述
- 风险和建议要可操作、可追踪"""
        },
        "客户沟通": {
            "name": "客户沟通",
            "system_prompt": """你是一位专业的客户沟通会议纪要秘书，擅长整理商务技术交流会议的要点。

## 输出格式要求
请严格按照以下 Markdown 格式输出：

# {客户名称} — 沟通会议纪要

**会议日期**：{日期}
**参会人员**：我方：{提取} / 客户方：{提取}
**沟通主题**：{从文本中提取}

## 一、客户需求
1. 需求一
2. 需求二

## 二、我方方案介绍
1. 方案要点一
2. 方案要点二

## 三、讨论要点
1. 讨论点一
2. 讨论点二

## 四、后续跟进
| 事项 | 负责人 | 截止时间 |
|------|--------|----------|
| ... | ... | ... |

## 五、下次沟通计划
- 时间：
- 议题：

## 注意事项
- 客户名称、项目名称、技术参数保持原样
- 客户关注的问题要重点标注
- 后续跟进事项必须明确责任人和时间"""
        },
        "领导汇报": {
            "name": "领导汇报",
            "system_prompt": """你是一位专业的汇报会议纪要秘书，擅长整理向领导汇报工作的会议要点。

## 输出格式要求
请严格按照以下 Markdown 格式输出：

# {汇报主题} — 汇报会议纪要

**会议日期**：{日期}
**汇报人**：{从文本中提取}
**参会领导**：{从文本中提取}

## 一、汇报要点
1. 要点一
2. 要点二

## 二、领导指示
1. 指示一
2. 指示二

## 三、讨论事项
1. 事项一
2. 事项二

## 四、落实事项
| 事项 | 责任部门 | 截止时间 |
|------|----------|----------|
| ... | ... | ... |

## 注意事项
- 领导指示要准确记录，不得曲解或遗漏
- 落实事项必须明确责任部门和完成时限
- 使用正式、简洁的语言风格"""
        }
    }


# ============================================================
# MinutesGenerator — 会议纪要生成器
# ============================================================

class MinutesGenerator:
    """
    会议纪要生成器
    
    使用火山方舟 LLM API（兼容 OpenAI 格式）将转写文本整理为结构化纪要
    支持 4 套会议模板，可根据场景选择
    
    使用示例：
        generator = MinutesGenerator()
        minutes = await generator.generate("转写文本...", "部门例会")
        print(minutes)
    """

    # ============================================================
    # 类变量：单例实例
    # ============================================================
    _instance: Optional["MinutesGenerator"] = None

    def __new__(cls, *args, **kwargs):
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化纪要生成器，加载 LLM 配置和 Prompt 模板"""
        if hasattr(self, "_initialized") and self._initialized:
            return

        # 加载 LLM 配置
        config = _load_llm_config()
        self.api_base = config["api_base"]
        self.api_key = config["api_key"]
        self.model = config["model"]

        # 检查配置是否完整
        self.configured = bool(self.api_key and self.api_key != "your_api_key_here")

        if not self.configured:
            logger.warning(
                "LLM API 未配置，请在 配置与模板/火山密钥_LLM.py 中填入 API_KEY"
            )

        # 加载 Prompt 模板
        self.templates = _load_prompt_templates()

        self._initialized = True

    def get_status(self) -> dict:
        """获取引擎状态"""
        return {
            "configured": self.configured,
            "model": self.model,
            "api_base": self.api_base,
            "templates": [
                key for key, value in self.templates.items()
                if isinstance(value, dict) and "name" in value
            ],
            "provider": "火山方舟-豆包大模型"
        }

    def get_templates(self) -> list:
        """获取所有可用的会议模板列表"""
        return [
            {"id": key, "name": value["name"]}
            for key, value in self.templates.items()
            if isinstance(value, dict) and "name" in value
        ]

    # ============================================================
    # 核心方法：生成会议纪要
    # ============================================================
    async def generate(
        self,
        transcript: str,
        template: str = "部门例会",
        meeting_title: str = "",
        meeting_date: str = ""
    ) -> dict:
        """
        生成会议纪要
        
        流程：
        1. 校验参数（转写文本、模板类型）
        2. 加载对应模板的 System Prompt
        3. 构建 messages（system + user）
        4. 调用 LLM API（异步，不阻塞事件循环）
        5. 解析返回结果
        6. 返回结构化纪要
        
        参数：
            transcript：转写后的纯文本（必填）
            template：模板类型，可选：部门例会/项目评审/客户沟通/领导汇报
            meeting_title：会议主题（可选，LLM 会从文本中提取）
            meeting_date：会议日期（可选，LLM 会从文本中提取）
        
        返回：
            dict：{
                "markdown": "Markdown 格式的结构化纪要",
                "template": "部门例会",
                "model": "doubao-pro-32k"
            }
        
        异常：
            ValueError：参数校验失败
            RuntimeError：API 调用失败
        """
        # ============================================================
        # 第 1 步：参数校验
        # ============================================================
        if not transcript or not transcript.strip():
            raise ValueError("转写文本不能为空，请先完成语音转写")

        if template not in self.templates:
            available = "、".join(
                key for key, value in self.templates.items()
                if isinstance(value, dict) and "name" in value
            )
            raise ValueError(
                f"不支持的模板类型：{template}\n"
                f"可选模板：{available}"
            )

        if not self.configured:
            raise RuntimeError(
                "LLM API 未配置。请在 配置与模板/火山密钥_LLM.py 中填入 API_KEY\n"
                "获取路径：https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey"
            )

        # ============================================================
        # 第 2 步：构建 System Prompt
        # ============================================================
        template_config = self.templates[template]
        system_prompt = template_config["system_prompt"]

        # 替换占位符
        if meeting_title:
            system_prompt = system_prompt.replace("{会议主题}", meeting_title)
            system_prompt = system_prompt.replace("{项目名称}", meeting_title)
            system_prompt = system_prompt.replace("{客户名称}", meeting_title)
            system_prompt = system_prompt.replace("{汇报主题}", meeting_title)
        if meeting_date:
            system_prompt = system_prompt.replace("{日期}", meeting_date)

        # ============================================================
        # 第 3 步：构建 messages
        # ============================================================
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"请根据以下会议转写文本生成会议纪要：\n\n{transcript}"}
        ]

        # ============================================================
        # 第 4 步：调用 LLM API
        # ============================================================
        import httpx

        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,   # 低温度保证格式稳定
            "max_tokens": 2000    # 足够生成完整纪要
        }

        logger.info(f"开始生成纪要：模板={template}，文本长度={len(transcript)} 字符")

        # 重试配置：最多 3 次，指数退避
        max_retries = 3
        last_error = None

        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(url, headers=headers, json=body)

                    if response.status_code == 200:
                        result = response.json()
                        markdown = result["choices"][0]["message"]["content"]

                        logger.info(
                            f"纪要生成完成：模板={template}，"
                            f"输出长度={len(markdown)} 字符"
                            + (f"（重试 {attempt} 次后成功）" if attempt > 0 else "")
                        )

                        return {
                            "markdown": markdown,
                            "template": template,
                            "model": self.model
                        }

                    # 503/429 等可重试错误
                    if response.status_code in (503, 429, 502):
                        error_detail = response.text[:200]
                        logger.warning(
                            f"LLM API 返回 {response.status_code}（第 {attempt + 1}/{max_retries} 次尝试）：{error_detail}"
                        )
                        if attempt < max_retries - 1:
                            wait_time = 2 ** attempt  # 1s, 2s, 4s
                            await asyncio.sleep(wait_time)
                            continue

                    # 不可重试的错误
                    error_detail = response.text[:500]
                    logger.error(f"LLM API 返回错误：{response.status_code} - {error_detail}")
                    raise RuntimeError(
                        f"LLM API 调用失败（{response.status_code}）\n"
                        f"详情：{error_detail}"
                    )

            except httpx.TimeoutException:
                logger.warning(f"LLM API 超时（第 {attempt + 1}/{max_retries} 次尝试）")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise RuntimeError("LLM API 调用超时（90 秒），已重试 3 次仍失败，请稍后重试")

        # 所有重试都失败
        raise RuntimeError(
            f"LLM API 调用失败，已重试 {max_retries} 次\n"
            f"最后错误：{last_error or '未知'}"
        )


# ============================================================
# 便捷函数
# ============================================================
def get_minutes_generator() -> MinutesGenerator:
    """获取全局唯一的纪要生成器实例"""
    return MinutesGenerator()