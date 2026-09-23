"""前端产物和源码是否一致。

启动器和打包脚本都用这里的判断，避免两处各写一套、结果一个更新、一个还在用旧页面。

压缩包里只有构建好的页面，没有前端源码。那种目录只检查页面文件齐不齐。
开发目录里有 front/src 时，还要核对构建记录：对不上就说明这份页面是按旧源码打的。
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

# 首页里对静态资源的引用，例如 src="/assets/index-xxxx.js"。
ASSET_REF_PATTERN = re.compile(r"""["'](/assets/[^"']+)["']""")

# 构建记录放在产物目录里。Vite 每次构建会先清空这个目录，所以记录要在构建成功后再写。
STAMP_NAME = "build-stamp.txt"

# 这些文件一变，打出来的页面就可能变。单独列出，不跟源码目录混在一起。
FINGERPRINT_FILES = (
    "front/index.html",
    "front/vite.config.ts",
    "front/package.json",
    "front/package-lock.json",
    "front/tsconfig.json",
    "front/tsconfig.app.json",
    "front/tsconfig.node.json",
)

# 页面源码和放进产物里的静态图。
FINGERPRINT_DIRS = ("front/src", "front/public")

# 走目录时跳过这些名字，它们不是源码。
SKIP_DIR_NAMES = {"node_modules", "__pycache__", ".vite"}


def dist_dir(root: Path) -> Path:
    """前端构建结果所在的目录。"""

    return root / "front" / "dist"


def source_present(root: Path) -> bool:
    """这个目录里有没有前端源码。

    发给同事的压缩包只带构建结果，没有 front/src。那种情况不能要求按源码重建，
    否则同事没有 Node 也打不开页面。
    """

    return (root / "front" / "src").is_dir()


def clear_dist(root: Path) -> None:
    """删掉已有的前端构建结果。

    构建失败时如果旧文件还在，下次启动会以为页面可用，打开的仍是上一版。
    所以只要决定重新构建，就先把整个产物目录删掉。
    """

    target = dist_dir(root)
    if target.exists():
        shutil.rmtree(target)


def source_fingerprint(root: Path) -> str:
    """给当前前端源码算一个指纹，源码有改动时指纹会变。"""

    digest = hashlib.sha256()
    for path in _fingerprint_files(root):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def write_stamp(root: Path) -> None:
    """构建成功后，把源码指纹写进产物目录。"""

    stamp = dist_dir(root) / STAMP_NAME
    stamp.write_text(
        "\n".join(
            [
                "# 源码指纹。和当前源码对不上时，启动会重新构建，避免打开旧页面。",
                source_fingerprint(root),
                "",
            ]
        ),
        encoding="utf-8",
    )


def read_stamp(root: Path) -> str:
    """读出产物里记下的源码指纹。没有记录时返回空字符串。"""

    stamp = dist_dir(root) / STAMP_NAME
    if not stamp.is_file():
        return ""
    for line in stamp.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            return text
    return ""


def completeness_problems(root: Path) -> list[str]:
    """检查构建结果能不能被浏览器正常打开，返回问题清单。"""

    problems: list[str] = []
    dist = dist_dir(root)
    index_file = dist / "index.html"
    assets_dir = dist / "assets"

    if not index_file.is_file():
        problems.append(f"找不到前端首页：{index_file}")
        return problems
    if not assets_dir.is_dir():
        problems.append(f"找不到前端静态资源目录：{assets_dir}")
        return problems

    # 显式用 utf-8 读。Windows 上按系统默认编码读会解出乱码甚至直接报错。
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


def frontend_problems(root: Path) -> list[str]:
    """检查页面能不能用，以及（有源码时）是不是按当前源码打的。"""

    problems = completeness_problems(root)
    if problems or not source_present(root):
        return problems

    current = source_fingerprint(root)
    recorded = read_stamp(root)
    if recorded != current:
        problems.append(
            "前端产物和当前源码不一致（或还没有构建记录）。"
            "直接沿用的话打开的会是旧页面，需要按当前源码重新构建"
        )
    return problems


def _fingerprint_files(root: Path) -> list[Path]:
    """列出参与指纹的文件，按相对路径排序，保证同一份源码每次算出的指纹相同。"""

    files: list[Path] = []
    for name in FINGERPRINT_FILES:
        path = root / name
        if path.is_file():
            files.append(path)
    for name in FINGERPRINT_DIRS:
        folder = root / name
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in path.relative_to(folder).parts):
                continue
            files.append(path)
    files.sort(key=lambda path: path.relative_to(root).as_posix())
    return files
