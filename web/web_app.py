"""AWS Builder ID - Web 管理界面
FastAPI + Jinja2 构建，支持外网远程操作
"""
import sys
import os
import json
import subprocess
import threading
import yaml
import socket
import urllib.request
import re
from pathlib import Path
from datetime import datetime

# 导入步骤跟踪器
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.helpers.step_tracker import step_tracker, STEPS

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

app = FastAPI(title="AWS Builder ID Manager")

BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))

# ========== 状态管理 ==========
running_status = {"running": False, "pid": None, "last_log": "", "started_at": None}

ACCOUNTS_FILE = BASE_DIR / "accounts.jsonl"
CONFIG_FILE = BASE_DIR / "config" / "config.yaml"


def load_accounts():
    """从 accounts.jsonl 加载已注册账号"""
    accounts = []
    if ACCOUNTS_FILE.exists():
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        accounts.append(json.loads(line))
                    except:
                        pass
    return accounts


def load_config_yaml():
    """加载配置文件原文"""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def parse_config_yaml(yaml_text):
    """简易 YAML 解析（只解析顶层和二级 key）"""
    import yaml
    try:
        return yaml.safe_load(yaml_text) or {}
    except:
        return {}


# ========== 配置字段定义 ==========
# 每个字段: {key, label, type, required, section, description, placeholder, options, depends_on}
CONFIG_FIELDS = [
    # ---- 邮箱服务 ----
    {"key": "email.worker_url", "section": "email", "label": "Cloudflare Worker 地址",
     "type": "url", "required": True,
     "description": "部署的临时邮箱 Worker 服务完整 URL，例如 https://temp-mail.xxx.workers.dev",
     "placeholder": "https://your-worker.workers.dev"},
    {"key": "email.domain", "section": "email", "label": "邮箱域名",
     "type": "text", "required": True,
     "description": "在 Cloudflare Email Routing 中配置的收信域名，用于生成 xxx@你的域名 的临时邮箱",
     "placeholder": "your-domain.com"},
    {"key": "email.admin_password", "section": "email", "label": "管理员密码",
     "type": "password", "required": False,
     "description": "Worker 的管理员 API 密码，用于清理/管理临时邮箱（非必填）",
     "placeholder": "选填"},
    {"key": "email.prefix_length", "section": "email", "label": "邮箱前缀长度",
     "type": "number", "required": False, "default": 10,
     "description": "生成的随机邮箱前缀字符数，越长越不容易撞车",
     "placeholder": "10"},
    {"key": "email.wait_timeout", "section": "email", "label": "邮件等待超时(秒)",
     "type": "number", "required": False, "default": 120,
     "description": "等待 AWS 验证邮件的最长时间，超过则放弃",
     "placeholder": "120"},
    {"key": "email.poll_interval", "section": "email", "label": "邮件轮询间隔(秒)",
     "type": "number", "required": False, "default": 3,
     "description": "每隔多少秒检查一次收件箱",
     "placeholder": "3"},

    # ---- 地区/代理 ----
    {"key": "region.current", "section": "region", "label": "目标地区",
     "type": "select", "required": True,
     "options": [{"value": "usa", "label": "🇺🇸 美国 (USA)"},
                 {"value": "germany", "label": "🇩🇪 德国 (Germany)"},
                 {"value": "japan", "label": "🇯🇵 日本 (Japan)"}],
     "description": "注册 AWS Builder ID 的目标地区，不同地区浏览器语言/时区/UA 不同"},
    {"key": "region.device_type", "section": "region", "label": "设备模拟类型",
     "type": "select", "required": False, "default": "desktop",
     "options": [{"value": "desktop", "label": "💻 Desktop (桌面浏览器)"},
                 {"value": "mobile", "label": "📱 Mobile (手机浏览器)"}],
     "description": "模拟桌面端还是移动端浏览器注册"},

    {"key": "region.use_proxy", "section": "proxy", "label": "启用代理",
     "type": "checkbox", "required": False, "default": False,
     "description": "是否通过代理注册。不开启则使用本机 IP。建议开启以获得纯净 IP"},
    {"key": "region.proxy_mode", "section": "proxy", "label": "代理模式",
     "type": "select", "required": False, "default": "dynamic",
     "depends_on": {"field": "region.use_proxy", "value": True},
     "options": [{"value": "static", "label": "🔒 静态代理 (固定 IP:PORT)"},
                 {"value": "dynamic", "label": "🔄 动态代理 (API 每次返回新 IP)"}],
     "description": "静态代理=用一个固定代理IP；动态代理=每次从API获取新IP"},
    {"key": "region.proxy_url", "section": "proxy", "label": "静态代理地址",
     "type": "text", "required": False, "default": "",
     "depends_on": {"field": "region.proxy_mode", "value": "static"},
     "description": "静态代理完整 URL，格式: http://ip:port 或 socks5://ip:port，需要认证则 http://user:pass@ip:port",
     "placeholder": "http://1.2.3.4:8080"},
    {"key": "region.proxy_api.url", "section": "proxy", "label": "动态代理 API 地址",
     "type": "url", "required": False, "default": "",
     "depends_on": {"field": "region.proxy_mode", "value": "dynamic"},
     "description": "动态代理提取 API，每次 GET 请求返回 ip:port 格式。例如 http://api.dailiyi.com/get?key=xxx",
     "placeholder": "http://your-proxy-api.com/get?key=***"},
    {"key": "region.proxy_api.protocol", "section": "proxy", "label": "代理协议",
     "type": "select", "required": False, "default": "http",
     "depends_on": {"field": "region.proxy_mode", "value": "dynamic"},
     "options": [{"value": "http", "label": "HTTP"},
                 {"value": "socks5", "label": "SOCKS5"}],
     "description": "动态代理使用的协议"},
    {"key": "region.proxy_api.auth_required", "section": "proxy", "label": "代理需要认证",
     "type": "checkbox", "required": False, "default": False,
     "depends_on": {"field": "region.proxy_mode", "value": "dynamic"},
     "description": "代理 API 返回的代理是否需要用户名密码"},
    {"key": "region.proxy_api.username", "section": "proxy", "label": "代理用户名",
     "type": "text", "required": False, "default": "",
     "depends_on": {"field": "region.proxy_api.auth_required", "value": True},
     "description": "代理认证用户名"},
    {"key": "region.proxy_api.password", "section": "proxy", "label": "代理密码",
     "type": "password", "required": False, "default": "",
     "depends_on": {"field": "region.proxy_api.auth_required", "value": True},
     "description": "代理认证密码"},
    {"key": "region.proxy_api.timeout", "section": "proxy", "label": "代理API超时(秒)",
     "type": "number", "required": False, "default": 10,
     "depends_on": {"field": "region.proxy_mode", "value": "dynamic"},
     "description": "请求代理API的超时时间"},

    # ---- 浏览器 ----
    {"key": "browser.headless", "section": "browser", "label": "无头模式",
     "type": "checkbox", "required": False, "default": True,
     "description": "无头模式=不显示浏览器界面（服务器推荐）。关闭则显示浏览器窗口（用于调试）"},
    {"key": "browser.slow_mo", "section": "browser", "label": "操作延迟(毫秒)",
     "type": "number", "required": False, "default": 100,
     "description": "每次浏览器操作的额外延迟，模拟人类操作速度。100=中等，0=最快但易被检测"},

    # ---- HTTP ----
    {"key": "http.timeout", "section": "http", "label": "HTTP 请求超时(秒)",
     "type": "number", "required": False, "default": 30,
     "description": "所有HTTP请求的最大等待时间"},
]

