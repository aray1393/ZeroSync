import json
import os
import threading
import datetime
import math
from PySide6.QtWidgets import QGraphicsDropShadowEffect
from PySide6.QtGui import QColor

import requests
from pathlib import Path
from typing import List

from git import Repo
from git.exc import InvalidGitRepositoryError, NoSuchPathError
from PySide6.QtCore import QObject, QTimer, Qt, Signal, QEvent, QPropertyAnimation, Property, QEasingCurve
from PySide6.QtGui import QColor, QBrush, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QTextEdit,
    QSizePolicy,
)

from git_service import GitProgressHandler, GitService
from models import FileEntry, ProjectHealth
from project_rules import (
    build_idle_status_text,
    detect_project_type,
    ensure_lines_in_file,
    get_gitattributes_template,
    get_gitignore_template,
    get_project_health,
)

APP_TITLE = "ZeroSync - Studio Guard v3.1"


class WorkerSignals(QObject):
    progress = Signal(int, str)
    done = Signal(str)
    error = Signal(str)
    remote_status = Signal(bool)
    refresh_done = Signal()
    ai_msg_done = Signal(str)

class GitErrorDialog(QDialog):
    # 定義跨執行緒的安全通道訊號
    ai_done_signal = Signal(str)

    def __init__(self, parent, raw_error: str, translated_error: str, ai_service, project_type: str):
        super().__init__(parent)
        self.parent_window = parent
        self.raw_error = raw_error
        self.ai_service = ai_service
        self.project_type = project_type
        
        # 1. 視窗基本設定
        self.setWindowTitle("⚠️ 系統任務失敗")
        self.setMinimumWidth(580)
        self.setMinimumHeight(450)
        
        # 2. 佈局建立
        layout = QVBoxLayout(self)
        
        # 報告顯示區
        self.text_area = QTextEdit()
        self.text_area.setReadOnly(True)
        self.text_area.setStyleSheet("""
            background-color: #1e1e1e; 
            color: #d4d4d4; 
            font-size: 14px; 
            padding: 12px; 
            border: 1px solid #333333;
            border-radius: 4px;
        """)
        self.text_area.setText(f"【系統初步判定】\n{translated_error}\n\n（若需進一步協助，請點擊下方 AI 診斷）")
        layout.addWidget(self.text_area)
        
        # 按鈕區佈局
        btn_layout = QHBoxLayout()
        
        # --- AI 診斷按鈕 ---
        self.btn_ai = QPushButton("🤖 AI 智能診斷")
        self.btn_ai.setStyleSheet("""
            QPushButton {
                background-color: #007acc; 
                color: white; 
                padding: 10px 20px; 
                font-weight: bold; 
                border-radius: 4px;
                border: 1px solid #005a9e;
            }
            QPushButton:hover {
                background-color: #0086e0;
                border: 1px solid #00F2FE;
            }
            QPushButton:pressed {
                background-color: #005a9e;
                padding-top: 11px;
                padding-bottom: 9px;
            }
        """)
        self.btn_ai.clicked.connect(self.run_ai_diagnosis)
        
        # --- Git Bash 按鈕 ---
        self.btn_bash = QPushButton("💻 開啟 Git Bash")
        self.btn_bash.setStyleSheet("""
            QPushButton {
                background-color: #3e3e42; 
                color: white; 
                padding: 10px 20px; 
                font-weight: bold; 
                border-radius: 4px;
                border: 1px solid #333333;
            }
            QPushButton:hover {
                background-color: #4e4e52;
                border: 1px solid #666666;
            }
            QPushButton:pressed {
                background-color: #2d2d30;
                padding-top: 11px;
                padding-bottom: 9px;
            }
        """)
        self.btn_bash.setToolTip("直接開啟專案專屬的命令列工具")
        self.btn_bash.clicked.connect(self.open_git_bash)
        
        # --- 關閉按鈕 ---
        self.btn_close = QPushButton("關閉")
        self.btn_close.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #d4d4d4;
                padding: 10px 20px; 
                border-radius: 4px;
                border: 1px solid #444444;
            }
            QPushButton:hover {
                background-color: #d44336;
                color: white;
                border: 1px solid #d44336;
            }
        """)
        self.btn_close.clicked.connect(self.accept)
        
        btn_layout.addWidget(self.btn_ai)
        btn_layout.addWidget(self.btn_bash)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_close)
        
        layout.addLayout(btn_layout)

        # 3. ✨ 特效與執行緒初始化
        import math
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        from PySide6.QtGui import QColor
        from PySide6.QtCore import QTimer

        self.original_style = self.btn_ai.styleSheet() 

        self.glow_effect = QGraphicsDropShadowEffect(self)
        self.glow_effect.setOffset(0, 0) 
        self.glow_effect.setBlurRadius(0) 
        self.glow_effect.setColor(QColor("#00F2FE"))
        self.btn_ai.setGraphicsEffect(self.glow_effect) 

        self.spinner_timer = QTimer(self)
        self.spinner_timer.timeout.connect(self._update_loading_animation)
        self.spinner_frames = ["|", "/", "-", "\\"]
        self.dot_frames = ["", ".", "..", "..."]
        self.tick_count = 0

        self.ai_done_signal.connect(self._on_diagnosis_done)

    def _update_loading_animation(self):
        import math
        spin = self.spinner_frames[self.tick_count % len(self.spinner_frames)]
        dots = self.dot_frames[self.tick_count % len(self.dot_frames)]
        self.btn_ai.setText(f"🤖 診斷中 {spin} {dots:<3}")

        breath = abs(math.sin(self.tick_count * 0.15)) 
        intensity = int(100 + 155 * breath) 
        color_hex = f"#00{intensity:02x}FF" 
        
        self.glow_effect.setBlurRadius(15 + 25 * breath) 
        self.glow_effect.setColor(QColor(color_hex))

        self.btn_ai.setStyleSheet(f"""
            QPushButton:disabled {{
                background-color: #1e1e1e;
                color: {color_hex};
                border: 2px solid {color_hex};
                border-radius: 4px;
                font-weight: bold;
            }}
        """)
        self.tick_count += 1

    def run_ai_diagnosis(self):
        if not self.ai_service or not self.ai_service.is_ready:
            self.text_area.setText("【AI 診斷失敗】\n請先至系統設定中填寫 AI API Key！")
            return
            
        self.btn_ai.setEnabled(False)
        self.tick_count = 0
        self.spinner_timer.start(50)

        import threading
        def _worker():
            try:
                result = self.ai_service.diagnose_error(self.raw_error, self.project_type)
                self.ai_done_signal.emit(result)
            except Exception as e:
                self.ai_done_signal.emit(f"【診斷異常】\n{str(e)}")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_diagnosis_done(self, result):
        self.spinner_timer.stop()
        self.glow_effect.setBlurRadius(0) 
        self.btn_ai.setStyleSheet(self.original_style)
        
        self.text_area.setText(f"【AI 專業診斷報告】\n\n{result}")
        self.btn_ai.setEnabled(True)
        self.btn_ai.setText("🤖 重新診斷")

    def open_git_bash(self):
        import subprocess
        import shutil
        from pathlib import Path
        
        base_dir = self.parent_window.repo_dir
        
        candidates = [
            base_dir / "bin" / "git" / "git-bash.exe",
            Path("C:/Program Files/Git/git-bash.exe"),
        ]
        
        system_bash = shutil.which("git-bash")
        if system_bash:
            candidates.append(Path(system_bash))
            
        bash_exe = next((p for p in candidates if p.exists()), None)
        
        if not bash_exe:
            self.text_area.append("\n\n❌ 【系統錯誤】\n找不到 Git Bash。")
            return
        
        try:
            subprocess.Popen([str(bash_exe)], cwd=str(base_dir), shell=False)
            self.text_area.append("\n\n✅ 【系統提示】已成功開啟 Git Bash。")
        except Exception as e:
            self.text_area.append(f"\n\n❌ 【啟動失敗】\n{str(e)}")

class SettingsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.setWindowTitle("⚙️ 系統設定")
        self.setFixedWidth(450)
        self.setFixedHeight(340)
        
        # 1. 記憶原始狀態 (為了按 X 時還原)
        try:
            self.original_name = parent.repo.git.config("user.name").strip()
        except Exception:
            self.original_name = ""
        self.original_colors = parent.load_team_colors() 

        # 主佈局
        main_layout = QVBoxLayout(self)
        form_layout = QFormLayout()

        # --- 修正：正確讀取 config 並對應欄位 ---
        from config_manager import ConfigManager
        self.config_manager = ConfigManager(parent.repo_dir)
        config = self.config_manager.load()

        # 直接使用 config 對象的屬性
        current_name = config.user_name if config.user_name else self.original_name
        current_pat = config.access_token
        current_ai_key = config.ai_api_key # 直接從 config 讀取原文

        # 這裡維持讀取顏色檔裡的 Discord 設定
        current_webhook = self.original_colors.get("_discord_webhook_config_", "")
        current_role_id = self.original_colors.get("_discord_role_id_config_", "")

        # 名字輸入
        self.name_input = QLineEdit(current_name)
        form_layout.addRow("使用者名字：", self.name_input)

        # PAT 輸入
        self.pat_input = QLineEdit(current_pat)
        self.pat_input.setPlaceholderText("在此貼上新的 PAT 權杖...")
        self.pat_input.setEchoMode(QLineEdit.Password)
        form_layout.addRow("修改 PAT：", self.pat_input)

        # Discord Webhook
        self.webhook_input = QLineEdit(current_webhook)
        self.webhook_input.setPlaceholderText("在此貼上 Discord Webhook URL（留空則不通知）")
        form_layout.addRow("Discord Webhook：", self.webhook_input)

        # Discord 身分組 ID
        self.role_id_input = QLineEdit(current_role_id)
        self.role_id_input.setPlaceholderText("填入 Discord 身分組 ID（留空則不標註）")
        form_layout.addRow("Discord 標註身分組 ID：", self.role_id_input)

        self.ai_api_input = QLineEdit(current_ai_key)
        self.ai_api_input.setPlaceholderText("在此貼上 Gemini 或 OpenAI API Key...")
        self.ai_api_input.setEchoMode(QLineEdit.Password)
        form_layout.addRow("AI 診斷 API Key：", self.ai_api_input)

        # --- 修改顏色區 (靠左排版) ---
        color_layout = QHBoxLayout()
        self.color_btn = QPushButton("🎨 修改系統主題顏色")
        self.color_btn.setObjectName("settings_action_btn")
        self.color_btn.clicked.connect(parent.pick_color)
        color_layout.addWidget(self.color_btn)
        color_layout.addStretch()
        form_layout.addRow("介面配色：", color_layout)

        main_layout.addLayout(form_layout)
        main_layout.addStretch()

        # --- 底部按鈕區 (右下角對齊) ---
        bottom_layout = QHBoxLayout()
        bottom_layout.addStretch()
        
        self.save_btn = QPushButton("💾 儲存並套用")
        self.save_btn.setObjectName("save_btn")
        self.save_btn.setFixedWidth(120)
        self.save_btn.clicked.connect(self.accept)
        bottom_layout.addWidget(self.save_btn)
        
        main_layout.addLayout(bottom_layout)

        # 設定按鈕樣式
        self.setStyleSheet("""
            QPushButton#settings_action_btn { background-color: #3e3e42; border: 1px solid #555; padding: 8px; border-radius: 4px; color: white;}
            QPushButton#settings_action_btn:hover { background-color: #505050; }
            QPushButton#settings_action_btn:pressed { background-color: #2d2d30; }
            
            QPushButton#save_btn { background-color: #007acc; border: none; padding: 10px; font-weight: bold; border-radius: 4px; color: white;}
            QPushButton#save_btn:hover { background-color: #005a9e; }
            QPushButton#save_btn:pressed { background-color: #004a80; }
        """)

    def get_values(self):
        return (
            self.name_input.text(),
            self.pat_input.text(),
            self.webhook_input.text().strip(),
            self.role_id_input.text().strip(),
            self.ai_api_input.text().strip(),
        )

    def reject(self):
        """當按下 X 或取消時，還原顏色"""
        try:
            with open(self.parent.color_registry_file, "w", encoding="utf-8") as f:
                json.dump(self.original_colors, f, indent=4, ensure_ascii=False)
            self.parent.refresh_data()
        except Exception:
            pass
        super().reject()

class VerticalToggle(QWidget):
    toggled = Signal(bool)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(14) 
        self.is_on = True
        self.setCursor(Qt.PointingHandCursor)
        self._position = 0.0
        self.anim = QPropertyAnimation(self, b"position")
        self.anim.setDuration(150)
        self.anim.setEasingCurve(QEasingCurve.InOutQuad)

    def get_position(self): return self._position
    def set_position(self, pos):
        self._position = pos
        self.update()
    position = Property(float, get_position, set_position)

    def mouseReleaseEvent(self, e):
        self.is_on = not self.is_on
        self.toggled.emit(self.is_on)
        # 動態計算：完全頂到底部
        self.anim.setEndValue(0.0 if self.is_on else float(self.height() - 12))
        self.anim.start()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        
        # 背景顏色
        bg_color = QColor("#4a7c59") if self.is_on else QColor("#8b3a3a")
        p.setBrush(bg_color)
        
        # ✨ 關鍵修改：從 drawRect 換成 drawRoundedRect，並加上 3 像素倒角
        # 參數分別是：(x, y, 寬, 高, x半徑, y半徑)
        p.drawRoundedRect(0, 0, self.width(), self.height(), 3, 3) 
        
        # 畫滑塊
        p.setBrush(QColor("#e0e0e0"))
        p.drawRoundedRect(1, self._position + 1, self.width() - 2, 10, 1, 1) 
        p.end()

class ZeroSyncPro(QMainWindow):
    def __init__(self, repo_path: str):
        super().__init__()
        self.repo_path = os.path.abspath(repo_path)
        self.repo_dir = Path(self.repo_path)
        self.color_registry_file = str(self.repo_dir / "ZeroSync_Colors.json")
        self.project_type, self.project_name = detect_project_type(self.repo_dir)
        self.project_health = ProjectHealth()
        self.idle_status_text = "系統就緒"
        self.file_entries: List[FileEntry] = []

        try:
            self.repo = Repo(self.repo_path)
        except (InvalidGitRepositoryError, NoSuchPathError):
            if not self.ask_initialize_repo():
                QMessageBox.critical(self, "錯誤", "這裡不是 Git 專案，已取消啟動。")
                raise SystemExit(1)
            self.repo = Repo(self.repo_path)

        self.service = GitService(self.repo_path)
        self.repo = self.service.repo

        self.signals = WorkerSignals()
        self.signals.progress.connect(self.update_ui_progress)
        self.signals.done.connect(self.on_task_done)
        self.signals.error.connect(self.on_error)
        self.signals.remote_status.connect(self.set_pull_highlight)
        self.signals.refresh_done.connect(self.on_refresh_done)
        self.signals.ai_msg_done.connect(self.on_ai_msg_done)

        self.spin_timer = QTimer()
        self.spin_timer.timeout.connect(self.update_spinner)
        self.spin_index = 0
        self.spin_chars = ["|", "/", "-", "\\"]

        self.check_timer = QTimer()
        self.check_timer.timeout.connect(self.check_remote_updates)
        self.check_timer.start(120000)

        self.id_timer = QTimer()
        self.id_timer.timeout.connect(self.auto_refresh_id)
        self.id_timer.start(120000)

        self.setWindowTitle(APP_TITLE)
        self.resize(1100, 800)
        self.init_ui()
        self.refresh_data()
        self.check_remote_updates()

    def load_runtime_config(self) -> dict:
        config_path = self.repo_dir / ".zerosync" / "ZeroSync_Config.json"
        if config_path.exists():
            try:
                return json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def get_current_git_user_name(self) -> str:
        try:
            name = self.repo.git.config("user.name").strip()
            if name:
                return name
        except Exception:
            pass
        return self.load_runtime_config().get("user_name", "Unknown User")

    def send_discord_timeline_report(self, user_name: str, commit_msg: str):
        """Push 成功後，把這次提交摘要同步送到 Discord。"""
        try:
            team_colors = self.load_team_colors()
            webhook_url = str(team_colors.get("_discord_webhook_config_", "")).strip()
            if not webhook_url:
                return

            role_id = str(team_colors.get("_discord_role_id_config_", "")).strip()
            mention_str = f"<@&{role_id}>" if role_id else ""

            user_hex = team_colors.get(user_name, "#ffffff")
            try:
                decimal_color = int(user_hex.lstrip("#"), 16)
            except Exception:
                decimal_color = 16777215

            now = datetime.datetime.now()
            time_str = now.strftime("[%m/%d %H:%M]")
            description = f"**{time_str} {user_name}：**\n{commit_msg}"

            payload = {
                "username": "ZeroSync Timeline",
                "content": mention_str,
                "embeds": [
                    {
                        "color": decimal_color,
                        "description": description,
                    }
                ],
            }

            def _post():
                try:
                    requests.post(webhook_url, json=payload, timeout=5)
                except Exception:
                    pass

            threading.Thread(target=_post, daemon=True).start()
        except Exception:
            pass

    def open_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec():
            # 接收 5 個變數
            new_name, new_pat, new_webhook, new_role_id, new_ai_api = dialog.get_values()

            # --- 修正：直接建立 ConfigManager，不再依賴錯誤的 service 屬性 ---
            from config_manager import ConfigManager
            config_mgr = ConfigManager(self.repo_dir)
            config = config_mgr.load()

            config.user_name = new_name
            # 直接把新值塞給 config 對象，save 時裝甲會自動執行 XOR 加密
            config.access_token = new_pat
            config.ai_api_key = new_ai_api

            # 處理 Discord Webhook (維持存在顏色設定檔的邏輯)
            team_colors = self.load_team_colors()
            team_colors["_discord_webhook_config_"] = new_webhook
            team_colors["_discord_role_id_config_"] = new_role_id

            try:
                # 執行加密存檔，這會寫入到 .zerosync/ZeroSync_Config.json
                config_mgr.save(config)

                # --- 安全修正：設定儲存後，立刻把 origin 洗成不含 PAT 的乾淨 URL ---
                # 注意：真正的 PAT 仍由 ConfigManager 保存；Git 指令執行時交給 GitService 臨時注入。
                # 這段只負責把舊版 https://pat:xxxxx@... 從 .git/config 裡清掉。
                remote_update_warning = ""
                try:
                    remote_url = (config.remote_url or "").strip()

                    if not remote_url and hasattr(self, "repo") and self.repo:
                        try:
                            if any(remote.name == "origin" for remote in self.repo.remotes):
                                remote_url = self.repo.remotes.origin.url or ""
                        except Exception:
                            remote_url = ""

                    if remote_url and hasattr(self, "service") and self.service:
                        clean_url = self.service.configure_remote(remote_url, new_pat)
                        if clean_url and clean_url != config.remote_url:
                            config.remote_url = clean_url
                            config_mgr.save(config)
                        self.service.refresh_repo()
                        self.repo = self.service.repo
                except Exception as remote_err:
                    # 不讓 remote 清理失敗吃掉設定儲存，但要告知使用者後續同步可能仍要檢查。
                    remote_update_warning = f"\n\n但遠端網址清理失敗，請稍後執行 Pull / Push 或手動檢查：\n{str(remote_err)}"

                with open(self.color_registry_file, "w", encoding="utf-8") as f:
                    json.dump(team_colors, f, indent=4, ensure_ascii=False)

                if new_name and hasattr(self, 'repo') and self.repo:
                    self.repo.git.config("user.name", new_name)

                QMessageBox.information(self, "成功", f"系統設定與 AI 裝甲已更新！{remote_update_warning}")
                self.refresh_data()
            except Exception as e:
                QMessageBox.critical(self, "錯誤", f"儲存設定失敗：{str(e)}")

    def ask_initialize_repo(self) -> bool:
        detected_type, detected_name = detect_project_type(self.repo_dir)
        msg = QMessageBox(self)
        msg.setWindowTitle("初始化專案")
        msg.setIcon(QMessageBox.Question)
        msg.setText("這裡不是 Git 專案。要不要直接幫你初始化？")
        msg.setInformativeText(f"偵測到：{detected_name}\n會建立 Git repo，並補上推薦的 .gitignore / .gitattributes。")
        yes_btn = msg.addButton("幫我初始化", QMessageBox.AcceptRole)
        msg.addButton("取消", QMessageBox.RejectRole)
        msg.exec()

        if msg.clickedButton() != yes_btn:
            return False

        from models import ZeroSyncConfig
        try:
            self.service.bootstrap_init_repo(ZeroSyncConfig(), detected_type)
            detail_lines = ["Git 專案與 ZeroSync 禁制令已部署完成。", "推薦的同步規則（.gitignore / .gitattributes）已就位。"]
            QMessageBox.information(self, "初始化完成", "\n".join(detail_lines))
            return True
        except Exception as e:
            QMessageBox.critical(self, "初始化失敗", f"部署過程出錯：\n{str(e)}")
            return False

    def open_project_target(self):
        try:
            if self.project_type == "unreal":
                project_files = list(self.repo_dir.glob("*.uproject"))
                if project_files:
                    os.startfile(str(project_files[0]))
                    return
            os.startfile(self.repo_path)
        except Exception as e:
            QMessageBox.critical(self, "出錯", self.service.translate_error(str(e)))

    def force_overwrite_local(self):
        reply = QMessageBox.warning(
            self,
            "⚠️ 終極警告",
            "這會刪除你目前【所有】未提交的修改，並強制同步至雲端最新版。\n\n你確定要放棄目前的全部心血，執行強制重置嗎？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            self.status_label.setText("🔥 正在執行毀滅性重置...")
            QApplication.processEvents()
            msg = self.service.force_overwrite_local()
            self.refresh_data()
            self.status_label.setText(msg)
            QMessageBox.information(self, "重生成功", "已經幫你把專案拉回雲端最新版本了。下次別再亂搞啦！")
        except Exception as e:
            self.handle_git_error(e)

    def clean_local_garbage(self):
        reply = QMessageBox.question(
            self,
            "🧹 大掃除確認",
            "確定要清理垃圾嗎？\n\n這會把專案資料夾裡所有「未追蹤」的檔案全部刪除。\n（如果你有剛新建但還沒 Commit 的檔案，也會一起消失喔！）",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            self.status_label.setText("🧹 正在清理未追蹤的殘留檔案...")
            QApplication.processEvents()
            msg = self.service.clean_local_garbage()
            self.refresh_data()
            self.status_label.setText(msg)
            QMessageBox.information(self, "清理完成", "專案環境已經恢復清爽了！")
        except Exception as e:
            self.handle_git_error(e)

    def fix_git_encoding(self):
        reply = QMessageBox.question(
            self,
            "🔠 編碼校正",
            "確定要強制修正 Git 的中文編碼設定嗎？\n這會讓那些用中文命名的檔案在列表裡恢復正常顯示。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            self.status_label.setText("🔠 正在校正 Git 編碼環境...")
            QApplication.processEvents()
            msg = self.service.fix_git_encoding()
            self.refresh_data()
            self.status_label.setText(msg)
            QMessageBox.information(self, "成功", "Git 編碼環境已優化！\n以後看到亂碼，點這個就對了。")
        except Exception as e:
            self.handle_git_error(e)

    def fix_remote_connection(self):
        reply = QMessageBox.question(
            self,
            "📡 修復連線",
            "確定要重置網路與遠端快取嗎？\n這會清除卡住的連線設定，幫你重新連上雲端。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            self.status_label.setText("📡 正在清理網路設定與遠端快取...")
            QApplication.processEvents()
            msg = self.service.fix_remote_connection()
            self.refresh_data()
            self.status_label.setText(msg)
            QMessageBox.information(self, "成功", "遠端快取已清理，現在應該能正常獲取雲端資料了！")
        except Exception as e:
            self.handle_git_error(e)

    def fix_upstream_tracking(self):
        reply = QMessageBox.question(
            self,
            "🔗 重建分支追蹤",
            "確定要強制把本地進度重新對接雲端嗎？\n專治『找不到雲端上傳節點』的錯誤。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            self.status_label.setText("🔗 正在重建上傳節點...")
            QApplication.processEvents()
            msg = self.service.fix_upstream_tracking()
            self.refresh_data()
            self.status_label.setText(msg)
            QMessageBox.information(self, "成功", "分支已重新對接！現在你應該可以正常點擊上傳了。")
        except Exception as e:
            self.handle_git_error(e)

    def fix_ghost_files(self):
        reply = QMessageBox.question(
            self,
            "👻 驅除幽靈檔案",
            "確定要強制洗刷 Git 索引嗎？\n這會把『不該追蹤的垃圾』踢出去，並將目前的正常變動自動存成一個 Commit。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        from PySide6.QtWidgets import QInputDialog
        # 讓你可以自己輸入這次救援的訊息，預設幫你填好
        msg, ok = QInputDialog.getText(self, "輸入救援訊息", "請輸入這次清理的 Commit 訊息：", text="清理幽靈檔案並儲存進度")
        if not ok or not msg.strip():
            return

        try:
            self.status_label.setText("👻 正在執行驅鬼儀式...")
            QApplication.processEvents()
            result_msg = self.service.fix_ghost_files(msg.strip())
            self.refresh_data()
            self.status_label.setText(result_msg)
            QMessageBox.information(self, "驅除成功", result_msg)
        except Exception as e:
            self.handle_git_error(e)

    def switch_view(self, index: int):
        self.view_stack.setCurrentIndex(index)
        active_style = "border-bottom: 2px solid #007acc; color: #ffffff; background-color: #1e1e1e;"
        inactive_style = "border-bottom: 2px solid transparent; color: #888888; background-color: #2d2d30;"
        base_style = "padding: 10px; border: none; font-weight: bold; "
        self.btn_tab_pending.setStyleSheet(base_style + (active_style if index == 0 else inactive_style))
        self.btn_tab_repair.setStyleSheet(base_style + (active_style if index == 1 else inactive_style))
        if index == 0:
            self.refresh_data()

    def init_ui(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #1e1e1e; }
            QLabel { color: #aaaaaa; font-family: "Microsoft JhengHei"; font-size: 14px; }
            QListWidget { background-color: #252526; color: #ffffff; border: 1px solid #333333; border-radius: 5px; }
            QLineEdit { background-color: #3c3c3c; color: white; border: 1px solid #555555; padding: 8px; border-radius: 3px; }
            QPushButton { color: white; border-radius: 5px; padding: 10px; font-weight: bold; font-family: "Microsoft JhengHei"; border: 1px solid transparent; }
            QPushButton:disabled, #push_btn:disabled, #pull_btn:disabled, #refresh_btn:disabled { background-color: #333333 !important; color: #666666 !important; border: 1px solid #444444 !important; }
            #refresh_btn { background-color: #333333; border: 1px solid #555555; }
            #refresh_btn:hover { background-color: #444444; border: 1px solid #888888; }
            #open_btn { background-color: #d44c00; }
            #open_btn:hover { background-color: #a33b00; }
            #settings_btn { background-color: #444444; border: 1px solid #555555; }
            #settings_btn:hover { background-color: #555555; }
            #pull_btn { background-color: #3e3e42; }
            #pull_btn:hover { background-color: #2d2d30; }
            #pull_btn[update_available="true"] { background-color: #ff9800; color: #000; }
            #pull_btn[update_available="true"]:hover { background-color: #e68a00; }
            #push_btn { background-color: #007acc; }
            #push_btn:hover { background-color: #005a9e; }
            QProgressBar { background-color: #333; border: 1px solid #444; border-radius: 10px; text-align: center; color: white; height: 12px; }
            QProgressBar::chunk { background-color: #007acc; border-radius: 10px; }
        """)

        main_layout = QVBoxLayout()
        splitter = QSplitter(Qt.Horizontal)

        log_c = QWidget()
        log_l = QVBoxLayout()
        header = QHBoxLayout()
        header.addWidget(QLabel("📜 專案歷史紀錄 (Timeline)"))
        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.setObjectName("refresh_btn")
        self.refresh_btn.setMinimumWidth(80)
        self.refresh_btn.clicked.connect(self.run_refresh_thread)
        header.addWidget(self.refresh_btn)
        log_l.addLayout(header)
        self.log_list = QListWidget()
        log_l.addWidget(self.log_list)
        log_c.setLayout(log_l)

        action_c = QWidget()
        action_l = QVBoxLayout()
        tool_l = QHBoxLayout()

        self.open_btn = QPushButton("🚀 開啟專案")
        self.open_btn.setObjectName("open_btn")
        self.open_btn.clicked.connect(self.open_project_target)

        self.settings_btn = QPushButton("⚙️ 系統設定")
        self.settings_btn.setObjectName("settings_btn")
        self.settings_btn.clicked.connect(self.open_settings)

        tool_l.addWidget(self.open_btn)
        tool_l.addWidget(self.settings_btn)
        action_l.addLayout(tool_l)

        self.tab_container = QFrame()
        self.tab_container.setObjectName("tab_container")
        self.tab_container.setStyleSheet("""
            QFrame#tab_container {
                background-color: #1e1e1e;
                border: 1px solid #3e3e42;
                border-radius: 4px;
            }
        """)
        container_layout = QVBoxLayout(self.tab_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        tab_layout = QHBoxLayout()
        tab_layout.setSpacing(0)
        self.btn_tab_pending = QPushButton("📂 待處理變動")
        self.btn_tab_repair = QPushButton("🛠️ 系統修復")
        tab_style = """
            QPushButton {
                padding: 10px;
                background-color: #2d2d30;
                border: none;
                border-bottom: 2px solid transparent;
                color: #888888;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3e3e42;
                color: #ffffff;
            }
        """
        self.btn_tab_pending.setStyleSheet(tab_style + "QPushButton { border-bottom: 2px solid #007acc; color: #ffffff; background-color: #1e1e1e; }")
        self.btn_tab_repair.setStyleSheet(tab_style)
        tab_layout.addWidget(self.btn_tab_pending)
        tab_layout.addWidget(self.btn_tab_repair)
        container_layout.addLayout(tab_layout)

        self.view_stack = QStackedWidget()
        self.view_stack.setStyleSheet("background-color: transparent; border: none;")
        container_layout.addWidget(self.view_stack)

        self.pending_page = QWidget()
        pending_layout = QVBoxLayout(self.pending_page)
        pending_layout.setContentsMargins(5, 5, 5, 5)
        self.file_list = QListWidget()
        self.file_list.setStyleSheet("border: none; background: transparent;")
        self.file_list.itemChanged.connect(self.check_push_readiness)
        pending_layout.addWidget(self.file_list)
        self.view_stack.addWidget(self.pending_page)

        self.repair_page = QWidget()
        repair_layout = QVBoxLayout(self.repair_page)
        repair_layout.setContentsMargins(10, 10, 10, 10)

        self.force_pull_btn = QPushButton("⚠️ 放棄本地修改 (強制從雲端覆蓋)")
        self.force_pull_btn.setToolTip("警告：此操作會刪除所有尚未上傳的修改，並將專案強制還原至雲端最新狀態。\n如果專案壞到救不回來，再使用此功能。")
        self.force_pull_btn.setStyleSheet("""
            QPushButton { padding: 12px; background-color: #8b0000; color: #ffffff; font-weight: bold; border: none; border-radius: 4px; }
            QPushButton:hover { background-color: #a52a2a; }
        """)
        self.force_pull_btn.clicked.connect(self.force_overwrite_local)
        repair_layout.addWidget(self.force_pull_btn)

        self.clean_garbage_btn = QPushButton("清理垃圾殘留 (刪除未追蹤檔案)")
        self.clean_garbage_btn.setToolTip("說明：這會刪除所有「沒有被 Git 追蹤」的新增檔案。\n放心，這不會影響到已經提交的正常專案進度。")
        self.clean_garbage_btn.setStyleSheet("""
            QPushButton { padding: 12px; background-color: #d2691e; color: #ffffff; font-weight: bold; border: none; border-radius: 4px; margin-top: 10px; }
            QPushButton:hover { background-color: #cd853f; }
        """)
        self.clean_garbage_btn.clicked.connect(self.clean_local_garbage)
        repair_layout.addWidget(self.clean_garbage_btn)

        self.fix_encoding_btn = QPushButton("修復中文路徑亂碼 (編碼校正)")
        self.fix_encoding_btn.setToolTip("說明：修正因 Git 編碼設定錯誤導致的檔案路徑亂碼問題。")
        self.fix_encoding_btn.setStyleSheet("""
            QPushButton { padding: 12px; background-color: rgb(70, 70, 70); color: #ffffff; font-weight: bold; border: none; border-radius: 4px; margin-top: 10px; }
            QPushButton:hover { background-color: rgb(99, 99, 99); }
        """)
        self.fix_encoding_btn.clicked.connect(self.fix_git_encoding)
        repair_layout.addWidget(self.fix_encoding_btn)

        self.fix_remote_btn = QPushButton("修復獲取失敗 (清理遠端快取與網路)")
        self.fix_remote_btn.setToolTip("說明：當你按刷新卻抓不到雲端資料時使用。")
        self.fix_remote_btn.setStyleSheet("""
            QPushButton { padding: 12px; background-color: rgb(70, 70, 70); color: #ffffff; font-weight: bold; border: none; border-radius: 4px; margin-top: 10px; }
            QPushButton:hover { background-color: rgb(99, 99, 99); }
        """)
        self.fix_remote_btn.clicked.connect(self.fix_remote_connection)
        repair_layout.addWidget(self.fix_remote_btn)

        self.fix_upstream_btn = QPushButton("修復上傳失敗 (重建分支追蹤)")
        self.fix_upstream_btn.setToolTip("說明：當你能獲取，但上傳時系統抱怨找不到雲端路徑時使用。")
        self.fix_upstream_btn.setStyleSheet("""
            QPushButton { padding: 12px; background-color: rgb(70, 70, 70); color: #ffffff; font-weight: bold; border: none; border-radius: 4px; margin-top: 10px; }
            QPushButton:hover { background-color: rgb(99, 99, 99); }
        """)
        self.fix_ghost_btn = QPushButton("驅除幽靈檔案 (重置 Git 索引)")
        self.fix_ghost_btn.setToolTip("說明：當組員誤加了一堆垃圾，導致 .gitignore 失效時使用。\n這會自動幫你洗刷 Git 索引，並產生一個救援 Commit。")
        self.fix_ghost_btn.setStyleSheet("""
            QPushButton { padding: 12px; background-color: rgb(70, 70, 70); color: #ffffff; font-weight: bold; border: none; border-radius: 4px; margin-top: 10px; }
            QPushButton:hover { background-color: rgb(99, 99, 99); }
        """)
        self.fix_ghost_btn.clicked.connect(self.fix_ghost_files)
        repair_layout.addWidget(self.fix_ghost_btn)


        self.fix_upstream_btn.clicked.connect(self.fix_upstream_tracking)
        repair_layout.addWidget(self.fix_upstream_btn)
        repair_layout.addStretch()
        self.view_stack.addWidget(self.repair_page)

        action_l.addWidget(self.tab_container)
        self.btn_tab_pending.clicked.connect(lambda: self.switch_view(0))
        self.btn_tab_repair.clicked.connect(lambda: self.switch_view(1))

        self.input_layout = QHBoxLayout()
        self.input_layout = QHBoxLayout()
        self.input_layout.setContentsMargins(0, 0, 0, 0)
        self.input_layout.setSpacing(1) 

        # 建立膠囊外框
        self.id_wrapper = QFrame()
        self.id_wrapper.setObjectName("id_wrapper")
        self.id_wrapper.setMinimumWidth(16)
        self.id_wrapper.setMaximumWidth(62)
        self.id_wrapper.setFixedHeight(30)
        self.id_wrapper.setStyleSheet("QFrame#id_wrapper { background-color: #3c3c3c; border: 1px solid #555555; border-radius: 3px; }")
        
        # 膠囊內部的排版
        wrapper_layout = QHBoxLayout(self.id_wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0) 
        wrapper_layout.setSpacing(0)
        wrapper_layout.setAlignment(Qt.AlignLeft)

        self.id_toggle = VerticalToggle()
        self.id_toggle.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        self.id_input = QLineEdit()
        self.id_input.setPlaceholderText("編號")
        self.id_input.setAlignment(Qt.AlignCenter)
        self.id_input.setStyleSheet("background: transparent; color: white; border: none; padding: 0;")
        self.id_input.textChanged.connect(self.check_push_readiness)
        self.id_input.installEventFilter(self)
        self.id_input.setCursor(Qt.SizeVerCursor)

        # 把東西塞進外框
        wrapper_layout.addWidget(self.id_toggle)
        wrapper_layout.addWidget(self.id_input)

        # 👈 【核心修正】先讓動態變形動畫引擎誕生！躺好待命
        self.wrapper_anim = QPropertyAnimation(self.id_wrapper, b"maximumWidth")
        self.wrapper_anim.setDuration(250)
        self.wrapper_anim.setEasingCurve(QEasingCurve.InOutQuart)

        # 👈 【核心修正】等所有元件跟動畫都準備好了，最後才綁定開關訊號！
        self.id_toggle.toggled.connect(self.on_id_toggle_changed)

        # 動態變形動畫引擎
        self.msg_input = QLineEdit()
        self.msg_input.setFixedHeight(30)
        self.msg_input.setPlaceholderText("請輸入這次改了什麼...")
        self.msg_input.textChanged.connect(self.check_push_readiness)
        self.msg_input.setTextMargins(6, 0, 85, 0)
        # 【核心修正】：強制把這個輸入框的上下內邊距歸零，字體就不會被切掉了
        self.msg_input.setStyleSheet("""
            QLineEdit {
                padding-top: 0px;
                padding-bottom: 0px;
            }
        """)

        # 2. 建立精緻版 AI 按鈕（高度同步改回 30 填滿外框）
        self.ai_suggest_btn = QPushButton("自動填寫")
        self.ai_suggest_btn.setObjectName("ai_suggest_btn")
        self.ai_suggest_btn.setFixedSize(75, 30)
        self.ai_suggest_btn.setCursor(Qt.PointingHandCursor)
        self.ai_suggest_btn.setToolTip("讓 AI 幫你看看改了什麼")
        self.ai_suggest_btn.setStyleSheet("""
            QPushButton {
                background-color: #3d2a5c;
                color: #d4a5ff;
                border: 2px solid #5a3d7a;
                border-right: none;
                font-weight: bold;
                border-top-right-radius: 3px;
                border-bottom-right-radius: 3px;
                border-top-left-radius: 3px;
                border-bottom-left-radius: 2px;
                padding: 0px;
                margin-top: 1px;
                margin-bottom: 1px;
            }
            QPushButton:hover { background-color: #4b3370; border-color: #734eb0; }
            QPushButton:pressed { background-color: #2b1d3d; border-color: #442d5d; }
            QPushButton:disabled { 
                background-color: #2a203b; 
                color: #7b629a; 
                border: 2px solid #3d2a5c;
                border-right: none;
                margin-top: 1px;
                margin-bottom: 1px;
                border-top-left-radius: 3px;
                border-bottom-left-radius: 3px;
            }
        """)
        self.ai_suggest_btn.clicked.connect(self.generate_ai_commit_msg)

        # 3. 建立內嵌排版（完全貼死右邊）
        inside_layout = QHBoxLayout(self.msg_input)
        inside_layout.setContentsMargins(0, 0, 1, 0) 
        inside_layout.addWidget(self.ai_suggest_btn, 0, Qt.AlignRight | Qt.AlignVCenter)

        # 4. 把大外框組裝起來（注意：這裡絕對不可以再加 ai_suggest_btn 囉！）
        self.input_layout.addWidget(self.id_wrapper)
        self.input_layout.addWidget(self.msg_input)
        action_l.addLayout(self.input_layout)

        status_b = QHBoxLayout()
        self.status_icon = QLabel("")
        self.status_icon.setFixedWidth(20)
        self.status_label = QLabel("系統就緒")
        status_b.addWidget(self.status_icon)
        status_b.addWidget(self.status_label)
        action_l.addLayout(status_b)

        self.pbar = QProgressBar()
        action_l.addWidget(self.pbar)

        btn_l = QHBoxLayout()
        self.pull_btn = QPushButton("[ ⯆ ] 獲取最新 (Pull)")
        self.pull_btn.setObjectName("pull_btn")
        self.pull_btn.setProperty("update_available", "false")
        self.pull_btn.setEnabled(False)
        self.pull_btn.clicked.connect(self.run_pull_thread)

        self.push_btn = QPushButton("[ ⯅ ] 點擊上傳 (Push)")
        self.push_btn.setObjectName("push_btn")
        self.push_btn.setEnabled(False)
        self.push_btn.clicked.connect(self.run_push_thread)

        btn_l.addWidget(self.pull_btn)
        btn_l.addWidget(self.push_btn)
        action_l.addLayout(btn_l)

        action_c.setLayout(action_l)
        splitter.addWidget(log_c)
        splitter.addWidget(action_c)
        splitter.setStretchFactor(0, 1)
        main_layout.addWidget(splitter)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

    def run_refresh_thread(self):
        self.refresh_btn.setEnabled(False)
        self.status_label.setText("📡 正在掃描雲端訊號...")
        self.spin_timer.start(150)

        def _worker():
            try:
                if self.service.repo and "origin" in self.service.repo.remotes:
                    self.service.repo.remotes.origin.fetch()
            except Exception:
                pass
            self.signals.refresh_done.emit()

        threading.Thread(target=_worker, daemon=True).start()

    def on_refresh_done(self):
        self.spin_timer.stop()
        self.refresh_data()
        self.refresh_btn.setEnabled(True)

    def refresh_data(self):
        self.service.refresh_repo()
        self.repo = self.service.repo
        self.project_type, self.project_name = detect_project_type(self.repo_dir)
        self.update_open_button_text()

        self.log_list.clear()
        try:
            local_commits = set()
            try:
                local_commits = {c.hexsha for c in self.repo.iter_commits(max_count=200)}
            except Exception:
                pass

            for c in list(self.repo.iter_commits("--all", max_count=50)):
                is_cloud_only = c.hexsha not in local_commits
                prefix = "☁️ [雲端]" if is_cloud_only else "●"
                time_str = c.authored_datetime.strftime("%m/%d %H:%M")
                item = QListWidgetItem(f"{prefix} [{time_str}] {c.author.name}: {c.message.strip()}")
                base_color = self.get_color_by_name(c.author.name)
                if is_cloud_only:
                    base_color.setAlpha(120)
                    item.setForeground(QBrush(base_color))
                    item.setBackground(QColor(40, 40, 50))
                else:
                    item.setForeground(QBrush(base_color))
                self.log_list.addItem(item)
        except Exception:
            pass

        self.file_entries = self.service.parse_status_entries()
        self.file_list.blockSignals(True)
        self.file_list.clear()
        for entry in self.file_entries:
            item = QListWidgetItem(entry.display)
            item.setData(Qt.UserRole, entry)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.file_list.addItem(item)
        self.file_list.blockSignals(False)

        self.project_health = get_project_health(
            self.repo_dir,
            self.repo,
            self.project_type,
            self.project_name,
            self.file_entries,
            self.service.is_lfs_installed(),
            self.service.get_tracking_branch_name(),
        )
        self.idle_status_text = build_idle_status_text(self.project_health)
        self.restore_idle_status()
        self.check_push_readiness()
        self.refresh_tooltips()

        if getattr(self, 'id_toggle', None) and self.id_toggle.is_on:
            next_id = self.service.get_next_commit_id()
            self.id_input.setText(next_id)

    def update_open_button_text(self):
        if self.project_type == "unreal":
            self.open_btn.setText("🚀 開啟 Unreal 專案")
        elif self.project_type == "unity":
            self.open_btn.setText("📁 開啟 Unity 專案")
        else:
            self.open_btn.setText("📁 開啟專案資料夾")

    def refresh_tooltips(self):
        tooltip_lines = [
            f"專案類型：{self.project_name}",
            f"LFS：{'已安裝' if self.project_health.lfs_installed else '未安裝'}",
            f"追蹤分支：{self.project_health.tracking_branch or '無'}",
        ]
        if self.project_health.gitignore_missing:
            tooltip_lines.append("缺少 .gitignore 規則：")
            tooltip_lines.extend(self.project_health.gitignore_missing[:8])
        if self.project_health.gitattributes_missing:
            tooltip_lines.append("缺少 .gitattributes 規則：")
            tooltip_lines.extend(self.project_health.gitattributes_missing[:8])
        if self.project_health.bad_tracked_files:
            tooltip_lines.append("不建議追蹤的檔案：")
            tooltip_lines.extend(self.project_health.bad_tracked_files[:8])
        tip = "\n".join(tooltip_lines)
        self.status_label.setToolTip(tip)
        self.refresh_btn.setToolTip(tip)

    def eventFilter(self, source, event):
        if source == self.id_input and event.type() == QEvent.Type.Wheel:
            # 關閉時禁止使用滾輪魔法
            if not self.id_toggle.is_on:
                return True
            try:
                current_text = self.id_input.text()
                current_val = int(current_text) if current_text.isdigit() else 0
                if event.angleDelta().y() > 0:
                    current_val += 1
                else:
                    current_val = max(1, current_val - 1)
                self.id_input.setText(str(current_val).zfill(3))
            except Exception:
                pass
            return True 
        return super().eventFilter(source, event)

    def on_id_toggle_changed(self, is_on):
        """當你撥動開關時，執行變形動畫"""
        if not hasattr(self, "wrapper_anim"):
            return
        if is_on:
            self.id_input.setEnabled(True)
            self.id_input.show()
            self.id_wrapper.setStyleSheet("QFrame#id_wrapper { background-color: #3c3c3c; border: 1px solid #555555; border-radius: 3px; }")
            
            # 展開魔法：動畫推動「最大寬度」，讓它慢慢長大
            self.id_wrapper.setMinimumWidth(16) 
            self.wrapper_anim.setPropertyName(b"maximumWidth")
            self.wrapper_anim.setStartValue(16)
            self.wrapper_anim.setEndValue(62)
            self.wrapper_anim.start()
            
            self.id_input.setText(self.service.get_next_commit_id())
        else:
            self.id_input.setEnabled(False)
            self.id_input.clear()
            self.id_input.hide() # 輸入框瞬間消失，排版會想瞬間縮小
            
            self.id_wrapper.setStyleSheet("QFrame#id_wrapper { background-color: #2d2d30; border: 1px solid #333333; border-radius: 3px; }")
            
            # 關閉魔法：動畫改控制「最小寬度」，硬是把外框撐住，然後慢慢縮小！
            self.id_wrapper.setMaximumWidth(62) 
            self.wrapper_anim.setPropertyName(b"minimumWidth")
            self.wrapper_anim.setStartValue(62)
            self.wrapper_anim.setEndValue(16) 
            self.wrapper_anim.start()
            
        self.check_push_readiness()

    def auto_refresh_id(self):
        """每 120 秒自動校準通靈序號"""
        # 同時檢查開關狀態與輸入狀態，確保不會在你準備好要 Push 時亂改你的數字
        if getattr(self, 'id_toggle', None) is None or not self.id_toggle.is_on or self.push_btn.isEnabled():
            return
            
        next_id = self.service.get_next_commit_id()
        if self.id_input.text() != next_id:
            self.id_input.setText(next_id)


    def load_team_colors(self) -> dict:
        if os.path.exists(self.color_registry_file):
            try:
                with open(self.color_registry_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def get_color_by_name(self, name):
        team_colors = self.load_team_colors()
        if name in team_colors:
            return QColor(team_colors[name])
        return QColor(255, 255, 255)

    def pick_color(self):
        try:
            my_name = self.repo.git.config("user.name").strip()
        except Exception:
            QMessageBox.warning(self, "錯誤", "抓不到你的 Git 名字，請確認 Git 環境設定。")
            return

        team_colors = self.load_team_colors()
        current_hex = team_colors.get(my_name, "#FFFFFF")
        c = QColorDialog.getColor(QColor(current_hex))
        if c.isValid():
            team_colors[my_name] = c.name()
            try:
                with open(self.color_registry_file, "w", encoding="utf-8") as f:
                    json.dump(team_colors, f, indent=4, ensure_ascii=False)
                self.refresh_data()
                QMessageBox.information(self, "顏色更新成功", f"你的顏色已設為 {c.name()}。\n記得在同步專案時勾選「ZeroSync_Colors.json」，大家才看得到喔！")
            except Exception as e:
                QMessageBox.critical(self, "存檔失敗", f"無法寫入色票檔案：{str(e)}")

    def check_push_readiness(self):
        # 防呆檢查：編號跟訊息都不能是空的才能按上傳
        has_id = len(self.id_input.text().strip()) > 0 if self.id_toggle.is_on else True
        has_msg = len(self.msg_input.text().strip()) > 0
        has_checked_files = any(self.file_list.item(i).checkState() == Qt.Checked for i in range(self.file_list.count()))
        self.push_btn.setEnabled(has_id and has_msg and has_checked_files)

    def set_pull_highlight(self, available):
        self.pull_btn.setEnabled(available)
        self.pull_btn.setProperty("update_available", "true" if available else "false")
        self.pull_btn.style().unpolish(self.pull_btn)
        self.pull_btn.style().polish(self.pull_btn)
        self.pull_btn.update()

    def check_remote_updates(self):
        def _check():
            try:
                divergence = self.service.get_remote_divergence(fetch_first=True)
                has_update = bool(divergence.get("has_remote") and divergence.get("behind", 0) > 0)
                self.signals.remote_status.emit(has_update)
                if has_update:
                    self.signals.refresh_done.emit()
            except Exception:
                self.signals.remote_status.emit(False)

        threading.Thread(target=_check, daemon=True).start()

    def update_spinner(self):
        self.status_icon.setText(self.spin_chars[self.spin_index])
        self.spin_index = (self.spin_index + 1) % 4

    def restore_idle_status(self):
        self.status_icon.setText("⚠" if self.project_health.issue_count > 0 else "")
        self.status_label.setText(self.idle_status_text)

    def update_ui_progress(self, val, msg):
        if val < 0:
            self.pbar.setRange(0, 0)
        else:
            self.pbar.setRange(0, 100)
            self.pbar.setValue(max(0, min(100, val)))
        self.status_label.setText(msg)

    def get_selected_entries(self) -> List[FileEntry]:
        selected = []
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.checkState() == Qt.Checked:
                entry = item.data(Qt.UserRole)
                if entry:
                    selected.append(entry)
        return selected

    def maybe_show_preflight_dialog(self, result) -> bool:
        if result.errors:
            msg = QMessageBox(self)
            msg.setWindowTitle("同步前檢查失敗")
            msg.setIcon(QMessageBox.Warning)
            msg.setText("這次不能直接上傳。")
            details = []
            if result.errors:
                details.append("【錯誤】")
                details.extend(f"- {x}" for x in result.errors)
            if result.warnings:
                details.append("\n【警告】")
                details.extend(f"- {x}" for x in result.warnings)
            msg.setInformativeText("\n".join(details))
            msg.exec()
            return False

        if result.warnings:
            msg = QMessageBox(self)
            msg.setWindowTitle("同步前警告")
            msg.setIcon(QMessageBox.Warning)
            msg.setText("檢查完了，能上傳，但有幾件事你最好知道。")
            details = "\n".join(f"- {x}" for x in result.warnings[:12])
            if len(result.warnings) > 12:
                details += f"\n- 其餘 {len(result.warnings) - 12} 項略。"
            msg.setInformativeText(details)
            continue_btn = msg.addButton("照樣上傳", QMessageBox.AcceptRole)
            msg.addButton("取消", QMessageBox.RejectRole)
            msg.exec()
            return msg.clickedButton() == continue_btn
        return True

    def run_pull_thread(self):
        try:
            if self.repo.is_dirty(untracked_files=True):
                answer = QMessageBox.question(self, "提醒", "你本地還有未提交變更，現在 Pull 可能撞出衝突。還是要繼續？", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes:
                    return
        except Exception:
            pass
        self.toggle_btns(False)
        self.spin_timer.start(150)
        threading.Thread(target=self._pull_logic, daemon=True).start()

    def _pull_logic(self):
        try:
            self.signals.done.emit(self.service.pull(GitProgressHandler(self.signals.progress)))
        except Exception as e:
            self.signals.error.emit(str(e))

    def run_push_thread(self):
        raw_msg = self.msg_input.text().strip()
        
        # 幫你把字串組裝得漂亮一點，例如: "[130] 更新場景光影"
        if self.id_toggle.is_on:
            commit_id = self.id_input.text().strip()
            final_msg = f"[{commit_id}] {raw_msg}"
        else:
            final_msg = raw_msg
        
        selected_entries = self.get_selected_entries()
        
        # 這裡改把組合好的 final_msg 丟給大腦檢查
        preflight = self.service.preflight_check(selected_entries, final_msg, self.project_type)
        if not self.maybe_show_preflight_dialog(preflight):
            return
            
        self.toggle_btns(False)
        self.spin_timer.start(150)
        # 這裡也是丟 final_msg 過去
        threading.Thread(target=self._push_logic, args=(final_msg, selected_entries), daemon=True).start()

    def _push_logic(self, msg: str, entries: List[FileEntry]):
        try:
            result = self.service.push(msg, entries, GitProgressHandler(self.signals.progress))
            self.send_discord_timeline_report(self.get_current_git_user_name(), msg)
            self.signals.done.emit(result)
        except Exception as e:
            self.signals.error.emit(str(e))

    def on_task_done(self, txt):
        self.spin_timer.stop()
        self.status_icon.setText("")
        self.status_label.setText(txt)
        self.pbar.setRange(0, 100)
        self.pbar.setValue(100)
        self.msg_input.clear()
        self.toggle_btns(True)
        QTimer.singleShot(1500, self.reset_progress_bar)

    def reset_progress_bar(self):
        if not self.refresh_btn.isEnabled():
            return
        self.pbar.setRange(0, 100)
        self.pbar.setValue(0)
        self.restore_idle_status()

    def show_token_expired_dialog(self):
        """PAT 失效時，直接導向對應組織的 Azure DevOps Token 頁面。"""
        token_url = "https://aex.dev.azure.com/me"
        if hasattr(self, "service") and self.service is not None:
            try:
                token_url = self.service.get_dynamic_token_url()
            except Exception:
                pass

        msg = QMessageBox(self)
        msg.setWindowTitle("通行證失效警告")
        msg.setIcon(QMessageBox.Critical)
        msg.setText("你的微軟通行證 (PAT) 已失效，無法繼續同步。")
        msg.setInformativeText(
            "別慌，不一定是你操作錯。<br><br>"
            "這通常代表 Token 貼錯、已過期、已撤銷，或缺少必要權限。<br><br>"
            "請點下方連結前往 Azure DevOps PAT 設定頁面，重新建立一組具有 <b>User Profile</b> 權限的 Token，"
            "再貼回系統設定即可。<br><br>"
            f"<a href='{token_url}'>前往 Token 設定頁面</a>"
        )
        msg.setTextFormat(Qt.RichText)
        msg.setTextInteractionFlags(Qt.TextBrowserInteraction)
        msg.exec()

    def handle_git_error(self, err):
        """集中處理 Git 錯誤，並喚起 AI 診斷視窗"""
        raw_error = str(err)

        if hasattr(self, "service") and self.service is not None:
            translated = self.service.translate_error(raw_error)
            if self.service.is_auth_error(raw_error):
                self.show_token_expired_dialog()
                return
        else:
            translated = raw_error

        # --- 核心升級：啟動 AI 診斷視窗 ---
        from ai_service import AIService
        from config_manager import ConfigManager
        
        # 讀取加密過的設定檔以取得 AI Key
        config_mgr = ConfigManager(self.repo_dir)
        config = config_mgr.load()
        ai_svc = AIService(config)
        
        # 彈出你剛剛寫好的 GitErrorDialog
        # 這裡會傳入：原始報錯、初步翻譯、AI 服務實例以及專案類型 (如 Unreal/Unity)
        dialog = GitErrorDialog(self, raw_error, translated, ai_svc, self.project_name)
        dialog.exec()
        
    def on_error(self, err):
        self.spin_timer.stop()
        self.status_icon.setText("❌")
        self.pbar.setRange(0, 100)
        self.handle_git_error(err)
        self.toggle_btns(True)

    def toggle_btns(self, enabled: bool):
        self.refresh_btn.setEnabled(enabled)
        self.open_btn.setEnabled(enabled)
        self.settings_btn.setEnabled(enabled)
        if enabled:
            self.refresh_data()
            self.check_remote_updates()
        else:
            self.pull_btn.setEnabled(False)
            self.push_btn.setEnabled(False)
    

    def generate_ai_commit_msg(self):
        # 1. 先把你勾選的檔案打包好
        selected_files = self.get_selected_entries()
        if not selected_files:
            QMessageBox.warning(self, "欸", "你連檔案都沒勾，AI 要怎麼幫你通靈？")
            return

        self.ai_suggest_btn.setEnabled(False)
        self.msg_input.clear()
        self.msg_input.setPlaceholderText("AI 正在通靈中.")

        # 2. 啟動點點點動畫引擎
        if not hasattr(self, "ai_timer"):
            self.ai_timer = QTimer(self)
            self.ai_timer.timeout.connect(self._animate_ai_text)
        self.ai_dot_count = 1
        self.ai_timer.start(400)

        # 3. 超級防呆執行緒（強制把檔案清單和專案類型餵給 AI）
        import threading
        def _worker(files_to_check, proj_type):
            from ai_service import AIService
            from config_manager import ConfigManager
            try:
                config = ConfigManager(self.repo_dir).load()
                ai_svc = AIService(config)
                result = ai_svc.suggest_commit_message(files_to_check, proj_type)
                self.signals.ai_msg_done.emit(result)
            except Exception as e:
                self.signals.ai_msg_done.emit(f"錯誤：{str(e)}")

        # 4. 發射！透過 args 把變數精準投遞進去
        threading.Thread(
            target=_worker, 
            args=(selected_files, self.project_type), 
            daemon=True
        ).start()

    def _animate_ai_text(self):
        # 負責讓點點點循環播放的魔術
        self.ai_dot_count = (self.ai_dot_count % 4) + 1
        dots = "." * self.ai_dot_count
        self.msg_input.setPlaceholderText(f"AI 正在通靈中{dots}")

    def on_ai_msg_done(self, result):
        # 1. 收到結果後，立刻把點點點動畫引擎關掉
        if hasattr(self, "ai_timer"):
            self.ai_timer.stop()
            
        self.ai_suggest_btn.setEnabled(True)
        
        # 2. 核心修正：不管是成功還是失敗，背景提示字通通強制還原！免得它陰魂不散
        self.msg_input.setPlaceholderText("請輸入這次改了什麼...")
        
        # 3. 判斷要填入文字還是彈出報錯
        if result.startswith("錯誤："):
            QMessageBox.warning(self, "AI 罷工", result)
        else:
            self.msg_input.setText(result)