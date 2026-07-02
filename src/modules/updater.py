"""自动更新模块 - 检测新版本 + 下载 + 应用更新

更新流程:
1. 启动时 & 每小时检测更新
   - 优先查 GitHub Release API (https://api.github.com/repos/qinchangxv/antigravity-tools/releases/latest)
   - GitHub 失败时 fallback 到旧服务器 (http://103.36.63.44:9680/version.json)
2. 对比本地版本号 (src/VERSION)
3. 有新版本 → 弹窗提示(含changelog) → 用户确认 → 下载更新包
4. 源码模式: 解压覆盖 src/ → 提示重启
   打包模式: 下载 zip → 批处理替换 → 自动重启

双源策略保证旧版（只查服务器）和新版（优先GitHub）都能收到更新通知。
"""

import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

from PySide6.QtCore import QObject, Signal, QTimer, Slot

logger = logging.getLogger(__name__)

# ─── 更新源（双源策略：GitHub 优先，服务器兜底）───
GITHUB_REPO = "qinchangxv/antigravity-tools"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
# GitHub token 认证（避免 rate limit，从环境变量读取，不在代码中硬编码）
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# 旧服务器（fallback）
UPDATE_SERVER = "http://103.36.63.44:9680"
VERSION_URL = f"{UPDATE_SERVER}/version.json"

# 本地版本文件
VERSION_FILE = Path(__file__).parent.parent / "VERSION"


def get_current_version() -> str:
    """读取本地版本号"""
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    return "0.0.0"


def _get_platform_key() -> str:
    """获取当前平台标识（windows / mac）"""
    if sys.platform == "darwin":
        return "mac"
    return "windows"


def _get_platform_asset_keyword() -> str:
    """GitHub Release asset 文件名关键词，用于匹配当前平台的下载包"""
    if sys.platform == "darwin":
        # macOS ARM 和 Intel 各有单独的包
        import platform
        machine = platform.machine().lower()
        if machine == "arm64" or "arm" in machine:
            return "macOS-ARM"
        return "macOS-Intel"
    return "Windows-x64"


def _compare_versions(remote: str, local: str) -> bool:
    """比较版本号，remote > local 返回 True"""
    try:
        r_parts = [int(x) for x in remote.strip().split(".")]
        l_parts = [int(x) for x in local.strip().split(".")]
        # 补齐长度
        max_len = max(len(r_parts), len(l_parts))
        r_parts += [0] * (max_len - len(r_parts))
        l_parts += [0] * (max_len - len(l_parts))
        return r_parts > l_parts
    except (ValueError, AttributeError):
        return False


