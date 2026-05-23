import locale
import os
import shutil
import subprocess
import sys
import logging
from logging.handlers import RotatingFileHandler
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QRectF, QPointF
from PySide6.QtGui import QPainter, QLinearGradient, QColor, QPen, QBrush
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

# --- 語系包定義 ---
LANG_PACK = {
    "zh": {
        "title": "ZeroSync 初始化精靈(通用版)",
        "header": "<b>ZeroSync 初次啟動配置</b>",
        "label_name": "開發者姓名:",
        "label_email": "開發者 Email:",
        "label_url": "雲端 Repository URL:",
        "label_token": "存取權杖 (PAT):",
        "label_path": "工作資料夾:",
        "label_lang": "介面語言 (Language):",
        "btn_browse": "瀏覽...",
        "mode_clone": "從雲端複製專案 (Clone)",
        "mode_init": "綁定既有資料夾 (Init)",
        "status_init": "正在初始化地基...",
        "status_deploy": "正在部署核心組件至專案目錄...",
        "status_scan": "正在啟動掃描程序...",
        "status_cloning": "正在下載雲端資產...",
        "status_lfs": "正在同步 LFS 大型美術資產，請保持網路暢通...",
        "status_pushing": "正在索引並衝擊雲端防線 (Push)...",
        "msg_success": "部署成功",
        "msg_success_desc": "ZeroSync 已自動安裝至專案目錄並建立桌面捷徑！",
        "msg_fail": "對接失敗",
    },
    "en": {
        "title": "ZeroSync Initializer",
        "header": "<b>ZeroSync Initial Configuration</b>",
        "label_name": "Developer Name:",
        "label_email": "Developer Email:",
        "label_url": "Cloud Repository URL:",
        "label_token": "Access Token (PAT):",
        "label_path": "Workspace Path:",
        "label_lang": "Language:",
        "btn_browse": "Browse...",
        "mode_clone": "Clone from Cloud",
        "mode_init": "Initialize Existing Folder",
        "status_init": "Initializing workspace...",
        "status_deploy": "Deploying core components to project...",
        "status_scan": "Starting scan process...",
        "status_cloning": "Deploying repository assets...",
        "status_lfs": "Syncing LFS large assets, keep network online...",
        "status_pushing": "Indexing and pushing assets to cloud...",
        "msg_success": "Deployment Successful",
        "msg_success_desc": "ZeroSync installed to project directory and shortcut created!",
        "msg_fail": "Deployment Failed",
    },
}

L = LANG_PACK["en"]
SYS_LANG = "en"

def update_global_language(lang_code):
    global L, SYS_LANG
    SYS_LANG = lang_code
    L = LANG_PACK.get(lang_code, LANG_PACK["en"])

