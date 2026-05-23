from models import ZeroSyncConfig

class AIService:
    def __init__(self, config: ZeroSyncConfig):
        self.api_key = config.ai_api_key
        self.is_ready = bool(self.api_key)
        self.provider = "none"
        self.model_name = "unknown"
        
        if self.is_ready:
            if self.api_key.startswith("sk-"):
                try:
                    from openai import OpenAI
                    self.provider = "openai"
                    self.client = OpenAI(api_key=self.api_key)
                    self.model_name = self._get_smart_openai_model(self.client)
                except Exception:
                    self.is_ready = False
                    self.provider = "error_openai"
            else:
                try:
                    # 換成新世代的 SDK
                    from google import genai
                    self.provider = "gemini"
                    # 新版只需要直接建立 Client
                    self.client = genai.Client(api_key=self.api_key)
                    self.model_name = self._get_smart_gemini_model(self.client)
                except Exception:
                    self.is_ready = False
                    self.provider = "error_gemini"

    def _get_smart_openai_model(self, client) -> str:
        """最高容錯選腦：排除所有非純文字的實驗品"""
        try:
            available = [m.id for m in client.models.list().data]
            priority_list = ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"]
            for target in priority_list:
                if target in available:
                    return target
            
            valid_fallbacks = []
            for m in available:
                name = m.lower()
                # 排除音訊、影像、預覽版
                if 'gpt' in name and not any(x in name for x in ['preview', 'realtime', 'audio', 'vision', 'instruct']):
                    valid_fallbacks.append(m)
            return valid_fallbacks[0] if valid_fallbacks else "gpt-4o-mini"
        except Exception:
            return "gpt-4o-mini"

    def _get_smart_gemini_model(self, client) -> str:
        """最高容錯選腦：精準狙擊並排除所有『耳背』與『眼花』的模型 (新版 SDK 適配)"""
        try:
            all_models = client.models.list()
            valid_models = []
            
            for m in all_models:
                name = m.name.lower()
                # 新版的 API 回傳結構稍微不同，直接用名字做最嚴格的安全過濾
                if 'flash' in name:
                    black_list = [
                        'tts', 'search', 'preview', 'vision', 'experimental', 
                        'audio', 'native', 'embedding', 'med', 'thinking'
                    ]
                    if any(x in name for x in black_list):
                        continue
                    valid_models.append(m.name)
            
            return valid_models[-1] if valid_models else "gemini-1.5-flash"
        except Exception:
            return "gemini-1.5-flash"

    def _sanitize_short_commit_message(self, text: str) -> str:
        import re

        text = (text or "").strip()
        if not text:
            return ""

        text = text.splitlines()[0].strip()
        text = re.sub(r"^[-*•\s]+", "", text)
        text = re.sub(r"^\[\d+\]\s*", "", text)
        text = re.sub(r"^(提交日誌|日誌|Commit message|commit message|Message|message)[:：]\s*", "", text)
        text = text.strip("『』「」\"'`“” ")
        text = re.sub(r"\s+", " ", text).strip()
        return text[:30].rstrip()

    def suggest_commit_message(self, entries, project_type: str) -> str:
        """依照待提交檔案產生 25 字以內的提交日誌。"""
        if self.provider.startswith("error_"):
            missing = "openai" if "openai" in self.provider else "google-genai"
            raise RuntimeError(f"AI 套件尚未安裝，請先執行：pip install {missing}")

        if not self.is_ready:
            raise RuntimeError("尚未設定 AI API Key，無法產生日誌。")

        file_lines = []
        for entry in list(entries)[:40]:
            status = getattr(entry, "status_code", "") or ""
            path = getattr(entry, "path", "") or getattr(entry, "display", "") or ""
            display = getattr(entry, "display", "") or ""
            if display and display != path:
                file_lines.append(f"{status} {path} ({display})")
            else:
                file_lines.append(f"{status} {path}")

        prompt_content = f"""
        你是 ZeroSync 的 Git 提交日誌助手。
        請根據以下修改檔案，產生一則繁體中文 Git 提交日誌。

        專案類型：{project_type}
        修改檔案：
        {chr(10).join(file_lines)}

        嚴格規則：
        - 只輸出提交日誌本身，不要解釋。
        - 25 個中文字以內。
        - 不要包含提交編號，例如 [132]。
        - 不要使用引號、句號、Markdown、emoji。
        - 要具體描述這次修改，不要只寫「更新」、「修改」、「調整」。
        """

        try:
            if self.provider == "openai":
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[{"role": "user", "content": prompt_content}],
                    temperature=0.2,
                    max_tokens=40,
                )
                raw = response.choices[0].message.content
            else:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt_content,
                )
                raw = response.text

            cleaned = self._sanitize_short_commit_message(raw)
            if not cleaned:
                raise RuntimeError("AI 沒有產生可用的日誌。")
            return cleaned
        except Exception as e:
            raise RuntimeError(f"產生 AI 日誌時發生異常：{str(e)}")

    def diagnose_error(self, error_msg: str, project_type: str) -> str:
        if self.provider.startswith("error_"):
            missing = "openai" if "openai" in self.provider else "google-genai"
            return f"系統提示：偵測到您想使用 AI 功能，但您的電腦環境尚未安裝「{missing}」套件。\n請在終端機輸入：pip install {missing}"
        
        if not self.is_ready:
            return "系統提示：尚未設定 API Key，無法進行診斷。"

        prompt_content = f"""
        你現在是 ZeroSync 的專業 Git 版本控制助手。
        請用繁體中文、親切、專業且具備耐心的語氣回答。沒有任何特殊角色設定。
        
        開發環境：{project_type}
        Git 報錯訊息：
        {error_msg}

        目前 ZeroSync 的「系統修復」面板提供以下按鈕及對應功能：
        1. 「⚠️ 放棄本地修改 (強制從雲端覆蓋)」: 執行 fetch & reset --hard origin & clean -fd
        2. 「清理垃圾殘留 (刪除未追蹤檔案)」: 執行 clean -fd (保留 ZeroSync 相關檔案)
        3. 「修復中文路徑亂碼 (編碼校正)」: 執行 git config core.quotepath false 等編碼修正
        4. 「修復獲取失敗 (清理遠端快取與網路)」: 執行 remote prune origin & fetch --all
        5. 「修復上傳失敗 (重建分支追蹤)」: 執行 branch --set-upstream-to
        6. 「驅除幽靈檔案 (重置 Git 索引)」: 執行 rm -r --cached . & add . & 產生救援 commit

        請給出以下格式的診斷報告：
        1. 【問題解析】：簡單明瞭地解釋發生了什麼問題（請避免過於艱澀的技術術語，讓美術人員也能聽懂）。
        2. 【一鍵修復建議】：判斷上述 6 個按鈕中，哪一個最適合解決此問題，並明確請使用者去點擊該按鈕。
        3. 【終極手動方案】：如果現有按鈕都無法解決，請提供需要在 Git Bash 中輸入的手動修復指令（使用 markdown code block），並提醒使用者：「您可以直接點擊視窗下方的『💻 開啟 Git Bash』按鈕來輸入指令。」
        """
        
        try:
            if self.provider == "openai":
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[{"role": "user", "content": prompt_content}]
                )
                return response.choices[0].message.content
            else:
                # ✨ 這裡換成了新版 SDK 的生成呼叫方式
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt_content
                )
                return response.text
        except Exception as e:
            return f"連線至 AI 診斷服務時發生異常，請檢查網路連線或 API Key 是否正確。\n\n目前嘗試的模型：{self.model_name}\n錯誤詳情：{str(e)}"