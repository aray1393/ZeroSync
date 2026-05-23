import hashlib
import os
import shutil
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

# --- 核心安全設定 ---
PORTABLE_GIT_URL = "https://github.com/git-for-windows/git/releases/download/v2.53.0.windows.3/PortableGit-2.53.0.3-64-bit.7z.exe"
EXPECTED_GIT_HASH = "b365da794b1d2225eb24d5f5e09ef7792cfd5fa26c3a3586210280c80dff3a2a"

class EnvSetupError(RuntimeError):
    pass

def _verify_file_hash(file_path: Path, expected_hash: str) -> bool:
    """鑑定檔案 DNA，只要錯一個位元就視為病毒，當場格殺"""
    sha256 = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha256.update(chunk)
        actual_hash = sha256.hexdigest().lower()
        return actual_hash == expected_hash.lower()
    except Exception:
        return False

def _download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    context = ssl._create_unverified_context()
    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers)
    
    with urllib.request.urlopen(req, context=context) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out)
    if not destination.exists() or destination.stat().st_size < 1000000:
        raise EnvSetupError("下載失敗：載下來的檔案太小或不存在。")
    if not _verify_file_hash(destination, EXPECTED_GIT_HASH):
        if destination.exists():
            destination.unlink()
        raise EnvSetupError("【資安警報】下載檔案 DNA 鑑定失敗。")

def _ensure_app():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app

def _portable_git_root(base_dir: Path) -> Path:
    return base_dir / "bin" / "git"

def _candidate_git_executables(base_dir: Path) -> list[Path]:
    root = _portable_git_root(base_dir)
    return [root / "cmd" / "git.exe", root / "bin" / "git.exe"]

def _find_system_git() -> str | None:
    return shutil.which("git")

def _find_portable_git(base_dir: Path) -> str | None:
    for candidate in _candidate_git_executables(base_dir):
        if candidate.exists():
            return str(candidate)
    return None

def _apply_git_runtime_guards() -> None:
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    os.environ["GCM_INTERACTIVE"] = "Never"

def _inject_portable_git_to_path(base_dir: Path) -> None:
    root = _portable_git_root(base_dir)
    paths = [
        str(root / "cmd"),
        str(root / "bin"),
        str(root / "mingw64" / "bin"),
        str(root / "usr" / "bin"),
    ]
    current = os.environ.get("PATH", "")
    new_parts = [p for p in paths if Path(p).exists()]
    if new_parts:
        os.environ["PATH"] = os.pathsep.join(new_parts + [current])
    git_exe = _find_portable_git(base_dir)
    if git_exe:
        os.environ["GIT_PYTHON_GIT_EXECUTABLE"] = git_exe
    _apply_git_runtime_guards()

def _extract_portable_git(installer_path: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    cmd = f'"{str(installer_path)}" -o"{str(target_dir)}" -y'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise EnvSetupError(f"Git 解壓失敗: {result.stderr or result.stdout}")

def _ensure_git(base_dir: Path) -> str:
    portable_git = _find_portable_git(base_dir)
    if portable_git:
        _inject_portable_git_to_path(base_dir)
        return portable_git

    system_git = _find_system_git()
    if system_git:
        _apply_git_runtime_guards()
        return system_git

    from PySide6.QtWidgets import QMessageBox, QApplication
    from PySide6.QtCore import Qt

    _ensure_app()

    msg = QMessageBox()
    msg.setWindowFlags(msg.windowFlags() | Qt.WindowStaysOnTopHint)
    msg.setWindowTitle("ZeroSync 環境初始化")
    msg.setText("這台電腦沒有偵測到 Git。\n\n要讓 ZeroSync 自動下載並鑑定安全的 Portable Git 嗎？")
    msg.setIcon(QMessageBox.Question)
    msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    msg.setDefaultButton(QMessageBox.Yes)
    
    if msg.exec() != QMessageBox.Yes:
        raise EnvSetupError("使用者取消安裝 Portable Git。")

    temp_installer = base_dir / "bin" / "PortableGit_setup.exe"
    target_dir = _portable_git_root(base_dir)
    
    try:
        info = QMessageBox()
        info.setWindowFlags(info.windowFlags() | Qt.WindowStaysOnTopHint)
        info.setWindowTitle("ZeroSync")
        info.setText("開始下載並鑑定安全 Git，這需要一點時間...")
        info.setIcon(QMessageBox.Information)
        info.setStandardButtons(QMessageBox.NoButton)
        info.show() 
        QApplication.processEvents() 

        _download_file(PORTABLE_GIT_URL, temp_installer)
        _extract_portable_git(temp_installer, target_dir)
        info.close() 
    finally:
        if temp_installer.exists():
            try:
                temp_installer.unlink()
            except Exception:
                pass

    _inject_portable_git_to_path(base_dir)
    portable_git = _find_portable_git(base_dir)
    if not portable_git:
        raise EnvSetupError("Portable Git 安裝完成，但找不到 git.exe。")
    return portable_git

def initialize(base_dir: str | Path) -> str:
    base_dir = Path(base_dir)
    git_exe = _ensure_git(base_dir)
    try:
        subprocess.run(["git", "lfs", "install"], check=False, capture_output=True)
    except Exception:
        pass
    return git_exe