def _fetch_github_release(timeout: int = 15) -> dict | None:
    """从 GitHub Release API 获取最新版本信息

    返回格式与旧服务器 version.json 兼容：
    {
        "version": "1.6.1",
        "changelog": "...",
        "download_url": "https://github.com/.../Antigravity-Tools-Windows-x64.zip",
        "sha256": "",
        "source": "github"
    }
    """
    try:
        import urllib.request
        req = urllib.request.Request(GITHUB_API_URL)
        req.add_header("User-Agent", "AntigravityTools/1.0")
        req.add_header("Accept", "application/vnd.github+json")
        if GITHUB_TOKEN:
            req.add_header("Authorization", f"token {GITHUB_TOKEN}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # 从 tag_name 提取版本号 (v1.5.9 → 1.5.9)
        tag = data.get("tag_name", "")
        version = tag.lstrip("v").strip()
        if not version:
            logger.warning("GitHub Release 无 tag_name")
            return None

        # changelog 用 release body
        changelog = data.get("body", "") or f"v{version} 更新"

        # 匹配当前平台的 asset（优先增量包，其次完整包）
        keyword = _get_platform_asset_keyword()
        download_url = ""
        src_download_url = ""
        for asset in data.get("assets", []):
            asset_name = asset.get("name", "")
            asset_url = asset.get("browser_download_url", "")
            asset_lower = asset_name.lower()
            if "-src" in asset_lower or ".src." in asset_lower:
                # 增量包（只含 src/）— [v1.6.1-fix] 增量包不限平台，所有平台通用
                src_download_url = asset_url
            elif keyword.lower() in asset_lower and not download_url:
                # 完整包（需要匹配平台）
                download_url = asset_url

        if not download_url:
            download_url = data.get("html_url", "")
            logger.info(f"GitHub Release 无 {keyword} asset，使用 Release 页面 URL")

        result = {
            "version": version,
            "changelog": changelog,
            "download_url": download_url,
            "src_download_url": src_download_url,
            "sha256": "",
            "source": "github",
        }
        logger.info(f"GitHub Release 检测到版本 {version}（源: GitHub），增量包={'有' if src_download_url else '无'}")
        return result

    except Exception as e:
        logger.warning(f"GitHub Release 检测失败: {e}，尝试旧服务器")
        return None


def _fetch_server_version(timeout: int = 10) -> dict | None:
    """从旧服务器获取版本信息（fallback）"""
    try:
        import urllib.request
        req = urllib.request.Request(VERSION_URL)
        req.add_header("User-Agent", "AntigravityTools/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            data["source"] = "server"
            logger.info(f"服务器检测到版本 {data.get('version', '?')}（源: 服务器）")
            return data
    except Exception as e:
        logger.warning(f"服务器检查更新也失败: {e}")
        return None


def _fetch_version_info(timeout: int = 15) -> dict | None:
    """获取版本信息 — GitHub 优先，服务器兜底"""
    # 1. 先查 GitHub Release
    info = _fetch_github_release(timeout=timeout)
    if info:
        platform_key = _get_platform_key()
        return {
            "version": info["version"],
            "platforms": {
                platform_key: {
                    "version": info["version"],
                    "changelog": info["changelog"],
                    "download_url": info["download_url"],
                    "src_download_url": info.get("src_download_url", ""),
                    "sha256": info.get("sha256", ""),
                }
            },
            "source": "github",
        }

    # 2. GitHub 失败，查旧服务器
    info = _fetch_server_version(timeout=timeout)
    if info:
        return info

    logger.warning("所有更新源均不可用")
    return None


def _download_update(url: str, dest: Path, progress_callback=None, timeout: int = 300) -> bool:
    """下载更新包，支持进度回调"""
    try:
        import urllib.request
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "AntigravityTools/1.0")

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 65536

            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total > 0:
                        progress_callback(downloaded, total)

        return dest.exists() and dest.stat().st_size > 0
    except Exception as e:
        logger.error(f"下载更新失败: {e}")
        return False


def _verify_sha256(file_path: Path, expected: str) -> bool:
    """验证文件 SHA256"""
    if not expected:
        return True  # 没有校验值则跳过
    import hashlib
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest() == expected


def _apply_src_only_update(zip_path: Path) -> bool:
    """增量更新：只替换 src/ 目录

    Windows: 批处理等待进程退出后 robocopy _internal/src/
    macOS: ditto 直接覆盖 .app/Contents/Resources/src/
    """
    import subprocess

    try:
        # [v1.6.1-fix] 不用 TemporaryDirectory，因为它在函数 return 后会自动删除，
        # 而批处理是异步执行的，等它跑 robocopy 时临时目录已经没了。
        # 改用手动管理的持久临时目录，批处理完成后自己清理。
        tmp_dir = Path(tempfile.gettempdir()) / "ag_src_update"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_dir)

        # 找到解压后的 src 目录
        extracted_src = tmp_dir / "src"
        if not extracted_src.exists():
            for sub in tmp_dir.iterdir():
                if sub.is_dir() and (sub / "src").exists():
                    extracted_src = sub / "src"
                    break
        if not extracted_src.exists():
            logger.error("增量包中未找到 src/ 目录")
            return False

        # [v1.6.1-fix] 把解压后的 src 复制到批处理旁边，避免临时目录被清理
        # 批处理用这个副本做 robocopy，而不是用解压目录本身
        stable_copy = Path(tempfile.gettempdir()) / "ag_src_stable"
        if stable_copy.exists():
            shutil.rmtree(stable_copy, ignore_errors=True)
        shutil.copytree(extracted_src, stable_copy)
        logger.info(f"增量包已复制到稳定目录: {stable_copy}")

        if sys.platform == "darwin":
            # macOS: 找到 .app 的 src 目录
            current_exe = Path(sys.executable)
            app_path = current_exe
            while app_path.parent.name != "" and not app_path.name.endswith(".app"):
                app_path = app_path.parent
            if not app_path.name.endswith(".app"):
                for p in current_exe.parents:
                    if p.name.endswith(".app"):
                        app_path = p
                        break

            src_dir = app_path / "Contents" / "Resources" / "src"
            if not src_dir.exists():
                logger.error(f"macOS src 目录不存在: {src_dir}")
                return False

            # ditto 覆盖
            subprocess.run(["rm", "-rf", str(src_dir)], capture_output=True, timeout=30)
            result = subprocess.run(
                ["ditto", str(stable_copy), str(src_dir)],
                capture_output=True, timeout=60
            )
            if result.returncode != 0:
                logger.error(f"ditto 覆盖 src 失败: {result.stderr.decode(errors='replace')[:200]}")
                return False

            # 清理临时目录
            shutil.rmtree(tmp_dir, ignore_errors=True)
            shutil.rmtree(stable_copy, ignore_errors=True)
            logger.info("macOS 增量更新成功")
            return True

        else:
            # Windows: PowerShell 替换 _internal/src/
            # [v1.6.1-fix] 改用 PowerShell 替代 bat，避免中文路径 GBK/UTF-8 编码乱码
            current_exe = Path(sys.executable)
            src_dir = current_exe.parent / "_internal" / "src"
            if not src_dir.exists():
                logger.error(f"Windows src 目录不存在: {src_dir}")
                return False

            ps_path = Path(tempfile.gettempdir()) / "ag_src_updater.ps1"
            ps_content = f"""Start-Sleep -Seconds 2

while (Get-Process -Id {os.getpid()} -ErrorAction SilentlyContinue) {{
    Start-Sleep -Milliseconds 500
}}

$ErrorActionPreference = "Stop"

Remove-Item -LiteralPath '{src_dir}' -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item -LiteralPath '{stable_copy}' -Destination '{src_dir}' -Recurse -Force

if (-not (Test-Path -LiteralPath '{src_dir}\\VERSION')) {{
    "VERSION missing after copy" | Out-File -LiteralPath '$TEMP\\ag_update_error.log' -Encoding utf8
    exit 12
}}

Remove-Item -LiteralPath '{tmp_dir}' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath '{stable_copy}' -Recurse -Force -ErrorAction SilentlyContinue

Start-Process -FilePath '{current_exe}'

Remove-Item -LiteralPath '{ps_path}' -Force -ErrorAction SilentlyContinue
"""
            ps_path.write_text(ps_content, encoding="utf-8-sig")

            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0

            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy", "Bypass",
                    "-File", str(ps_path),
                ],
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            logger.info("Windows 增量更新 PowerShell 已启动")
            return True

    except Exception as e:
        logger.error(f"增量更新失败: {e}")
        return False


