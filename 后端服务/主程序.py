# ============================================================
# 文件名：主程序.py
# 功能：FastAPI 应用主入口 — 路由注册、CORS 配置、静态文件服务
# 说明：这是整个后端服务的核心文件，所有 API 路由在此注册
# 参考：zhanymkanov/fastapi-best-practices（GitHub 12k+ Stars）
# ============================================================

import os
import json
import uuid
import time
import threading
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

# ============================================================
# FastAPI 核心依赖
# ============================================================
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ============================================================
# 导入业务模块
# ============================================================
from 后端服务.语音转写 import get_stt_engine, transcribe_audio
from 后端服务.语音转写_流式 import get_streaming_asr_engine
from 后端服务.语音转写_录音文件 import get_file_asr_engine
from 后端服务.纪要生成 import get_minutes_generator
from 后端服务.说话人分离 import get_speaker_diarization_engine

# ============================================================
# 加载环境变量
# ============================================================
# 尝试从 配置与模板/环境配置.env 加载
# 如果文件不存在，使用系统环境变量
try:
    from dotenv import load_dotenv
    env_path = os.path.join("配置与模板", "环境配置.env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
        print("✅ 已加载环境配置文件：配置与模板/环境配置.env")
    else:
        print("⚠️  未找到环境配置文件，使用系统环境变量")
except ImportError:
    print("⚠️  python-dotenv 未安装，使用系统环境变量")

# ============================================================
# 创建 FastAPI 应用实例
# ============================================================
app = FastAPI(
    title="华科储能智能会议助手",
    description="面向储能行业从业者的本地化智能会议效率工具 — API 服务",
    version="1.0.0",
    docs_url="/docs",       # Swagger UI 文档地址
    redoc_url="/redoc"      # ReDoc 文档地址
)

# ============================================================
# 配置 CORS（跨域资源共享）
# ============================================================
# 允许前端页面（浏览器）跨域访问后端 API
# 开发阶段允许所有来源，生产环境应限制为具体域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],            # 允许所有来源
    allow_credentials=True,
    allow_methods=["*"],            # 允许所有 HTTP 方法
    allow_headers=["*"],            # 允许所有请求头
)

# ============================================================
# 配置静态文件服务
# ============================================================
# 将 前端界面/ 目录中的静态资源（CSS/JS）挂载到 /static 路径
# 前端页面通过根路径 "/" 的专用路由提供
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "前端界面")
frontend_dir = os.path.abspath(frontend_dir)

if os.path.exists(frontend_dir):
    # 挂载静态资源目录（CSS、JS 等）
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


# ============================================================
# 根路径路由：提供前端主页面
# ============================================================
@app.get("/")
async def serve_frontend():
    """
    提供前端主页面
    
    返回 前端界面/界面.html 的内容
    阶段 3 完成后，此页面将包含完整的左右分屏界面
    """
    index_path = os.path.join(frontend_dir, "界面.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse(
        {"message": "前端界面尚未创建，将在阶段 3 完成"},
        status_code=200
    )

# ============================================================
# 全局状态管理
# ============================================================
# 转写任务状态存储（内存中，重启后清空）
# 结构：{task_id: {"status": "pending|processing|completed|failed", "text": "...", "error": "..."}}
stt_tasks = {}

# ============================================================
# Pydantic 请求模型定义
# ============================================================

class MinutesRequest(BaseModel):
    """纪要生成请求"""
    text: str               # 转写后的文本内容
    template: str = "部门例会"  # 会议模板类型
    task_id: str = ""       # 可选：关联语音转写任务，便于保存音频路径

class TranslateRequest(BaseModel):
    """翻译请求"""
    text: str               # 待翻译的文本
    direction: str = "zh2en"  # 翻译方向：zh2en（中→英）或 en2zh（英→中）

class WeeklyRequest(BaseModel):
    """周报生成请求"""
    records: list           # 本周纪要列表 [{"title": "...", "date": "...", "minutes": "..."}]

class ExportRequest(BaseModel):
    """文档导出请求"""
    markdown: str           # Markdown 格式的纪要/周报内容
    title: str              # 文档标题
    doc_type: str = "minutes"  # 文档类型：minutes（纪要）或 weekly（周报）

class SaveRecordRequest(BaseModel):
    """历史记录保存请求"""
    title: str = ""
    template: str = "部门例会"
    transcript: str = ""
    minutes: str = ""
    task_id: str = ""

