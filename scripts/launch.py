"""一键启动器：把整个项目当成一个本地工具跑起来。

用法：
    python scripts/launch.py           启动服务，就绪后自动打开浏览器
    python scripts/launch.py --check    只做环境自检，不启动服务、不写任何文件

这个脚本要解决的是「同事解压之后双击就能用」，所以它负责把之前那串手工步骤
（建环境、建前端、配模型、起服务、开浏览器）全部串起来。

有两条容易踩坑的地方，代码里都做了处理，看注释即可：
1. 项目里所有运行时路径（front/dist、config/、data/、logs/）都是相对当前工作目录
   找的，不是相对项目根。所以启动前必须先把工作目录切到项目根。
2. 前端产物不完整时（有 index.html 但缺 assets 目录），后端会静默跳过静态资源挂载，
   表现为白屏而服务端一条错都不报。所以启动前必须把产物校验完整。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# 本文件在 scripts/ 目录下，往上一级就是项目根。
ROOT = Path(__file__).resolve().parent.parent

# 固定只听本机回环地址。不要用 "localhost"：它在 Windows 上可能先解析到 ::1，
# 而服务只监听了 127.0.0.1，浏览器打开就会连不上（项目里已经踩过一次这个坑）。
HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# 用它来判断「这个端口上跑的到底是不是本项目」，避免把别人的服务当成自己的。
BOOTSTRAP_PATH = "/webui/bootstrap"
BOOTSTRAP_MARKER = "paper_agent_workspace"

# 判断密钥的两个特征，用于打包和播种前的自检。
API_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{16,}")
API_KEY_FIELD_PATTERN = re.compile(r'"api_key"\s*:\s*"[^"]+"')

# 前端 index.html 里对静态资源的引用，例如 src="/assets/index-xxxx.js"。
ASSET_REF_PATTERN = re.compile(r"""["'](/assets/[^"']+)["']""")


def _enter_project_root() -> None:
    """把工作目录切到项目根，并让项目根可以被 import。

    必须在导入 main 之前调用。原因是 main.py 在被导入的那一刻就会初始化日志、
    建 SQLite 表和各个数据目录，而这些路径全都是相对「当前工作目录」解析的；
    而直接运行 scripts/launch.py 时，当前目录未必是项目根，Python 放进搜索路径的
    也是 scripts/ 而不是项目根。
    """

    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def check_frontend() -> list[str]:
    """检查前端构建产物是否完整，返回问题清单（空列表表示没问题）。

    这里特意检查得比较细。后端的逻辑是「assets 目录存在才挂载 /assets」，
    如果产物里只有 index.html 而没有 assets，浏览器请求 JS 时会落进后端的
    单页应用兜底、拿到一份 HTML，结果就是白屏，而且服务端日志干干净净——
    这种问题排查起来极其费劲，所以在启动前就必须拦住。
    """

    problems: list[str] = []
    dist = ROOT / "front" / "dist"
    index_file = dist / "index.html"
    assets_dir = dist / "assets"

    if not index_file.is_file():
        problems.append(f"找不到前端首页：{index_file}")
        return problems
    if not assets_dir.is_dir():
        problems.append(f"找不到前端静态资源目录：{assets_dir}")
        return problems

    # 中文注释：显式用 utf-8 读。Windows 上按系统默认编码读会解出乱码甚至直接报错。
    html = index_file.read_text(encoding="utf-8")

    if "/src/main.ts" in html:
        problems.append(
            "front/dist/index.html 引用的是 /src/main.ts，说明放进去的是未构建的源文件，"
            "而不是真正的构建产物"
        )

    for ref in sorted({match for match in ASSET_REF_PATTERN.findall(html)}):
        if not (dist / ref.lstrip("/")).is_file():
            problems.append(f"index.html 引用了并不存在的资源：{ref}")

    return problems


def example_seed_problems() -> list[str]:
    """检查示例配置能不能安全地拿来当首次运行的模板。"""

    example = ROOT / "config" / "model.example.json"
    if not example.is_file():
        return [f"找不到示例配置：{example}"]

    text = example.read_text(encoding="utf-8")
    if API_KEY_FIELD_PATTERN.search(text):
        return [
            "config/model.example.json 里填了非空的 api_key。"
            "示例文件是要发给别人的模板，不能带真密钥，请先清空再打包"
        ]
    return []


def seed_model_config() -> bool:
    """首次运行时用示例配置播种 config/model.json，返回是否本次新建。

    示例里已经把接口地址和模型名填好了，只留空密钥，所以同事启动后只需要在
    设置页把自己的 key 粘进去，不用去查这些参数。
    """

    target = ROOT / "config" / "model.json"
    if target.exists():
        return False

    example = ROOT / "config" / "model.example.json"
    problems = example_seed_problems()
    if problems:
        raise RuntimeError(problems[0])

    target.parent.mkdir(parents=True, exist_ok=True)
    # 中文注释：按字节整体复制，不做解码再编码。这个文件是带中文说明的 UTF-8，
    # 在 Windows 上用普通文本方式读写会按系统默认编码处理，容易解出乱码或写坏。
    shutil.copyfile(example, target)
    return True


def _port_in_use(port: int) -> bool:
    """判断端口上是否已经有程序在监听。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((HOST, port)) == 0