def _apply_frozen_update(zip_path: Path) -> bool:
    """打包模式下的自动更新

    Windows: 下载 zip → 批处理替换 → 重启
    macOS: 下载 zip → ditto 覆盖 .app → 重启
    """
    import subprocess

    try:
        if sys.platform == "darwin":
            return _apply_frozen_update_mac(zip_path)
        else:
            return _apply_frozen_update_windows(zip_path)
    except Exception as e:
        logger.error(f"打包模式更新失败: {e}")
        return False


def _apply_frozen_update_mac(zip_path: Path) -> bool:
    """macOS 打包模式：ditto 覆盖 .app"""
    import subprocess

    try:
        current_exe = Path(sys.executable)
        app_path = current_exe
        while app_path.parent.name != "" and not app_path.name.endswith(".app"):
            app_path = app_path.parent
        if not app_path.name.endswith(".app"):
            for p in current_exe.parents:
                if p.name.endswith(".app"):
                    app_path = p
                    break

        if not app_path.name.endswith(".app"):
            logger.error(f"无法定位 .app 目录，当前 exe: {current_exe}")
            return False

        logger.info(f"当前 .app 路径: {app_path}")

        with tempfile.TemporaryDirectory(prefix="ag_update_") as tmp_dir:
            tmp = Path(tmp_dir)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmp)

            new_app = None
            for app_dir in tmp.rglob("*.app"):
                new_app = app_dir
                break

            if not new_app:
                logger.error("更新包中未找到 .app 文件")
                return False

            logger.info(f"新版本 .app: {new_app}")

            subprocess.run(["rm", "-rf", str(app_path)], capture_output=True, timeout=30)
            result = subprocess.run(
                ["ditto", str(new_app), str(app_path)],
                capture_output=True, timeout=120
            )
            if result.returncode != 0:
                logger.error(f"ditto 覆盖失败: {result.stderr.decode(errors='replace')[:200]}")
                return False

            exe_in_app = app_path / "Contents" / "MacOS" / "Antigravity Tools"
            if exe_in_app.exists():
                subprocess.run(["chmod", "+x", str(exe_in_app)], capture_output=True)

        logger.info("macOS 更新覆盖成功")
        return True

    except Exception as e:
        logger.error(f"macOS 打包模式更新失败: {e}")
        return False


