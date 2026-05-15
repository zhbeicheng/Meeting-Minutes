# ============================================================
# 文件名：测试_语音转写.py
# 功能：语音转写引擎的独立测试脚本
# 使用：python 测试脚本/测试_语音转写.py
#
# 测试内容：
#   1. 引擎初始化测试（单例模式验证）
#   2. 模型加载测试
#   3. WAV 文件转写测试
#   4. 引擎状态查询测试
# ============================================================

import os
import sys
import time

# ============================================================
# 将项目根目录加入 Python 路径
# ============================================================
# 确保可以从 测试脚本/ 目录导入 后端服务/ 模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ============================================================
# 加载环境变量
# ============================================================
try:
    from dotenv import load_dotenv
    env_path = os.path.join(
        os.path.dirname(__file__), "..", "配置与模板", "环境配置.env"
    )
    if os.path.exists(env_path):
        load_dotenv(env_path)
        print("✅ 已加载环境配置")
except ImportError:
    pass

from 后端服务.语音转写 import STTEngine, get_stt_engine


def test_singleton():
    """
    测试 1：单例模式验证
    
    验证多次获取引擎实例返回的是同一个对象
    这是性能优化的关键——模型只加载一次
    """
    print("=" * 60)
    print("测试 1：单例模式验证")
    print("=" * 60)

    engine1 = get_stt_engine()
    engine2 = get_stt_engine()
    engine3 = STTEngine()

    if engine1 is engine2 is engine3:
        print("✅ 通过：三次获取返回同一个实例")
        print(f"   实例 ID：{id(engine1)}")
    else:
        print("❌ 失败：获取到了不同的实例")
        return False

    return True


def test_model_loading():
    """
    测试 2：模型加载测试
    
    验证 faster-whisper 模型能否正常加载
    首次运行会自动下载 small 模型（约 2GB）
    """
    print()
    print("=" * 60)
    print("测试 2：模型加载测试")
    print("=" * 60)

    engine = get_stt_engine()

    # 检查初始状态
    status_before = engine.get_status()
    print(f"加载前状态：loaded={status_before['loaded']}")

    if status_before["loaded"]:
        print("✅ 模型已加载（可能是之前测试加载的）")
        return True

    try:
        print("正在加载模型（首次运行需下载约 2GB，请耐心等待）...")
        start = time.time()

        # 触发模型加载
        engine._load_model()

        elapsed = time.time() - start
        status_after = engine.get_status()

        if status_after["loaded"]:
            print(f"✅ 通过：模型加载成功，耗时 {elapsed:.1f} 秒")
            print(f"   模型大小：{status_after['model_size']}")
            print(f"   推理设备：{status_after['device']}")
            return True
        else:
            print(f"❌ 失败：模型加载后状态仍为未加载")
            return False

    except Exception as e:
        print(f"❌ 失败：{e}")
        return False


def test_transcribe_wav():
    """
    测试 3：WAV 文件转写测试
    
    使用项目自带的测试音频或生成一个简单的测试音频
    验证转写流程的完整性
    """
    print()
    print("=" * 60)
    print("测试 3：WAV 文件转写测试")
    print("=" * 60)

    # 查找测试音频文件
    test_audio_dir = os.path.join(
        os.path.dirname(__file__), "..", "演示素材"
    )
    test_files = []

    if os.path.exists(test_audio_dir):
        for f in os.listdir(test_audio_dir):
            if f.endswith((".wav", ".mp3", ".m4a", ".webm")):
                test_files.append(os.path.join(test_audio_dir, f))

    if not test_files:
        print("⚠️  未找到测试音频文件")
        print("   请在 演示素材/ 目录放置一个 WAV/MP3 文件后重试")
        print("   或使用 API 上传音频进行测试：")
        print("   curl -X POST http://127.0.0.1:8001/api/stt -F \"file=@你的音频.wav\"")
        return None  # 跳过而非失败

    test_file = test_files[0]
    print(f"测试文件：{os.path.basename(test_file)}")
    print(f"文件大小：{os.path.getsize(test_file) / 1024:.1f} KB")

    try:
        engine = get_stt_engine()
        start = time.time()

        result = engine.transcribe(test_file)

        elapsed = time.time() - start

        print(f"✅ 通过：转写成功，耗时 {elapsed:.1f} 秒")
        print(f"   音频时长：{result['duration']} 秒")
        print(f"   实时率 RTF：{result['rtf']}（<1 表示比实时快）")
        print(f"   检测语言：{result['language']}")
        print(f"   文本长度：{len(result['text'])} 字符")
        print(f"   分段数量：{result['segment_count']}")
        print(f"   转写文本预览：{result['text'][:100]}...")
        return True

    except Exception as e:
        print(f"❌ 失败：{e}")
        return False


def test_engine_status():
    """
    测试 4：引擎状态查询测试
    
    验证 get_status 方法返回正确的状态信息
    """
    print()
    print("=" * 60)
    print("测试 4：引擎状态查询")
    print("=" * 60)

    engine = get_stt_engine()
    status = engine.get_status()

    required_keys = ["loaded", "model_size", "device", "error"]
    missing = [k for k in required_keys if k not in status]

    if missing:
        print(f"❌ 失败：缺少字段 {missing}")
        return False

    print("✅ 通过：状态信息完整")
    for key, value in status.items():
        print(f"   {key}：{value}")

    return True


def main():
    """
    运行所有测试
    """
    print()
    print("╔" + "═" * 58 + "╗")
    print("║" + "  华科储能智能会议助手 — 语音转写引擎测试".center(52) + "║")
    print("╚" + "═" * 58 + "╝")
    print()

    results = {}

    # 按顺序执行测试
    results["单例模式"] = test_singleton()
    results["模型加载"] = test_model_loading()
    results["WAV转写"] = test_transcribe_wav()
    results["状态查询"] = test_engine_status()

    # 汇总结果
    print()
    print("=" * 60)
    print("测试汇总")
    print("=" * 60)

    passed = 0
    failed = 0
    skipped = 0

    for name, result in results.items():
        if result is True:
            print(f"  ✅ {name}：通过")
            passed += 1
        elif result is False:
            print(f"  ❌ {name}：失败")
            failed += 1
        else:
            print(f"  ⚠️  {name}：跳过（无测试文件）")
            skipped += 1

    print()
    print(f"通过：{passed}，失败：{failed}，跳过：{skipped}")
    print()

    if failed > 0:
        print("⚠️  存在失败项，请检查上述错误信息")
        sys.exit(1)
    else:
        print("✅ 所有可执行测试通过！")
        print()
        print("提示：如需测试完整转写流程，请：")
        print("  1. 在 演示素材/ 目录放置一个 WAV/MP3 音频文件")
        print("  2. 重新运行本测试脚本")
        print("  或使用 API 测试：")
        print("  curl -X POST http://127.0.0.1:8001/api/stt -F \"file=@你的音频.wav\"")


if __name__ == "__main__":
    main()