def _is_our_app(port: int) -> bool:
    """判断端口上监听的是不是本项目自己的服务。"""

    try:
        import httpx
    except ImportError:
        return False

    url = f"http://{HOST}:{port}{BOOTSTRAP_PATH}"
    try:
        # 中文注释：必须 trust_env=False。否则 httpx 会读取系统代理设置，
        # 在装了公司代理的机器上，连本机回环地址的探测请求都会被转发到代理，
        # 结果永远探测失败，甚至会误判成「端口被别的程序占用」。
        with httpx.Client(trust_env=False, timeout=0.8) as client:
            response = client.get(url)
    except Exception:
        return False

    if response.status_code != 200:
        return False
    # 中文注释：不能只看状态码。后端的单页应用兜底对任何未知路径都返回 200，
    # 所以必须检查返回内容里那个只有本项目才有的标识。
    try:
        return response.json().get("runtime_surface") == BOOTSTRAP_MARKER
    except Exception:
        return False


def inspect_port(port: int) -> str:
    """返回端口状态：free（空闲）/ ours（本项目已在跑）/ foreign（被别人占用）。"""

    if not _port_in_use(port):
        return "free"
    return "ours" if _is_our_app(port) else "foreign"


def announce_first_run() -> None:
    """首次运行要下载 Python 和依赖，提前说一声，免得用户以为是卡死了。"""

    print()
    print("=" * 62)
    print("  首次运行：需要下载 Python 运行环境和项目依赖，可能要几分钟。")
    print("  下载期间没有进度输出是正常的，请不要关闭这个窗口。")
    print("=" * 62)
    print()


def open_browser_when_ready(port: int, first_run: bool) -> None:
    """在后台等端口真正就绪后再打开浏览器。

    这个函数跑在单独的线程里，主线程留给服务本身。这样服务如果启动失败，
    异常和报错能立刻在主线程打出来，而不是让用户对着一个不动的窗口干等。
    """

    # 中文注释：首次运行要下载依赖，给的时间宽一些。
    timeout_seconds = 300 if first_run else 60
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        if _is_our_app(port):
            path = "/settings" if first_run else "/"
            url = f"http://{HOST}:{port}{path}"
            print()
            print(f"  服务已就绪，正在打开浏览器：{url}")
            print(f"  如果浏览器没有自动打开，请手动访问上面这个地址。")
            print("  关闭这个窗口就会停止服务。")
            print()
            # 中文注释：打开失败时（比如这台机器没配默认浏览器）把地址打出来，
            # 让用户可以自己复制，而不是干等着。
            if not webbrowser.open(url):
                print(f"  （自动打开浏览器失败，请手动访问：{url}）")
            return
        time.sleep(0.4)

    print()
    print(f"  等待服务就绪超时（{timeout_seconds} 秒）。请查看上面的日志排查原因。")
    print()


