from __future__ import annotations
import logging
logger = logging.getLogger("ZeroSync")
import os
import re
import subprocess
import shutil
import tempfile
import base64
import json
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.error import URLError, HTTPError

from git import RemoteProgress, Repo
from git.exc import InvalidGitRepositoryError, NoSuchPathError

from models import FileEntry, PreflightResult, ZeroSyncConfig
from project_rules import (
    detect_project_type,
    ensure_lines_in_file,
    get_auto_heal_cleanup_targets,
    get_gitattributes_template,
    get_gitignore_template,
)


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class GitProgressHandler(RemoteProgress):
    def __init__(self, signal):
        super().__init__()
        self.signal = signal

    def update(self, op_code, cur_count, max_count=None, message=""):
        percent = int(cur_count / max_count * 100) if max_count else 0
        display_msg = message if message else f"處理物件: {cur_count}"
        if max_count:
            display_msg += f" / {max_count}"
        if self.signal is not None:
            self.signal.emit(percent, display_msg)


class GitService:
    def __init__(self, repo_path: str, require_repo: bool = True):
        self.repo_path = os.path.abspath(repo_path)
        self.repo_dir = Path(self.repo_path)
        self.repo: Optional[Repo] = None
        self.require_repo = require_repo
        if self.refresh_repo() is None and require_repo:
            raise InvalidGitRepositoryError(self.repo_path)

    def get_next_commit_id(self) -> str:
        """自動偵測 Git 歷史紀錄，回傳下一個三位數對齊的編號"""
        if self.repo is None:
            return "001"
            
        try:
            # 讓 GitPython 直接優雅地拿出最後一顆 commit
            last_commit = next(self.repo.iter_commits(max_count=1))
            last_msg = last_commit.message.strip()
            
            # 用正規表示法抓數字 (支援 [001] 或 001 格式)
            import re
            match = re.search(r'^\[?(\d+)\]?', last_msg)
            
            if match:
                next_id = int(match.group(1)) + 1
            else:
                next_id = 1
                
        except StopIteration:
            # 全新專案，連第一顆 commit 都還沒有的情況
            next_id = 1
        except Exception as e:
            logger.debug(f"讀取序號失敗略過: {str(e)}")
            next_id = 1

        # 回傳補零後的字串
        return str(next_id).zfill(3) 

    def smart_sync(self, progress_callback=None):
        """
        智慧型同步邏輯：
        先嘗試正常拉取，如果因為 .gitignore 或 .gitattributes 衝突，就自動重置它們並重試。
        """
        token = self._prepare_remote_auth(require_token=False)
        try:
            self._run_streaming_command(
                ["git", "-c", "credential.helper=", "pull", "--progress"],
                check=True,
                progress_callback=progress_callback,
                token=token,
            )
            return True, "同步成功"
        except Exception as e:
            err_msg = str(e)
            if ".gitignore" in err_msg or ".gitattributes" in err_msg:
                try:
                    self.repo.git.checkout('--', '.gitignore', '.gitattributes')
                    self._run_streaming_command(
                        ["git", "-c", "credential.helper=", "pull", "--progress"],
                        check=True,
                        progress_callback=progress_callback,
                        token=token,
                    )
                    return True, "已自動排除規則衝突並完成同步"
                except Exception as retry_err:
                    return False, f"自動排除衝突失敗: {retry_err}"
            return False, f"同步失敗（非規則衝突）: {err_msg}"

    def purge_self_from_index(self) -> None:
        if not self.repo:
            return

        try:
            # 1. 踢掉 EXE
            self.repo.git.rm("--cached", "ZeroSync.exe", "--ignore-unmatch")
            
            # 2. 這裡一定要有 working_tree=False ！！！
            if self.repo.is_dirty(index=True, working_tree=False):
                # 這裡一定要有 --allow-empty ！！！
                self.repo.git.commit("--allow-empty", "-m", "chore: [Auto] ZeroSync 排除追蹤保護", no_verify=True)
        except Exception as e:
            # 現在 logger 活過來了，這行才不會報錯
            logger.debug(f"除名檢查略過: {str(e)}")

    def _build_env(self, token: str = "") -> dict:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "Never"

        token = (token or "").strip()
        if token:
            env["GIT_ASKPASS"] = str(self._ensure_askpass_script())
            env["ZEROSYNC_GIT_PAT"] = token
        return env

    def _ensure_askpass_script(self) -> Path:
        """建立不含 PAT 的 Git AskPass 腳本，PAT 只透過環境變數臨時傳入。

        注意：不要把 AskPass 腳本寫進 self.repo_dir。
        Clone 模式會先在目標資料夾執行驗證，若這時把 .zerosync/zerosync_askpass.cmd
        建在目標資料夾內，Git 之後執行 `git clone <url> .` 會判定目的地非空而失敗。
        因此腳本固定放到系統暫存區，不污染使用者選擇的專案資料夾。
        """
        script_dir = Path(tempfile.gettempdir()) / "ZeroSync"
        script_dir.mkdir(parents=True, exist_ok=True)

        if os.name == "nt":
            askpass_path = script_dir / "zerosync_askpass.cmd"
            script = """@echo off
setlocal
set PROMPT_TEXT=%*
echo %PROMPT_TEXT% | findstr /I "Username" >nul
if not errorlevel 1 (
    echo pat
    exit /b 0
)
echo %ZEROSYNC_GIT_PAT%
exit /b 0
"""
            askpass_path.write_text(script, encoding="utf-8")
        else:
            askpass_path = script_dir / "zerosync_askpass.sh"
            script = """#!/bin/sh
case "$*" in
  *Username*|*username*) printf '%s\n' 'pat' ;;
  *) printf '%s\n' "$ZEROSYNC_GIT_PAT" ;;
esac
"""
            askpass_path.write_text(script, encoding="utf-8")
            try:
                askpass_path.chmod(0o700)
            except Exception:
                pass

        return askpass_path

    def clean_remote_url(self, url: str) -> str:
        """移除 HTTP(S) remote URL 內的帳密資訊，只保留乾淨遠端網址。"""
        url = (url or "").strip()
        if not url:
            return ""
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            return url
        clean_host = parts.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parts.scheme, clean_host, parts.path, parts.query, parts.fragment))

    def _extract_token_from_url(self, url: str) -> str:
        """從舊版 https://pat:TOKEN@host 遠端網址擷取 PAT，供遷移時救援用。"""
        try:
            parts = urlsplit((url or "").strip())
            if parts.scheme not in {"http", "https"}:
                return ""
            return unquote(parts.password or "").strip()
        except Exception:
            return ""

    def _get_origin_url(self) -> str:
        if self.repo is None:
            self.refresh_repo()
        if self.repo is None:
            return ""
        try:
            if any(remote.name == "origin" for remote in self.repo.remotes):
                return self.repo.remotes.origin.url or ""
        except Exception:
            return ""
        return ""

    def _get_saved_pat(self) -> str:
        """從 ZeroSync 設定檔讀取目前保存的 PAT。"""
        try:
            from config_manager import ConfigManager
            config = ConfigManager(self.repo_dir).load()
            return (config.access_token or "").strip()
        except Exception:
            return ""

    def _remote_requires_pat(self, url: str) -> bool:
        lower = (url or "").lower()
        return "dev.azure.com" in lower or "visualstudio.com" in lower

    def _sanitize_origin_url(self) -> str:
        """若 origin 還藏著帳密，立刻洗成乾淨 URL。"""
        if self.repo is None:
            self.refresh_repo()
        if self.repo is None:
            return ""
        try:
            if not any(remote.name == "origin" for remote in self.repo.remotes):
                return ""
            current_url = self.repo.remotes.origin.url or ""
            clean_url = self.clean_remote_url(current_url)
            if clean_url and clean_url != current_url:
                self.repo.git.remote("set-url", "origin", clean_url)
                logger.info("已將 origin URL 清理為不含 PAT 的安全格式。")
            return clean_url or current_url
        except Exception as e:
            logger.debug(f"清理 origin URL 時略過：{e}")
            return ""

    def _prepare_remote_auth(self, require_token: bool = False) -> str:
        """
        準備遠端認證：
        1. 優先使用 .zerosync 設定檔內的 PAT。
        2. 若是舊版帶 PAT 的 origin，遷移時可臨時擷取使用。
        3. 不論如何都把 origin 洗成乾淨 URL。
        """
        origin_url = self._get_origin_url()
        token = self._get_saved_pat() or self._extract_token_from_url(origin_url)
        clean_url = self._sanitize_origin_url()
        check_url = clean_url or origin_url
        if require_token and not token and self._remote_requires_pat(check_url):
            raise RuntimeError("缺少 PAT。請到系統設定填入新的存取權杖。")
        return token

    def _redact_sensitive_text(self, text: str, token: str = "") -> str:
        """避免錯誤訊息或 Log 把 PAT 連同 URL 一起噴出來。"""
        if not text:
            return ""
        safe = re.sub(r"https://[^\s/@:]+:[^\s/@]+@", "https://***:***@", text)
        safe = re.sub(r"https://[^\s/@]+@", "https://***@", safe)
        if token:
            safe = safe.replace(token, "***")
        return safe

    def refresh_repo(self) -> Optional[Repo]:
        try:
            self.repo = Repo(self.repo_path)
        except (InvalidGitRepositoryError, NoSuchPathError):
            self.repo = None
        return self.repo

    def get_dynamic_token_url(self) -> str:
        """依照目前 origin 自動推導 Azure DevOps PAT 設定頁面。"""
        default_url = "https://aex.dev.azure.com/me"
        if self.repo is None:
            self.refresh_repo()
        if self.repo is None:
            return default_url
        try:
            if any(remote.name == "origin" for remote in self.repo.remotes):
                remote_url = self.repo.remotes.origin.url
                if "dev.azure.com/" in remote_url:
                    org_name = remote_url.split("dev.azure.com/", 1)[1].split("/", 1)[0].strip()
                    if org_name:
                        return f"https://dev.azure.com/{org_name}/_usersSettings/tokens"
        except Exception:
            pass
        return default_url

    def is_auth_error(self, raw_error: str) -> bool:
        text = (raw_error or "").strip().lower()
        auth_keywords = [
            "401",
            "authentication failed",
            "fatal: authentication",
            "terminal prompts disabled",
            "could not read password",
            "http basic: access denied",
            "personal access token",
            "pat",
            "認證失敗",
            "遠端登入失敗",
            "權杖",
            "存取權杖",
        ]
        return any(keyword in text for keyword in auth_keywords)

    def fix_ghost_files(self, message: str, progress_callback: Optional[Callable[[int, str], None]] = None) -> str:
        """強制重置 Git 索引，清除被誤加的 .gitignore 排除檔案 (v3.1 強化版)"""
        try:
            self._emit_progress(progress_callback, 10, "正在搜刮所有變動檔案...")
            self.repo.git.add("-A")
            
            self._emit_progress(progress_callback, 30, "正在粉碎舊有的 Git 索引快取...")
            self.repo.git.rm("-r", "--cached", ".", "--ignore-unmatch")
            
            self._emit_progress(progress_callback, 60, "正在依照 .gitignore 重新對齊...")
            self.repo.git.add(".")
            
            staged = self.repo.git.diff("--cached", "--name-only").strip()
            if not staged:
                self._emit_progress(progress_callback, 100, "索引已重置")
                return "索引已重置，但目前沒有需要提交的變更。"
            
            self._emit_progress(progress_callback, 85, "正在封印幽靈檔案並存檔...")
            self.repo.git.commit("-m", message, no_verify=True)
            
            self.refresh_repo()
            self._emit_progress(progress_callback, 100, "驅鬼完成")
            return "✨ 幽靈檔案已驅除！索引重置並自動存檔完成。"
        except Exception as e:
            err_msg = self.translate_error(str(e))
            raise RuntimeError(f"驅除幽靈檔案失敗：{err_msg}")

    def auto_heal_defenses(self) -> None:
        """自動修補 Git 規則，並把不該被追蹤的垃圾檔案從索引移除。"""
        if not self.repo_dir.exists():
            return

        project_type, _ = detect_project_type(self.repo_dir)

        required_ignore = get_gitignore_template(project_type)
        required_attr = get_gitattributes_template(project_type)
        ensure_lines_in_file(self.repo_dir, ".gitignore", required_ignore)
        if required_attr:
            ensure_lines_in_file(self.repo_dir, ".gitattributes", required_attr)

        self.refresh_repo()
        if not self.repo:
            return

        cleanup_targets = get_auto_heal_cleanup_targets(project_type)
        if not cleanup_targets:
            return

        try:
            self.repo.git.rm("-r", "--cached", "--ignore-unmatch", *cleanup_targets)
        except Exception:
            pass

    def run_command(self, args: List[str], check: bool = True, token: str = "") -> Tuple[int, str, str]:
        proc = subprocess.run(
            args,
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            env=self._build_env(token),
            creationflags=CREATE_NO_WINDOW,
        )
        stdout = self._redact_sensitive_text(proc.stdout.strip(), token)
        stderr = self._redact_sensitive_text(proc.stderr.strip(), token)
        if check and proc.returncode != 0:
            raise RuntimeError(stderr or stdout or "指令執行失敗")
        return proc.returncode, stdout, stderr

    def _emit_progress(self, progress_callback: Optional[Callable[[int, str], None]], percent: int, message: str) -> None:
        if progress_callback is None:
            return
        progress_callback(int(percent), message)

    def _get_progress_callback(
        self,
        progress_handler: Optional[GitProgressHandler] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Optional[Callable[[int, str], None]]:
        if progress_callback is not None:
            return progress_callback
        if progress_handler is not None and progress_handler.signal is not None:
            return progress_handler.signal.emit
        return None

    def _extract_percent(self, line: str) -> Optional[int]:
        match = re.search(r"(\d+)%", line)
        return int(match.group(1)) if match else None

    def _scale_percent(self, percent: int, start: int, end: int) -> int:
        percent = max(0, min(100, percent))
        return start + int((end - start) * (percent / 100.0))

    def _parse_clone_progress(self, line: str) -> Optional[Tuple[int, str]]:
        text = line.strip()
        if not text:
            return None
        percent = self._extract_percent(text)
        lower = text.lower()
        if text.startswith("remote:") or "enumerating objects" in lower or "counting objects" in lower:
            return -1, "正在與雲端同步物件資訊..."
        if "receiving objects" in lower and percent is not None:
            return self._scale_percent(percent, 8, 72), f"正在下載專案檔案... ({percent}%)"
        if "resolving deltas" in lower and percent is not None:
            return self._scale_percent(percent, 72, 90), f"正在整理版本差異... ({percent}%)"
        if "updating files" in lower and percent is not None:
            return self._scale_percent(percent, 90, 98), f"正在寫入專案檔案... ({percent}%)"
        if "filtering content" in lower or "downloading" in lower:
            if percent is not None:
                return self._scale_percent(percent, 98, 99), f"正在同步 LFS 大型檔案... ({percent}%)"
            return -1, "正在同步 LFS 大型檔案..."
        return None

    def _parse_push_progress(self, line: str) -> Optional[Tuple[int, str]]:
        text = line.strip()
        if not text:
            return None
        percent = self._extract_percent(text)
        lower = text.lower()
        if text.startswith("remote:"):
            return -1, "遠端正在處理這次提交..."
        if "enumerating objects" in lower or "counting objects" in lower:
            if percent is not None:
                return self._scale_percent(percent, 10, 25), f"正在整理要上傳的檔案... ({percent}%)"
            return -1, "正在整理要上傳的檔案..."
        if "compressing objects" in lower and percent is not None:
            return self._scale_percent(percent, 25, 55), f"正在壓縮要上傳的內容... ({percent}%)"
        if "writing objects" in lower and percent is not None:
            return self._scale_percent(percent, 55, 92), f"正在上傳到雲端... ({percent}%)"
        if "to http" in lower or "to https" in lower or "branch" in lower or lower == "done":
            return 97, "正在完成雲端提交..."
        return None

    def _parse_pull_progress(self, line: str) -> Optional[Tuple[int, str]]:
        text = line.strip()
        if not text:
            return None
        percent = self._extract_percent(text)
        lower = text.lower()
        if text.startswith("remote:") or "enumerating objects" in lower or "counting objects" in lower:
            return -1, "正在檢查雲端最新狀態..."
        if "receiving objects" in lower and percent is not None:
            return self._scale_percent(percent, 10, 70), f"正在下載更新內容... ({percent}%)"
        if "resolving deltas" in lower and percent is not None:
            return self._scale_percent(percent, 70, 90), f"正在整理版本差異... ({percent}%)"
        if "updating files" in lower and percent is not None:
            return self._scale_percent(percent, 90, 98), f"正在更新本地檔案... ({percent}%)"
        if "fast-forward" in lower or "already up to date" in lower:
            return 98, "正在完成更新..."
        return None

    def _run_streaming_command(
        self,
        args: List[str],
        check: bool = True,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        parser: Optional[Callable[[str], Optional[Tuple[int, str]]]] = None,
        cwd: Optional[str] = None,
        token: str = "",
    ) -> Tuple[int, str, str]:
        proc = subprocess.Popen(
            args,
            cwd=cwd or self.repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            env=self._build_env(token),
            creationflags=CREATE_NO_WINDOW,
        )
        lines: List[str] = []
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            if line:
                lines.append(self._redact_sensitive_text(line, token))
            if parser and progress_callback:
                parsed = parser(line)
                if parsed:
                    percent, message = parsed
                    self._emit_progress(progress_callback, percent, message)
        proc.wait()
        output = "\n".join(lines).strip()
        if check and proc.returncode != 0:
            raise RuntimeError(output or "指令執行失敗")
        return proc.returncode, output, ""

    def is_lfs_installed(self) -> bool:
        try:
            code, out, _ = self.run_command(["git", "lfs", "version"], check=False)
            return code == 0 and "git-lfs" in out.lower()
        except Exception:
            return False

    def get_tracking_branch_name(self) -> Optional[str]:
        if self.repo is None:
            return None
        try:
            if self.repo.head.is_detached:
                return None
            tracking = self.repo.active_branch.tracking_branch()
            if tracking:
                return tracking.name
            branch_name = self.repo.active_branch.name
            if any(remote.name == "origin" for remote in self.repo.remotes) and branch_name in self.repo.remotes.origin.refs:
                return f"origin/{branch_name}"
        except Exception:
            pass
        return None

    def get_remote_divergence(self, fetch_first: bool = False) -> Dict[str, object]:
        result = {"has_remote": False, "tracking_branch": None, "ahead": 0, "behind": 0, "error": None}
        if self.repo is None:
            return result
        try:
            if not any(remote.name == "origin" for remote in self.repo.remotes):
                return result
            result["has_remote"] = True
            token = self._prepare_remote_auth(require_token=False)
            if fetch_first:
                self.run_command(["git", "-c", "credential.helper=", "fetch", "origin"], check=False, token=token)
                self.refresh_repo()
            tracking = self.get_tracking_branch_name()
            result["tracking_branch"] = tracking
            if not tracking:
                return result
            counts = self.repo.git.rev_list("--left-right", "--count", f"HEAD...{tracking}").strip()
            left, right = counts.split()
            result["ahead"], result["behind"] = int(left), int(right)
            return result
        except Exception as e:
            result["error"] = self._redact_sensitive_text(str(e))
            return result

    def parse_status_entries(self) -> List[FileEntry]:
        self.auto_heal_defenses()
        self.purge_self_from_index()
        entries: List[FileEntry] = []
        if self.repo is None:
            return entries

        seen: set[tuple[str, str, tuple[str, ...]]] = set()
        try:
            status = self.repo.git.status("--porcelain")
            if not status:
                return entries
            for raw_line in status.splitlines():
                if len(raw_line) < 4:
                    continue

                status_code = raw_line[:2]
                body = raw_line[3:].strip().strip('"')
                display_path = body
                label = "修改"

                if " -> " in body:
                    old_path, new_path = body.split(" -> ", 1)
                    display_path = new_path.strip('"')
                    stage_paths = [old_path.strip('"'), new_path.strip('"')]
                    label = "重新命名"
                else:
                    stage_paths = [body]
                    if "??" in status_code or "A" in status_code:
                        label = "新增"
                    elif "D" in status_code:
                        label = "刪除"

                normalized_display = display_path.replace("\\", "/")
                normalized_stage_paths = tuple(path.replace("\\", "/") for path in stage_paths)
                key = (normalized_display, status_code, normalized_stage_paths)
                if key in seen:
                    continue
                seen.add(key)

                entries.append(
                    FileEntry(
                        path=normalized_display,
                        display=f"📝 [{label}] {display_path}",
                        status_code=status_code,
                        stage_paths=list(normalized_stage_paths),
                    )
                )
        except Exception:
            pass
        return entries

    def translate_error(self, raw_error: str) -> str:
        text = (raw_error or "").strip()
        lower = text.lower()
        rules = [
            (["non-fast-forward", "fetch first"], "雲端版本比你本地新，先按「獲取最新」再上傳。"),
            (["authentication failed", "fatal: authentication", "terminal prompts disabled"], "遠端登入失敗，請檢查 Git 認證、PAT 或 Azure 權限。"),
            (["nothing to commit"], "沒有新的變更可提交。"),
            (["merge conflict", "conflict ("], "發生衝突，不能直接同步，先處理衝突檔案。"),
            (["git-lfs", "lfs"], "Git LFS 相關操作失敗，請確認 LFS 已安裝且規則正確。"),
            (["缺少 pat", "缺少存取權杖"], "你有填遠端網址，但沒填 PAT。"),
        ]
        for keys, msg in rules:
            if any(k in lower for k in keys):
                return f"{msg}\n\n原始訊息：\n{text}"
        return text or "發生未知錯誤。"

    def is_low_quality_message(self, msg: str) -> bool:
        low_quality_words = {"test", "123", "aaa", "更新", "修改", "改一下", "fix", "tmp", "temp", "qq", "x", "版本", "提交"}
        normalized = msg.strip().lower()
        if len(normalized) < 4 or normalized in low_quality_words:
            return True
        digits = sum(ch.isdigit() for ch in normalized)
        return digits >= max(3, len(normalized) - 1)

    def preflight_check(self, selected_entries: List[FileEntry], message: str, project_type: str) -> PreflightResult:
        result = PreflightResult(ok=True)
        if self.repo is None:
            result.ok, result.errors = False, ["目前沒有有效的 Git 專案。"]
            return result
        if not selected_entries:
            result.ok, result.errors = False, ["你沒有勾任何檔案，上傳空氣嗎？"]
            return result
        if not message.strip():
            result.ok, result.errors = False, ["提交說明是空的，別當謎語人。"]
        if self.is_low_quality_message(message):
            result.warnings.append("這次提交說明有點水。")
        try:
            if self.repo.git.diff("--name-only", "--diff-filter=U").strip():
                result.ok, result.errors = False, ["目前有衝突還沒解決，先別硬上。"]
        except Exception:
            pass
        div = self.get_remote_divergence(fetch_first=True)
        if div["has_remote"]:
            if div.get("behind", 0) > 0:
                result.ok, result.errors = False, [f"遠端比你新 {div['behind']} 個提交，先 Pull。"]
            elif not div.get("tracking_branch"):
                result.warnings.append("目前分支未追蹤遠端，Push 可能失敗。")
        else:
            result.warnings.append("未設定遠端 origin，僅能本地提交。")
        return result

    def _require_repo(self) -> Repo:
        if self.repo is None:
            raise RuntimeError("目前沒有可用的 Git 專案")
        return self.repo

    def pull(self, progress_handler: GitProgressHandler) -> str:
        self._require_repo()
        callback = self._get_progress_callback(progress_handler=progress_handler)
        token = self._prepare_remote_auth(require_token=True)
        self._emit_progress(callback, -1, "正在連線並檢查雲端更新...")

        try:
            self._run_streaming_command(
                ["git", "-c", "credential.helper=", "pull", "--progress"],
                check=True,
                progress_callback=callback,
                parser=self._parse_pull_progress,
                token=token,
            )
        except Exception as e:
            err_msg = str(e)
            if ".gitignore" in err_msg or ".gitattributes" in err_msg:
                try:
                    self._emit_progress(callback, -1, "偵測到規則衝突，正在自動校準環境...")
                    self.repo.git.checkout('--', '.gitignore', '.gitattributes')
                    self._run_streaming_command(
                        ["git", "-c", "credential.helper=", "pull", "--progress"],
                        check=True,
                        progress_callback=callback,
                        parser=self._parse_pull_progress,
                        token=token,
                    )
                except Exception as retry_err:
                    raise RuntimeError(f"自動排除衝突失敗: {retry_err}")
            else:
                raise e

        self.refresh_repo()
        self._emit_progress(callback, 100, "下載完成")
        return "✅ 下載成功"

    def push(
        self,
        msg: str,
        entries: List[FileEntry],
        progress_handler: Optional[GitProgressHandler],
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> str:
        repo = self._require_repo()
        callback = self._get_progress_callback(progress_handler=progress_handler, progress_callback=progress_callback)

        previous_head: Optional[str] = None
        created_commit: Optional[str] = None
        try:
            previous_head = repo.head.commit.hexsha
        except Exception:
            previous_head = None

        self._emit_progress(callback, 5, "正在整理本地變更...")
        for entry in entries:
            for stage_path in entry.stage_paths:
                repo.git.add("-A", "--", stage_path)
        staged = repo.git.diff("--cached", "--name-only").strip()
        if not staged:
            raise RuntimeError("nothing to commit")

        self._emit_progress(callback, 15, "正在建立提交紀錄...")
        repo.git.commit("-m", msg, no_verify=True)
        try:
            created_commit = repo.head.commit.hexsha
        except Exception:
            created_commit = None

        if any(remote.name == "origin" for remote in repo.remotes):
            token = self._prepare_remote_auth(require_token=True)
            try:
                self._emit_progress(callback, -1, "正在與雲端建立上傳連線...")
                self._run_streaming_command(
                    ["git", "-c", "credential.helper=", "push", "--progress"],
                    check=True,
                    progress_callback=callback,
                    parser=self._parse_push_progress,
                    token=token,
                )
                self.refresh_repo()
                self._emit_progress(callback, 100, "上傳完成")
                return "🚀 上傳成功"
            except Exception as push_err:
                safe_push_err = self._redact_sensitive_text(str(push_err), token)
                self._emit_progress(callback, -1, "上傳失敗，正在退回本地提交...")
                try:
                    current_head = repo.head.commit.hexsha if repo.head.is_valid() else None
                    if created_commit and current_head == created_commit:
                        if previous_head:
                            # 只拆掉 ZeroSync 剛剛建立的 commit，保留檔案內容回到待處理變動。
                            repo.git.reset("--mixed", previous_head)
                        else:
                            # 極少數情況：這是 repo 的第一顆 commit。刪除 HEAD 後重置索引，保留工作區檔案。
                            repo.git.update_ref("-d", "HEAD")
                            repo.git.reset()
                    self.refresh_repo()
                except Exception as rollback_err:
                    safe_rollback_err = self._redact_sensitive_text(str(rollback_err), token)
                    raise RuntimeError(
                        "上傳失敗，而且自動退回本地提交也失敗。\n\n"
                        f"上傳錯誤：{safe_push_err}\n\n"
                        f"退回錯誤：{safe_rollback_err}"
                    )

                raise RuntimeError(
                    "上傳失敗，已自動退回本地提交；你的修改仍保留在待處理變動中。\n\n"
                    f"原始錯誤：{safe_push_err}"
                )

        self._emit_progress(callback, 100, "已完成本地提交")
        return "✅ 已完成本地提交"

    def force_overwrite_local(self) -> str:
        repo = self._require_repo()
        current_branch = repo.active_branch.name
        token = self._prepare_remote_auth(require_token=True)

        self.purge_self_from_index()

        self.run_command(
            ["git", "-c", "credential.helper=", "fetch", "origin"],
            check=True,
            token=token,
        )

        # reset --hard 會觸發 Git LFS smudge / checkout。
        # 這裡不能用 repo.git.reset() 裸跑，否則 Azure DevOps / LFS 可能拿不到 PAT。
        self.run_command(
            ["git", "-c", "credential.helper=", "reset", "--hard", f"origin/{current_branch}"],
            check=True,
            token=token,
        )

        # 保險再補一次 LFS pull，避免 reset 時因環境或 smudge 狀態漏抓大型資產。
        if self.is_lfs_installed():
            self.run_command(
                ["git", "-c", "credential.helper=", "lfs", "pull"],
                check=True,
                token=token,
            )

        # 這裡就是精準消音護盾
        try:
            self.run_command(
                ["git", "clean", "-fd", "-e", "ZeroSync.exe", "-e", "logs/*"],
                check=True,
            )
        except Exception as e:
            err_msg = str(e)
            # 只有當錯誤訊息裡「只」包含這些你保護的檔案時，我們才讓它 pass
            # 如果有其他嚴重的錯誤，它依然會觸發下方的 raise
            ignored_targets = ["ZeroSync.exe", "logs", "zerosync_debug.log"]
            if any(target in err_msg for target in ignored_targets):
                logger.debug(f"已攔截預期內的權限衝突，跳過報錯：{err_msg}")
            else:
                # 如果是其他的 Git 錯誤（比如硬碟滿了、索引毀損），就照樣噴給使用者看
                raise e

        self.refresh_repo()
        return "✨ 專案已重整完畢"

    def clean_local_garbage(self) -> str:
        repo = self._require_repo()
        
        try:
            self.repo.git.clean("-fd", "-e", "ZeroSync.exe", "-e", "logs/*")
        except Exception as e:
            err_msg = str(e)
            ignored_targets = ["ZeroSync.exe", "logs", "zerosync_debug.log"]
            if any(target in err_msg for target in ignored_targets):
                logger.debug(f"清理垃圾時略過特定檔案鎖定。")
            else:
                raise e
            
        return "✨ 垃圾清理完畢"

    def fix_git_encoding(self) -> str:
        repo = self._require_repo()
        for key in ["core.quotepath", "gui.encoding", "i18n.commitencoding", "i18n.logoutputencoding"]:
            repo.git.config(key, "false" if "quote" in key else "utf-8")
        return "✨ 編碼校正完成"

    def fix_remote_connection(self) -> str:
        repo = self._require_repo()
        token = self._prepare_remote_auth(require_token=False)
        try:
            repo.git.config("--local", "--unset", "http.proxy")
            repo.git.config("--local", "--unset", "https.proxy")
        except Exception:
            pass
        self.run_command(["git", "-c", "credential.helper=", "remote", "prune", "origin"], check=False, token=token)
        self.run_command(["git", "-c", "credential.helper=", "fetch", "--all"], check=False, token=token)
        self.refresh_repo()
        return "✨ 雲端連線已重置"

    def fix_upstream_tracking(self) -> str:
        repo = self._require_repo()
        current_branch = repo.active_branch.name
        repo.git.branch(f"--set-upstream-to=origin/{current_branch}", current_branch)
        return "✨ 分支追蹤已修復"

    def set_user_identity(self, user_name: str, user_email: str) -> None:
        if self.repo is None:
            self.refresh_repo()
        assert self.repo is not None
        if user_name:
            self.repo.git.config("user.name", user_name)
        if user_email:
            self.repo.git.config("user.email", user_email)

    def setup_remote(self, url: str, token: str = "") -> str:
        """
        保留舊函式名稱避免呼叫端爆掉，但現在只回傳乾淨 URL。
        PAT 不再寫進 remote URL。
        """
        return self.clean_remote_url(url)

    def verify_microsoft_auth(self, config: ZeroSyncConfig) -> tuple[bool, str]:
        """驗證 PAT 權杖是否能與雲端對接，確保初始化順利。"""
        if not config.remote_url or not config.access_token:
            return False, "缺少雲端 URL 或存取權杖 (PAT)。"
        try:
            clean_url = self.clean_remote_url(config.remote_url)
            code, out, err = self.run_command(
                ["git", "-c", "credential.helper=", "ls-remote", clean_url],
                check=False,
                token=config.access_token,
            )
            if code == 0:
                return True, "認證成功"
            return False, f"驗證失敗：{err or out}"
        except Exception as e:
            return False, self._redact_sensitive_text(str(e), config.access_token)

    def configure_remote(self, remote_url: str, token: str = "") -> str:
        if self.repo is None:
            self.refresh_repo()
        assert self.repo is not None

        clean_url = self.clean_remote_url(remote_url)
        self.repo.git.config("--local", "credential.helper", "")
        if not clean_url:
            return ""
        if any(remote.name == "origin" for remote in self.repo.remotes):
            self.repo.git.remote("set-url", "origin", clean_url)
        else:
            self.repo.git.remote("add", "origin", clean_url)
        return clean_url

    def _remote_has_heads(self, token: str = "") -> bool:
        code, out, _ = self.run_command(
            ["git", "-c", "credential.helper=", "ls-remote", "--heads", "origin"],
            check=False,
            token=token,
        )
        return code == 0 and bool(out.strip())

    def _detect_remote_default_branch(self, token: str = "") -> str:
        code, out, _ = self.run_command(
            ["git", "-c", "credential.helper=", "ls-remote", "--symref", "origin", "HEAD"],
            check=False,
            token=token,
        )
        if code == 0 and out:
            for line in out.splitlines():
                if line.startswith("ref:") and "HEAD" in line:
                    return line.split()[1].replace("refs/heads/", "")
        return "main"

    def seed_templates(self, project_type: str) -> tuple[list[str], list[str]]:
        ignore_lines = get_gitignore_template(project_type)
        attr_lines = get_gitattributes_template(project_type)
        ensure_lines_in_file(self.repo_dir, ".gitignore", ignore_lines)
        ensure_lines_in_file(self.repo_dir, ".gitattributes", attr_lines)
        return ignore_lines, attr_lines

    def bootstrap_init_repo(
        self,
        config: ZeroSyncConfig,
        project_type: str,
        initial_message: str = "初始化 ZeroSync 專案規則",
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> str:
        self._emit_progress(progress_callback, 15, "正在建立 Git 倉庫...")
        if self.repo is None:
            Repo.init(self.repo_path)
            self.refresh_repo()
        assert self.repo is not None

        self.repo.git.config("--local", "credential.helper", "")
        self.set_user_identity(config.user_name, config.user_email)
        self._emit_progress(progress_callback, 25, "正在套用使用者與遠端設定...")

        if config.remote_url:
            self.configure_remote(config.remote_url, config.access_token)

        if self.is_lfs_installed():
            self._emit_progress(progress_callback, -1, "正在初始化 Git LFS...")
            self.run_command(["git", "lfs", "install"], check=False)

        remote_seeded = False
        if config.remote_url and self._remote_has_heads(token=config.access_token):
            self._emit_progress(progress_callback, -1, "正在檢查雲端既有分支...")
            self._run_streaming_command(
                ["git", "-c", "credential.helper=", "fetch", "origin", "--progress"],
                check=True,
                progress_callback=progress_callback,
                parser=self._parse_pull_progress,
                token=config.access_token,
            )
            self.refresh_repo()
            branch = self._detect_remote_default_branch(token=config.access_token)
            self.repo.git.checkout("-B", branch, f"origin/{branch}")
            self._emit_progress(progress_callback, 85, "已對齊雲端預設分支")
            remote_seeded = True

        if not remote_seeded:
            self._emit_progress(progress_callback, 35, "正在寫入專案同步規則...")
            self.seed_templates(project_type)
            self.repo.git.add(".gitignore", ".gitattributes")
            if self.repo.git.diff("--cached", "--name-only").strip():
                self._emit_progress(progress_callback, 50, "正在建立初始化提交...")
                self.repo.git.commit("-m", initial_message, no_verify=True)
                if config.remote_url:
                    self._emit_progress(progress_callback, -1, "正在上傳初始化規則到雲端...")
                    self._run_streaming_command(
                        ["git", "-c", "credential.helper=", "push", "-u", "origin", self.repo.active_branch.name, "--progress"],
                        check=True,
                        progress_callback=progress_callback,
                        parser=self._parse_push_progress,
                        token=config.access_token,
                    )
                    self.refresh_repo()

        self._emit_progress(progress_callback, 90, "Git 基礎設定完成")
        return "初始化完成"

    def bootstrap_clone_repo(
        self,
        config: ZeroSyncConfig,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> str:
        clean_url = self.clean_remote_url(config.remote_url)
        if not clean_url:
            raise RuntimeError("缺少雲端 URL，不能執行 clone。")
        if not (config.access_token or "").strip() and self._remote_requires_pat(clean_url):
            raise RuntimeError("缺少 PAT。請到系統設定填入新的存取權杖。")

        # Git 原生的 `git clone URL .` 要求目標資料夾必須完全空白。
        # 但 ZeroSync 初次部署時，目標資料夾可能已經有 .zerosync、bin、logs、exe 或原始碼啟動檔。
        # 這些是 ZeroSync 自己的啟動殘留，不應該讓 Clone 直接失敗，所以這裡改成：
        # 1. 目標空白：照舊 clone 到 `.`。
        # 2. 只有 ZeroSync 啟動檔：clone 到臨時資料夾，再把 repo 內容搬回目標。
        # 3. 有其他使用者檔案：拒絕，避免覆蓋心血。
        allowed = {
            "ZeroSync.exe",
            "ZeroSync_Colors.json",
            "main.py",
            "main_window.py",
            "git_service.py",
            "project_rules.py",
            "models.py",
            "env_setup.py",
            "config_manager.py",
            "ai_service.py",
            "requirements.txt",
            "requirements_zerosync.txt",
            "run.bat",
            "setup.bat",
            ".zerosync",
            "__pycache__",
            "bin",
            "logs",
        }

        existing_items = list(self.repo_dir.iterdir()) if self.repo_dir.exists() else []
        dirty_items = [e.name for e in existing_items if e.name not in allowed]
        if dirty_items:
            preview = ", ".join(dirty_items[:8])
            if len(dirty_items) > 8:
                preview += " ..."
            raise RuntimeError(f"資料夾不乾淨，請改用綁定模式或選擇空資料夾。偵測到：{preview}")

        self._emit_progress(progress_callback, -1, "正在向雲端發出 Clone 請求...")

        if not existing_items:
            self._run_streaming_command(
                ["git", "-c", "credential.helper=", "clone", "--progress", clean_url, "."],
                check=True,
                progress_callback=progress_callback,
                parser=self._parse_clone_progress,
                token=config.access_token,
            )
        else:
            self._emit_progress(progress_callback, -1, "目標資料夾含 ZeroSync 啟動檔，正在使用安全臨時區複製...")
            with tempfile.TemporaryDirectory(prefix="zerosync_clone_", dir=str(self.repo_dir.parent)) as tmp:
                tmp_path = Path(tmp)
                self._run_streaming_command(
                    ["git", "-c", "credential.helper=", "clone", "--progress", clean_url, str(tmp_path)],
                    check=True,
                    progress_callback=progress_callback,
                    parser=self._parse_clone_progress,
                    cwd=str(self.repo_dir.parent),
                    token=config.access_token,
                )

                self._emit_progress(progress_callback, -1, "正在把雲端專案放入目標資料夾...")
                for item in tmp_path.iterdir():
                    dest = self.repo_dir / item.name
                    if dest.exists():
                        raise RuntimeError(
                            f"Clone 內容與本機啟動檔發生名稱衝突：{item.name}。"
                            "請改選空資料夾，或先移走同名檔案後再試。"
                        )
                    shutil.move(str(item), str(dest))

        self.refresh_repo()
        assert self.repo is not None
        self.repo.git.config("--local", "credential.helper", "")
        # 保險：即使 Git 未來行為改變，也強制把 origin 洗乾淨。
        self.configure_remote(clean_url, config.access_token)
        self.set_user_identity(config.user_name, config.user_email)
        self._emit_progress(progress_callback, 100, "雲端專案已複製完成")
        return "雲端專案已複製完成"