import base64
import ctypes
import json
from ctypes import wintypes
from pathlib import Path

from models import ZeroSyncConfig


class ConfigManager:
    CRYPTO_VERSION = 3
    DPAPI_PREFIX = "dpapi:v1:"

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.config_dir = self.base_dir / ".zerosync"
        self.config_path = self.config_dir / "ZeroSync_Config.json"
        self.last_decrypt_failed = False

    def _protect_bytes(self, data: bytes) -> bytes:
        if not data:
            return b""

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte)),
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        in_buffer = ctypes.create_string_buffer(data)
        in_blob = DATA_BLOB(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
        out_blob = DATA_BLOB()

        if not crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        ):
            raise RuntimeError("DPAPI 加密失敗")

        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)

    def _unprotect_bytes(self, data: bytes) -> bytes:
        if not data:
            return b""

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte)),
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        in_buffer = ctypes.create_string_buffer(data)
        in_blob = DATA_BLOB(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
        out_blob = DATA_BLOB()

        if not crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        ):
            raise RuntimeError("DPAPI 解密失敗")

        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)

    def _encode_token(self, token: str) -> str:
        token = token or ""
        if not token:
            return ""

        encrypted = self._protect_bytes(token.encode("utf-8"))
        return self.DPAPI_PREFIX + base64.b64encode(encrypted).decode("ascii")

    def _decode_token(self, stored: str) -> str:
        stored = stored or ""
        if not stored:
            return ""

        try:
            if not stored.startswith(self.DPAPI_PREFIX):
                self.last_decrypt_failed = True
                return ""

            raw = base64.b64decode(stored[len(self.DPAPI_PREFIX):].encode("ascii"))
            return self._unprotect_bytes(raw).decode("utf-8")
        except Exception:
            self.last_decrypt_failed = True
            return ""

    def load(self) -> ZeroSyncConfig:
        self.last_decrypt_failed = False

        if not self.config_path.exists():
            return ZeroSyncConfig()

        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            return ZeroSyncConfig(
                user_name=raw.get("user_name", ""),
                user_email=raw.get("user_email", ""),
                remote_url=raw.get("remote_url", ""),
                access_token=self._decode_token(raw.get("access_token", "")),
                ai_api_key=self._decode_token(raw.get("ai_api_key", "")),
                language=raw.get("language", "auto"),
            )
        except Exception:
            return ZeroSyncConfig()

    def save(self, config: ZeroSyncConfig) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)

        payload = {
            "crypto_version": self.CRYPTO_VERSION,
            "user_name": config.user_name,
            "user_email": config.user_email,
            "remote_url": config.remote_url,
            "access_token": self._encode_token(config.access_token),
            "ai_api_key": self._encode_token(config.ai_api_key),
            "language": getattr(config, "language", "auto"),
        }

        self.config_path.write_text(
            json.dumps(payload, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )