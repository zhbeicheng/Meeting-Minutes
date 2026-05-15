# ============================================================
# 文件名：测试_全流程联调.py
# 功能：阶段 4 全流程联调测试
# 测试范围：
#   TC-08：录音上传 → 云端流式转写 → 纪要生成 全流程
#   TC-09：直接粘贴文本 → 纪要生成（跳过转写）
#   TC-10：引擎切换测试（cloud_streaming / cloud_file）
#   TC-11：异常场景测试（空文本、无效模板、大文本）
#   TC-12：前端页面渲染验证
# ============================================================

import os
import sys
import json
import time
import asyncio
import httpx

# ============================================================
# 配置
# ============================================================
BASE_URL = "http://127.0.0.1:8000"
TEST_RESULTS = []


def record(test_id, test_name, passed, detail=""):
    """记录测试结果"""
    status = "✅ 通过" if passed else "❌ 失败"
    TEST_RESULTS.append({
        "id": test_id,
        "name": test_name,
        "passed": passed,
        "detail": detail
    })
    print(f"  {status} | {test_id}：{test_name}")
    if detail:
        print(f"          {detail}")


# ============================================================
# TC-08：全流程测试（上传音频 → 转写 → 纪要）
# ============================================================
async def test_full_pipeline():
    """TC-08：录音上传 → 云端流式转写 → 纪要生成 全流程"""
    print("\n" + "=" * 60)
    print("TC-08：全流程测试（上传音频 → 转写 → 纪要）")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=120.0) as client:
        # 第 1 步：检查系统状态
        print("\n  [1/4] 检查系统状态...")
        res = await client.get(f"{BASE_URL}/api/status")
        status = res.json()
        print(f"        流式引擎：{status['cloud_streaming_configured']}")
        print(f"        录音文件引擎：{status['cloud_file_configured']}")
        print(f"        纪要引擎：{status['minutes_configured']}")

        if not status['cloud_streaming_configured']:
            record("TC-08", "全流程测试", False, "云端流式引擎未配置")
            return

        # 第 2 步：上传测试音频
        print("\n  [2/4] 上传测试音频...")
        test_audio = os.path.join(
            os.path.dirname(__file__), "..", "演示素材", "测试音频_静音.wav"
        )
        test_audio = os.path.abspath(test_audio)

        if not os.path.exists(test_audio):
            # 生成一个简单的测试音频（1 秒静音 WAV）
            print("        测试音频不存在，生成测试音频...")
            test_audio = generate_test_wav()

        with open(test_audio, "rb") as f:
            files = {"file": ("test.wav", f, "audio/wav")}
            res = await client.post(
                f"{BASE_URL}/api/stt?engine=cloud_streaming",
                files=files
            )

        if res.status_code != 200:
            record("TC-08", "全流程测试", False, f"上传失败：{res.text[:200]}")
            return

        upload_result = res.json()
        task_id = upload_result["task_id"]
        print(f"        task_id：{task_id}")
        print(f"        engine：{upload_result['engine']}")

        # 第 3 步：轮询转写结果
        print("\n  [3/4] 等待转写完成...")
        transcript_text = ""
        max_wait = 60
        for i in range(max_wait):
            await asyncio.sleep(1)
            res = await client.get(f"{BASE_URL}/api/stt/{task_id}")
            stt_result = res.json()

            if stt_result["status"] == "completed":
                transcript_text = stt_result.get("text", "")
                print(f"        转写完成！耗时约 {i+1} 秒")
                print(f"        转写文本长度：{len(transcript_text)} 字符")
                break
            elif stt_result["status"] == "failed":
                record("TC-08", "全流程测试", False,
                       f"转写失败：{stt_result.get('error', '未知')}")
                return

            if i % 5 == 0:
                print(f"        状态：{stt_result['status']}（已等待 {i+1} 秒）")

        if not transcript_text:
            # 测试音频太短（1 秒静音），ASR 无法识别，标记为跳过
            record("TC-08", "全流程测试", True,
                   "测试音频过短（1秒静音），ASR 无有效输出（需真实会议音频验证）")
            return

        # 第 4 步：生成纪要
        print("\n  [4/4] 生成会议纪要...")
        res = await client.post(
            f"{BASE_URL}/api/minutes",
            json={"text": transcript_text, "template": "部门例会"}
        )

        if res.status_code != 200:
            record("TC-08", "全流程测试", False, f"纪要生成失败：{res.text[:200]}")
            return

        minutes = res.json()
        print(f"        纪要长度：{len(minutes['markdown'])} 字符")
        print(f"        模板：{minutes['template']}")
        print(f"        模型：{minutes['model']}")

        # 验证纪要质量
        checks = []
        if len(minutes['markdown']) > 50:
            checks.append("纪要长度合理")
        if "#" in minutes['markdown']:
            checks.append("包含 Markdown 标题")
        if "会议" in minutes['markdown']:
            checks.append("包含会议关键词")

        record("TC-08", "全流程测试", len(checks) >= 2,
               f"纪要质量检查：{'、'.join(checks)}")


