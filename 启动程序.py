# ============================================================
# 文件名：启动程序.py
# 功能：华科储能智能会议助手 — 一键启动入口
# 说明：默认以原生桌面 GUI 窗口启动，--web 参数回退到浏览器模式
# 使用：
#   python3 启动程序.py          → 原生桌面窗口（推荐）
#   python3 启动程序.py --web    → 浏览器模式
# ============================================================

import subprocess
import webbrowser
import time
import sys
import os


def main():
    """
    一键启动华科储能智能会议助手

    启动模式：
    - 默认：原生桌面 GUI 窗口（pywebview）
    - --web：浏览器模式（兼容旧版）
    """
    # 解析命令行参数
    use_web_mode = "--web" in sys.argv

    # ============================================================
    # 第 1 步：打印欢迎信息
    # ============================================================
    print("=" * 60)
    print("  🏭 华科储能智能会议助手")
    print("  版本：V1.0")
    print("  本地化智能会议效率工具")
    if use_web_mode:
        print("  启动模式：浏览器")
    else:
        print("  启动模式：原生桌面窗口")
    print("=" * 60)
    print()

    # ============================================================
    # 第 2 步：检查 Python 版本
    # ============================================================
    print("🔍 检查 Python 版本...")
    if sys.version_info < (3, 9):
        print("❌ 错误：需要 Python 3.9 或更高版本")
        print(f"   当前版本：{sys.version}")
        sys.exit(1)
    print(f"   ✅ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    print()

    # ============================================================
    # 第 3 步：检查环境配置文件
    # ============================================================
    print("🔍 检查环境配置...")
    env_file = os.path.join("配置与模板", "环境配置.env")
    if not os.path.exists(env_file):
        print("⚠️  警告：未找到环境配置文件")
        print(f"   请编辑 配置与模板/环境配置.env 并填入 API Key")
        print("   然后重命名为 .env 或设置环境变量")
    else:
        print("   ✅ 环境配置文件已就绪")
    print()

    # ============================================================
    # 第 4 步：检查并创建必要目录
    # ============================================================
    print("🔍 检查工作目录...")
    required_dirs = ["录音缓存", "历史记录", "模型文件"]
    for dir_name in required_dirs:
        if not os.path.exists(dir_name):
            os.makedirs(dir_name, exist_ok=True)
            print(f"   📁 已创建目录：{dir_name}/")
    print("   ✅ 工作目录已就绪")
    print()

    # ============================================================
    # 第 5 步：根据模式选择启动方式
    # ============================================================
    if use_web_mode:
        _start_web_mode()
    else:
        _start_gui_mode()


def _start_web_mode():
    """浏览器模式：启动后端 + 打开浏览器"""
    print("🚀 启动后端服务...")
    print(f"   地址：http://127.0.0.1:8000")
    print(f"   API 文档：http://127.0.0.1:8000/docs")
    print()

    server_process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "后端服务.主程序:app",
            "--host", "127.0.0.1",
            "--port", "8000",
            "--log-level", "info"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    print("⏳ 等待服务启动...")
    time.sleep(2)

    if server_process.poll() is not None:
        print("❌ 后端服务启动失败！")
        stderr_output = server_process.stderr.read().decode()
        print(stderr_output)
        sys.exit(1)

    print("🌐 正在打开浏览器...")
    webbrowser.open("http://127.0.0.1:8000")
    print()
    print("=" * 60)
    print("  ✅ 华科储能智能会议助手已启动！")
    print()
    print("  前端界面：http://127.0.0.1:8000")
    print("  API 文档：http://127.0.0.1:8000/docs")
    print()
    print("  按 Ctrl+C 停止服务")
    print("=" * 60)

    try:
        server_process.wait()
    except KeyboardInterrupt:
        print()
        print("🛑 正在停止服务...")
        server_process.terminate()
        print("✅ 服务已停止")


def _start_gui_mode():
    """原生桌面 GUI 模式：使用 pywebview 启动原生窗口"""
    # 检查 pywebview 是否已安装
    try:
        import webview
    except ImportError:
        print("❌ 未安装 pywebview，请运行：pip3 install pywebview")
        print("   或使用浏览器模式：python3 启动程序.py --web")
        sys.exit(1)

    print("🖥️  正在启动原生桌面窗口...")
    print()

    # 直接调用桌面启动器
    desktop_launcher = os.path.join(os.path.dirname(__file__), "桌面启动器.py")
    subprocess.run([sys.executable, desktop_launcher])


if __name__ == "__main__":
    main()