SECTION_META = {
    "email": {"title": "📧 邮箱服务配置", "icon": "📧",
              "desc": "连接你的 Cloudflare Temp Email Worker，用于接收 AWS 验证邮件"},
    "region": {"title": "🌍 地区设置", "icon": "🌍",
               "desc": "选择注册目标地区，注册机会自动配置语言/时区/UA 伪装"},
    "proxy": {"title": "🔌 代理配置", "icon": "🔌",
              "desc": "配置代理以确保 IP 归属地与所选地区一致，建议使用住宅代理"},
    "browser": {"title": "🌐 浏览器设置", "icon": "🌐",
                "desc": "浏览器指纹伪装参数"},
    "http": {"title": "⚡ 网络设置", "icon": "⚡",
             "desc": "HTTP 请求超时等网络参数"},
}

DEFAULT_CONFIG = """# AWS Builder ID 自动注册 - 配置文件
email:
  worker_url: "https://your-worker.workers.dev"
  domain: "your-domain.com"
  prefix_length: 10
  wait_timeout: 120
  poll_interval: 3
  admin_password: ""
browser:
  headless: true
  slow_mo: 100
region:
  current: "usa"
  device_type: "desktop"
  use_proxy: false
  proxy_mode: "dynamic"
  proxy_url: ""
  proxy_api:
    url: ""
    timeout: 10
    protocol: "http"
    auth_required: false
    username: ""
    password: ""
  profiles:
    germany:
      locale: "de-DE"
      timezone: "Europe/Berlin"
      accept_language: "de-DE,de;q=0.9,en;q=0.8"
      desktop_user_agents:
        - "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        - "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
      mobile_user_agents:
        - "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
        - "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    japan:
      locale: "ja-JP"
      timezone: "Asia/Tokyo"
      accept_language: "ja-JP,ja;q=0.9,en;q=0.8"
      desktop_user_agents:
        - "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        - "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
      mobile_user_agents:
        - "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
        - "Mozilla/5.0 (Linux; Android 14; SO-51D) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    usa:
      locale: "en-US"
      timezone: "America/New_York"
      accept_language: "en-US,en;q=0.9"
      desktop_user_agents:
        - "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        - "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
      mobile_user_agents:
        - "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
        - "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
http:
  timeout: 30
"""


