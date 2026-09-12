"""打包脚本：把项目打成一个可以直接发给同事的 zip。

用法（在项目根目录执行）：
    uv run python scripts/package.py

它做四件事：
1. 构建前端产物（同事那边不需要装 Node，所以产物必须由我们构建好一起打进去）；
2. 把需要的文件按白名单复制到一个临时目录；
3. 用多种手段确认包里没有 API 密钥；
4. 打成 zip，再把 zip 内容复查一遍。

为什么不用 git 来生成包：这个目录根本不是 git 仓库，而且 .gitignore 里有一条裸的
`dist/` 规则会匹配到任意层级的 front/dist，任何走 git 的归档都会静默丢掉整个前端。
所以这里完全自己控制复制哪些文件。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 要放进包里的单个文件。
INCLUDE_FILES = [
    "main.py",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "start.bat",
    "README.md",
    "使用说明.md",
    "config/system.yaml",
    "config/model.example.json",
    "scripts/launch.py",
]

# 要递归放进包里的目录。
INCLUDE_DIRS = [
    "src",
    "front/dist",
]

# 递归复制时要跳过的目录名和文件后缀。
SKIP_DIR_NAMES = {"__pycache__", ".vite", "node_modules"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".tsbuildinfo", ".log"}

# 绝对不允许出现在包里的路径（相对项目根）。出现的就说明打包逻辑写错了。
FORBIDDEN = [
    "config/model.json",
    "data",
    "logs",
    ".venv",
    "front/node_modules",
]

# 密钥特征：一种是我们自己用的 sk- 开头格式，一种是任何填了值的 api_key 字段。
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r'"api_key"\s*:\s*"[^"]+"'),
]

# 需要按文本扫描的文件类型。没有后缀的也要扫（例如 .python-version）。
TEXT_SUFFIXES = {
    "", ".py", ".json", ".yaml", ".yml", ".md", ".txt", ".ts", ".js",
    ".css", ".html", ".bat", ".toml", ".lock", ".cfg", ".ini", ".cmd",
}


def log(message: str) -> None:
    print(f"  {message}")


def run_command(args: list[str], cwd: Path) -> None:
    """执行一条外部命令，失败就抛出异常并带上原始输出。"""

    log("执行：" + " ".join(args))
    result = subprocess.run(args, cwd=cwd, shell=(os.name == "nt"))
    if result.returncode != 0:
        raise RuntimeError(f"命令执行失败（退出码 {result.returncode}）：{' '.join(args)}")


def build_frontend() -> None:
    """构建前端产物。

    这里必须检查退出码。构建脚本是 `vue-tsc --noEmit && vite build`，
    类型检查不过就会失败；如果放任不管，打出来的包里会是上一次的旧产物，
    而同事那边根本不知道为什么界面不对劲。
    """

    print()
    print("[1/6] 构建前端产物")
    run_command(["npm", "run", "front:install"], ROOT)
    run_command(["npm", "run", "front:build"], ROOT)

    index_file = ROOT / "front" / "dist" / "index.html"
    assets_dir = ROOT / "front" / "dist" / "assets"
    if not index_file.is_file() or not assets_dir.is_dir():
        raise RuntimeError("前端构建完成，但没有产出完整的 front/dist（缺 index.html 或 assets 目录）")
    log("前端产物已就绪")


def copy_tree(source: Path, target: Path) -> int:
    """递归复制目录，跳过不需要的东西，返回复制的文件数。"""

    copied = 0
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        if any(part in SKIP_DIR_NAMES for part in relative.parts):
            continue
        if item.is_dir():
            continue
        if item.suffix in SKIP_SUFFIXES:
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, destination)
        copied += 1
    return copied


def stage_project(staging: Path) -> None:
    """按白名单把要发布的文件复制到临时目录。"""

    print()
    print("[2/6] 复制需要发布的内容")
    for name in INCLUDE_FILES:
        source = ROOT / name
        if not source.is_file():
            raise RuntimeError(f"缺少要打包的文件：{name}")
        destination = staging / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        # 中文注释：按字节复制，不做文本解码。这些文件里有中文，Windows 上
        # 用文本方式读写容易按系统默认编码处理从而弄出乱码。
        shutil.copyfile(source, destination)
        log(f"文件 {name}")

    for name in INCLUDE_DIRS:
        source = ROOT / name
        if not source.is_dir():
            raise RuntimeError(f"缺少要打包的目录：{name}")
        count = copy_tree(source, staging / name)
        log(f"目录 {name}/（{count} 个文件）")

    # 中文注释：包目录本身不需要带上，否则以后重新打包会把旧的包越滚越大。
    for item in staging.glob("Paper-Agent-*.zip"):
        item.unlink()


def stage_uv(staging: Path) -> None:
    """把 uv.exe 复制进包，这样同事什么都不用装。"""

    print()
    print("[3/6] 准备内置的 uv")
    found = find_uv()
    if found is None:
        raise RuntimeError(
            "本机找不到 uv.exe。请先安装 uv，或手动把一个 uv.exe 放到项目的 tools/ 目录下再重新打包"
        )
    tools_dir = staging / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(found, tools_dir / "uv.exe")
    size_mb = (tools_dir / "uv.exe").stat().st_size / 1024 / 1024
    log(f"已内置 {found}（{size_mb:.1f} MB）")


def find_uv() -> Path | None:
    """在本机找 uv.exe：先看项目自己的 tools/，再看 PATH，最后看常见安装位置。"""

    local = ROOT / "tools" / "uv.exe"
    if local.is_file():
        return local

    on_path = shutil.which("uv")
    if on_path:
        return Path(on_path)

    candidates = [
        Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "uv.exe",
        Path(os.environ.get("USERPROFILE", "")) / ".cargo" / "bin" / "uv.exe",
        Path(sys.base_prefix) / "Scripts" / "uv.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def staged_files(staging: Path) -> list[Path]:
    """列出打包目录里的所有文件。"""

    return sorted(item for item in staging.rglob("*") if item.is_file())


def assert_no_forbidden(staging: Path) -> None:
    """确认打包目录里没有不该出现的东西。"""

    for name in FORBIDDEN:
        if (staging / name).exists():
            raise RuntimeError(f"打包目录里出现了不该有的内容：{name}")


def scan_text(path: Path) -> list[str]:
    """把一个文件按文本扫一遍，返回命中的密钥特征。"""

    if path.suffix.lower() not in TEXT_SUFFIXES:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    hits = []
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            hits.append(match.group(0)[:40])
    return hits


def assert_no_secrets(staging: Path) -> None:
    """扫描打包目录，任何密钥特征都直接中止打包。

    这一步是「包里不会有密钥」的主要保证。它之所以成立，是因为示例配置里的
    api_key 是空值，而真正的 config/model.json 从来就不在白名单里——
    两件事叠加，密钥在结构上就没有进入包的路径。扫描只是最后一道兜底。
    """

    print()
    print("[4/6] 扫描打包内容里的密钥特征")
    findings: list[str] = []
    for path in staged_files(staging):
        for hit in scan_text(path):
            findings.append(f"{path.relative_to(staging)} -> {hit}")

    if findings:
        for finding in findings:
            log(f"[!] {finding}")
        raise RuntimeError("打包目录里发现了疑似密钥，已中止打包")

    log(f"已扫描 {len(staged_files(staging))} 个文件，未发现密钥特征")


def run_self_check(staging: Path) -> None:
    """对打包目录跑一次启动器自检，确认同事那边能正常启动。"""

    print()
    print("[5/6] 对打包目录运行启动器自检")
    result = subprocess.run(
        [sys.executable, str(staging / "scripts" / "launch.py"), "--check"],
        cwd=staging,
    )
    if result.returncode != 0:
        raise RuntimeError("启动器自检未通过，这个包发出去同事会启动失败")

    # 中文注释：上面的启动器自检只查环境（解释器、前端产物、目录结构），它**不导入
    # 应用本体**，所以拦不住另一类事故：应用在导入期就要读某个文件，而那个文件没被打进包。
    # 现在就有这样的文件——src/agents/skills/ 下的技能文档。Prompts.py 一被导入
    # （也就是 uvicorn 一启动）就要读它，读不到当场抛异常、界面全无。这类"运行时依赖的
    # 非 .py 文件"以后只会更多，所以这里补一步：真的把应用导入一次。
    _run_import_smoke(staging)


# 中文注释：应用在导入期会自己写出来的目录。导入自检之后必须把它们清掉，
# 否则会被 write_zip 一起打进包，而这两样恰恰是包最不该带的东西
# （下面 FORBIDDEN 列表就是专门拦它们的）。
_IMPORT_SMOKE_SIDE_EFFECTS = ("data", "logs")


def _run_import_smoke(staging: Path) -> None:
    """在打包目录里真的把应用导入一次，确认它能装配起来。

    中文注释：这一步必须排在 assert_no_forbidden 之后跑，而且**自检不许改动要发布的
    内容**——导入应用本身会写东西（日志目录、会话目录），还可能顺手写出 .pyc 字节码。
    所以这里做两件事：跑完把已知的副作用目录清掉，再拿文件清单前后对比，多出任何东西
    都当场中止，绝不带着它们去打包。
    """

    before = {item.relative_to(staging).as_posix() for item in staged_files(staging)}
    result = subprocess.run(
        # 中文注释：-B 表示这次导入不要写 .pyc 字节码。不加它的话，自检会在 src/ 各处
        # 留下一堆 __pycache__，而它们紧接着就会被 write_zip 打进包里。
        [sys.executable, "-B", "-c", "import main"],
        cwd=staging,
    )
    for name in _IMPORT_SMOKE_SIDE_EFFECTS:
        leftover = staging / name
        if leftover.exists():
            shutil.rmtree(leftover, ignore_errors=True)
    if result.returncode != 0:
        raise RuntimeError(
            "打包目录里的应用导入失败，这个包发出去同事会启动不起来。"
            "最常见的原因是应用运行时需要读的文件没被打进包（见 INCLUDE_FILES / INCLUDE_DIRS）"
        )
    after = {item.relative_to(staging).as_posix() for item in staged_files(staging)}
    appeared = sorted(after - before)
    if appeared:
        shown = "、".join(appeared[:5])
        raise RuntimeError(f"导入自检往打包目录里写了内容（{shown} 等 {len(appeared)} 个），不能就这样打包")
    log("应用能在打包目录里正常导入")


def write_zip(staging: Path, zip_path: Path) -> None:
    """把打包目录压成 zip。"""

    print()
    print("[6/6] 生成压缩包")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in staged_files(staging):
            archive.write(path, Path("Paper-Agent") / path.relative_to(staging))
    log(f"已生成 {zip_path.name}")


def verify_zip(zip_path: Path) -> None:
    """把打好的 zip 再打开复查一遍。

    单独做这一步是因为前面的扫描只覆盖了磁盘上的打包目录；如果打包逻辑出了意外
    （比如把整个包含旧 zip 的目录又打了一层），磁盘扫描就看不出来。所以这里重新
    读一次压缩包里的实际内容，重跑同样的检查。
    """

    print()
    print("复查压缩包内容")
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()

        for name in names:
            stripped = name.split("Paper-Agent/", 1)[-1]
            for forbidden in FORBIDDEN:
                if stripped == forbidden or stripped.startswith(forbidden + "/"):
                    raise RuntimeError(f"压缩包里出现了不该有的内容：{name}")

        scanned = 0
        for name in names:
            if name.endswith("/"):
                continue
            if Path(name).suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                text = archive.read(name).decode("utf-8", errors="ignore")
            except Exception:
                continue
            scanned += 1
            for pattern in SECRET_PATTERNS:
                match = pattern.search(text)
                if match:
                    raise RuntimeError(f"压缩包里发现疑似密钥：{name} -> {match.group(0)[:40]}")

        log(f"压缩包内 {len(names)} 个条目，扫描了 {scanned} 个文本文件，未发现密钥")


def report(zip_path: Path) -> None:
    """打印压缩包摘要和文件清单，方便人工复核。"""

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    size_mb = zip_path.stat().st_size / 1024 / 1024

    print()
    print("=" * 66)
    print(f"  打包完成：{zip_path}")
    print(f"  大小：{size_mb:.1f} MB")
    print(f"  SHA-256：{digest}")
    print("=" * 66)
    print()
    print("  压缩包内容：")
    with zipfile.ZipFile(zip_path) as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            if info.is_dir():
                continue
            relative = info.filename.split("Paper-Agent/", 1)[-1]
            print(f"    {relative}")
    print()
    print("  请人工确认上面这份清单里没有 config/model.json、data、logs。")


def main() -> int:
    # 中文注释：输出被重定向到管道时 Python 默认是块缓冲，而子进程（npm、自检）的输出
    # 会立刻写出来，两边的日志就会错位——自检结果可能跑到步骤标题前面去。
    # 改成按行刷新，保证日志顺序和实际执行顺序一致。
    sys.stdout.reconfigure(line_buffering=True)

    staging: Path | None = None
    try:
        build_frontend()

        staging = Path(tempfile.mkdtemp(prefix="paper_agent_package_"))
        stage_project(staging)
        stage_uv(staging)

        assert_no_forbidden(staging)
        assert_no_secrets(staging)
        run_self_check(staging)

        zip_path = ROOT / f"Paper-Agent-{datetime.now().strftime('%Y%m%d')}.zip"
        write_zip(staging, zip_path)
        verify_zip(zip_path)
        report(zip_path)
        return 0
    except Exception as exc:
        print()
        print(f"打包失败：{exc}")
        if staging is not None:
            print(f"临时目录保留在：{staging}")
        return 1
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
