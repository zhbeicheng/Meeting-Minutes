# ============================================================
# 文件名：测试_说话人分离.py
# 功能：测试 pyannote.audio 说话人分离全流程
#
# 使用方式：
#   1. 设置 HuggingFace Token：
#      export HF_TOKEN="hf_xxxxxxxxxxxx"
#   2. 运行测试：
#      python3 测试脚本/测试_说话人分离.py
#
# 首次运行会下载模型（~280MB），请耐心等待
# ============================================================

import os
import sys
import time
import logging

# 添加项目路径
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)
sys.path.insert(0, os.path.join(_project_root, "后端服务"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
logger = logging.getLogger("test_diarization")


def find_test_audio():
    """查找测试音频文件"""
    cache_dir = os.path.join(_project_root, "后端服务", "录音缓存")
    if os.path.isdir(cache_dir):
        wav_files = sorted(
            [f for f in os.listdir(cache_dir) if f.endswith(".wav")],
            reverse=True
        )
        if wav_files:
            return os.path.join(cache_dir, wav_files[0])
    return None


def main():
    print("=" * 60)
    print("  华科储能智能会议助手 - 说话人分离测试")
    print("=" * 60)

    # 检查 Token
    token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
    if not token:
        print("\n❌ 未配置 HuggingFace Token！")
        print("\n请按以下步骤操作：")
        print("  1. 访问 https://huggingface.co/settings/tokens")
        print("  2. 创建 Access Token（类型选 'Read'）")
        print("  3. 访问 https://huggingface.co/pyannote/speaker-diarization-community-1")
        print("  4. 点击 'Agree and access repository' 接受模型使用条款")
        print("  5. 运行：export HF_TOKEN='hf_你的token'")
        print("  6. 重新运行本脚本")
        return

    print(f"\n✅ Token 已配置：{token[:12]}...")

    # 查找测试音频
    audio_path = find_test_audio()
    if not audio_path:
        print("\n❌ 未找到测试音频文件！")
        print("请先在桌面应用中录制一段多人对话音频")
        return

    print(f"\n📁 测试音频：{os.path.basename(audio_path)}")
    file_size = os.path.getsize(audio_path) / 1024
    print(f"   大小：{file_size:.1f} KB")

    # 导入引擎
    from 说话人分离 import get_speaker_diarization_engine

    engine = get_speaker_diarization_engine()
    engine.set_token(token)

    print(f"\n🔧 引擎状态：{engine.get_status()}")

    # 加载模型
    print("\n⏳ 正在加载说话人分离模型...")
    print("   （首次运行将下载 ~280MB 模型，请耐心等待）")
    t0 = time.time()

    if not engine.load_model():
        print("\n❌ 模型加载失败！")
        print("\n可能原因：")
        print("  1. Token 无效或过期")
        print("  2. 未接受模型使用条款")
        print("  3. 网络连接问题")
        print("\n请检查后重试")
        return

    t1 = time.time()
    print(f"✅ 模型加载完成（耗时 {t1 - t0:.1f} 秒）")

    # 运行说话人分离
    print("\n⏳ 正在进行说话人分离...")
    t2 = time.time()

    segments = engine.diarize(audio_path)

    t3 = time.time()
    print(f"✅ 说话人分离完成（耗时 {t3 - t2:.1f} 秒）")

    if not segments:
        print("\n❌ 未检测到说话人分段")
        return

    # 显示结果
    speaker_count = len(set(s["speaker"] for s in segments))
    print(f"\n📊 检测到 {speaker_count} 位说话人，共 {len(segments)} 个分段：\n")

    for seg in segments:
        duration = seg["end"] - seg["start"]
        bar = "█" * min(int(duration * 10), 40)
        print(f"  {seg['speaker']:15s} "
              f"[{seg['start']:6.2f}s → {seg['end']:6.2f}s] "
              f"({duration:5.2f}s) {bar}")

    # 尝试与转写文本合并
    print("\n⏳ 正在合并转写文本...")
    try:
        from 语音转写_流式 import get_streaming_asr_engine
        asr_engine = get_streaming_asr_engine()
        result = asr_engine.transcribe(audio_path, engine_type="cloud_streaming")

        if result.get("text"):
            merged = engine.merge_with_transcript(
                segments,
                result["text"],
                result.get("segments", [])
            )
            print(f"\n📝 带说话人标签的会议记录：\n")
            print(merged)
        else:
            print("⚠️ 转写文本为空，跳过合并")
    except Exception as e:
        print(f"⚠️ 转写合并跳过：{e}")

    print("\n" + "=" * 60)
    print("  ✅ 说话人分离测试完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()