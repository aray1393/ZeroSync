from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import List, Tuple

from git import Repo

from models import FileEntry, ProjectHealth


COMMON_IGNORE = [
    "bin/",
    ".zerosync/",
    "*.exe",
    "*.pdb",
    "__pycache__/",
    "build/",
    "dist/",
    "*.spec",
    "logs/",
]


UNREAL_IGNORE = [
    "**/Binaries/*",
    "**/DerivedDataCache/*",
    "**/Intermediate/*",
    "**/Saved/*",
    ".vs/",
    "*.VC.db",
    "*.opensdf",
    "*.sdf",
    "*.suo",
    "*.xcodeproj",
    "*.xcworkspace",
]


UNITY_IGNORE = [
    "[Ll]ibrary/",
    "[Tt]emp/",
    "[Oo]bj/",
    "[Bb]uild/",
    "[Bb]uilds/",
    "[Ll]ogs/",
    "[Mm]emoryCaptures/",
    "UserSettings/",
]


COMMON_BAD_DIRS = ["bin/", ".zerosync/", "__pycache__/", "dist/"]


UNREAL_BAD_DIRS = ["Saved/", "Intermediate/", "DerivedDataCache/", "Binaries/"]
UNITY_BAD_DIRS = ["Library/", "Temp/", "Obj/", "Build/", "Builds/", "Logs/"]


COMMON_AUTO_HEAL_TARGETS = ["bin/", ".zerosync/", "*.exe"]


def read_text_file_lines(repo_dir: Path, filename: str) -> List[str]:
    """讀取文字檔內容，若不存在回傳空清單"""
    file_path = repo_dir / filename
    if not file_path.exists():
        return []
    try:
        return file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []


def ensure_lines_in_file(repo_dir: Path, filename: str, required_lines: List[str]) -> None:
    """
    確保檔案包含必要行，且解決 Git 換行符號衝突問題。
    只有內容真的不一樣時，才會執行寫入動作。
    """
    file_path = repo_dir / filename
    
    # 1. 讀取現有內容並標準化（去掉多餘空格，統一換行）
    if file_path.exists():
        existing_lines = read_text_file_lines(repo_dir, filename)
        current_content = "\n".join(line.strip() for line in existing_lines if line.strip()).strip()
    else:
        current_content = ""
        
    # 2. 準備最新的規則內容
    target_content = "\n".join(line.strip() for line in required_lines if line.strip()).strip()
    
    # 3. 核心防護：內容完全一致就收手，不一致才寫入，且強制使用 \n (LF)
    if current_content != target_content:
        # newline="\n" 是防止 Windows 偷偷塞入 \r\n 的關鍵
        file_path.write_text(target_content + "\n", encoding="utf-8", newline="\n")


def detect_project_type(repo_dir: Path) -> Tuple[str, str]:
    if any(repo_dir.glob("*.uproject")) or ((repo_dir / "Content").exists() and (repo_dir / "Config").exists()):
        return "unreal", "Unreal Engine 專案"
    if (repo_dir / "Assets").exists() and (repo_dir / "ProjectSettings").exists():
        return "unity", "Unity 專案"
    return "unknown", "一般專案"


def get_gitignore_template(project_type: str) -> List[str]:
    if project_type == "unreal":
        return COMMON_IGNORE + UNREAL_IGNORE
    if project_type == "unity":
        return COMMON_IGNORE + UNITY_IGNORE
    return COMMON_IGNORE.copy()


def get_gitattributes_template(project_type: str) -> List[str]:
    if project_type == "unreal":
        return [
            "*.uasset filter=lfs diff=lfs merge=lfs -text lockable",
            "*.umap filter=lfs diff=lfs merge=lfs -text lockable",
            "*.fbx filter=lfs diff=lfs merge=lfs -text",
            "*.psd filter=lfs diff=lfs merge=lfs -text",
            "*.wav filter=lfs diff=lfs merge=lfs -text",
            "*.png filter=lfs diff=lfs merge=lfs -text",
        ]
    if project_type == "unity":
        return [
            "*.psd filter=lfs diff=lfs merge=lfs -text",
            "*.fbx filter=lfs diff=lfs merge=lfs -text",
            "*.blend filter=lfs diff=lfs merge=lfs -text",
            "*.wav filter=lfs diff=lfs merge=lfs -text",
            "*.png filter=lfs diff=lfs merge=lfs -text",
            "*.unitypackage filter=lfs diff=lfs merge=lfs -text",
        ]
    return []


def get_high_risk_extensions(project_type: str) -> set[str]:
    base = {".psd", ".fbx", ".blend", ".wav", ".png", ".jpg", ".jpeg", ".tga", ".exr"}
    if project_type == "unreal":
        base |= {".uasset", ".umap"}
    elif project_type == "unity":
        base |= {".unity", ".prefab", ".mat", ".anim", ".controller"}
    return base