# ========== 从结构化字段构建 YAML ==========
def fields_to_yaml(fields_dict):
    """将前端提交的扁平字段转成 YAML 配置"""
    import yaml
    
    # 从默认配置加载完整结构
    config = yaml.safe_load(DEFAULT_CONFIG)
    
    # 映射扁平 key 到嵌套结构
    field_map = {
        "email.worker_url": lambda v: config.setdefault("email", {})["worker_url"],
        "email.domain": lambda v: config.setdefault("email", {})["domain"],
        "email.admin_password": lambda v: config.setdefault("email", {})["admin_password"],
        "email.prefix_length": lambda v: config.setdefault("email", {})["prefix_length"],
        "email.wait_timeout": lambda v: config.setdefault("email", {})["wait_timeout"],
        "email.poll_interval": lambda v: config.setdefault("email", {})["poll_interval"],
        "region.current": lambda v: config.setdefault("region", {})["current"],
        "region.device_type": lambda v: config.setdefault("region", {})["device_type"],
        "region.use_proxy": lambda v: config.setdefault("region", {})["use_proxy"],
        "region.proxy_mode": lambda v: config.setdefault("region", {})["proxy_mode"],
        "region.proxy_url": lambda v: config.setdefault("region", {})["proxy_url"],
        "region.proxy_api.url": lambda v: config.setdefault("region", {}).setdefault("proxy_api", {})["url"],
        "region.proxy_api.protocol": lambda v: config.setdefault("region", {}).setdefault("proxy_api", {})["protocol"],
        "region.proxy_api.auth_required": lambda v: config.setdefault("region", {}).setdefault("proxy_api", {})["auth_required"],
        "region.proxy_api.username": lambda v: config.setdefault("region", {}).setdefault("proxy_api", {})["username"],
        "region.proxy_api.password": lambda v: config.setdefault("region", {}).setdefault("proxy_api", {})["password"],
        "region.proxy_api.timeout": lambda v: config.setdefault("region", {}).setdefault("proxy_api", {})["timeout"],
        "browser.headless": lambda v: config.setdefault("browser", {})["headless"],
        "browser.slow_mo": lambda v: config.setdefault("browser", {})["slow_mo"],
        "http.timeout": lambda v: config.setdefault("http", {})["timeout"],
    }
    
    # 先重置所有字段到默认值
    for key, setter in field_map.items():
        default_val = None
        for field_def in CONFIG_FIELDS:
            if field_def["key"] == key:
                default_val = field_def.get("default")
                break
        setter(default_val)
    
    # 然后应用用户提交的值
    for key, value in fields_dict.items():
        if key in field_map:
            # 类型转换
            if isinstance(value, str):
                # 布尔型
                if value.lower() in ("true", "false"):
                    value = value.lower() == "true"
                # 数字型
                else:
                    try:
                        if "." in value:
                            value = float(value)
                        else:
                            value = int(value)
                    except (ValueError, TypeError):
                        pass
            field_map[key](value)
    
    return yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False)