# ============================================================
# 本地历史记录与导出目录
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RECORDS_DIR = PROJECT_ROOT / "历史记录"
EXPORTS_DIR = PROJECT_ROOT / "导出文件"
RECORDS_DIR.mkdir(exist_ok=True)
EXPORTS_DIR.mkdir(exist_ok=True)

def _safe_filename(name: str, default: str = "会议纪要") -> str:
    """生成适合本地保存的安全文件名"""
    cleaned = re.sub(r'[\\/:*?"<>|\\s]+', "_", (name or default).strip())
    return cleaned[:60] or default

def _record_path(record_id: str) -> Path:
    """获取单条历史记录路径"""
    return RECORDS_DIR / f"{record_id}.json"

def _extract_title(markdown: str, fallback: str = "未命名会议") -> str:
    """从 Markdown 标题中提取会议标题"""
    for line in (markdown or "").splitlines():
        if line.startswith("# "):
            return line.replace("#", "", 1).strip() or fallback
    return fallback

def _save_meeting_record(
    title: str,
    template: str,
    transcript: str,
    minutes: str,
    task_id: str = ""
) -> dict:
    """保存会议历史记录，便于后续查询、周报汇总和导出"""
    now = datetime.now()
    record_id = f"meeting_{now.strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:4]}"
    task = stt_tasks.get(task_id, {}) if task_id else {}
    record = {
        "id": record_id,
        "title": title or _extract_title(minutes),
        "template": template,
        "transcript": transcript,
        "minutes": minutes,
        "task_id": task_id,
        "audio_path": task.get("file_path", ""),
        "created_at": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "word_path": ""
    }
    with open(_record_path(record_id), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return record

def _list_meeting_records() -> list:
    """读取本地历史记录列表，按时间倒序返回摘要"""
    records = []
    for path in RECORDS_DIR.glob("meeting_*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                record = json.load(f)
            records.append({
                "id": record.get("id"),
                "title": record.get("title", "未命名会议"),
                "template": record.get("template", ""),
                "date": record.get("date", ""),
                "created_at": record.get("created_at", ""),
                "audio_path": record.get("audio_path", ""),
                "transcript_len": len(record.get("transcript", "")),
                "minutes_len": len(record.get("minutes", "")),
            })
        except Exception:
            continue
    return sorted(records, key=lambda item: item.get("created_at", ""), reverse=True)

async def _call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 2400) -> str:
    """复用豆包 LLM 配置，供纪要、周报和翻译等文本任务调用"""
    import httpx

    generator = get_minutes_generator()
    if not generator.configured:
        raise RuntimeError("LLM API 未配置，请先配置 配置与模板/火山密钥_LLM.py")

    url = f"{generator.api_base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {generator.api_key}",
        "Content-Type": "application/json"
    }
    body = {
        "model": generator.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.25,
        "max_tokens": max_tokens
    }

    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(url, headers=headers, json=body)
    if response.status_code != 200:
        raise RuntimeError(f"LLM 调用失败（{response.status_code}）：{response.text[:300]}")
    result = response.json()
    return result["choices"][0]["message"]["content"]

# ============================================================
# API 路由：系统状态
# ============================================================

@app.get("/api/status")
async def get_status():
    """
    获取系统运行状态
    
    返回：
    - model_status：Whisper 模型加载状态
    - api_status：LLM API 连接状态
    - local_model_status：本地 Ollama 模型状态（预留）
    - disk_usage：磁盘剩余空间
    """
    # 检查磁盘空间
    import shutil
    disk_usage = shutil.disk_usage(".")
    disk_free_gb = round(disk_usage.free / (1024 ** 3), 1)

    # 检查 LLM API 配置
    api_key = os.getenv("LLM_API_KEY", "")
    api_configured = bool(api_key and api_key != "your_api_key_here")

    # 检查 STT 引擎状态
    try:
        engine = get_stt_engine()
        engine_status = engine.get_status()
        model_status = "loaded" if engine_status["loaded"] else "not_loaded"
    except Exception:
        model_status = "error"

    # 检查云端 ASR 引擎状态（流式 + 录音文件）
    try:
        streaming_engine = get_streaming_asr_engine()
        streaming_status = streaming_engine.get_status()
    except Exception:
        streaming_status = {"configured": False}

    try:
        file_engine = get_file_asr_engine()
        file_status = file_engine.get_status()
    except Exception:
        file_status = {"configured": False}

    # 检查纪要生成引擎状态
    try:
        minutes_gen = get_minutes_generator()
        minutes_status = minutes_gen.get_status()
    except Exception:
        minutes_status = {"configured": False, "templates": []}

    return {
        "model_status": model_status,
        "model_size": os.getenv("WHISPER_MODEL_SIZE", "small"),
        "api_status": "connected" if api_configured else "not_configured",
        "minutes_configured": minutes_status.get("configured", False),
        "minutes_model": minutes_status.get("model", ""),
        "cloud_streaming_configured": streaming_status.get("configured", False),
        "cloud_file_configured": file_status.get("configured", False),
        "local_model_status": "not_deployed",
        "disk_free_gb": disk_free_gb,
        "server_time": datetime.now().isoformat()
    }