def _apply_frozen_update_windows(zip_path: Path) -> bool:
    """Windows 打包模式：批处理替换"""
    import subprocess

    try:
        current_exe = Path(sys.executable)
        app_dir = current_exe.parent

        with tempfile.TemporaryDirectory(prefix="ag_update_") as tmp_dir:
            tmp = Path(tmp_dir)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmp)

            new_exe = None
            for exe_path in tmp.rglob("*.exe"):
                if "Antigravity Tools" in exe_path.name or "antigravity" in exe_path.name.lower():
                    new_exe = exe_path
                    break
            if not new_exe:
                for exe_path in tmp.rglob("*.exe"):
                    new_exe = exe_path
                    break
            if not new_exe:
                logger.error("更新包中未找到 exe 文件")
                return False

            new_app_dir = new_exe.parent

            bat_path = Path(tempfile.gettempdir()) / "ag_updater.bat"
            bat_content = f"""@echo off
chcp 65001 >nul 2>&1
timeout /t 2 /nobreak >nul

:wait_loop
tasklist /fi "pid eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul
if %errorlevel% == 0 goto wait_loop

timeout /t 1 /nobreak >nul

robocopy "{new_app_dir}" "{app_dir}" /E /IS /IT /NFL /NDL /NJH /NJS /nc /ns /np

start "" "{current_exe}"

del "%~f0"
"""

            bat_path.write_text(bat_content, encoding="gbk", errors="replace")

            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0

            subprocess.Popen(
                ["cmd", "/c", str(bat_path)],
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            logger.info("更新批处理已启动，即将退出应用以完成更新")
            return True

    except Exception as e:
        logger.error(f"Windows 打包模式更新失败: {e}")
        return False


def _apply_update(zip_path: Path) -> bool:
    """应用更新包 — 解压覆盖 src/ 目录

    注意：PyInstaller 打包后此功能不可用（代码在 _MEIPASS 临时目录中）。
    打包模式下提示用户到官网下载新版。
    """
    try:
        if getattr(sys, 'frozen', False):
            logger.warning("打包模式下不支持自动更新覆盖，请手动下载新版")
            return False

        project_root = Path(__file__).parent.parent.parent  # 项目根目录
        src_dir = project_root / "src"

        # 解压到临时目录
        with tempfile.TemporaryDirectory(prefix="ag_update_") as tmp_dir:
            tmp = Path(tmp_dir)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmp)

            # 查找解压后的 src 目录（可能在根目录或子目录下）
            extracted_src = tmp / "src"
            if not extracted_src.exists():
                # 可能有一层包装目录
                for sub in tmp.iterdir():
                    if sub.is_dir() and (sub / "src").exists():
                        extracted_src = sub / "src"
                        break

            if not extracted_src.exists():
                logger.error("更新包中未找到 src/ 目录")
                return False

            # 备份当前 src
            backup_dir = project_root / f"src_backup_{get_current_version()}"
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            shutil.copytree(src_dir, backup_dir)

            # 复制新文件覆盖
            for item in extracted_src.iterdir():
                dest = src_dir / item.name
                if dest.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)

            # 更新版本号文件
            version_in_zip = extracted_src.parent / "VERSION"
            if version_in_zip.exists():
                shutil.copy2(version_in_zip, VERSION_FILE)

            # 清理备份（延迟删除，避免文件锁定）
            try:
                shutil.rmtree(backup_dir)
            except Exception:
                pass

        logger.info("更新应用成功")
        return True

    except Exception as e:
        logger.error(f"应用更新失败: {e}")
        return False