def config_to_fields(yaml_text):
    """将 YAML 配置解析为扁平字段字典"""
    config = parse_config_yaml(yaml_text)
    fields = {}
    
    def get_nested(cfg, *keys):
        for k in keys:
            if isinstance(cfg, dict):
                cfg = cfg.get(k)
            else:
                return None
        return cfg
    
    # 提取所有字段
    fields["email.worker_url"] = get_nested(config, "email", "worker_url") or ""
    fields["email.domain"] = get_nested(config, "email", "domain") or ""
    fields["email.admin_password"] = get_nested(config, "email", "admin_password") or ""
    fields["email.prefix_length"] = get_nested(config, "email", "prefix_length") or 10
    fields["email.wait_timeout"] = get_nested(config, "email", "wait_timeout") or 120
    fields["email.poll_interval"] = get_nested(config, "email", "poll_interval") or 3
    fields["region.current"] = get_nested(config, "region", "current") or "usa"
    fields["region.device_type"] = get_nested(config, "region", "device_type") or "desktop"
    fields["region.use_proxy"] = get_nested(config, "region", "use_proxy") or False
    fields["region.proxy_mode"] = get_nested(config, "region", "proxy_mode") or "dynamic"
    fields["region.proxy_url"] = get_nested(config, "region", "proxy_url") or ""
    fields["region.proxy_api.url"] = get_nested(config, "region", "proxy_api", "url") or ""
    fields["region.proxy_api.protocol"] = get_nested(config, "region", "proxy_api", "protocol") or "http"
    fields["region.proxy_api.auth_required"] = get_nested(config, "region", "proxy_api", "auth_required") or False
    fields["region.proxy_api.username"] = get_nested(config, "region", "proxy_api", "username") or ""
    fields["region.proxy_api.password"] = get_nested(config, "region", "proxy_api", "password") or ""
    fields["region.proxy_api.timeout"] = get_nested(config, "region", "proxy_api", "timeout") or 10
    fields["browser.headless"] = get_nested(config, "browser", "headless")
    fields["browser.slow_mo"] = get_nested(config, "browser", "slow_mo") or 100
    fields["http.timeout"] = get_nested(config, "http", "timeout") or 30
    
    return fields


# ========== 环境检查 ==========

def check_chrome():
    """检查 Chrome 浏览器"""
    chrome_paths = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/home/chanlaoban/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome",
        "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    ]
    for path in chrome_paths:
        p = Path(path)
        if p.exists():
            try:
                result = subprocess.run([str(p), "--version"], capture_output=True, text=True, timeout=10)
                version = result.stdout.strip() or result.stderr.strip()
                return {"found": True, "path": str(p), "version": version}
            except Exception:
                return {"found": True, "path": str(p), "version": "unknown"}
    return {"found": False, "path": "", "version": ""}


def check_chromedriver():
    """检查 ChromeDriver"""
    driver_paths = [
        "/usr/local/bin/chromedriver",
        "/usr/bin/chromedriver",
        "/home/chanlaoban/.local/bin/chromedriver",
        "/mnt/c/Users/Administrator/Desktop/auto-config-tool/chromedriver.exe",
    ]
    for path in driver_paths:
        p = Path(path)
        if p.exists():
            try:
                result = subprocess.run([str(p), "--version"], capture_output=True, text=True, timeout=10)
                version = result.stdout.strip() or result.stderr.strip()
                return {"found": True, "path": str(p), "version": version}
            except Exception:
                return {"found": True, "path": str(p), "version": "unknown"}
    return {"found": False, "path": "", "version": ""}


def check_python_packages():
    """检查关键 Python 包"""
    required = ["selenium", "requests", "undetected-chromedriver", "yaml"]
    available = {}
    for pkg in required:
        try:
            if pkg == "yaml":
                import yaml
            else:
                __import__(pkg.replace("-", "_"))
            available[pkg] = True
        except ImportError:
            available[pkg] = False
    return available


