# ============================================================
# 文件名：测试_纪要生成.py
# 功能：纪要生成引擎的单元测试
# 测试范围：
#   TC-03：引擎初始化与状态查询
#   TC-04：模板列表获取
#   TC-05：纪要生成（正常流程）
#   TC-06：纪要生成（异常流程：空文本）
#   TC-07：纪要生成（异常流程：无效模板）
# ============================================================

import os
import sys
import asyncio

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from 后端服务.纪要生成 import get_minutes_generator


def test_engine_init():
    """TC-03：引擎初始化与状态查询"""
    print("\n" + "=" * 60)
    print("TC-03：引擎初始化与状态查询")
    print("=" * 60)

    generator = get_minutes_generator()
    status = generator.get_status()

    print(f"  配置状态：{status['configured']}")
    print(f"  模型名称：{status['model']}")
    print(f"  API 地址：{status['api_base']}")
    print(f"  可用模板：{status['templates']}")
    print(f"  服务商：{status['provider']}")

    assert isinstance(status["templates"], list), "模板列表应为 list"
    assert len(status["templates"]) >= 4, "至少应有 4 套模板"
    print("  ✅ 通过")


def test_get_templates():
    """TC-04：模板列表获取"""
    print("\n" + "=" * 60)
    print("TC-04：模板列表获取")
    print("=" * 60)

    generator = get_minutes_generator()
    templates = generator.get_templates()

    for t in templates:
        print(f"  📋 {t['id']}：{t['name']}")

    template_ids = [t["id"] for t in templates]
    assert "部门例会" in template_ids, "应包含部门例会模板"
    assert "项目评审" in template_ids, "应包含项目评审模板"
    assert "客户沟通" in template_ids, "应包含客户沟通模板"
    assert "领导汇报" in template_ids, "应包含领导汇报模板"
    print("  ✅ 通过")


async def test_generate_normal():
    """TC-05：纪要生成（正常流程）"""
    print("\n" + "=" * 60)
    print("TC-05：纪要生成（正常流程）")
    print("=" * 60)

    generator = get_minutes_generator()

    if not generator.configured:
        print("  ⚠️  跳过：LLM API 未配置")
        print("     请在 配置与模板/火山密钥_LLM.py 中填入 API_KEY")
        return

    # 模拟一段储能项目会议转写文本
    transcript = """
    今天我们开一个部门周例会。参会的有张工、李工、王工，还有小刘。
    第一个议题是坤智储能系统在新疆项目的进度。张工说目前设备已经到现场了，
    预计下周可以开始安装调试。但是有个问题，当地的电网接入审批还没下来，
    需要李工这周去一趟新疆，跟当地供电公司对接一下。
    第二个议题是下个月的储能技术交流会。王工负责准备技术方案，
    重点介绍我们的坤融交直流一体机。小刘负责会场和物料。
    最后，领导强调安全问题，最近行业里出了几起储能电站事故，
    要求我们所有项目都要做一次安全排查，下周五之前完成。
    下次会议定在下周一上午九点。
    """

    try:
        result = await generator.generate(
            transcript=transcript,
            template="部门例会"
        )

        print(f"  模板：{result['template']}")
        print(f"  模型：{result['model']}")
        print(f"  纪要长度：{len(result['markdown'])} 字符")
        print(f"\n  --- 生成的纪要 ---")
        print(result["markdown"][:500])
        if len(result["markdown"]) > 500:
            print("  ...（内容已截断）")

        assert len(result["markdown"]) > 50, "纪要内容不应过短"
        assert "#" in result["markdown"], "纪要应包含 Markdown 标题"
        print("\n  ✅ 通过")

    except Exception as e:
        print(f"  ❌ 失败：{e}")
        raise


async def test_generate_empty_text():
    """TC-06：纪要生成（异常流程：空文本）"""
    print("\n" + "=" * 60)
    print("TC-06：纪要生成（异常流程：空文本）")
    print("=" * 60)

    generator = get_minutes_generator()

    try:
        await generator.generate(transcript="", template="部门例会")
        print("  ❌ 失败：应该抛出 ValueError")
        assert False, "空文本应抛出异常"
    except ValueError as e:
        print(f"  ✅ 正确抛出 ValueError：{e}")
    except Exception as e:
        print(f"  ⚠️  抛出其他异常：{type(e).__name__}：{e}")


async def test_generate_invalid_template():
    """TC-07：纪要生成（异常流程：无效模板）"""
    print("\n" + "=" * 60)
    print("TC-07：纪要生成（异常流程：无效模板）")
    print("=" * 60)

    generator = get_minutes_generator()

    try:
        await generator.generate(transcript="测试文本", template="不存在的模板")
        print("  ❌ 失败：应该抛出 ValueError")
        assert False, "无效模板应抛出异常"
    except ValueError as e:
        print(f"  ✅ 正确抛出 ValueError：{e}")
    except Exception as e:
        print(f"  ⚠️  抛出其他异常：{type(e).__name__}：{e}")


async def main():
    """运行所有测试"""
    print("=" * 60)
    print("  纪要生成引擎 — 单元测试")
    print("=" * 60)

    # 同步测试
    test_engine_init()
    test_get_templates()

    # 异步测试
    await test_generate_empty_text()
    await test_generate_invalid_template()
    await test_generate_normal()

    print("\n" + "=" * 60)
    print("  测试完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())