# ============================================================
# API 路由：音频上传与转写（阶段 1 实现核心逻辑）
# ============================================================

@app.post("/api/stt")
async def upload_audio(
    file: UploadFile = File(...),
    engine: str = "local"
):
    """
    上传音频文件，触发语音转写任务
    
    流程：
    1. 接收音频文件（WAV/MP3/M4A/WebM）
    2. 保存到 录音缓存/ 目录
    3. 生成唯一 task_id
    4. 根据 engine 参数选择本地或云端引擎
    5. 在后台线程中启动转写任务（不阻塞 HTTP 响应）
    6. 立即返回 task_id 供前端轮询
    
    参数：
        file：音频文件（FormData 格式）
        engine：引擎类型
            - "local"：本地 Whisper（默认，完全离线）
            - "cloud_streaming"：火山引擎流式 ASR（WebSocket，适合本地文件）
            - "cloud_file"：火山引擎录音文件 ASR（HTTP，需音频 URL）
    
    返回：
        {"task_id": "xxx", "status": "pending", "filename": "xxx", "engine": "local"}
    """
    # 生成唯一任务 ID（取 UUID 前 8 位，便于显示）
    task_id = str(uuid.uuid4())[:8]

    # 保存音频文件到缓存目录
    cache_dir = os.path.join(os.path.dirname(__file__), "..", "录音缓存")
    os.makedirs(cache_dir, exist_ok=True)

    file_ext = os.path.splitext(file.filename or "audio.wav")[1] or ".wav"
    save_path = os.path.join(cache_dir, f"{task_id}{file_ext}")

    content = await file.read()
    with open(save_path, "wb") as f:
        f.write(content)

    # 记录任务状态（初始为 pending）
    stt_tasks[task_id] = {
        "status": "pending",
        "filename": file.filename,
        "file_path": save_path,
        "engine": engine,
        "text": None,
        "segments": None,
        "speaker_text": None,
        "diarization_segments": None,
        "error": None,
        "created_at": time.time()
    }

    # ============================================================
    # 在后台线程中启动转写任务
    # ============================================================
    def run_transcription():
        try:
            # 更新状态为处理中
            stt_tasks[task_id]["status"] = "processing"

            # 根据引擎类型选择转写方式
            if engine in ("cloud_streaming", "cloud_file"):
                # 云端引擎：需要异步调用，在线程中创建新的事件循环
                import asyncio as _asyncio
                loop = _asyncio.new_event_loop()
                _asyncio.set_event_loop(loop)
                try:
                    result = loop.run_until_complete(
                        transcribe_audio(save_path, engine_type=engine)
                    )
                finally:
                    loop.close()
            else:
                # 本地引擎
                import asyncio as _asyncio
                loop = _asyncio.new_event_loop()
                _asyncio.set_event_loop(loop)
                try:
                    result = loop.run_until_complete(
                        transcribe_audio(save_path, engine_type="local")
                    )
                finally:
                    loop.close()

            # 转写成功，保存结果
            speaker_text = ""
            diarization_segments = []
            try:
                diarization = get_speaker_diarization_engine()
                diarization_segments = diarization.diarize(save_path)
                if diarization_segments and result.get("text"):
                    speaker_text = diarization.merge_with_transcript(
                        diarization_segments,
                        result["text"],
                        result.get("segments", [])
                    )
            except Exception as diarization_error:
                print(f"⚠️ 说话人分离失败：{diarization_error}")

            stt_tasks[task_id]["status"] = "completed"
            stt_tasks[task_id]["text"] = result["text"]
            stt_tasks[task_id]["segments"] = result.get("segments", [])
            stt_tasks[task_id]["speaker_text"] = speaker_text
            stt_tasks[task_id]["diarization_segments"] = diarization_segments
            stt_tasks[task_id]["engine"] = result.get("engine", engine)

        except Exception as e:
            # 转写失败，记录错误信息
            stt_tasks[task_id]["status"] = "failed"
            stt_tasks[task_id]["error"] = str(e)

    # 启动后台线程
    thread = threading.Thread(target=run_transcription, daemon=True)
    thread.start()

    return {
        "task_id": task_id,
        "status": "pending",
        "filename": file.filename,
        "engine": engine
    }