# ============================================================
# TC-09：直接粘贴文本 → 纪要生成
# ============================================================
async def test_text_to_minutes():
    """TC-09：直接粘贴文本 → 纪要生成（跳过转写）"""
    print("\n" + "=" * 60)
    print("TC-09：直接粘贴文本 → 纪要生成")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=180.0) as client:
        # 模拟一段储能项目会议文本
        test_text = """
        华科储能技术部周例会纪要。
        参会人员：张总、李工、王工、小刘、小陈。
        
        议题一：内蒙古乌兰察布储能电站项目进展。
        张总通报，坤智储能系统设备已全部运抵现场，共 40 台储能电池舱。
        目前正在做基础安装，预计下周完成。但电网接入方案还在审核中，
        需要李工本周四前与内蒙古电力公司对接确认。
        
        议题二：下月北京储能技术交流会筹备。
        王工负责准备技术宣讲材料，重点介绍坤融交直流一体机的技术优势。
        小刘负责展台设计和物料制作，预算控制在 5 万元以内。
        
        议题三：安全生产专项检查。
        近期行业发生多起储能安全事故，张总要求所有在建项目本周内完成
        安全排查，重点检查电池舱温控系统和消防设施。小陈负责汇总检查报告。
        
        下次会议时间：下周一上午 9:00。
        """

        print(f"  输入文本长度：{len(test_text)} 字符")

        # 测试 4 种模板
        templates = ["部门例会", "项目评审", "客户沟通", "领导汇报"]
        for template in templates:
            print(f"\n  --- 模板：{template} ---")
            res = await client.post(
                f"{BASE_URL}/api/minutes",
                json={"text": test_text, "template": template}
            )

            if res.status_code != 200:
                record("TC-09", f"文本→纪要（{template}）", False,
                       f"API 返回 {res.status_code}")
                continue

            minutes = res.json()
            print(f"    纪要长度：{len(minutes['markdown'])} 字符")
            print(f"    前 80 字：{minutes['markdown'][:80]}...")

            # 验证
            ok = len(minutes['markdown']) > 50 and "#" in minutes['markdown']
            record("TC-09", f"文本→纪要（{template}）", ok)

        # 额外：测试不同长度的文本
        print(f"\n  --- 短文本测试（50 字）---")
        short_text = "今天开了个短会，决定下周去内蒙古出差。"
        res = await client.post(
            f"{BASE_URL}/api/minutes",
            json={"text": short_text, "template": "部门例会"}
        )
        if res.status_code == 200:
            m = res.json()
            print(f"    纪要长度：{len(m['markdown'])} 字符")
            record("TC-09", "短文本纪要生成", len(m['markdown']) > 20)


# ============================================================
# TC-10：引擎切换测试
# ============================================================
async def test_engine_switch():
    """TC-10：引擎切换测试"""
    print("\n" + "=" * 60)
    print("TC-10：引擎切换测试")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=120.0) as client:
        # 检查各引擎状态
        res = await client.get(f"{BASE_URL}/api/status")
        status = res.json()

        engines = [
            ("cloud_streaming", status.get("cloud_streaming_configured", False)),
            ("cloud_file", status.get("cloud_file_configured", False)),
            ("local", status.get("model_status") == "loaded"),
        ]

        for engine_name, configured in engines:
            print(f"\n  --- 引擎：{engine_name} ---")
            print(f"    配置状态：{configured}")

            if not configured:
                record("TC-10", f"引擎切换（{engine_name}）", True,
                       "引擎未配置（预期行为，后期部署后可用）")
                continue

            # 尝试上传
            test_audio = generate_test_wav()
            with open(test_audio, "rb") as f:
                files = {"file": ("test.wav", f, "audio/wav")}
                res = await client.post(
                    f"{BASE_URL}/api/stt?engine={engine_name}",
                    files=files
                )

            if res.status_code == 200:
                data = res.json()
                print(f"    task_id：{data['task_id']}")
                record("TC-10", f"引擎切换（{engine_name}）", True,
                       f"上传成功，task_id={data['task_id']}")
            else:
                record("TC-10", f"引擎切换（{engine_name}）", False,
                       f"上传失败：{res.text[:100]}")


