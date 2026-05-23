# ZeroSync
A custom Git workflow tool for game development teams to simplify LFS and sync issues
# ZeroSync: 遊戲開發協作與版控管線工具

ZeroSync 是一套專為遊戲開發團隊設計的自動化協作工具。它旨在解決美術與非技術背景成員在面對複雜 Git 指令時的恐懼，並有效處理大型專案常見的 Git LFS 衝突與權限管理問題。

## 核心解決方案
* **降低學習門檻**：無需死記艱澀的終端機指令，一鍵完成版本同步。
* **資安防護**：整合 Windows DPAPI 系統級加密，保護團隊的 API Key 與 Token。
* **開發環境自動化**：自動鑑定並部署 Git 環境，強制規範 LF 換行符號以防跨平台災難。
* **異步穩定性**：透過 PySide6 與 QThread 架構，確保大檔案傳輸期間 UI 不會卡死，提供即時進度反饋。
* **自動除錯管線**：內建幽靈檔案清理（Git 索引重建）與衝突排除機制。

## 如何使用
1. 確保已安裝 Python 3.14 與必要依賴套件。
2. 執行 `run_dev.bat` 啟動 ZeroSync 介面。
3. 按照介面提示進行初始化，系統將自動處理設定。

## 授權條款
本專案採用 MIT License。

# ZeroSync: Game Development Pipeline & Git Sync Tool

ZeroSync is an automated collaboration tool designed for game development teams. It aims to eliminate the fear of complex Git commands for artists and non-technical members while effectively managing common issues like Git LFS conflicts and permission errors.

## Core Features
* **Simplified Workflow**: Replaces complex terminal commands with a one-click synchronization experience.
* **Security First**: Integrates Windows DPAPI for system-level encryption to protect API Keys and Tokens.
* **Automated Environment**: Automatically identifies and deploys Git environments, enforcing LF line endings to prevent cross-platform catastrophes.
* **UI Stability**: Built with PySide6 and QThread architecture to ensure the UI remains responsive during heavy file transfers.
* **Auto-Repair Pipeline**: Built-in mechanisms for cleaning phantom files (Git index reset) and conflict resolution.

## How to Run
1. Ensure Python 3.14 is installed.
2. Install necessary dependencies (e.g., `pip install PySide6`).
3. Run `run_dev.bat` to launch the ZeroSync interface.
4. Follow the on-screen prompts to initialize your repository.

## Roadmap
- [ ] Implement cross-language support for the user interface.
- [ ] Optimize Git LFS handling for cross-platform environments.

## License
This project is licensed under the **MIT License**.
