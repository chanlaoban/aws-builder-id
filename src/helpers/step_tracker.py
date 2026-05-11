"""
注册步骤状态跟踪器
将每一步的执行状态写入 JSON 文件，供 Web UI 实时读取
"""
import json
import os
import time
from pathlib import Path

STATUS_FILE = Path(__file__).parent.parent.parent / "step_status.json"

# 预定义的所有步骤
STEPS = [
    {"id": "proxy_check",    "label": "代理检测",     "icon": "🔌"},
    {"id": "create_email",   "label": "创建邮箱",     "icon": "📧"},
    {"id": "launch_browser", "label": "启动浏览器",   "icon": "🌐"},
    {"id": "open_page",      "label": "打开注册页",   "icon": "📄"},
    {"id": "cookie",         "label": "关闭Cookie",   "icon": "🍪"},
    {"id": "click_signup",   "label": "点击注册",     "icon": "👆"},
    {"id": "fill_email",     "label": "填写邮箱",     "icon": "✉️"},
    {"id": "fill_name",      "label": "填写姓名",     "icon": "👤"},
    {"id": "wait_code",      "label": "等待验证码",   "icon": "⏳"},
    {"id": "fill_code",      "label": "填写验证码",   "icon": "🔢"},
    {"id": "set_password",   "label": "设置密码",     "icon": "🔑"},
    {"id": "save_account",   "label": "保存账号",     "icon": "💾"},
]


class StepTracker:
    def __init__(self):
        self._reset()

    def _reset(self):
        self.data = {
            "started_at": None,
            "current_step": None,
            "steps": {s["id"]: {"status": "pending", "message": ""} for s in STEPS},
            "error": None,
            "finished": False,
        }

    def start(self):
        """标记开始运行"""
        self._reset()
        self.data["started_at"] = time.time()
        self._save()

    def report(self, step_id, status="done", message=""):
        """
        报告某一步的状态
        status: pending / running / done / error
        """
        if step_id not in self.data["steps"]:
            return
        
        self.data["steps"][step_id] = {"status": status, "message": message}
        self.data["current_step"] = step_id if status == "running" else None
        
        # 如果是错误状态，记录全局错误
        if status == "error":
            self.data["error"] = message or f"步骤 {step_id} 失败"
        
        self._save()

    def error(self, message):
        """报告全局错误"""
        self.data["error"] = message
        # 将当前正在执行的步骤标记为错误
        if self.data["current_step"] and self.data["current_step"] in self.data["steps"]:
            s = self.data["steps"][self.data["current_step"]]
            if s["status"] == "running":
                s["status"] = "error"
                s["message"] = message
        self._save()

    def finish(self):
        """标记完成"""
        self.data["finished"] = True
        self._save()

    def _save(self):
        try:
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get_status(self):
        """获取当前状态（供 web API 调用）"""
        if not STATUS_FILE.exists():
            return {"started_at": None, "steps": {}, "current_step": None, "error": None, "finished": False}
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"started_at": None, "steps": {}, "current_step": None, "error": None, "finished": False}


# 全局单例
step_tracker = StepTracker()