def setup_logger(base_dir: Path):
    """ZeroSync 專屬黑盒子：全域日誌系統 (自動收納版)"""
    log_dir = base_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "zerosync_debug.log"
    
    logger = logging.getLogger("ZeroSync")
    logger.setLevel(logging.DEBUG)
    
    if not logger.handlers:
        fh = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] - %(message)s', datefmt='%m-%d %H:%M:%S')
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)
        logger.addHandler(fh)
        logger.addHandler(ch)

    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.error("❌ 系統發生未預期的致命崩潰！", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = handle_exception
    return logger

class GlowProgressBar(QProgressBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextVisible(False)
        self.setFixedHeight(14)
        self.offset = -0.5
        self.glow_timer = QTimer(self)
        self.glow_timer.timeout.connect(self._update_glow)
        self.glow_timer.start(16)

    def _update_glow(self):
        self.offset += 0.02
        if self.offset > 1.5: self.offset = -0.5
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        bg_rect = QRectF(0, 0, self.width(), self.height())
        painter.setBrush(QColor(25, 25, 25))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(bg_rect, 7, 7)
        is_indet = (self.maximum() == 0)
        p_w = self.width() if is_indet else (self.value() / self.maximum() * self.width() if self.maximum() > 0 else 0)
        if p_w > 0:
            p_rect = QRectF(0, 0, p_w, self.height())
            painter.setBrush(QColor(0, 100, 180))
            painter.drawRoundedRect(p_rect, 7, 7)
            grad = QLinearGradient(QPointF(self.width() * self.offset, 0), QPointF(self.width() * self.offset + self.width() * 0.3, 0))
            grad.setColorAt(0, QColor(0, 255, 128, 0))
            grad.setColorAt(0.5, QColor(57, 255, 20, 220))
            grad.setColorAt(1, QColor(0, 255, 128, 0))
            painter.setBrush(grad)
            painter.drawRoundedRect(p_rect, 7, 7)
        painter.end()

class GitWorkerThread(QThread):
    progress_signal = Signal(int, str)
    finished_signal = Signal(bool, str)

    def __init__(self, target_path, config, mode, project_type, service, base_dir):
        super().__init__()
        self.target_path = Path(target_path)
        self.config = config
        self.mode = mode
        self.project_type = project_type
        self.service = service
        self.base_dir = Path(base_dir)

    def run(self):
        logger = logging.getLogger("ZeroSync")
        try:
            logger.info(f"啟動工作執行緒 - 模式: {self.mode}, 目標: {self.target_path}")
            self.progress_signal.emit(-1, "正在向雲端驗證身分...")
            is_ok, result = self.service.verify_microsoft_auth(self.config)
            if not is_ok: raise RuntimeError(f"身分認證失敗：{result}")
            
            if self.mode == "clone":
                self.service.bootstrap_clone_repo(self.config, progress_callback=self.progress_signal.emit)
                deploy_app_to_target(self.base_dir, self.target_path)
            else:
                self.progress_signal.emit(-1, L["status_deploy"])
                deploy_app_to_target(self.base_dir, self.target_path)
                self.service.bootstrap_init_repo(self.config, self.project_type, progress_callback=self.progress_signal.emit)
                all_entries = self.service.parse_status_entries()
                if all_entries:
                    self.service.push("Initial commit via ZeroSync", all_entries, None, progress_callback=self.progress_signal.emit)
            
            self.finished_signal.emit(True, "完成")
        except Exception as e:
            logger.error(f"工作執行緒發生錯誤: {str(e)}")
            self.finished_signal.emit(False, str(e))

class InitializationDialog(QDialog):
    def __init__(self, repo_dir: Path, saved, parent=None):
        super().__init__(parent)
        self.repo_dir = repo_dir
        self.saved = saved
        self.init_ui()
        self.apply_saved_data()

    def init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.header_label = QLabel()
        self.header_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #05B8CC; margin-bottom: 10px;")
        self.main_layout.addWidget(self.header_label)

        self.form = QFormLayout()
        self.name_input = QLineEdit()
        self.email_input = QLineEdit()
        self.url_input = QLineEdit()
        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.Password)
        self.path_input = QLineEdit()
        self.browse_btn = QPushButton()
        self.browse_btn.clicked.connect(self._browse_folder)
        
        self.path_box = QHBoxLayout()
        self.path_box.addWidget(self.path_input)
        self.path_box.addWidget(self.browse_btn)

        self.lang_combo = QComboBox()
        self.lang_combo.addItems(["System Default", "繁體中文", "English"])
        self.lang_combo.currentIndexChanged.connect(self.on_lang_changed)

        self.label_name_obj = QLabel()
        self.label_email_obj = QLabel()
        self.label_url_obj = QLabel()
        self.label_token_obj = QLabel()
        self.label_path_obj = QLabel()
        self.label_lang_obj = QLabel()

        self.form.addRow(self.label_name_obj, self.name_input)
        self.form.addRow(self.label_email_obj, self.email_input)
        self.form.addRow(self.label_url_obj, self.url_input)
        self.form.addRow(self.label_token_obj, self.token_input)
        self.form.addRow(self.label_path_obj, self.path_box)
        self.form.addRow(self.label_lang_obj, self.lang_combo)
        self.main_layout.addLayout(self.form)
        
        self.clone_radio = QRadioButton()
        self.attach_radio = QRadioButton()
        self.attach_radio.setChecked(True)
        self.main_layout.addWidget(self.clone_radio)
        self.main_layout.addWidget(self.attach_radio)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.main_layout.addWidget(self.buttons)

        self.retranslate_ui()
        self.setStyleSheet("QDialog { background-color: #1e1e1e; } QLabel { color: #ccc; } QLineEdit { background-color: #3c3c3c; color: white; border: 1px solid #555; } QPushButton { background-color: #444; color: white; } QRadioButton { color: #ccc; }")

    def retranslate_ui(self):
        self.setWindowTitle(L["title"])
        self.header_label.setText(L["header"])
        self.label_name_obj.setText(L["label_name"])
        self.label_email_obj.setText(L["label_email"])
        self.label_url_obj.setText(L["label_url"])
        self.label_token_obj.setText(L["label_token"])
        self.label_path_obj.setText(L["label_path"])
        self.label_lang_obj.setText(L["label_lang"])
        self.browse_btn.setText(L["btn_browse"])
        self.clone_radio.setText(L["mode_clone"])
        self.attach_radio.setText(L["mode_init"])

    def apply_saved_data(self):
        self.name_input.setText(self.saved.user_name)
        self.email_input.setText(self.saved.user_email)
        self.url_input.setText(self.saved.remote_url)
        self.token_input.setText(self.saved.access_token)
        self.path_input.setText(str(self.repo_dir))
        if hasattr(self.saved, "language"):
            idx = {"zh": 1, "en": 2}.get(self.saved.language, 0)
            self.lang_combo.blockSignals(True)
            self.lang_combo.setCurrentIndex(idx)
            self.lang_combo.blockSignals(False)

    def on_lang_changed(self, index):
        lang_map = {0: "auto", 1: "zh", 2: "en"}
        code = lang_map.get(index, "auto")
        if code == "auto":
            try:
                loc = locale.getlocale()[0]
                code = "zh" if loc and "zh" in loc.lower() else "en"
            except: code = "en"
        update_global_language(code)
        self.retranslate_ui()

    def _browse_folder(self):
        d = QFileDialog.getExistingDirectory(self, L["label_path"], self.path_input.text())
        if d: self.path_input.setText(d)

    def get_config(self):
        from models import ZeroSyncConfig
        lang_map = {0: "auto", 1: "zh", 2: "en"}
        return ZeroSyncConfig(
            user_name=self.name_input.text().strip(),
            user_email=self.email_input.text().strip(),
            remote_url=self.url_input.text().strip(),
            access_token=self.token_input.text().strip(),
            language=lang_map.get(self.lang_combo.currentIndex(), "auto"),
        )

    def selected_mode(self): return "clone" if self.clone_radio.isChecked() else "attach"

class ProgressDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(L["title"])
        self.setFixedSize(450, 150)
        self.setWindowFlags(Qt.Window | Qt.WindowTitleHint | Qt.CustomizeWindowHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("QDialog { background-color: #1e1e1e; }")
        layout = QVBoxLayout(self)
        self.label = QLabel(L["status_scan"])
        self.label.setStyleSheet("color: white; font-weight: bold;")
        layout.addWidget(self.label)
        self.pbar = GlowProgressBar(self)
        self.pbar.setRange(0, 0)
        layout.addWidget(self.pbar)

    def update_progress(self, val, text):
        self.label.setText(text)
        if val < 0: self.pbar.setRange(0, 0)
        else:
            self.pbar.setRange(0, 100)
            self.pbar.setValue(val)
        QApplication.processEvents()

def main(external_base: str = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    # 優先使用傳進來的正確路徑
    if external_base:
        base_dir = Path(external_base)
    else:
        # 如果是單獨跑這個檔案的備案
        if getattr(sys, "frozen", False):
            base_dir = Path(sys.executable).parent
        else:
            base_dir = Path(__file__).resolve().parent.parent

    # ✨ 啟動黑盒子
    logger = setup_logger(base_dir)
    logger.info("="*40)
    logger.info("🚀 ZeroSync 初始化啟動程序開始")
    logger.info("="*40)

    from config_manager import ConfigManager
    config_manager = ConfigManager(base_dir)
    config = config_manager.load()

    lang = getattr(config, "language", "auto")
    if lang == "auto":
        try:
            loc = locale.getlocale()[0]
            lang = "zh" if loc and "zh" in loc.lower() else "en"
        except: lang = "en"
    update_global_language(lang)
    logger.info(f"偵測語系: {lang}")

    import env_setup
    try: 
        env_setup.initialize(base_dir)
    except Exception as e:
        logger.error(f"環境初始化錯誤: {str(e)}")
        QMessageBox.critical(None, "環境初始化錯誤", str(e))
        return 1

    from git_service import GitService
    from main_window import ZeroSyncPro
    from project_rules import detect_project_type

    while not (base_dir / ".git").exists():
        dialog = InitializationDialog(base_dir, config)
        if dialog.exec() != QDialog.Accepted: return 0
        try:
            config = dialog.get_config()
            target_dir = Path(dialog.path_input.text().strip())
            target_dir.mkdir(parents=True, exist_ok=True)
            service = GitService(str(target_dir), require_repo=False)
            proj_type, _ = detect_project_type(target_dir)
            
            prog = ProgressDialog()
            thread = GitWorkerThread(target_dir, config, dialog.selected_mode(), proj_type, service, base_dir)
            thread.progress_signal.connect(prog.update_progress)
            res = {"ok": False, "err": ""}
            thread.finished_signal.connect(lambda s, m: [res.update({"ok": s, "err": m}), prog.accept()])
            thread.start()
            prog.exec()
            
            if not res["ok"]: raise Exception(res["err"])
            base_dir = target_dir
            ConfigManager(base_dir).save(config)
            logger.info(f"專案部署成功: {base_dir}")
            break
        except Exception as e: 
            logger.error(f"部署過程發生錯誤: {str(e)}")
            QMessageBox.critical(None, L["msg_fail"], str(e))

    window = ZeroSyncPro(str(base_dir))
    window.show()
    app.setQuitOnLastWindowClosed(True)
    return app.exec()

def deploy_app_to_target(curr_base, target_base):
    if not getattr(sys, "frozen", False): return
    curr_exe = Path(sys.executable)
    target_exe = target_base / curr_exe.name
    if curr_exe.resolve() != target_exe.resolve():
        try:
            shutil.copy2(curr_exe, target_exe)
            if (curr_base / "bin").exists():
                if (target_base / "bin").exists(): shutil.rmtree(target_base / "bin")
                shutil.copytree(curr_base / "bin", target_base / "bin")
            ps = (
                f"$s=(New-Object -Com WScript.Shell).CreateShortcut('{Path(os.environ['USERPROFILE'])}/Desktop/ZeroSync.lnk');"
                f"$s.TargetPath='{target_exe}';$s.WorkingDirectory='{target_base}';$s.Save()"
            )
            subprocess.run(["powershell", "-Command", ps], capture_output=True, creationflags=0x08000000)
        except: pass

if __name__ == "__main__":
    # 1. 決定基礎路徑 (核心改動就在這！)
    if getattr(sys, "frozen", False):
        # 如果以後你有打包成 EXE，路徑就在 EXE 旁邊
        current_base = Path(sys.executable).parent
    else:
        #  動態更新模式：因為 main.py 住在 ZeroSync_App 裡
        # 我們用 .parent.parent 退回到專案根目錄，去那裡讀取組員的 .zerosync
        current_base = Path(__file__).resolve().parent.parent

    # 2. 啟動程式並把正確的路徑傳進去
    # 注意：確保你的 main() 函數有接收這個路徑參數喔！
    sys.exit(main(str(current_base)))