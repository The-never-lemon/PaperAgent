"""一键启动器：把整个项目当成一个本地工具跑起来。

用法：
    python scripts/launch.py           启动服务，就绪后自动打开浏览器
    python scripts/launch.py --check    只做环境自检，不启动服务、不写任何文件

这个脚本要解决的是「同事解压之后双击就能用」，所以它负责把之前那串手工步骤
（建环境、建前端、配模型、起服务、开浏览器）全部串起来。

有三处容易踩坑的地方，代码里都做了处理，看注释即可：
1. 项目里所有运行时路径（front/dist、config/、data/、logs/）都是相对当前工作目录
   找的，不是相对项目根。所以启动前必须先把工作目录切到项目根。
2. 前端产物不完整时（有 index.html 但缺 assets 目录），后端会静默跳过静态资源挂载，
   表现为白屏而服务端一条错都不报。所以启动前必须把产物校验完整。
3. 产物目录完整但过期时（拷贝或旧压缩包里带着上一版 front/dist），不能当成最新页面。
   有源码时要和构建记录核对，对不上就先删掉再构建；构建失败就拒绝启动。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

# 前端产物是否齐、是否按当前源码打出来，启动和打包共用同一套判断。
from frontend_build import clear_dist, frontend_problems, write_stamp

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

# 绿色版 Node 的下载来源与落地位置。
# 中文注释：构建前端需要 npm。这里不要求使用者先去装一遍 Node，而是按需下载一份
# 官方绿色版放进项目自己的目录——和打包时内置 uv.exe 是同一个思路（需要什么工具就
# 带上什么工具）。选绿色版而不是系统安装，是因为它不需要管理员权限、不改动系统，
# 不想要了删掉 tools/node 就干净了。
NODE_DIST_INDEX_URL = "https://nodejs.org/dist/index.json"
NODE_DIST_BASE_URL = "https://nodejs.org/dist"
NODE_TOOLS_SUBDIR = ("tools", "node")


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
    """检查前端构建产物能不能用，返回问题清单（空列表表示没问题）。

    有两层。第一层看文件齐不齐：后端是「assets 目录存在才挂载 /assets」，
    如果只有首页没有静态资源，浏览器要 JS 时会拿到一份 HTML，结果就是白屏，
    服务端日志还干干净净。第二层在有源码时核对构建记录：记录缺失或和源码对不上，
    说明这是一份旧页面，不能直接打开。发给同事的压缩包没有源码，只做第一层。
    """

    return frontend_problems(ROOT)


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


# 中文注释：Nougat 和主程序不能装在同一个环境里，它要的旧版依赖会把现有的包拽乱。
# 显卡版 PyTorch 也不在普通软件源里，所以这里按已经试通过的阿里云地址直接装。
_NOUGAT_DIR = ROOT / "tools" / "nougat_trial"
_ALIYUN_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
_TORCH_WHEEL = (
    "https://mirrors.aliyun.com/pytorch-wheels/cu128/"
    "torch-2.11.0%2Bcu128-cp312-cp312-win_amd64.whl"
)
_TORCHVISION_WHEEL = (
    "https://mirrors.aliyun.com/pytorch-wheels/cu128/"
    "torchvision-0.26.0%2Bcu128-cp312-cp312-win_amd64.whl"
)


def _nougat_python() -> Path:
    """试验环境里的 Python。Windows 和其它系统的目录不一样。"""

    folder = "Scripts" if os.name == "nt" else "bin"
    name = "python.exe" if os.name == "nt" else "python"
    return _NOUGAT_DIR / ".venv" / folder / name


def _uv_executable() -> str:
    """找 uv：启动脚本传进来的优先，其次是项目自带的，再看系统里有没有。"""

    configured = (os.environ.get("UV") or "").strip().strip('"')
    if configured and Path(configured).is_file():
        return configured
    bundled = ROOT / "tools" / "uv.exe"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("uv") or "uv"


def nougat_ready() -> bool:
    """已经装好、而且是 Nougat 能用的那几版依赖时，返回真。"""

    python = _nougat_python()
    if not python.is_file():
        return False
    # 中文注释：pypdfium2 太新、transformers 太新，Nougat 会在导入时直接失败。
    # 显卡版 PyTorch 的版本号里带 +cu，普通源上的 CPU 包没有这段。
    check = (
        "import importlib.metadata as meta, nougat, torch; "
        "assert meta.version('pypdfium2').startswith('4.'); "
        "assert meta.version('transformers').startswith('4.38.'); "
        "assert '+cu' in torch.__version__"
    )
    result = subprocess.run([str(python), "-c", check], capture_output=True, text=True)
    return result.returncode == 0


def _nougat_weight_dir() -> Path:
    """权重放在仓库里的这个目录。不提交，第一次启动时再下载。"""

    return _NOUGAT_DIR / "weights"


def _cached_nougat_weight_dir() -> Path:
    """以前下到用户缓存里的那份。仓库里还没有时，可以先从这里拷过来。"""

    torch_home = (os.environ.get("TORCH_HOME") or "").strip()
    base = Path(torch_home) if torch_home else Path.home() / ".cache" / "torch"
    return base / "hub" / "nougat-0.1.0-small"


# 中文注释：主文件大约 1GB。太小说明上次下到一半，不能当成已经有了。
_NOUGAT_WEIGHTS = {
    "config.json": 100,
    "pytorch_model.bin": 900_000_000,
    "special_tokens_map.json": 20,
    "tokenizer.json": 100_000,
    "tokenizer_config.json": 20,
}
_NOUGAT_WEIGHT_URLS = [
    "https://github.com/facebookresearch/nougat/releases/download/0.1.0-small/{name}",
    "https://huggingface.co/facebook/nougat-small/resolve/main/{name}",
]


def nougat_weights_ready() -> bool:
    """五个权重文件都在，并且主文件不是半截的，才算下完。"""

    folder = _nougat_weight_dir()
    for name, minimum in _NOUGAT_WEIGHTS.items():
        path = folder / name
        if not path.is_file() or path.stat().st_size < minimum:
            return False
    return True


def _copy_cached_weights() -> bool:
    """仓库里还没有权重时，如果用户缓存里已经有完整的一份，就拷过来。"""

    source = _cached_nougat_weight_dir()
    target = _nougat_weight_dir()
    if not source.is_dir():
        return False
    target.mkdir(parents=True, exist_ok=True)
    copied = False
    for name, minimum in _NOUGAT_WEIGHTS.items():
        src = source / name
        dest = target / name
        if dest.is_file() and dest.stat().st_size >= minimum:
            continue
        if not src.is_file() or src.stat().st_size < minimum:
            return False
        shutil.copyfile(src, dest)
        copied = True
    return copied


def _download_file(url: str, dest: Path) -> None:
    """把一个文件下到临时名字，下完再换过去，避免留下半截文件。"""

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "paper-agent"})
    with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as handle:
        total = int(response.headers.get("Content-Length") or 0)
        got = 0
        last_report = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            got += len(chunk)
            if got - last_report >= 50 * 1024 * 1024:
                if total:
                    print(f"    {dest.name}  {got // (1024 * 1024)} / {total // (1024 * 1024)} MB")
                else:
                    print(f"    {dest.name}  {got // (1024 * 1024)} MB")
                last_report = got
    if not partial.is_file() or partial.stat().st_size < _NOUGAT_WEIGHTS.get(dest.name, 1):
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"{dest.name} 没有下完整")
    partial.replace(dest)


def ensure_nougat_weights() -> int:
    """缺权重就下载到仓库目录。已经齐了就什么都不做，也不提交这些文件。"""

    if nougat_weights_ready():
        return 0
    copied = _copy_cached_weights()
    if copied and nougat_weights_ready():
        print("  已把本机缓存里的 Nougat 权重放到项目目录。")
        return 0
    print()
    print("=" * 62)
    print("  正在下载 Nougat 模型权重（大约 1GB）。")
    print("  文件放在 tools/nougat_trial/weights，下完之前请不要关闭窗口。")
    print("=" * 62)
    print()
    folder = _nougat_weight_dir()
    for name, minimum in _NOUGAT_WEIGHTS.items():
        dest = folder / name
        if dest.is_file() and dest.stat().st_size >= minimum:
            continue
        last_error = ""
        for pattern in _NOUGAT_WEIGHT_URLS:
            url = pattern.format(name=name)
            print(f"  下载 {name}")
            try:
                _download_file(url, dest)
                last_error = ""
                break
            except Exception as exc:
                last_error = str(exc)
                print(f"    这个地址失败：{last_error}")
        if not dest.is_file() or dest.stat().st_size < minimum:
            print(f"  [!] {name} 没有下完。{last_error}")
            return 1
    if not nougat_weights_ready():
        print("  [!] 权重文件不完整。")
        return 1
    print("  Nougat 权重已就绪。")
    return 0


def ensure_nougat_env() -> int:
    """没有 Nougat 环境时用阿里云装一份，并确认权重也在。都齐了就直接返回。"""

    packages_ok = nougat_ready()
    weights_ok = nougat_weights_ready()
    if packages_ok and weights_ok:
        return 0
    if not packages_ok:
        if os.name != "nt":
            print("  [!] 自动准备 Nougat 目前只支持 Windows。全文阅读需要先手动装好 tools/nougat_trial。")
            return 1
        print()
        print("=" * 62)
        print("  正在准备全文阅读环境（Nougat）。")
        print("  其中显卡版 PyTorch 大约 2.6GB，从阿里云下载，请不要关闭窗口。")
        print("=" * 62)
        print()
        uv = _uv_executable()
        python = _nougat_python()
        steps = [
            [uv, "venv", "--python", "3.12", str(_NOUGAT_DIR / ".venv")],
            [
                uv, "pip", "install", "--python", str(python),
                "--index-url", _ALIYUN_INDEX,
                "nougat-ocr==0.1.17",
                "transformers==4.38.2",
                "albumentations==1.4.24",
                "pypdfium2==4.30.0",
                "requests",
            ],
            [
                uv, "pip", "install", "--python", str(python),
                "--index-url", _ALIYUN_INDEX,
                _TORCH_WHEEL,
                _TORCHVISION_WHEEL,
            ],
        ]
        for command in steps:
            result = subprocess.run(command, cwd=ROOT)
            if result.returncode != 0:
                print("  [!] Nougat 环境没有装完。全文阅读会失败，请看上面的报错。")
                return result.returncode
        if not nougat_ready():
            print("  [!] Nougat 装完后仍然导入失败。")
            return 1
        print("  Nougat 环境已就绪。")
    return ensure_nougat_weights()


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

    print(f"  前端构建产物：{'可用' if not frontend_problems else '有问题'}")
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


def node_tools_dir() -> Path:
    """返回项目里放绿色版 Node 的目录。"""

    return ROOT.joinpath(*NODE_TOOLS_SUBDIR)


def local_npm() -> str | None:
    """在项目自带的绿色版 Node 里找 npm，没找到返回 None。"""

    root = node_tools_dir()
    if not root.is_dir():
        return None
    # 官方压缩包解出来是 node-vXX.YY.ZZ-win-x64/ 这种带版本号的目录，所以按通配找。
    for pattern in ("*/npm.cmd", "*/bin/npm"):
        found = sorted(root.glob(pattern))
        if found:
            return str(found[0])
    return None


def resolve_npm() -> str | None:
    """确定这次该用哪个 npm：优先项目自带的绿色版，其次系统 PATH 里的。

    中文注释：先看自带的，是为了让"上次自动装过"的人直接沿用那一次的结果，
    既不重复询问，也不依赖系统 PATH 有没有配好。
    """

    return local_npm() or shutil.which("npm")


def has_npm() -> bool:
    """判断现在有没有可用的 npm（构建前端要用）。"""

    return resolve_npm() is not None


def node_archive_platform() -> str | None:
    """返回当前平台对应的 Node 官方包后缀（例如 win-x64），不支持的平台返回 None。

    中文注释：自动安装只对 Windows 做了——这个项目本来就是用 start.bat 在 Windows 上
    分发的。别的系统不去猜包名，老实返回 None，走手动安装那条提示。
    """

    if os.name != "nt":
        return None
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        return "win-x64"
    if machine in {"arm64", "aarch64"}:
        return "win-arm64"
    return None


def download_node_archive(platform_tag: str) -> Path:
    """下载最新的 Node LTS 绿色版压缩包到临时目录，返回压缩包路径。"""

    with urllib.request.urlopen(NODE_DIST_INDEX_URL, timeout=30) as response:
        releases = json.load(response)
    # 中文注释：取第一个带 lts 标记的版本，而不是写死版本号——写死的话过一阵就过期了。
    latest_lts = next(item for item in releases if item.get("lts"))
    version = str(latest_lts["version"])
    archive_name = f"node-{version}-{platform_tag}.zip"
    url = f"{NODE_DIST_BASE_URL}/{version}/{archive_name}"

    working_dir = Path(tempfile.mkdtemp(prefix="paper_agent_node_"))
    target = working_dir / archive_name
    print(f"    正在下载 {url}")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, target.open("wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            while chunk := response.read(1 << 20):
                handle.write(chunk)
                received += len(chunk)
                if total:
                    # 中文注释：三十多兆，不显示进度会让人以为卡死了。
                    print(f"\r    已下载 {received / 1048576:.1f} / {total / 1048576:.1f} MB", end="")
    except Exception:
        # 中文注释：下载中途断了就把半个文件清掉，别在别人机器上留三十多兆垃圾。
        shutil.rmtree(working_dir, ignore_errors=True)
        raise
    print()
    return target


def install_portable_node() -> bool:
    """下载并解压一份绿色版 Node 到项目的 tools/node 下，成功返回 True。"""

    platform_tag = node_archive_platform()
    if platform_tag is None:
        print("    [!] 自动安装目前只支持 Windows，请在别处装好 Node.js 后再启动")
        return False
    try:
        archive = download_node_archive(platform_tag)
    except Exception as exc:  # noqa: BLE001
        print(f"    [!] 下载失败：{exc}")
        return False

    destination = node_tools_dir()
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(destination)
    except Exception as exc:  # noqa: BLE001
        print(f"    [!] 解压失败：{exc}")
        return False
    finally:
        # 中文注释：压缩包下载在临时目录里，解压完就删掉，不在别人机器上留垃圾。
        shutil.rmtree(archive.parent, ignore_errors=True)

    npm = local_npm()
    if npm is None:
        print("    [!] 解压完了却没在里面找到 npm，请手动检查 tools/node 目录")
        return False
    print(f"    已准备好：{npm}")
    return True


def ask_to_install_node() -> bool:
    """问使用者要不要现在自动装一份 Node，返回是否要装。

    中文注释：只有人对着控制台双击启动时才会问。管道、重定向、定时任务这类没有
    交互终端的场景一律按"不装"处理——绝不能让一个没人看着的脚本自己下三十多兆。
    默认选项是"装"，直接回车就走安装。
    """

    if not sys.stdin.isatty():
        return False
    print()
    print("  没有找到 npm（构建前端需要它）。")
    print("  可以现在自动下载一份 Node.js 绿色版放进项目的 tools/node/ 下：")
    print("  不需要管理员权限、不改动系统，不想要了删掉那个目录就能撤销。")
    print()
    try:
        answer = input("  要现在下载安装吗？[Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"", "y", "yes"}


def build_frontend_in_place() -> bool:
    """就地构建前端产物，成功返回 True。

    中文注释：从 git 仓库克隆出来的目录里是没有 front/dist 的（构建产物不入库），
    所以启动时缺产物就地构建一次，双击 start.bat 就能直接用。

    构建要跑 npm install 加 vite build，第一遍可能要几分钟，所以每一步都打印出来，
    不能让它静默卡着让人以为程序死了。
    """

    npm = resolve_npm()
    if npm is None:
        return False
    # 中文注释：项目自带的绿色版 Node 不在系统 PATH 里。npm 自身能跑（它用的是同一目录里
    # 的 node.exe），但 npm 干活的时候会再按名字去叫 node——实测 npm --version 正常、
    # 而 npm install 会因为找不到 node 直接失败。所以把它所在目录临时加到子进程 PATH 的
    # 最前面：只影响这一次构建，不动系统环境变量。
    environment = None
    vendor_npm = local_npm()
    if vendor_npm is not None:
        environment = {
            **os.environ,
            "PATH": str(Path(vendor_npm).parent) + os.pathsep + os.environ.get("PATH", ""),
        }
    print()
    print("  前端页面要按当前源码重新构建（第一次或源码有变动时会比较慢）")
    print("  先删掉旧的构建结果，避免构建失败后还打开上一版页面")
    try:
        clear_dist(ROOT)
    except OSError as exc:
        print(f"    [!] 删不掉旧的前端产物：{exc}")
        print("    [!] 请先关掉正在运行的服务，再重新启动")
        return False
    for script in ("front:install", "front:build"):
        print(f"    > npm run {script}")
        # 中文注释：Windows 上的 npm 其实是个 .cmd 批处理文件，不能当普通可执行文件
        # 直接启动（会报"系统找不到指定的文件"），必须交给系统 shell 去跑。
        # 打包脚本 scripts/package.py 里用的是同一个写法。
        result = subprocess.run(
            [npm, "run", script], cwd=ROOT, shell=(os.name == "nt"), env=environment
        )
        if result.returncode != 0:
            print(f"    [!] 这条命令失败了（退出码 {result.returncode}），上面的输出里有原因")
            print("    [!] 旧页面已经删掉，本次不会再用上一版界面启动")
            return False
    write_stamp(ROOT)
    print("    前端构建完成")
    return True


def frontend_build_hint(problems: list[str]) -> list[str]:
    """前端产物有问题时，补一条「怎么修」的提示。

    中文注释：提示往哪个方向指，取决于现在有没有可用的 npm——有的话启动时会自动
    构建，剩下的失败基本是构建本身报错；没有的话启动时会问要不要自动下载一份绿色版
    Node（也可以自己装）。--check 是只读模式，不会问也不会装，只把这条路说清楚。
    """

    if not problems:
        return []
    if has_npm():
        return [
            "修复方式：直接启动就会自动构建前端；也可以手动执行 "
            "npm run front:install 和 npm run front:build"
        ]
    return [
        "修复方式：本机没有 npm。直接双击启动时会询问是否自动下载一份绿色版 Node"
        "（放进项目的 tools/node/ 下，不需要管理员权限）；",
        "也可以自己装好 Node.js（自带 npm），再执行 "
        "npm run front:install 和 npm run front:build",
    ]


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

    # 第一步：确认前端产物和当前源码一致。缺产物、产物坏了、或产物是按旧源码打的，
    # 都先删掉再构建。git 克隆出来的目录没有 front/dist；拷走的目录常常带着一份旧的。
    # 构建失败就拒绝启动，不能继续打开旧页面。压缩包里没有前端源码，只检查产物齐不齐。
    problems = check_frontend()
    if problems and not has_npm():
        # 中文注释：构建前端要 npm。没有就先问一句要不要自动装一份绿色版 Node。
        # 这一步只发生在真正的启动路径上——--check 是只读的，既不问也不下载；
        # 而且没有人对着终端时（管道、定时任务）ask_to_install_node 直接返回 False。
        if ask_to_install_node():
            install_portable_node()
    if problems and has_npm():
        if build_frontend_in_place():
            problems = check_frontend()
    if problems:
        print()
        print("  无法启动：前端页面还不能用。")
        for problem in problems:
            print(f"    - {problem}")
        for hint in frontend_build_hint(problems):
            print(f"    - {hint}")
        print()
        print("  如果你是从压缩包解压出来的目录，说明包里的前端产物有问题，请找发布的人重新要一份。")
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

    # 中文注释：全文阅读用的 Nougat 不在主程序的依赖里。没装过就在这里装上，
    # 已经能导入就跳过，避免每次启动都重新下载那份很大的显卡版 PyTorch。
    if ensure_nougat_env() != 0:
        return 1

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
