from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class ProjectHealth:
    project_type: str = "unknown"
    project_name: str = "一般專案"
    lfs_installed: bool = False
    gitignore_missing: List[str] = field(default_factory=list)
    gitattributes_missing: List[str] = field(default_factory=list)
    risky_files: List[str] = field(default_factory=list)
    bad_tracked_files: List[str] = field(default_factory=list)
    tracking_branch: Optional[str] = None
    remote_ok: bool = False

    @property
    def issue_count(self) -> int:
        return len(self.gitignore_missing) + len(self.gitattributes_missing) + len(self.bad_tracked_files)

@dataclass
class FileEntry:
    path: str
    display: str
    status_code: str
    stage_paths: List[str] = field(default_factory=list)

@dataclass
class PreflightResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    high_risk_files: List[str] = field(default_factory=list)

@dataclass
class ZeroSyncConfig:
    user_name: str = ""
    user_email: str = ""
    remote_url: str = ""
    access_token: str = ""
    ai_api_key: str = ""
    language: str = "auto"