@app.get("/api/stt/{task_id}")
async def get_stt_result(task_id: str):
    """
    查询语音转写任务的状态和结果
    
    前端轮询策略：
    - 每 1 秒查询一次
    - status 为 "completed" 时停止轮询，展示转写文本
    - status 为 "failed" 时显示错误信息
    
    参数：
        task_id：任务 ID
    
    返回：
        {"task_id": "xxx", "status": "pending|processing|completed|failed", "text": "...", "error": "..."}
    """
    task = stt_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")

    return {
        "task_id": task_id,
        "status": task["status"],
        "text": task.get("text"),
        "speaker_text": task.get("speaker_text"),
        "diarization_segments": task.get("diarization_segments"),
        "error": task.get("error")
    }

# ============================================================
# API 路由：纪要生成（阶段 2 实现核心逻辑）
# ============================================================

@app.post("/api/minutes")
async def generate_minutes(request: MinutesRequest):
    """
    根据转写文本生成结构化会议纪要
    
    流程：
    1. 接收转写文本和模板类型
    2. 加载对应模板的 System Prompt
    3. 调用 LLM API 生成纪要
    4. 返回 Markdown 格式的纪要
    
    参数：
        text：转写后的纯文本
        template：会议模板类型（部门例会/项目评审/客户沟通/领导汇报）
    
    返回：
        {"success": true, "markdown": "# 会议纪要\n...", "template": "部门例会", "model": "doubao-pro-32k"}
    """
    try:
        generator = get_minutes_generator()
        result = await generator.generate(
            transcript=request.text,
            template=request.template
        )
        title = _extract_title(result["markdown"], fallback=f"{request.template}纪要")
        record = _save_meeting_record(
            title=title,
            template=request.template,
            transcript=request.text,
            minutes=result["markdown"],
            task_id=request.task_id
        )
        return {
            "success": True,
            "markdown": result["markdown"],
            "template": result["template"],
            "model": result["model"],
            "record_id": record["id"]
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"纪要生成失败：{e}")

# ============================================================
# API 路由：翻译（阶段 5 实现核心逻辑）
# ============================================================

@app.post("/api/translate")
async def translate_text(request: TranslateRequest):
    """
    翻译会议纪要（中英双向）
    
    参数：
        text：待翻译的 Markdown 文本
        direction：翻译方向（zh2en / en2zh）
    
    返回：
        {"success": true, "translated": "翻译后的文本"}
    """
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="翻译文本不能为空")

    direction_label = "中文翻译为英文" if request.direction == "zh2en" else "英文翻译为中文"
    system_prompt = f"""你是一位专业的储能行业双语翻译专家，正在为华科储能会议纪要做{direction_label}。

要求：
1. 保持 Markdown 标题、列表、表格结构。
2. 储能行业术语、项目名称、技术参数要准确，数字和单位不得改写。
3. 公司名、人名、产品名首次出现时可保留原文并补充译名。
4. 语言风格正式、清晰，适合客户沟通和海外项目材料。
5. 只输出翻译后的正文，不要解释翻译过程。"""
    try:
        translated = await _call_llm(
            system_prompt=system_prompt,
            user_prompt=f"请翻译以下会议纪要：\n\n{request.text}",
            max_tokens=2600
        )
        return {"success": True, "translated": translated, "direction": request.direction}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"翻译失败：{e}")

# ============================================================
# API 路由：周报汇总（阶段 6 实现核心逻辑）
# ============================================================