class UpdateChecker(QObject):
    """自动更新检查器 — 在主线程中运行，通过信号通知UI"""

    # 信号：发现新版本 (version, changelog, download_url, sha256)
    update_available = Signal(str, str, str, str)
    # 信号：更新下载进度 (downloaded_bytes, total_bytes)
    download_progress = Signal(int, int)
    # 信号：更新完成 (success: bool, message: str)
    update_finished = Signal(bool, str)
    # 信号：检查完成但无更新 (is_manual: bool)
    no_update = Signal(bool)

    # 存储增量包 URL（检测到时保存，下载时用）
    _src_download_url = ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.check_update)
        self._checking = False
        self._downloading = False
        self._manual_check = False  # 标记是否为手动检查
        self._notified_version = ""  # 已经提示过的版本号，同一版本不重复弹窗

    def start_periodic_check(self, interval_ms: int = 3600_000):
        """启动定期检查（默认1小时）"""
        # 首次延迟5秒检查（等UI完全加载）
        QTimer.singleShot(5000, self.check_update)
        self._timer.start(interval_ms)

    def stop(self):
        """停止定期检查"""
        self._timer.stop()

    @Slot()
    def check_update(self):
        """检查是否有新版本（后台线程）"""
        if self._checking or self._downloading:
            return
        self._checking = True

        def _do_check():
            current = get_current_version()
            info = _fetch_version_info()
            self._checking = False

            if not info:
                self.no_update.emit(self._manual_check)
                return

            # 分平台读取版本信息
            platform_key = _get_platform_key()
            platforms = info.get("platforms", {})

            if platforms:
                # 新格式：分平台
                platform_info = platforms.get(platform_key)
                if not platform_info:
                    # 该平台没有发布更新，不提示
                    logger.info(f"平台 {platform_key} 暂无更新信息")
                    self.no_update.emit(self._manual_check)
                    return
                remote_ver = platform_info.get("version", "0.0.0")
                changelog = platform_info.get("changelog", "")
                download_url = platform_info.get("download_url", "")
                sha256 = platform_info.get("sha256", "")
                self._src_download_url = platform_info.get("src_download_url", "")
            else:
                # 兼容旧格式（无 platforms 字段）
                remote_ver = info.get("version", "0.0.0")
                changelog = info.get("changelog", "")
                download_url = info.get("download_url", "")
                sha256 = info.get("sha256", "")
                self._src_download_url = info.get("src_download_url", "")

            if _compare_versions(remote_ver, current):
                # 检查是否跳过了此版本
                from ..utils.store import load_setting
                skip_ver = load_setting("skip_version", "")
                if skip_ver == remote_ver and not self._manual_check:
                    logger.info(f"版本 {remote_ver} 已被跳过")
                    self.no_update.emit(self._manual_check)
                elif self._notified_version == remote_ver and not self._manual_check:
                    # 已经提示过这个版本了，用户没关窗口前不重复弹
                    logger.info(f"版本 {remote_ver} 已提示过，不重复弹窗")
                    self.no_update.emit(self._manual_check)
                else:
                    self._notified_version = remote_ver
                    self.update_available.emit(remote_ver, changelog, download_url, sha256)
            else:
                self.no_update.emit(self._manual_check)

            self._manual_check = False

        threading.Thread(target=_do_check, daemon=True).start()

    def download_and_apply(self, download_url: str, sha256: str = ""):
        """下载并应用更新（后台线程）

        源码模式：下载 zip 解压覆盖 src/
        打包模式：打开浏览器下载完整安装包
        """
        if self._downloading:
            return
        self._downloading = True

        def _do_download():
            try:
                # 打包模式
                if getattr(sys, 'frozen', False):
                    tmp_dir = Path(tempfile.gettempdir()) / "antigravity-update"
                    tmp_dir.mkdir(exist_ok=True)

                    def _progress(downloaded, total):
                        self.download_progress.emit(downloaded, total)

                    # 优先尝试增量更新（只下载 src/）
                    if self._src_download_url:
                        src_zip = tmp_dir / "update-src.zip"
                        logger.info(f"尝试增量更新: {self._src_download_url}")
                        if _download_update(self._src_download_url, src_zip, _progress):
                            if _apply_src_only_update(src_zip):
                                self.update_finished.emit(True, "UPDATE_NEED_RESTART")
                                return
                            else:
                                logger.warning("增量更新失败，回退到完整包")
                        else:
                            logger.warning("增量包下载失败，回退到完整包")

                    # 完整包更新
                    zip_path = tmp_dir / "update.zip"

                    if not _download_update(download_url, zip_path, _progress):
                        self.update_finished.emit(False, "下载更新包失败")
                        return

                    # 校验
                    if sha256 and not _verify_sha256(zip_path, sha256):
                        self.update_finished.emit(False, "文件校验失败，可能被篡改")
                        return

                    # 应用更新（完整包替换）
                    if _apply_frozen_update(zip_path):
                        self.update_finished.emit(True, "UPDATE_NEED_RESTART")
                    else:
                        self.update_finished.emit(False, "应用更新失败")
                    return

                # 源码模式：下载 zip 解压覆盖 src/
                tmp_dir = Path(tempfile.gettempdir()) / "antigravity-update"
                tmp_dir.mkdir(exist_ok=True)
                zip_path = tmp_dir / "update.zip"

                def _progress(downloaded, total):
                    self.download_progress.emit(downloaded, total)

                if not _download_update(download_url, zip_path, _progress):
                    self.update_finished.emit(False, "下载更新包失败")
                    return

                # 校验
                if sha256 and not _verify_sha256(zip_path, sha256):
                    self.update_finished.emit(False, "文件校验失败，可能被篡改")
                    return

                # 应用更新
                if _apply_update(zip_path):
                    self.update_finished.emit(True, "更新成功，需要重启应用才能生效。")
                else:
                    self.update_finished.emit(False, "应用更新失败")

            except Exception as e:
                self.update_finished.emit(False, f"更新出错: {e}")
            finally:
                self._downloading = False
                # 清理临时文件
                try:
                    tmp_dir = Path(tempfile.gettempdir()) / "antigravity-update"
                    if tmp_dir.exists():
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass

        threading.Thread(target=_do_download, daemon=True).start()