def check_ip_and_proxy(proxy_url=None):
    """检查 IP 信息和代理连通性"""
    result = {
        "public_ip": None,
        "location": None,
        "isp": None,
        "is_proxy": None,
        "risk_score": None,
        "error": None
    }
    
    try:
        if proxy_url:
            # 通过代理检测
            proxy_handler = urllib.request.ProxyHandler({
                "http": proxy_url,
                "https": proxy_url,
            })
            opener = urllib.request.build_opener(proxy_handler)
            resp = opener.open("http://httpbin.org/ip", timeout=15)
            data = json.loads(resp.read())
            result["public_ip"] = data.get("origin", "unknown")
        else:
            # 直接检测
            resp = urllib.request.urlopen("http://httpbin.org/ip", timeout=10)
            data = json.loads(resp.read())
            result["public_ip"] = data.get("origin", "unknown")
    except Exception as e:
        result["error"] = f"IP 检测失败: {str(e)}"
        return result
    
    # 查询 IP 地理位置
    ip = result["public_ip"]
    try:
        resp = urllib.request.urlopen(f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,city,isp,org,as,query,proxy,hosting", timeout=5)
        geo = json.loads(resp.read())
        if geo.get("status") == "success":
            result["location"] = f"{geo.get('country', '?')} - {geo.get('city', '?')}"
            result["isp"] = geo.get("isp", geo.get("org", "unknown"))
            result["is_proxy"] = geo.get("proxy", False)
            result["hosting"] = geo.get("hosting", False)
            
            # 风险评分：简单规则
            score = 0
            reasons = []
            if geo.get("proxy"):
                score += 30
                reasons.append("被标记为代理/VPN")
            if geo.get("hosting"):
                score += 20
                reasons.append("数据中心IP")
            if geo.get("countryCode") and geo.get("countryCode") not in ("US", "DE", "JP", "CA", "GB", "AU"):
                score += 10
                reasons.append(f"非主流注册地区 ({geo.get('countryCode')})")
            result["risk_score"] = {"score": min(score, 100), "reasons": reasons}
    except Exception as e:
        result["error_ipquery"] = str(e)
    
    return result


def check_email_worker(worker_url=None):
    """检查邮箱 Worker 连通性"""
    if not worker_url or worker_url == "https://your-worker.workers.dev":
        return {"reachable": False, "error": "未配置 Worker 地址"}
    try:
        resp = urllib.request.urlopen(worker_url, timeout=10)
        return {"reachable": resp.status == 200, "status": resp.status}
    except Exception as e:
        return {"reachable": False, "error": str(e)}


def run_environment_check(proxy_url=None, worker_url=None):
    """全量环境检测"""
    checks = {}
    
    # 1. Chrome
    checks["chrome"] = check_chrome()
    
    # 2. ChromeDriver
    checks["chromedriver"] = check_chromedriver()
    
    # 3. Python 包
    checks["packages"] = check_python_packages()
    
    # 4. IP / 代理
    checks["network"] = check_ip_and_proxy(proxy_url if proxy_url else None)
    
    # 5. 邮箱 Worker
    checks["email_worker"] = check_email_worker(worker_url)
    
    # 6. 系统信息
    import platform
    checks["system"] = {
        "platform": platform.platform(),
        "python": sys.version,
        "hostname": socket.gethostname(),
    }
    
    # 综合评估
    issues = []
    if not checks["chrome"]["found"]:
        issues.append("❌ Chrome 浏览器未安装")
    if not checks["chromedriver"]["found"]:
        issues.append("❌ ChromeDriver 未安装")
    missing_pkgs = [k for k, v in checks["packages"].items() if not v]
    if missing_pkgs:
        issues.append(f"❌ 缺少 Python 包: {', '.join(missing_pkgs)}")
    if checks["network"].get("error"):
        issues.append(f"⚠️ 网络检测异常: {checks['network']['error']}")
    if checks["network"].get("risk_score") and checks["network"]["risk_score"]["score"] > 50:
        issues.append(f"⚠️ IP 风险较高: {checks['network']['risk_score']['reasons']}")
    if not checks.get("email_worker", {}).get("reachable") and worker_url and worker_url != "https://your-worker.workers.dev":
        issues.append(f"⚠️ 邮箱 Worker 不可达: {checks['email_worker'].get('error', '')}")
    
    checks["summary"] = {
        "passed": len(issues) == 0,
        "issues": issues,
        "total_checks": len(issues) + sum(1 for k, v in checks.items() if k != "summary" and isinstance(v, dict) and v.get("found") is not False)
    }
    
    return checks


# ========== 页面路由 ==========

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    accounts = load_accounts()
    config_text = load_config_yaml()
    fields = config_to_fields(config_text) if config_text else {}
    
    return templates.TemplateResponse(request, "index.html", {
        "accounts": accounts,
        "account_count": len(accounts),
        "status": running_status,
        "config_yaml": config_text,
        "current_region": fields.get("region.current", "usa"),
        "config_fields_json": json.dumps(fields),
        "field_definitions_json": json.dumps(CONFIG_FIELDS),
        "section_meta_json": json.dumps(SECTION_META),
        "flow_steps_json": json.dumps(STEPS),
    })


# ========== API 路由 ==========

@app.get("/api/config-fields")
async def get_config_fields():
    """获取当前配置的结构化字段"""
    config_text = load_config_yaml()
    fields = config_to_fields(config_text) if config_text else {}
    return JSONResponse({
        "fields": fields,
        "definitions": CONFIG_FIELDS,
        "section_meta": SECTION_META,
    })


@app.post("/api/config-fields")
async def save_config_fields(data: dict):
    """保存结构化配置字段"""
    try:
        yaml_text = fields_to_yaml(data)
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(yaml_text)
        return JSONResponse({"success": True, "message": "配置已保存"})
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)})