def run_check() -> int:
    """自检模式：只检查环境，不启动服务、不写任何文件。

    打包脚本会拿这个模式检查打包目录，所以这里必须是只读的——
    一旦它真去播种 config/model.json，打包出来的包里就会多出一个本不该有的文件。
    """

    print()
    print("环境自检（只读，不会修改任何文件）")
    print("-" * 62)

    frontend_problems = check_frontend()
    seed_problems = example_seed_problems()
    has_model_config = (ROOT / "config" / "model.json").exists()

    print(f"  前端构建产物：{'完整' if not frontend_problems else '有问题'}")
    print(f"  示例配置模板：{'可安全使用' if not seed_problems else '有问题'}")
    print(f"  已有模型配置：{'是（首次运行不会覆盖）' if has_model_config else '否（首次运行会播种）'}")
    print("-" * 62)

    problems = frontend_problems + seed_problems
    if not problems:
        print("自检通过，可以打包或启动。")
        return 0

    print("自检发现问题：")
    for problem in problems:
        print(f"  [!] {problem}")
    for hint in frontend_build_hint(frontend_problems):
        print(f"  [!] {hint}")
    return 1


def frontend_build_hint(problems: list[str]) -> list[str]:
    """前端产物有问题时，补一条「怎么修」的提示。"""

    if not problems:
        return []
    return ["修复方式：在项目根目录执行 npm run front:install 和 npm run front:build"]


def resolve_port(cli_port: int | None) -> int:
    """确定监听端口：命令行参数 > 环境变量 PAPERS_PORT > 默认 8000。"""

    if cli_port:
        return cli_port
    raw = (os.environ.get("PAPERS_PORT") or "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        print(f"  [!] 环境变量 PAPERS_PORT 不是合法端口号：{raw}，改用默认端口 {DEFAULT_PORT}")
        return DEFAULT_PORT


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-Agent 一键启动器")
    parser.add_argument("--check", action="store_true", help="只做环境自检，不启动服务")
    parser.add_argument("--port", type=int, default=None, help="覆盖监听端口")
    args = parser.parse_args()

    # 中文注释：切工作目录必须排在最前面，后面所有相对路径都靠它。
    _enter_project_root()

    if args.check:
        return run_check()

    port = resolve_port(args.port)

    # 第一步：确认前端产物完整。这是唯一没法在运行时补救的东西。
    problems = check_frontend()
    if problems:
        print()
        print("  无法启动：前端构建产物不完整。")
        for problem in problems:
            print(f"    - {problem}")
        for hint in frontend_build_hint(problems):
            print(f"    - {hint}")
        print()
        print("  如果你是从别人那里拿到的这个目录，说明压缩包本身有问题，请找发布的人重新要一份。")
        print()
        return 1

    # 第二步：端口检查。已经在跑就直接开浏览器，别重复启动。
    state = inspect_port(port)
    if state == "ours":
        url = f"http://{HOST}:{port}/"
        print(f"  服务已经在运行中，正在打开浏览器：{url}")
        webbrowser.open(url)
        return 0
    if state == "foreign":
        print()
        print(f"  无法启动：端口 {port} 已经被别的程序占用。")
        print(f"  可以先设一个别的端口再启动，例如在命令行里执行：")
        print(f"      set PAPERS_PORT=8100")
        print(f"      start.bat")
        print()
        return 1

    # 第三步：首次运行播种模型配置。返回 True 说明这次是第一次。
    try:
        first_run = seed_model_config()
    except RuntimeError as exc:
        print(f"  无法启动：{exc}")
        return 1

    if first_run:
        announce_first_run()

    print()
    print("=" * 62)
    print(f"  正在启动 Paper-Agent（{HOST}:{port}）")
    print("=" * 62)

    # 中文注释：等就绪和开浏览器都放到后台线程，主线程留给服务本体。
    # 这样万一启动过程中出异常，报错会直接打在用户眼前，不会被吞掉。
    threading.Thread(
        target=open_browser_when_ready,
        args=(port, first_run),
        daemon=True,
    ).start()

    # 中文注释：这里直接导入 main 模块拿它的 app 对象，复用项目自己的 create_app()，
    # 不再另写一套。注意必须排在 _enter_project_root() 之后，因为它导入时就会
    # 初始化日志和各个数据目录。
    from main import app  # noqa: E402

    import uvicorn  # noqa: E402

    # 中文注释：reload 必须关掉。它是给开发时改代码自动重启用，会额外拉一个进程，
    # 对「双击就用」的场景百害无益。
    uvicorn.run(app, host=HOST, port=port, reload=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