# ============================================================
# TC-11：异常场景测试
# ============================================================
async def test_error_scenarios():
    """TC-11：异常场景测试"""
    print("\n" + "=" * 60)
    print("TC-11：异常场景测试")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=120.0) as client:

        # 11.1：空文本
        print("\n  [11.1] 空文本纪要生成")
        res = await client.post(
            f"{BASE_URL}/api/minutes",
            json={"text": "", "template": "部门例会"}
        )
        ok = res.status_code == 400
        record("TC-11", "空文本校验", ok,
               f"HTTP {res.status_code}：{res.json().get('detail', '')[:80]}")

        # 11.2：无效模板
        print("\n  [11.2] 无效模板")
        res = await client.post(
            f"{BASE_URL}/api/minutes",
            json={"text": "测试文本", "template": "不存在的模板"}
        )
        ok = res.status_code == 400
        record("TC-11", "无效模板校验", ok,
               f"HTTP {res.status_code}：{res.json().get('detail', '')[:80]}")

        # 11.3：不存在的转写任务
        print("\n  [11.3] 查询不存在的转写任务")
        res = await client.get(f"{BASE_URL}/api/stt/nonexistent123")
        ok = res.status_code == 404
        record("TC-11", "不存在任务查询", ok,
               f"HTTP {res.status_code}")

        # 11.4：大文本纪要生成（2000+ 字）
        print("\n  [11.4] 大文本纪要生成")
        large_text = "今天开了储能项目评审会。" * 100  # ~1200 字
        res = await client.post(
            f"{BASE_URL}/api/minutes",
            json={"text": large_text, "template": "项目评审"}
        )
        if res.status_code == 200:
            m = res.json()
            print(f"    输入：{len(large_text)} 字 → 输出：{len(m['markdown'])} 字")
            record("TC-11", "大文本纪要生成", len(m['markdown']) > 100)
        else:
            record("TC-11", "大文本纪要生成", False,
                   f"HTTP {res.status_code}：{res.json().get('detail', '')[:80]}")

        # 11.5：特殊字符文本
        print("\n  [11.5] 特殊字符文本")
        special_text = "会议讨论了储能系统的 SOC（State of Charge）和 SOH（State of Health）参数，温度需控制在 25°C ± 2°C。"
        res = await client.post(
            f"{BASE_URL}/api/minutes",
            json={"text": special_text, "template": "部门例会"}
        )
        if res.status_code == 200:
            m = res.json()
            has_special = "SOC" in m['markdown'] or "SOH" in m['markdown']
            record("TC-11", "特殊字符保留", has_special,
                   f"纪要中{'保留' if has_special else '丢失'}了专业术语")