@app.post("/api/weekly")
async def generate_weekly(request: WeeklyRequest):
    """
    将本周多条会议纪要聚合为结构化工作周报
    
    参数：
        records：本周纪要列表
    
    返回：
        {"success": true, "markdown": "# 工作周报\n..."}
    """
    records = request.records or []
    if not records:
        records = _list_meeting_records()[:8]
        detailed_records = []
        for item in records:
            path = _record_path(item["id"])
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    detailed_records.append(json.load(f))
        records = detailed_records

    if not records:
        raise HTTPException(status_code=400, detail="暂无可汇总的会议纪要")

    source_text = "\n\n".join(
        f"## {record.get('title', '未命名会议')}\n"
        f"日期：{record.get('date', '')}\n"
        f"模板：{record.get('template', '')}\n"
        f"{record.get('minutes') or record.get('markdown') or ''}"
        for record in records
    )

    system_prompt = """你是一位熟悉国企、央企工作汇报风格的部门周报撰写专家。

请将多份会议纪要汇总为一份高密度、业务导向的工作周报。

输出要求：
# 华科储能部门工作周报

**周期**：根据材料日期概括

## 一、本周工作概述
用 2-3 句话概括本周关键工作，不写空泛套话。

## 二、重点工作进展
按项目、客户、技术、管理等主题归类，每条包含进展、问题、影响。

## 三、风险问题与协调事项
只保留需要管理层关注或跨部门协调的事项。

## 四、下周工作计划
输出可执行计划，尽量明确责任主体和时间节点。

## 五、待办清单
用表格输出：事项、责任人/部门、截止时间、来源会议。

风格要求：正式、简洁、信息密度高，贴近人工撰写的国企部门周报。"""

    try:
        markdown = await _call_llm(
            system_prompt=system_prompt,
            user_prompt=f"以下是本周会议纪要材料，请汇总为周报：\n\n{source_text}",
            max_tokens=3200
        )
        return {"success": True, "markdown": markdown, "record_count": len(records)}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"周报生成失败：{e}")

# ============================================================
# API 路由：文档导出（阶段 7 实现核心逻辑）
# ============================================================

@app.post("/api/export")
async def export_docx(request: ExportRequest):
    """
    将 Markdown 纪要/周报导出为 Word 文档
    
    参数：
        markdown：Markdown 格式的内容
        title：文档标题
        doc_type：文档类型（minutes / weekly）
    
    返回：
        .docx 文件（浏览器自动下载）
    """
    try:
        from docx import Document
        from docx.shared import Pt
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError:
        raise HTTPException(status_code=500, detail="python-docx 未安装，无法导出 Word")

    doc = Document()
    styles = doc.styles
    styles["Normal"].font.name = "宋体"
    styles["Normal"].font.size = Pt(11)

    title = request.title or _extract_title(request.markdown)
    heading = doc.add_heading(title, level=0)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    for raw_line in (request.markdown or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            doc.add_heading(line.replace("## ", "", 1), level=1)
        elif line.startswith("### "):
            doc.add_heading(line.replace("### ", "", 1), level=2)
        elif line.startswith("- "):
            doc.add_paragraph(line[2:], style="List Bullet")
        elif re.match(r"^\d+\.\s+", line):
            doc.add_paragraph(re.sub(r"^\d+\.\s+", "", line), style="List Number")
        elif line.startswith("|") and line.endswith("|"):
            # 表格先按普通文本输出，保证导出稳定；后续可增强为真实 Word 表格。
            doc.add_paragraph(line)
        else:
            doc.add_paragraph(line.replace("**", ""))

    filename = f"{_safe_filename(title)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
    output_path = EXPORTS_DIR / filename
    doc.save(output_path)

    return FileResponse(
        output_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename
    )

# ============================================================
# API 路由：模板管理（阶段 8 实现核心逻辑）
# ============================================================

@app.get("/api/templates")
async def get_templates():
    """
    获取所有会议模板列表
    
    返回：
        [{"id": "部门例会", "name": "部门例会"}, ...]
    """
    try:
        generator = get_minutes_generator()
        templates = generator.get_templates()
        return {"templates": templates}
    except Exception:
        return {
            "templates": [
                {"id": "部门例会", "name": "部门例会"},
                {"id": "项目评审", "name": "项目评审"},
                {"id": "客户沟通", "name": "客户沟通"},
                {"id": "领导汇报", "name": "领导汇报"}
            ]
        }

# ============================================================
# API 路由：历史记录（阶段 7 实现核心逻辑）
# ============================================================

@app.get("/api/records")
async def get_records(week: Optional[str] = None):
    """
    获取历史会议纪要列表
    
    参数：
        week：可选，按周筛选（格式：2026-05-15）
    
    返回：
        [{"id": "xxx", "title": "...", "date": "...", ...}, ...]
    """
    records = _list_meeting_records()
    if week:
        records = [record for record in records if record.get("date", "").startswith(week)]
    return {
        "records": records
    }

@app.get("/api/records/{record_id}")
async def get_record_detail(record_id: str):
    """获取单条历史会议记录详情"""
    path = _record_path(record_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="历史记录不存在")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

@app.post("/api/records")
async def save_record(request: SaveRecordRequest):
    """手动保存当前会议记录"""
    record = _save_meeting_record(
        title=request.title or _extract_title(request.minutes),
        template=request.template,
        transcript=request.transcript,
        minutes=request.minutes,
        task_id=request.task_id
    )
    return {"success": True, "record": record}

# ============================================================
# 启动入口
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