@app.get("/api/check-env")
async def check_environment():
    """全量环境检测"""
    config_text = load_config_yaml()
    config = parse_config_yaml(config_text) if config_text else {}
    
    proxy_url = None
    if config.get("region", {}).get("use_proxy"):
        proxy_mode = config.get("region", {}).get("proxy_mode", "static")
        if proxy_mode == "static":
            proxy_url = config.get("region", {}).get("proxy_url", "")
        elif proxy_mode == "dynamic":
            proxy_api = config.get("region", {}).get("proxy_api", {})
            if proxy_api.get("url"):
                proxy_url = f"http://{proxy_api.get('protocol', 'http')}://{proxy_api.get('url')}"
    
    worker_url = config.get("email", {}).get("worker_url", "")
    
    import asyncio
    result = await asyncio.to_thread(run_environment_check, proxy_url, worker_url)
    return JSONResponse(result)


# ========== 原有 API 保持兼容 ==========

@app.post("/api/start")
async def start_registration():
    """启动 AWS Builder ID 注册"""
    if running_status["running"]:
        return JSONResponse({"success": False, "message": "已有注册任务在运行"})
    
    def run_registration():
        running_status["running"] = True
        running_status["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        env = os.environ.copy()
        # 清除代理环境变量，避免影响 Worker API 调用和浏览器自动化
        for key in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
            env.pop(key, None)
        # 使用 xvfb 虚拟显示运行浏览器（非无头模式，降低被检测概率）
        env['DISPLAY'] = ':99'
        try:
            # 先确保 xvfb 在运行
            subprocess.run(['Xvfb', ':99', '-screen', '0', '1920x1080x24'],
                         capture_output=True, timeout=5)
        except:
            pass
        try:
            result = subprocess.run(
                ['xvfb-run', '-a', sys.executable, str(BASE_DIR / "src" / "runners" / "main.py")],
                cwd=str(BASE_DIR),
                capture_output=True,
                text=True,
                timeout=600,
                env=env
            )
            running_status["last_log"] = result.stdout[-2000:] if result.stdout else ""
            if result.stderr:
                running_status["last_log"] += "\n[错误]\n" + result.stderr[-1000:]
        except subprocess.TimeoutExpired:
            running_status["last_log"] = "[超时] 注册任务执行超过 600 秒"
        except Exception as e:
            running_status["last_log"] = f"[异常] {str(e)}"
        finally:
            running_status["running"] = False
    
    thread = threading.Thread(target=run_registration, daemon=True)
    thread.start()
    return JSONResponse({"success": True, "message": "注册任务已启动"})


@app.get("/api/status")
async def get_status():
    accounts = load_accounts()
    return JSONResponse({
        "running": running_status["running"],
        "started_at": running_status["started_at"],
        "last_log": running_status["last_log"],
        "account_count": len(accounts),
    })


@app.get("/api/accounts")
async def get_accounts():
    return JSONResponse(load_accounts())


@app.get("/api/flow-status")
async def get_flow_status():
    """获取注册流程实时状态"""
    status = step_tracker.get_status()
    return JSONResponse({
        "steps": STEPS,
        "status": status,
    })


@app.post("/api/config")
async def save_config(config_text: str = Form(...)):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(config_text)
        return JSONResponse({"success": True, "message": "配置已保存"})
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)})


@app.get("/api/config")
async def get_config():
    return JSONResponse({"config": load_config_yaml()})


# ========== 启动 ==========

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8892))
    print(f"🌐 AWS Builder ID Web 管理界面启动于 http://127.0.0.1:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