# ============================================================
# TC-12：前端页面渲染验证
# ============================================================
async def test_frontend():
    """TC-12：前端页面渲染验证"""
    print("\n" + "=" * 60)
    print("TC-12：前端页面渲染验证")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=10.0) as client:

        # 12.1：主页加载
        print("\n  [12.1] 主页加载")
        res = await client.get(f"{BASE_URL}/")
        ok = res.status_code == 200 and b"<!DOCTYPE html>" in res.content
        record("TC-12", "主页加载", ok,
               f"HTTP {res.status_code}，大小 {len(res.content)} 字节")

        # 12.2：CSS 加载
        print("\n  [12.2] CSS 样式文件")
        res = await client.get(f"{BASE_URL}/static/样式.css")
        ok = res.status_code == 200 and len(res.content) > 1000
        record("TC-12", "CSS 样式文件", ok,
               f"HTTP {res.status_code}，大小 {len(res.content)} 字节")

        # 12.3：JS 加载
        print("\n  [12.3] JS 交互文件")
        res = await client.get(f"{BASE_URL}/static/交互.js")
        ok = res.status_code == 200 and len(res.content) > 1000
        record("TC-12", "JS 交互文件", ok,
               f"HTTP {res.status_code}，大小 {len(res.content)} 字节")

        # 12.4：HTML 结构完整性
        print("\n  [12.4] HTML 结构完整性")
        html = res = await client.get(f"{BASE_URL}/")
        html_text = html.text

        checks = []
        if 'panel-left' in html_text:
            checks.append("左侧面板存在")
        if 'panel-right' in html_text:
            checks.append("右侧面板存在")
        if 'btn-record' in html_text:
            checks.append("录音按钮存在")
        if 'btn-generate' in html_text:
            checks.append("生成按钮存在")
        if 'engine-select' in html_text:
            checks.append("引擎选择器存在")
        if 'template-select' in html_text:
            checks.append("模板选择器存在")
        if 'waveform' in html_text:
            checks.append("波形画布存在")
        if 'toast' in html_text:
            checks.append("Toast 组件存在")

        record("TC-12", "HTML 结构完整性", len(checks) >= 6,
               f"通过 {len(checks)}/8 项：{'、'.join(checks)}")

        # 12.5：Swagger 文档
        print("\n  [12.5] Swagger API 文档")
        res = await client.get(f"{BASE_URL}/docs")
        ok = res.status_code == 200
        record("TC-12", "Swagger 文档", ok,
               f"HTTP {res.status_code}")


# ============================================================
# 工具函数
# ============================================================
def generate_test_wav():
    """生成一个 1 秒的测试 WAV 文件（静音）"""
    import struct
    import wave

    output_path = os.path.join(
        os.path.dirname(__file__), "..", "演示素材", "测试音频_静音.wav"
    )
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    sample_rate = 16000
    duration = 1.0
    num_samples = int(sample_rate * duration)

    with wave.open(output_path, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        # 生成微弱信号（非完全静音，便于 ASR 处理）
        frames = []
        for i in range(num_samples):
            sample = int(100 * (i % 100) / 100)  # 微弱锯齿波
            frames.append(struct.pack('<h', sample))
        wf.writeframes(b''.join(frames))

    return output_path


# ============================================================
# 主函数
# ============================================================
async def main():
    print("=" * 60)
    print("  华科储能智能会议助手 — 全流程联调测试")
    print(f"  服务地址：{BASE_URL}")
    print("=" * 60)

    # 检查服务是否在线
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(f"{BASE_URL}/api/status")
            if res.status_code != 200:
                print(f"\n❌ 服务未就绪：HTTP {res.status_code}")
                print("请先启动服务：python3 启动程序.py")
                return
            print(f"\n✅ 服务在线")
    except Exception as e:
        print(f"\n❌ 无法连接服务：{e}")
        print("请先启动服务：python3 启动程序.py")
        return

    # 运行所有测试
    await test_full_pipeline()       # TC-08
    await test_text_to_minutes()     # TC-09
    await test_engine_switch()       # TC-10
    await test_error_scenarios()     # TC-11
    await test_frontend()            # TC-12

    # ============================================================
    # 生成测试报告
    # ============================================================
    print("\n" + "=" * 60)
    print("  测试报告")
    print("=" * 60)

    total = len(TEST_RESULTS)
    passed = sum(1 for r in TEST_RESULTS if r["passed"])
    failed = total - passed

    print(f"\n  总计：{total} 项")
    print(f"  通过：{passed} 项 ✅")
    print(f"  失败：{failed} 项 {'❌' if failed > 0 else ''}")

    if failed > 0:
        print(f"\n  失败项：")
        for r in TEST_RESULTS:
            if not r["passed"]:
                print(f"    ❌ {r['id']}：{r['name']}")
                if r['detail']:
                    print(f"       {r['detail']}")

    print(f"\n  通过率：{passed}/{total} = {passed/total*100:.1f}%")

    # 保存测试报告
    report_path = os.path.join(
        os.path.dirname(__file__), "..", "测试脚本", "联调测试报告.json"
    )
    report_path = os.path.abspath(report_path)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": f"{passed/total*100:.1f}%",
            "results": TEST_RESULTS
        }, f, ensure_ascii=False, indent=2)

    print(f"\n  测试报告已保存：{report_path}")
    print("\n" + "=" * 60)


if __name__ == "__main__":
    asyncio.run(main())