def get_bad_directories(project_type: str) -> List[str]:
    if project_type == "unreal":
        return COMMON_BAD_DIRS + UNREAL_BAD_DIRS
    if project_type == "unity":
        return COMMON_BAD_DIRS + UNITY_BAD_DIRS
    return COMMON_BAD_DIRS.copy()


def get_auto_heal_cleanup_targets(project_type: str) -> List[str]:
    targets = list(COMMON_AUTO_HEAL_TARGETS)
    if project_type == "unreal":
        targets.extend(["Saved/", "Intermediate/", "DerivedDataCache/", "Binaries/"])
    elif project_type == "unity":
        targets.extend(["Library/", "Temp/", "Obj/", "Build/", "Builds/", "Logs/"])
    return targets


def normalize_gitignore_line(line: str) -> str:
    line = line.strip()
    if not line or line.startswith("#"):
        return ""
    return line.replace("\\", "/")


def _simplify_git_pattern(pattern: str) -> str:
    pattern = pattern.replace("\\", "/").strip()
    if pattern.startswith("!"):
        pattern = pattern[1:].strip()

    while pattern.startswith("/"):
        pattern = pattern[1:]
    if pattern.startswith("**/"):
        pattern = pattern[3:]

    for suffix in ("/**", "/*", "/"):
        if pattern.endswith(suffix):
            pattern = pattern[: -len(suffix)]
            break

    return pattern.strip()


def _gitignore_patterns_equivalent(rule: str, target: str) -> bool:
    rule_norm = rule.replace("\\", "/").strip()
    target_norm = target.replace("\\", "/").strip()

    if rule_norm == target_norm:
        return True

    rule_simple = _simplify_git_pattern(rule_norm)
    target_simple = _simplify_git_pattern(target_norm)
    if not rule_simple or not target_simple:
        return False

    if rule_simple == target_simple:
        return True

    rule_lower = rule_simple.lower()
    target_lower = target_simple.lower()

    return (
        fnmatch(target_lower, rule_lower)
        or fnmatch(rule_lower, target_lower)
        or fnmatch(target_norm.lower(), rule_norm.lower())
        or fnmatch(rule_norm.lower(), target_norm.lower())
    )


def has_gitignore_rule(lines: List[str], target: str) -> bool:
    normalized_lines = [normalize_gitignore_line(x) for x in lines]
    positive_rules = [x for x in normalized_lines if x and not x.startswith("!")]
    target = target.replace("\\", "/").strip()

    return any(_gitignore_patterns_equivalent(rule, target) for rule in positive_rules)


def has_gitattributes_rule(lines: List[str], pattern: str) -> bool:
    if not pattern.strip():
        return False

    target_parts = pattern.split()
    target_glob = target_parts[0]
    target_needs_lockable = "lockable" in target_parts

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()
        if not parts:
            continue

        glob = parts[0]
        if glob != target_glob:
            continue

        has_lfs = (
            "filter=lfs" in line
            and "diff=lfs" in line
            and "merge=lfs" in line
            and "-text" in line
        )
        if not has_lfs:
            continue
        if target_needs_lockable and "lockable" not in line:
            continue
        return True

    return False


def get_project_health(
    repo_dir: Path,
    repo: Repo,
    project_type: str,
    project_name: str,
    file_entries: List[FileEntry],
    lfs_installed: bool,
    tracking_branch: str | None,
) -> ProjectHealth:
    health = ProjectHealth(project_type=project_type, project_name=project_name)
    health.lfs_installed = lfs_installed

    required_ignore = get_gitignore_template(project_type)
    required_attr = get_gitattributes_template(project_type)

    ignore_lines = read_text_file_lines(repo_dir, ".gitignore")
    attr_lines = read_text_file_lines(repo_dir, ".gitattributes")

    health.gitignore_missing = [line for line in required_ignore if not has_gitignore_rule(ignore_lines, line)]
    health.gitattributes_missing = [line for line in required_attr if not has_gitattributes_rule(attr_lines, line)]

    health.tracking_branch = tracking_branch
    health.remote_ok = tracking_branch is not None and any(remote.name == "origin" for remote in repo.remotes)

    risky_ext = get_high_risk_extensions(project_type)
    bad_dirs = tuple(path.replace("\\", "/").lower() for path in get_bad_directories(project_type))

    for entry in file_entries:
        normalized = entry.path.replace("\\", "/")
        normalized_lower = normalized.lower()
        ext = Path(normalized_lower).suffix.lower()

        if ext in risky_ext:
            health.risky_files.append(entry.path)

        if normalized_lower.startswith(bad_dirs) or ext == ".exe":
            health.bad_tracked_files.append(entry.path)

    return health


def build_idle_status_text(project_health: ProjectHealth) -> str:
    parts = [project_health.project_name]
    parts.append("LFS 就緒" if project_health.lfs_installed else "LFS 未安裝")
    parts.append(f"追蹤 {project_health.tracking_branch}" if project_health.remote_ok else "未設定追蹤分支")

    issues = project_health.issue_count
    parts.append(f"規則/清單異常 {issues} 項" if issues > 0 else "環境正常")
    return "｜".join(parts)