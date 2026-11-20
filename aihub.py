#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI Hub —— 本地统一面板，管理 Ollama / Claude Code / OpenClaw / 本地项目。

用法：python3 aihub.py
仅使用 Python 标准库，无第三方依赖。
"""

import base64
import binascii
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PORT = 8799
CSRF_HEADER = "X-AIHub"             # 写接口必须携带，浏览器会先发预检，跨站页面无法伪造
MAX_BODY_BYTES = 64 * 1024 * 1024   # 含图片上传的请求体上限
ALLOWED_HOSTS = {"127.0.0.1:%d" % PORT, "localhost:%d" % PORT, "[::1]:%d" % PORT}
ALLOWED_ORIGINS = {"http://" + h for h in ALLOWED_HOSTS}
CSP = ("default-src 'self'; img-src 'self' data: blob:; "
       "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; frame-ancestors 'none'")
HOME = os.path.expanduser("~")
OLLAMA = "http://127.0.0.1:11434"
OPENCLAW_PORT = 18789

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_FILE = os.path.join(BASE_DIR, "projects.json")
ENV_FILE = os.path.join(BASE_DIR, "envs.json")
RUNTIME_FILE = os.path.join(BASE_DIR, "projects.runtime.json")
MODELS_FILE = os.path.join(BASE_DIR, "models.json")
AI_TOOLS_FILE = os.path.join(BASE_DIR, "ai_tools.json")
CLIS_FILE = os.path.join(BASE_DIR, "clis.json")
CLAUDE_SETTINGS_FILE = os.path.join(HOME, ".claude", "settings.json")
CLI_BACKUP_DIR = os.path.join(BASE_DIR, ".cli-backups")
KEYCHAIN_ACCOUNT = "aihub"
LOGS_DIR = os.path.join(BASE_DIR, "logs")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DEFAULT_SCAN_ROOTS = ["~/Documents", "~/Projects", "~/code", "~/Desktop", "~/Downloads", "~/workspace"]

# 保证能调用 brew / claude / openclaw
ENV = dict(os.environ)
ENV["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + ENV.get("PATH", "/usr/bin:/bin")
ENV["HOMEBREW_NO_AUTO_UPDATE"] = "1"

# 启动/试运行使用的 shell 参数：
# 默认沿用登录 shell（-lc），因为 fnm / nvm / sdkman 的初始化常写在 ~/.zshrc，
# 移除后会导致版本管理工具失效；设置 AIHUB_SHELL_NOLOGIN=1 可切到非登录 shell（-c），
# 避免用户 dotfiles 污染执行环境、提升跨机器可复现性。
USE_LOGIN_SHELL = os.environ.get("AIHUB_SHELL_NOLOGIN") != "1"


def shell_args():
    return ["-lc"] if USE_LOGIN_SHELL else ["-c"]

DEFAULT_MODELS = {
    "ollama": "qwen3:8b",
    "claude": "",          # 由「工具」页的全局绑定决定，未绑定时交给 CLI 自身默认值
    "openclaw": "",
}


def port_open(port, host=None):
    """检查端口是否被占用；默认同时探测 IPv4 / IPv6 回环，避免漏判。"""
    for addr in ([host] if host else ("127.0.0.1", "::1")):
        try:
            with socket.create_connection((addr, port), timeout=0.3):
                return True
        except OSError:
            continue
    return False


_URL_CACHE = {}
_URL_LOCK = threading.Lock()


def local_url(port, ttl=15):
    """探测本机实际可访问的回环地址，避免 localhost 的 IPv4/IPv6 解析差异。"""
    now = time.time()
    with _URL_LOCK:
        hit = _URL_CACHE.get(port)
        if hit and now - hit[0] < ttl:
            return hit[1]
    url = "http://localhost:%d" % port
    for host, label in (("127.0.0.1", "127.0.0.1"), ("::1", "[::1]")):
        try:
            with socket.create_connection((host, port), timeout=0.2):
                url = "http://%s:%d" % (label, port)
                break
        except OSError:
            continue
    with _URL_LOCK:
        _URL_CACHE[port] = (now, url)
    return url


def collect_status():
    st = {}

    ollama = {"running": port_open(11434), "models": []}
    if ollama["running"]:
        try:
            with urlopen(OLLAMA + "/api/tags", timeout=2) as r:
                ollama["models"] = [m["name"] for m in json.load(r).get("models", [])]
        except Exception:
            pass
    st["ollama"] = ollama

    st["openclaw"] = {"running": port_open(OPENCLAW_PORT)}

    version = ""
    try:
        p = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                           timeout=8, env=ENV)
        version = (p.stdout or "").strip()
    except Exception:
        pass
    st["claude"] = {"running": bool(version), "version": version}
    return st


def run_cmd(args, timeout=180, cwd=None):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL, env=ENV, cwd=cwd or HOME)
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    return p.returncode, out, err


# ---------------- 图片上传 ----------------

MAX_IMAGES = 6                       # 每条消息最多图片数
MAX_IMAGE_BYTES = 15 * 1024 * 1024   # 单张上限
MAX_TOTAL_IMAGE_BYTES = 40 * 1024 * 1024
MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
            "image/webp": "webp", "image/bmp": "bmp"}


def _strip_dataurl(value):
    """兼容前端直接发 data URL 的情况，去掉前缀只留 base64。"""
    s = str(value or "")
    if s.startswith("data:"):
        i = s.find(";base64,")
        if i >= 0:
            s = s[i + 8:]
    return s.strip()


def parse_images(payload):
    """从前端 payload 提取并校验图片。

    返回 (images, error)；images 每项为 {"mime", "blob"(bytes), "b64"(str)}。
    """
    raw = payload.get("images")
    if not raw:
        return [], None
    if not isinstance(raw, list):
        return [], "图片数据格式错误"
    out, total = [], 0
    for item in raw[:MAX_IMAGES]:
        if not isinstance(item, dict):
            continue
        mime = str(item.get("mime") or "")
        if not mime.startswith("image/"):
            return [], "不支持的图片类型：%s" % (mime or "未知")
        b64 = _strip_dataurl(item.get("data"))
        try:
            blob = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return [], "图片数据无法解析"
        if not blob:
            return [], "图片内容为空"
        if len(blob) > MAX_IMAGE_BYTES:
            return [], "单张图片超过 %dMB，请压缩后再发" % (MAX_IMAGE_BYTES // 1024 // 1024)
        total += len(blob)
        if total > MAX_TOTAL_IMAGE_BYTES:
            return [], "图片总量过大，请减少张数或压缩后再发"
        out.append({"mime": mime, "blob": blob, "b64": b64})
    return out, None


def save_images_disk(images):
    """把图片写到 uploads/ 目录，返回文件路径列表（给 Claude / OpenClaw 用路径读取）。"""
    if not images:
        return []
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    paths = []
    for img in images:
        ext = MIME_EXT.get(img["mime"], "png")
        name = "%s-%s.%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6], ext)
        path = os.path.join(UPLOAD_DIR, name)
        with open(path, "wb") as f:
            f.write(img["blob"])
        paths.append(path)
    return paths


def prompt_with_images(message, images):
    """Claude / OpenClaw 只收文本提示：图片落盘，把路径写进提示词，让它们用读文件工具查看。"""
    if not images:
        return message
    paths = save_images_disk(images)
    lines = "\n".join("- " + p for p in paths)
    head = (message + "\n\n") if message else ""
    return head + "（用户随本条消息上传了 %d 张图片，请先用读文件工具查看这些路径后再回答：\n%s\n）" % (len(paths), lines)


def history_b64_images(entry):
    """多轮历史里每条消息自带的图片（仅 Ollama 用），坏数据静默跳过。"""
    out = []
    raw = entry.get("images")
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_IMAGES]:
        if not isinstance(item, dict):
            continue
        b64 = _strip_dataurl(item.get("data"))
        try:
            blob = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            continue
        if blob:
            out.append(b64)
    return out


# ---------------- 三个后端 ----------------

def stream_ollama(model, messages, emit):
    """逐块转发 Ollama 的流式输出。"""
    body = json.dumps({"model": model, "messages": messages, "stream": True}).encode()
    req = Request(OLLAMA + "/api/chat", data=body,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=900) as r:
        for line in r:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("error"):
                raise RuntimeError(obj["error"])
            piece = (obj.get("message") or {}).get("content") or ""
            if piece:
                emit("delta", {"text": piece})
            if obj.get("done"):
                break


def call_claude(model, prompt, resume_id, env_extra=None):
    """调用 claude -p，返回 (文本, session_id, 错误)。"""
    args = ["claude", "-p", prompt, "--output-format", "json"]
    if model:
        args += ["--model", model]
    if resume_id:
        args += ["--resume", resume_id]
    env = dict(ENV)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(args, capture_output=True, text=True, timeout=600,
                       stdin=subprocess.DEVNULL, env=env, cwd=HOME)
    code, out, err = p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()

    data = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except Exception:
                continue
    if data is None:
        return None, None, (err or out or "claude 无输出")[-400:]

    text = data.get("result") or ""
    sid = data.get("session_id")
    if data.get("is_error"):
        return None, sid, (text or "claude 调用失败")[-400:]
    return text, sid, None


CLAUDE_TIMEOUT = 900


def _blocks_text(content):
    """把 tool_result 的 content（字符串或 block 列表）压成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "\n".join(parts)
    return ""


def stream_claude(model, prompt, resume_id, workdir, readonly, emit, env_extra=None):
    """以 stream-json 运行 claude，边执行边推送文本与工具事件。

    返回 (session_id, error)。工具调用与结果通过 emit 回传。
    """
    args = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--permission-mode", "plan" if readonly else "acceptEdits"]
    if model:
        args += ["--model", model]
    if resume_id:
        args += ["--resume", resume_id]

    env = dict(ENV)
    if env_extra:
        env.update(env_extra)

    proc = subprocess.Popen(
        args, cwd=workdir, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, start_new_session=True,
    )
    timed_out = {"value": False}

    def kill():
        timed_out["value"] = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass

    watchdog = threading.Timer(CLAUDE_TIMEOUT, kill)
    watchdog.daemon = True
    watchdog.start()

    session_id, noise, error = "", [], ""
    try:
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            if not line.startswith("{"):
                noise.append(line[:300])
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            kind = obj.get("type")
            if kind == "system":
                if obj.get("subtype") == "init":
                    session_id = obj.get("session_id") or session_id
            elif kind == "assistant":
                for block in (obj.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        emit("delta", {"text": block["text"]})
                    elif block.get("type") == "tool_use":
                        emit("tool", {"id": block.get("id") or "",
                                      "name": block.get("name") or "工具",
                                      "input": block.get("input") or {}})
            elif kind == "user":
                for block in (obj.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        emit("tool_result", {"id": block.get("tool_use_id") or "",
                                             "content": _blocks_text(block.get("content")),
                                             "error": bool(block.get("is_error"))})
            elif kind == "result":
                session_id = obj.get("session_id") or session_id
                if obj.get("is_error"):
                    error = (obj.get("result") or "Claude 执行失败")[-400:]
                break
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                pass
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass

    if not error and timed_out["value"]:
        error = "Claude 执行超时（%d 秒）" % CLAUDE_TIMEOUT
    if not error and not session_id and proc.returncode not in (0, None):
        error = (noise[-1] if noise else "claude 退出码 %s" % proc.returncode)[-400:]
    return session_id, error


def call_openclaw(prompt, session_id):
    """调用 openclaw agent，返回 (文本, session_id, 错误)。"""
    args = ["openclaw", "agent", "-m", prompt, "--session-id", session_id, "--json"]
    code, out, err = run_cmd(args, timeout=600)

    data = None
    idx = out.find("{")
    if idx >= 0:
        try:
            data = json.loads(out[idx:])
        except Exception:
            data = None
    if data is None:
        return None, session_id, (err or out or "openclaw 无输出")[-400:]

    text = ""
    try:
        text = data["result"]["payloads"][0]["text"]
    except Exception:
        text = json.dumps(data.get("result"), ensure_ascii=False)[:500]

    if str(data.get("status", "")).lower() == "error":
        return None, session_id, text[-400:]
    return text, session_id, None


# ---------------- 项目管理 ----------------

def load_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json_file(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_projects():
    data = load_json_file(PROJECTS_FILE, [])
    if isinstance(data, dict):
        data = data.get("projects") or []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, dict) and p.get("id") and p.get("path")]


def save_projects(projects):
    write_json_file(PROJECTS_FILE, projects)


def load_runtime():
    data = load_json_file(RUNTIME_FILE, {})
    return data if isinstance(data, dict) else {}


def save_runtime(runtime):
    write_json_file(RUNTIME_FILE, runtime)


# ---------------- 模型与密钥 ----------------
# 密钥统一走这三个函数；将来要支持 Windows，只改这里（可用 DPAPI 实现）。

def set_secret(ref, value):
    try:
        code, out, err = run_cmd(["security", "add-generic-password", "-U",
                                  "-a", KEYCHAIN_ACCOUNT, "-s", ref, "-w", value], timeout=15)
        if code != 0:
            return False, (err or out or "写入钥匙串失败")[:200]
        return True, ""
    except Exception as exc:
        return False, str(exc)[:200]


def get_secret(ref):
    if not ref:
        return ""
    try:
        code, out, _ = run_cmd(["security", "find-generic-password",
                                "-a", KEYCHAIN_ACCOUNT, "-s", ref, "-w"], timeout=15)
        return out if code == 0 else ""
    except Exception:
        return ""


def delete_secret(ref):
    if not ref:
        return
    try:
        run_cmd(["security", "delete-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", ref], timeout=15)
    except Exception:
        pass


def mask_key(value):
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 8:
        return v[:2] + "****"
    return v[:4] + "****" + v[-4:]


def load_models():
    data = load_json_file(MODELS_FILE, [])
    if isinstance(data, dict):
        data = data.get("accounts") or []
    if not isinstance(data, list):
        return []
    return [a for a in data if isinstance(a, dict) and a.get("id")]


def save_models(accounts):
    write_json_file(MODELS_FILE, accounts)
    try:
        os.chmod(MODELS_FILE, 0o600)
    except OSError:
        pass


def to_num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def clean_models(value):
    if isinstance(value, str):
        value = value.splitlines()
    out = []
    for item in value or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            mid = str(item.get("model_id") or "").strip()
        else:
            text = str(item or "").strip()
            if "|" in text:
                name, mid = [x.strip() for x in text.split("|", 1)]
            else:
                name, mid = text, text
        if not mid:
            continue
        out.append({"name": name or mid, "model_id": mid})
    return out[:60]


CAP_CODE = ("strong", "normal", "weak")
CAP_SPEED = ("fast", "normal", "slow")
CAP_PRICE = ("cheap", "normal", "high")


def clean_caps(raw):
    """模型能力标签，用于按能力过滤模型；未填的字段走默认值。"""
    if not isinstance(raw, dict):
        return None
    code = str(raw.get("code") or "normal").strip()
    speed = str(raw.get("speed") or "normal").strip()
    price = str(raw.get("price") or "normal").strip()
    try:
        context = int(float(raw.get("context") or 0))
    except (TypeError, ValueError):
        context = 0
    return {
        "vision": raw.get("vision") is True,
        "tools": raw.get("tools") is True,
        "code": code if code in CAP_CODE else "normal",
        "speed": speed if speed in CAP_SPEED else "normal",
        "price": price if price in CAP_PRICE else "normal",
        "context": max(0, min(context, 10000)),
    }


def account_status(a):
    if a.get("enabled") is False:
        return "off"
    expire_at = str(a.get("expire_at") or "")
    if expire_at and expire_at < time.strftime("%Y-%m-%d"):
        return "expired"
    quota = a.get("quota") or {}
    limit = to_num(quota.get("limit"))
    used = to_num(quota.get("used"))
    if limit > 0 and used >= limit:
        return "empty"
    if limit > 0 and used >= limit * 0.8:
        return "low"
    return "ok"


def public_account(a):
    out = dict(a)
    out.pop("key", None)
    cfg = resolve_balance_cfg(a)
    out["status"] = account_status(a)
    out["key_set"] = bool(a.get("key_hint"))
    out["balance_cfg"] = cfg
    out["balance_supported"] = bool(cfg)
    return out


def handle_model_save(payload):
    accounts = load_models()
    acc_id = str(payload.get("id") or "").strip()
    account = next((a for a in accounts if a["id"] == acc_id), None) if acc_id else None
    if account is None:
        acc_id = "acc-" + uuid.uuid4().hex[:8]
        account = {"id": acc_id}
        accounts.append(account)

    ref = account.get("key_ref") or ("aihub.models." + acc_id)
    new_key = str(payload.get("key") or "").strip()
    if new_key and "****" not in new_key:
        ok, err = set_secret(ref, new_key)
        if not ok:
            return {"ok": False, "message": "写入钥匙串失败：%s" % err}
        account["key_ref"] = ref
        account["key_hint"] = mask_key(new_key)
    elif not account.get("key_hint"):
        account["key_hint"] = "未配置"

    quota = payload.get("quota") or {}
    old_base = str(account.get("base_url") or "")
    new_base = str(payload.get("base_url") or "").strip()
    old_cfg = resolve_balance_cfg(account)
    raw_cfg = payload.get("balance_cfg")
    if isinstance(raw_cfg, dict) and str(raw_cfg.get("path") or "").strip() and str(raw_cfg.get("field") or "").strip():
        new_cfg = {
            "base": "origin" if raw_cfg.get("base") == "origin" else "root",
            "path": str(raw_cfg.get("path")).strip(),
            "field": str(raw_cfg.get("field")).strip(),
            "field2": str(raw_cfg.get("field2") or "").strip(),
            "unit": str(raw_cfg.get("unit") or "元").strip(),
        }
    else:
        new_cfg = None
    account["balance_cfg"] = new_cfg
    if (old_base and old_base != new_base) or old_cfg != new_cfg:
        account.pop("balance", None)
        account.pop("balance_unit", None)
        account.pop("balance_at", None)

    models = clean_models(payload.get("models"))
    caps_in = payload.get("caps")
    old_caps = {m.get("model_id"): m.get("caps") for m in (account.get("models") or [])
                if isinstance(m, dict) and m.get("caps")}
    for m in models:
        raw_caps = caps_in.get(m["model_id"]) if isinstance(caps_in, dict) and m["model_id"] in caps_in \
            else old_caps.get(m["model_id"])
        caps = clean_caps(raw_caps)
        if caps:
            m["caps"] = caps

    account.update({
        "vendor": str(payload.get("vendor") or "").strip() or "未命名平台",
        "base_url": str(payload.get("base_url") or "").strip(),
        "protocol": str(payload.get("protocol") or "openai").strip() or "openai",
        "models": models,
        "quota": {
            "limit": to_num(quota.get("limit")),
            "used": to_num(quota.get("used")),
            "unit": str(quota.get("unit") or "tokens").strip(),
            "period": str(quota.get("period") or "month").strip(),
        },
        "expire_at": str(payload.get("expire_at") or "").strip(),
        "note": str(payload.get("note") or "").strip()[:300],
        "enabled": payload.get("enabled") is not False,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_models(accounts)
    return {"ok": True, "account": public_account(account)}


def handle_model_delete(payload):
    acc_id = str(payload.get("id") or "").strip()
    accounts = load_models()
    target = next((a for a in accounts if a["id"] == acc_id), None)
    if not target:
        return {"ok": False, "message": "记录不存在"}
    delete_secret(target.get("key_ref") or "")
    save_models([a for a in accounts if a["id"] != acc_id])
    return {"ok": True}


def handle_model_copy(payload):
    """把 key 直接写入本机剪贴板，密钥不经过浏览器。"""
    acc_id = str(payload.get("id") or "").strip()
    target = next((a for a in load_models() if a["id"] == acc_id), None)
    if not target:
        return {"ok": False, "message": "记录不存在"}
    secret = get_secret(target.get("key_ref") or "")
    if not secret:
        return {"ok": False, "message": "钥匙串中未找到该 key"}
    try:
        subprocess.run(["pbcopy"], input=secret, text=True, timeout=5)
        return {"ok": True, "message": "已复制到剪贴板"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)[:200]}


def handle_model_fetch(payload):
    """用 key 调平台的 /models 接口，拉取可用模型 ID。"""
    acc_id = str(payload.get("id") or "").strip()
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    protocol = str(payload.get("protocol") or "").strip()
    key = str(payload.get("key") or "").strip()

    if acc_id and (not key or not base_url):
        target = next((a for a in load_models() if a["id"] == acc_id), None)
        if target:
            key = key or get_secret(target.get("key_ref") or "")
            base_url = base_url or str(target.get("base_url") or "").rstrip("/")
            protocol = protocol or target.get("protocol") or "openai"
    if not base_url:
        return {"ok": False, "message": "请先填写 Base URL"}
    if not key:
        return {"ok": False, "message": "请先填写 API Key"}

    headers = {"Accept": "application/json"}
    if protocol == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = "Bearer " + key

    try:
        req = Request(base_url + "/models", headers=headers)
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:160]
        except Exception:
            detail = ""
        if exc.code in (401, 403):
            return {"ok": False, "message": "Key 无效或无权限（HTTP %d）" % exc.code}
        if exc.code == 404:
            return {"ok": False, "message": "该平台没有 /models 接口，请手动填写"}
        return {"ok": False, "message": "请求失败：HTTP %d %s" % (exc.code, detail)}
    except Exception as exc:
        return {"ok": False, "message": "无法连接：%s" % str(exc)[:140]}

    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list) and isinstance(data, dict):
        items = data.get("models")
    ids = []
    for item in items or []:
        if isinstance(item, dict):
            mid = item.get("id") or item.get("model") or item.get("name")
        else:
            mid = item
        mid = str(mid or "").strip()
        if mid and mid not in ids:
            ids.append(mid)
    if not ids:
        return {"ok": False, "message": "接口未返回模型列表，请手动填写"}
    return {"ok": True, "models": ids[:200], "count": len(ids[:200])}


def _http_json(url, key, protocol="openai"):
    headers = {"Accept": "application/json"}
    if protocol == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = "Bearer " + key
    req = Request(url, headers=headers)
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


BALANCE_DEFAULTS = {
    "deepseek": {"base": "origin", "path": "/user/balance",
                 "field": "balance_infos[0].total_balance", "unit": "元"},
    "moonshot": {"base": "root", "path": "/users/me/balance",
                 "field": "data.available_balance", "unit": "元"},
    "siliconflow": {"base": "root", "path": "/user/info",
                    "field": "data.balance", "unit": "元"},
    "openrouter": {"base": "root", "path": "/credits",
                   "field": "data.total_credits", "field2": "data.total_usage", "unit": "美元"},
}


def balance_vendor(base_url):
    host = urlparse(str(base_url or "")).netloc.lower()
    for token in BALANCE_DEFAULTS:
        if token in host:
            return token
    return ""


def resolve_balance_cfg(account):
    """account 里显式配置优先；老数据没有该字段时按域名取内置预设。"""
    if "balance_cfg" in account:
        cfg = account.get("balance_cfg")
        if isinstance(cfg, dict) and cfg.get("path") and cfg.get("field"):
            return cfg
        return None
    vendor = balance_vendor(account.get("base_url"))
    return dict(BALANCE_DEFAULTS[vendor]) if vendor else None


def pick_path(data, path):
    cur = data
    for token in re.findall(r"[^.\[\]]+", str(path or "")):
        if isinstance(cur, list):
            idx = int(token) if token.isdigit() else -1
            if idx < 0 or idx >= len(cur):
                return None
            cur = cur[idx]
        elif isinstance(cur, dict):
            cur = cur.get(token)
        else:
            return None
        if cur is None:
            return None
    return cur


def query_balance(base_url, key, cfg):
    """按配置拉取余额，返回 (余额, 单位)。"""
    root = str(base_url or "").rstrip("/")
    parsed = urlparse(root)
    base = ("%s://%s" % (parsed.scheme or "https", parsed.netloc)) if cfg.get("base") == "origin" else root
    data = _http_json(base + str(cfg.get("path") or ""), key)
    value = pick_path(data, cfg.get("field"))
    if value is None:
        raise ValueError("未找到字段 %s" % cfg.get("field"))
    value = float(value)
    if cfg.get("field2"):
        used = pick_path(data, cfg.get("field2"))
        value = value - float(used or 0)
    return value, cfg.get("unit") or "元"


def handle_model_balance(payload):
    acc_id = str(payload.get("id") or "").strip()
    accounts = load_models()
    account = next((a for a in accounts if a["id"] == acc_id), None)
    if not account:
        return {"ok": False, "message": "记录不存在"}
    cfg = resolve_balance_cfg(account)
    if not cfg:
        return {"ok": False, "message": "该平台未配置余额接口"}
    key = get_secret(account.get("key_ref") or "")
    if not key:
        return {"ok": False, "message": "钥匙串中没有该 key"}
    try:
        balance, unit = query_balance(account.get("base_url") or "", key, cfg)
    except LookupError as exc:
        return {"ok": False, "message": str(exc)}
    except HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "message": "Key 无效或无权限（HTTP %d）" % exc.code}
        if exc.code == 404:
            return {"ok": False, "message": "该平台余额接口不可用（HTTP 404）"}
        return {"ok": False, "message": "查询失败：HTTP %d" % exc.code}
    except ValueError as exc:
        return {"ok": False, "message": "解析失败：%s" % str(exc)[:120]}
    except Exception as exc:
        return {"ok": False, "message": "无法连接：%s" % str(exc)[:120]}

    account["balance"] = round(balance, 4)
    account["balance_unit"] = unit
    account["balance_at"] = time.strftime("%Y-%m-%d %H:%M")
    account["updated_at"] = account["balance_at"]
    save_models(accounts)
    return {"ok": True, "account": public_account(account),
            "message": "余额 %s%s" % (unit, round(balance, 2))}


def handle_model_test(payload):
    """用 /models 或最小对话请求验证 key 是否可用。"""
    acc_id = str(payload.get("id") or "").strip()
    account = next((a for a in load_models() if a["id"] == acc_id), None) if acc_id else None
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    protocol = str(payload.get("protocol") or "").strip()
    key = str(payload.get("key") or "").strip()
    models = clean_models(payload.get("models"))
    if account:
        base_url = base_url or str(account.get("base_url") or "").rstrip("/")
        protocol = protocol or account.get("protocol") or "openai"
        models = models or account.get("models") or []
        key = key or get_secret(account.get("key_ref") or "")
    if not base_url:
        return {"ok": False, "message": "请先填写 Base URL"}
    if not key:
        return {"ok": False, "message": "请先填写 API Key"}
    protocol = protocol or "openai"

    headers = {"Accept": "application/json"}
    if protocol == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = "Bearer " + key

    started = time.time()
    try:
        with urlopen(Request(base_url + "/models", headers=headers), timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = data.get("models") if isinstance(data, dict) else []
        count = len(items or [])
        ms = int((time.time() - started) * 1000)
        return {"ok": True, "latency": ms,
                "message": "Key 可用" + ("，返回 %d 个模型" % count if count else "")}
    except HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "message": "Key 无效或无权限（HTTP %d）" % exc.code}
        if exc.code != 404:
            return {"ok": False, "message": "接口返回异常（HTTP %d）" % exc.code}
    except Exception as exc:
        return {"ok": False, "message": "无法连接：%s" % str(exc)[:120]}

    if not models:
        return {"ok": False, "message": "该平台没有 /models 接口，请先填一个模型再测试"}
    model = models[0]["model_id"]
    body = {"model": model, "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}]}
    path = "/messages" if protocol == "anthropic" else "/chat/completions"
    req = Request(base_url + path, data=json.dumps(body).encode(),
                  headers=dict(headers, **{"Content-Type": "application/json"}))
    try:
        with urlopen(req, timeout=25) as resp:
            resp.read()
        ms = int((time.time() - started) * 1000)
        return {"ok": True, "latency": ms, "message": "Key 可用（模型 %s）" % model}
    except HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "message": "Key 无效或无权限（HTTP %d）" % exc.code}
        if exc.code in (400, 404, 422):
            return {"ok": False, "message": "Key 可能有效，但模型 %s 不可用（HTTP %d）" % (model, exc.code)}
        return {"ok": False, "message": "测试失败（HTTP %d）" % exc.code}
    except Exception as exc:
        return {"ok": False, "message": "无法连接：%s" % str(exc)[:120]}


def collect_models():
    accounts = load_models()
    accounts.sort(key=lambda a: (a.get("enabled") is False, a.get("vendor") or ""))
    return {
        "accounts": [public_account(a) for a in accounts],
        "keychain": os.path.exists("/usr/bin/security") or os.path.exists("/bin/security"),
    }


ANTHROPIC_ENDPOINTS = {
    "moonshot.cn": "https://api.moonshot.cn/anthropic",
    "bigmodel.cn": "https://open.bigmodel.cn/api/anthropic",
}


def anthropic_base(account):
    """取该账号可用于 Claude Code 的 Anthropic 兼容地址，不支持返回空。"""
    explicit = str(account.get("anthropic_base_url") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    if str(account.get("protocol") or "") == "anthropic":
        return str(account.get("base_url") or "").strip().rstrip("/")
    base = str(account.get("base_url") or "").strip()
    for host, url in ANTHROPIC_ENDPOINTS.items():
        if host in base:
            return url
    return ""


def claude_models_payload():
    """列出模型页里能直接给 Claude Code 用的模型（Anthropic 兼容 + 已配密钥）。"""
    models = []
    for a in load_models():
        if a.get("enabled") is False:
            continue
        if not anthropic_base(a) or not get_secret(a.get("key_ref")):
            continue
        for m in a.get("models") or []:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("model_id") or m.get("name") or "").strip()
            if not mid:
                continue
            models.append({
                "account": a["id"],
                "vendor": a.get("vendor") or "未命名平台",
                "model": mid,
                "label": str(m.get("name") or mid).strip(),
            })
    return {"models": models, "binding": claude_binding()}


def claude_account_env(account_id):
    """把模型账号转成 claude 进程的环境变量，密钥只在本地读取。"""
    acc = next((a for a in load_models() if a.get("id") == account_id), None)
    if not acc:
        return None, "模型账号不存在：%s" % account_id
    vendor = acc.get("vendor") or "该平台"
    base = anthropic_base(acc)
    if not base:
        return None, "%s 不支持 Anthropic 协议，无法用于 Claude Code" % vendor
    key = get_secret(acc.get("key_ref"))
    if not key:
        return None, "%s 未配置 API Key" % vendor
    return {
        "ANTHROPIC_BASE_URL": base,
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_API_KEY": key,
    }, ""


# ---------------- CLI 模型绑定（Claude Code） ----------------
# 全局唯一绑定：切换时写入 ~/.claude/settings.json，同时供 AI Hub 内部调用复用。

CLAUDE_ENV_KEYS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                   "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL")
CLAUDE_SHELL_FILES = (".zshrc", ".zprofile", ".zshenv",
                      ".bashrc", ".bash_profile", ".profile")
CLI_LOCK = threading.Lock()
_CLAUDE_VERSION = {"at": 0.0, "value": ""}
_CLAUDE_MODELS = {"at": 0.0, "value": []}


def load_cli_state():
    data = load_json_file(CLIS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_cli_state(state):
    write_json_file(CLIS_FILE, state)
    try:
        os.chmod(CLIS_FILE, 0o600)
    except OSError:
        pass


def claude_binding():
    """当前生效的 Claude Code 绑定，未绑定返回 None。"""
    entry = load_cli_state().get("claude")
    active = entry.get("active") if isinstance(entry, dict) else None
    if not isinstance(active, dict):
        return None
    if not str(active.get("account") or "").strip() or not str(active.get("model") or "").strip():
        return None
    return active


def claude_binding_env(binding=None):
    """把绑定转成 claude 进程环境变量与模型，返回 (env, model, error)。"""
    binding = binding or claude_binding()
    if not binding:
        return None, "", ""
    acc = next((a for a in load_models() if a.get("id") == binding.get("account")), None)
    if not acc:
        return None, "", "绑定的模型账号不存在：%s" % binding.get("account")
    vendor = acc.get("vendor") or binding.get("account")
    if acc.get("enabled") is False:
        return None, "", "%s 已停用" % vendor
    base = anthropic_base(acc)
    if not base:
        return None, "", "%s 不支持 Anthropic 协议，无法用于 Claude Code" % vendor
    key = get_secret(acc.get("key_ref"))
    if not key:
        return None, "", "%s 未配置 API Key" % vendor
    model = str(binding.get("model") or "").strip()
    env = {"ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_API_KEY": key}
    if model:
        env["ANTHROPIC_MODEL"] = model
        env["ANTHROPIC_SMALL_FAST_MODEL"] = model
    return env, model, ""


def claude_call_config():
    """AI Hub 内部调用 claude 时的模型与环境：优先全局绑定。"""
    binding = claude_binding()
    if not binding:
        return DEFAULT_MODELS.get("claude", ""), None
    env, model, err = claude_binding_env(binding)
    if err:
        return str(binding.get("model") or ""), None
    return model, env


def _read_claude_settings():
    """读取设置文件，返回 (dict 或 None, 错误)。文件不存在时视为空配置。"""
    try:
        with open(CLAUDE_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, ""
    except Exception as exc:
        return None, "无法解析 %s：%s" % (CLAUDE_SETTINGS_FILE, str(exc)[:160])
    if not isinstance(data, dict):
        return None, "%s 不是 JSON 对象，已停止写入" % CLAUDE_SETTINGS_FILE
    return data, ""


def _write_claude_settings(data):
    os.makedirs(os.path.dirname(CLAUDE_SETTINGS_FILE), exist_ok=True)
    tmp = CLAUDE_SETTINGS_FILE + ".aihub.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)   # 明文密钥只允许当前用户读取
    os.replace(tmp, CLAUDE_SETTINGS_FILE)


def claude_settings_drift():
    """对比配置文件里的托管键与最后写入值，判断绑定是否仍然生效。"""
    entry = load_cli_state().get("claude")
    entry = entry if isinstance(entry, dict) else {}
    result = {"path": CLAUDE_SETTINGS_FILE, "state": "unbound", "keys": [], "detail": ""}
    if not isinstance(entry.get("active"), dict):
        return result
    data, err = _read_claude_settings()
    if err:
        result.update({"state": "invalid", "detail": err})
        return result
    if data is None:
        result.update({"state": "missing", "detail": "配置文件不存在，绑定未落地"})
        return result
    applied = entry.get("applied") if isinstance(entry.get("applied"), dict) else {}
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    diffs = [k for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL")
             if str(env.get(k) or "") != str(applied.get(k) or "")]
    if applied.get("apiKeyHelper") and str(data.get("apiKeyHelper") or "") != str(applied.get("apiKeyHelper")):
        diffs.append("apiKeyHelper")
    if diffs:
        result.update({"state": "drift", "keys": diffs,
                       "detail": "配置被外部修改：" + "、".join(diffs)})
    else:
        result["state"] = "synced"
    return result


def claude_shell_conflicts():
    """检查 shell 启动文件里是否 export 了会覆盖配置文件的变量。"""
    hits = []
    pattern = re.compile(r"^\s*(?:export\s+)?(ANTHROPIC_(?:BASE_URL|AUTH_TOKEN|API_KEY|MODEL))\s*=", re.M)
    for name in CLAUDE_SHELL_FILES:
        path = os.path.join(HOME, name)
        text = read_text(path, 60000)
        if not text:
            continue
        keys = sorted(set(m.group(1) for m in pattern.finditer(text)))
        if keys:
            hits.append({"file": path, "keys": keys})
    return hits


def claude_version():
    now = time.time()
    if now - _CLAUDE_VERSION["at"] < 60:
        return _CLAUDE_VERSION["value"]
    value = ""
    try:
        code, out, _ = run_cmd(["claude", "--version"], timeout=8)
        if code == 0:
            value = (out.splitlines() or [""])[0][:80]
    except Exception:
        value = ""
    _CLAUDE_VERSION.update({"at": now, "value": value})
    return value


def _claude_binding_models():
    """给绑定下拉用的模型列表，短缓存避免每次轮询都读钥匙串。"""
    now = time.time()
    if now - _CLAUDE_MODELS["at"] < 30:
        return _CLAUDE_MODELS["value"]
    models = claude_models_payload().get("models") or []
    _CLAUDE_MODELS.update({"at": now, "value": models})
    return models


def apply_claude_binding(account_id, model, key_mode="helper"):
    """把全局绑定写入 ~/.claude/settings.json，首次写入前自动备份。"""
    account_id = str(account_id or "").strip()
    model = str(model or "").strip()
    key_mode = "helper" if key_mode == "helper" else "plain"
    acc = next((a for a in load_models() if a.get("id") == account_id), None)
    if not acc:
        return {"ok": False, "message": "模型账号不存在：%s" % account_id}
    vendor = acc.get("vendor") or account_id
    if acc.get("enabled") is False:
        return {"ok": False, "message": "%s 已停用" % vendor}
    base = anthropic_base(acc)
    if not base:
        return {"ok": False, "message": "%s 不支持 Anthropic 协议，无法用于 Claude Code" % vendor}
    key = get_secret(acc.get("key_ref"))
    if not key:
        return {"ok": False, "message": "%s 未配置 API Key" % vendor}
    if not model:
        return {"ok": False, "message": "请选择要绑定的模型"}
    valid = [str(m.get("model_id") or m.get("name") or "").strip()
             for m in acc.get("models") or [] if isinstance(m, dict)]
    if valid and model not in valid:
        return {"ok": False,
                "message": "模型 %s 不在 %s 的模型列表里，可先在「模型」页拉取模型列表" % (model, vendor)}

    helper = ""
    if key_mode == "helper" and acc.get("key_ref"):
        helper = "security find-generic-password -a %s -s %s -w 2>/dev/null" % (
            shlex.quote(KEYCHAIN_ACCOUNT), shlex.quote(str(acc["key_ref"])))
    if key_mode == "helper" and not helper:
        key_mode = "plain"

    with CLI_LOCK:
        data, err = _read_claude_settings()
        if err:
            return {"ok": False, "message": err}
        existed = os.path.exists(CLAUDE_SETTINGS_FILE)
        data = data or {}
        state = load_cli_state()
        entry = state.get("claude") if isinstance(state.get("claude"), dict) else {}
        if not isinstance(entry.get("backup"), dict):
            # 深拷贝：后面会就地修改 data，避免备份跟着被改
            original = json.loads(json.dumps(data)) if existed else None
            entry["backup"] = {
                "file": CLAUDE_SETTINGS_FILE,
                "existed": existed,
                "content": original,
                "at": int(time.time()),
            }
            try:
                os.makedirs(CLI_BACKUP_DIR, exist_ok=True)
                os.chmod(CLI_BACKUP_DIR, 0o700)
                backup_file = os.path.join(
                    CLI_BACKUP_DIR, "claude-settings-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
                with open(backup_file, "w", encoding="utf-8") as f:
                    json.dump({"existed": existed, "content": original},
                              f, ensure_ascii=False, indent=2)
                os.chmod(backup_file, 0o600)
                entry["backup"]["path"] = backup_file
            except OSError:
                pass

        env = data.get("env") if isinstance(data.get("env"), dict) else {}
        env = dict(env)
        env["ANTHROPIC_BASE_URL"] = base
        env["ANTHROPIC_MODEL"] = model
        env["ANTHROPIC_SMALL_FAST_MODEL"] = model
        if key_mode == "helper":
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
            env.pop("ANTHROPIC_API_KEY", None)
            data["apiKeyHelper"] = helper
        else:
            env["ANTHROPIC_AUTH_TOKEN"] = key
            env["ANTHROPIC_API_KEY"] = key
            data.pop("apiKeyHelper", None)
        data["env"] = env
        try:
            _write_claude_settings(data)
        except Exception as exc:
            return {"ok": False, "message": "写入 %s 失败：%s" % (CLAUDE_SETTINGS_FILE, str(exc)[:160])}

        entry["active"] = {
            "account": account_id,
            "vendor": vendor,
            "model": model,
            "label": "%s · %s" % (vendor, model),
            "key_mode": key_mode,
            "at": int(time.time()),
        }
        entry["applied"] = {
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            "apiKeyHelper": helper,
        }
        entry.pop("verify", None)
        state["claude"] = entry
        save_cli_state(state)

    return {
        "ok": True,
        "message": "已切换为 %s · %s，新开的 claude 会话生效" % (vendor, model),
        "binding": entry["active"],
        "settings": claude_settings_drift(),
    }


def restore_claude_binding():
    """还原切换前的 Claude Code 配置。"""
    with CLI_LOCK:
        state = load_cli_state()
        entry = state.get("claude") if isinstance(state.get("claude"), dict) else {}
        backup = entry.get("backup") if isinstance(entry.get("backup"), dict) else None
        if backup and backup.get("existed") and isinstance(backup.get("content"), dict):
            try:
                _write_claude_settings(backup["content"])
            except Exception as exc:
                return {"ok": False, "message": "恢复失败：%s" % str(exc)[:160]}
        else:
            data, err = _read_claude_settings()
            if err:
                return {"ok": False, "message": err}
            data = data or {}
            env = data.get("env") if isinstance(data.get("env"), dict) else {}
            env = dict(env)
            removed = False
            for key_name in CLAUDE_ENV_KEYS:
                if key_name in env:
                    env.pop(key_name, None)
                    removed = True
            if removed:
                if env:
                    data["env"] = env
                else:
                    data.pop("env", None)
            if (entry.get("applied") or {}).get("apiKeyHelper"):
                data.pop("apiKeyHelper", None)
            if not data:
                try:
                    os.remove(CLAUDE_SETTINGS_FILE)
                except OSError:
                    pass
            else:
                try:
                    _write_claude_settings(data)
                except Exception as exc:
                    return {"ok": False, "message": "恢复失败：%s" % str(exc)[:160]}
        for key_name in ("active", "applied", "backup", "verify"):
            entry.pop(key_name, None)
        state["claude"] = entry
        save_cli_state(state)
    return {"ok": True, "message": "已恢复切换前的 Claude Code 配置"}


def _record_claude_verify(ok, message, latency, checks, output=""):
    state = load_cli_state()
    entry = state.get("claude") if isinstance(state.get("claude"), dict) else {}
    entry["verify"] = {"at": int(time.time()), "ok": bool(ok),
                       "message": message, "latency": latency}
    state["claude"] = entry
    save_cli_state(state)
    return {"ok": bool(ok), "message": message, "latency": latency,
            "checks": checks, "output": output}


def verify_claude_binding(deep=True):
    """先做静态预检；deep 为真时再用当前绑定真实调用一次 claude。"""
    binding = claude_binding()
    if not binding:
        return {"ok": False, "message": "还没有绑定模型，请先选择模型"}
    if not shutil.which("claude"):
        return {"ok": False, "message": "本机没有找到 claude 命令"}

    checks = []
    env, model, err = claude_binding_env(binding)
    if err:
        checks.append({"name": "账号与密钥", "ok": False, "detail": err})
        return _record_claude_verify(False, err, 0, checks)
    checks.append({"name": "账号与密钥", "ok": True,
                   "detail": "%s · %s" % (binding.get("vendor") or "", model)})
    drift = claude_settings_drift()
    checks.append({"name": "配置文件", "ok": drift["state"] == "synced",
                   "detail": drift.get("detail") or ("已写入 %s" % drift["path"])})
    conflicts = claude_shell_conflicts()
    checks.append({"name": "shell 覆盖", "ok": True, "warn": bool(conflicts),
                   "detail": "；".join("%s 含 %s" % (c["file"], "、".join(c["keys"]))
                                      for c in conflicts) or "未发现"})

    if not deep:
        ok = all(c["ok"] for c in checks)
        message = "预检通过" if ok else "预检发现问题"
        if ok and conflicts:
            message = "预检通过，但 shell 里的变量可能覆盖配置"
        return {"ok": ok, "checks": checks, "message": message}

    started = time.time()
    run_env = dict(ENV)
    run_env.update(env)
    try:
        proc = subprocess.run(
            ["claude", "-p", "只回复 OK", "--output-format", "json", "--model", model],
            capture_output=True, text=True, timeout=90,
            stdin=subprocess.DEVNULL, cwd=HOME, env=run_env)
    except subprocess.TimeoutExpired:
        return _record_claude_verify(False, "验证超时（90 秒）", 90000, checks)
    except Exception as exc:
        return _record_claude_verify(False, "验证失败：%s" % str(exc)[:200], 0, checks)
    elapsed = int((time.time() - started) * 1000)

    out = (proc.stdout or "").strip()
    data = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
    if data is None:
        detail = ((proc.stderr or "") + "\n" + out).strip()[-300:] or ("退出码 %s" % proc.returncode)
        return _record_claude_verify(False, "没有拿到有效结果：%s" % detail, elapsed, checks)
    if data.get("is_error"):
        return _record_claude_verify(
            False, "调用失败：%s" % str(data.get("result") or "Claude 返回错误")[:300], elapsed, checks)
    text = str(data.get("result") or "").strip()
    message = "验证通过 · %s · %s · %dms" % (binding.get("vendor") or "", model, elapsed)
    return _record_claude_verify(True, message, elapsed, checks, text[-200:])


def clis_payload():
    """工具页的 CLI 绑定区块数据。"""
    state = load_cli_state()
    entry = state.get("claude") if isinstance(state.get("claude"), dict) else {}
    path = shutil.which("claude")
    return {"clis": [{
        "id": "claude",
        "label": "Claude Code",
        "available": bool(path),
        "path": path or "",
        "version": claude_version() if path else "",
        "config_path": CLAUDE_SETTINGS_FILE,
        "binding": claude_binding(),
        "settings": claude_settings_drift(),
        "conflicts": claude_shell_conflicts(),
        "verify": entry.get("verify") if isinstance(entry.get("verify"), dict) else {},
        "models": _claude_binding_models(),
        "has_backup": isinstance(entry.get("backup"), dict),
    }]}


def handle_cli_bind(payload):
    if str(payload.get("cli") or "claude").strip() != "claude":
        return {"ok": False, "message": "暂只支持 Claude Code"}
    return apply_claude_binding(payload.get("account"), payload.get("model"),
                                payload.get("key_mode") or "helper")


def handle_cli_restore(payload):
    if str(payload.get("cli") or "claude").strip() != "claude":
        return {"ok": False, "message": "暂只支持 Claude Code"}
    return restore_claude_binding()


def handle_cli_verify(payload):
    if str(payload.get("cli") or "claude").strip() != "claude":
        return {"ok": False, "message": "暂只支持 Claude Code"}
    return verify_claude_binding(deep=payload.get("deep") is not False)


def project_by_id(pid):
    for p in load_projects():
        if p["id"] == pid:
            return p
    return None


def log_path_for(p):
    os.makedirs(LOGS_DIR, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(p["id"])) or "project"
    return os.path.join(LOGS_DIR, safe + ".log")


_RUNTIME_CHECK = {}


def project_process_env(p):
    """把项目绑定的运行时/工具链注入子进程环境，不改全局环境。

    数据来源：项目自带的 runtime（venv/node，GitHub 导入写入）+ envs.json 中
    按 realpath 记录的项目级绑定（java/go/rust/python，页面切换写入）。
    """
    env = dict(ENV)
    rt = p.get("runtime") if isinstance(p.get("runtime"), dict) else {}
    venv = rt.get("venv") or ""
    if venv:
        env["PATH"] = os.path.join(venv, "bin") + ":" + env.get("PATH", "/usr/bin:/bin")
        env["VIRTUAL_ENV"] = venv
        env.pop("PYTHONHOME", None)
    node = rt.get("node") or ""
    if node:
        node_dir = os.path.dirname(node)
        if node_dir and node_dir not in env["PATH"].split(":"):
            env["PATH"] = node_dir + ":" + env["PATH"]

    # 项目级绑定：从 envs.json 读取（兼容旧的 {"node": version} 结构）
    path = normalize_path(p.get("path") or "")
    if not path:
        return env
    binding = load_env_prefs().get(os.path.realpath(path)) or {}
    if not isinstance(binding, dict):
        return env

    def prepend_path(directory):
        if directory and directory not in env.get("PATH", "").split(":"):
            env["PATH"] = directory + ":" + env.get("PATH", "/usr/bin:/bin")

    # Java：javaPath 优先；否则尝试 sdkman/jenv 的版本目录
    java_ver = binding.get("java")
    java_home = binding.get("javaPath") or ""
    if java_ver and not java_home:
        provider = binding.get("provider") or ""
        if provider == "sdkman":
            candidate = os.path.expanduser("~/.sdkman/candidates/java/%s" % java_ver)
            if os.path.isdir(candidate):
                java_home = candidate
        elif provider == "jenv":
            candidate = os.path.expanduser("~/.jenv/versions/%s" % java_ver)
            if os.path.isdir(candidate):
                java_home = candidate
    if java_home and os.path.isdir(java_home):
        env["JAVA_HOME"] = java_home
        prepend_path(os.path.join(java_home, "bin"))

    # Go：goPath 作为 GOROOT
    go_ver = binding.get("go")
    goroot = binding.get("goPath") if go_ver else ""
    if go_ver and goroot and os.path.isdir(goroot):
        env["GOROOT"] = goroot
        prepend_path(os.path.join(goroot, "bin"))

    # Rust：设置 RUSTUP_TOOLCHAIN，rustup shims 在 ~/.cargo/bin
    rust_ver = binding.get("rust")
    if rust_ver:
        env["RUSTUP_TOOLCHAIN"] = rust_ver
        prepend_path(os.path.expanduser("~/.cargo/bin"))

    # Python 版本偏好：若指定了解释器路径则前置其目录
    py = binding.get("python")
    if py and os.path.isfile(py):
        prepend_path(os.path.dirname(py))

    return env


def runtime_broken(p):
    """检查 GitHub 导入项目的运行环境是否还在；返回问题描述，正常时返回空串。"""
    rt = p.get("runtime") if isinstance(p.get("runtime"), dict) else {}
    venv = rt.get("venv") or ""
    if not venv:
        return ""
    key = p.get("id") or venv
    cached = _RUNTIME_CHECK.get(key)
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    message = ""
    python = os.path.join(venv, "bin", "python")
    if not os.path.exists(python):
        message = "运行环境已失效（.venv 不存在），请点「修复环境」重新安装依赖"
    else:
        try:
            out = subprocess.run([python, "-c", "import sys;print(sys.version.split()[0])"],
                                 capture_output=True, text=True, timeout=6).stdout.strip()
        except Exception as exc:
            out = ""
            message = "运行环境不可用（%s），请点「修复环境」重建" % str(exc)[:60]
        want = str(rt.get("pythonVersion") or "")
        major = ".".join(want.split(".")[:2])
        if not message and out and major and not out.startswith(major):
            message = "Python 已从 %s 变为 %s，建议点「修复环境」重建虚拟环境" % (want, out)
    _RUNTIME_CHECK[key] = (time.time(), message)
    return message


def clean_port(value):
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def normalize_admin(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if not value.startswith("/"):
        value = "/" + value
    return value[:60]


def normalize_path(value):
    """容错处理手工输入的路径：补全开头的 /、支持 ~、尝试相对 HOME 的位置。"""
    path = os.path.expanduser(str(value or "").strip())
    if not path:
        return ""
    if not os.path.isabs(path):
        for base in ("/", HOME):
            candidate = os.path.join(base, path)
            if os.path.isdir(candidate):
                path = candidate
                break
    return os.path.normpath(path)


PROJECT_MARKERS = ("package.json", "pom.xml", "build.gradle", "build.gradle.kts",
                   "docker-compose.yml", "docker-compose.yaml", "compose.yml",
                   "go.mod", "Cargo.toml", "manage.py", "composer.json", "pyproject.toml")


def looks_like_project_dir(d):
    return any(os.path.exists(os.path.join(d, m)) for m in PROJECT_MARKERS)


def workspace_list():
    """把同一父目录下的多个服务（如 api/frontend）归并为一个工作区。"""
    projects = load_projects()
    paths = {normalize_path(p.get("path") or "") for p in projects}
    out = {}
    for p in projects:
        path = normalize_path(p.get("path") or "")
        parent = os.path.dirname(path)
        if parent not in paths and looks_like_project_dir(parent):
            wid = "w:" + parent
            ws = out.setdefault(wid, {"id": wid, "name": os.path.basename(parent),
                                      "path": parent, "services": []})
        else:
            wid = p["id"]
            ws = out.setdefault(wid, {"id": wid, "name": p.get("name") or os.path.basename(path),
                                      "path": path, "services": []})
        ws["services"].append({"id": p["id"], "name": p.get("name"),
                               "path": path, "port": p.get("port")})
    return sorted(out.values(), key=lambda w: w["name"])


def workspace_by_id(wid):
    return next((w for w in workspace_list() if w["id"] == wid), None)


def exec_capture(args, timeout=10):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=ENV)
        return p.stdout or ""
    except Exception:
        return ""


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        # 先回收自己启动的僵尸进程，避免误判为运行中
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


_PORTS_CACHE = {"ts": 0.0, "items": []}
_PORTS_LOCK = threading.Lock()


def port_from_addr(addr):
    part = addr.rsplit(":", 1)[-1].strip()
    return int(part) if part.isdigit() else None


def list_listeners(force=False):
    """返回本机所有 TCP 监听端口：[{pid, command, port}]，缓存 2 秒。"""
    now = time.time()
    with _PORTS_LOCK:
        if not force and now - _PORTS_CACHE["ts"] < 2.0:
            return list(_PORTS_CACHE["items"])
    out = exec_capture(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"])
    items, pid, command, seen = [], None, "", set()
    me = os.getpid()
    for line in out.splitlines():
        tag, val = line[:1], line[1:]
        if tag == "p":
            pid = int(val) if val.isdigit() else None
            command = ""
        elif tag == "c":
            command = val
        elif tag == "n" and pid and pid != me:
            port = port_from_addr(val)
            if port and (pid, port) not in seen:
                seen.add((pid, port))
                items.append({"pid": pid, "command": command, "port": port})
    with _PORTS_LOCK:
        _PORTS_CACHE["ts"] = now
        _PORTS_CACHE["items"] = items
    return list(items)


def invalidate_ports_cache():
    with _PORTS_LOCK:
        _PORTS_CACHE["ts"] = 0.0
        _PORTS_CACHE["items"] = []


def pids_cwd(pids):
    """批量查询进程工作目录：{pid: cwd}。"""
    result = {}
    pids = [p for p in set(pids) if p]
    for i in range(0, len(pids), 30):
        chunk = pids[i:i + 30]
        out = exec_capture(["lsof", "-a", "-d", "cwd",
                            "-p", ",".join(str(x) for x in chunk), "-Fn"])
        cur = None
        for line in out.splitlines():
            if line.startswith("p"):
                cur = int(line[1:]) if line[1:].isdigit() else None
            elif line.startswith("n") and cur:
                result[cur] = line[1:]
    return result


def ps_table():
    out = exec_capture(["ps", "-axo", "pid=,pgid=,pcpu=,rss="])
    table = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            pid, pgid = int(parts[0]), int(parts[1])
            table[pid] = {"pgid": pgid, "cpu": float(parts[2]), "rss": int(parts[3])}
        except ValueError:
            continue
    return table


def is_under(cwd, base):
    if not cwd or not base:
        return False
    try:
        cwd = os.path.realpath(cwd)
        base = os.path.realpath(base)
    except Exception:
        return False
    return cwd == base or cwd.startswith(base + os.sep)


# 编辑器 / 语言服务等进程也常常监听端口，展示与停止时排除，避免误伤
NOISE_COMMANDS = ("code helper", "language-server", "tsserver", "eslint", "prettier",
                  "pylance", "jedi", "gopls", "rust-analyzer", "docker desktop")


def collect_projects():
    projects = load_projects()
    runtime = load_runtime()
    listeners = list_listeners()
    ps = ps_table()
    cwds = pids_cwd([l["pid"] for l in listeners])

    useful = [l for l in listeners
              if not any(n in (l["command"] or "").lower() for n in NOISE_COMMANDS)]

    by_pid = {}
    for l in useful:
        by_pid.setdefault(l["pid"], {"command": l["command"], "ports": set()})["ports"].add(l["port"])

    out = []
    for p in projects:
        path = normalize_path(p.get("path") or "")
        entry = runtime.get(p["id"]) if isinstance(runtime.get(p["id"]), dict) else {}
        mpid = entry.get("pid")
        tree = set()
        if mpid:
            tree = {pid for pid, info in ps.items() if info["pgid"] == mpid}
            if pid_alive(mpid):
                tree.add(mpid)

        ports, pids = set(), set()
        for pid in tree:
            info = by_pid.get(pid)
            if info:
                ports |= info["ports"]
                pids.add(pid)
        # 没经本页面启动、但工作目录在项目内的监听进程，同样算作“运行中”
        for pid, info in by_pid.items():
            if is_under(cwds.get(pid, ""), path):
                ports |= info["ports"]
                pids.add(pid)

        hint = clean_port(p.get("port"))
        managed = bool(mpid and pid_alive(mpid))
        started = entry.get("started") if isinstance(entry, dict) else None
        running = bool(ports) or managed
        uptime = int(time.time() - started) if managed and started else 0
        cpu = round(sum(ps.get(x, {}).get("cpu", 0) for x in (tree | pids)), 1)
        mem = sum(ps.get(x, {}).get("rss", 0) for x in (tree | pids)) // 1024
        env = env_report(path) if path else {"issues": []}

        out.append({
            "id": p["id"],
            "name": p.get("name") or os.path.basename(path.rstrip("/")) or path,
            "path": path,
            "start": p.get("start") or "",
            "type": p.get("type") or "",
            "port": hint,
            "admin": p.get("admin") or "",
            "source": p.get("source") or "",
            "commit": p.get("commit") or "",
            "kind": p.get("kind") or "",
            "envBroken": runtime_broken(p),
            "running": running,
            "starting": running and not ports and 0 < uptime < 20,
            "managed": managed,
            "pids": sorted(pids),
            "ports": sorted(ports),
            "portUrls": {str(n): local_url(n) for n in sorted(ports)},
            "cpu": cpu,
            "mem": mem,
            "uptime": uptime,
            "env": env,
        })

    by_length = sorted(out, key=lambda x: len(x["path"]), reverse=True)

    project_paths = {x["path"] for x in out}
    group_cache = {}
    for x in out:
        parent = os.path.dirname(x["path"])
        if parent not in group_cache:
            group_cache[parent] = (parent not in project_paths and looks_like_project_dir(parent))
        x["group"] = os.path.basename(parent) if group_cache[parent] else ""
        if x["group"]:
            x["workspace_id"] = "w:" + parent
            x["workspace_name"] = x["group"]
            x["workspace_path"] = parent
        else:
            x["workspace_id"] = x["id"]
            x["workspace_name"] = x["name"]
            x["workspace_path"] = x["path"]

    overview = []
    for l in useful:
        cwd = cwds.get(l["pid"], "")
        owner = next((x["name"] for x in by_length if is_under(cwd, x["path"])), "")
        overview.append({"port": l["port"], "pid": l["pid"], "command": l["command"],
                         "cwd": cwd, "project": owner})
    overview.sort(key=lambda x: (x["port"], x["pid"]))
    return {"projects": out, "listeners": overview, "logDir": LOGS_DIR}


def start_project(p):
    runtime = load_runtime()
    entry = runtime.get(p["id"]) if isinstance(runtime.get(p["id"]), dict) else {}
    mpid = entry.get("pid")
    if mpid and pid_alive(mpid):
        return {"ok": True, "message": "已在运行（PID %s）" % mpid}

    path = os.path.expanduser(p.get("path") or "")
    if not os.path.isdir(path):
        return {"ok": False, "message": "项目目录不存在：%s" % path}
    command = (p.get("start") or "").strip()
    if not command:
        return {"ok": False, "message": "请先在「编辑」中填写启动命令"}

    broken = runtime_broken(p)
    if broken:
        return {"ok": False, "message": broken}

    command, env_note = wrap_command(command, path)
    hint = clean_port(p.get("port"))
    if hint and port_open(hint):
        return {"ok": False, "message": "端口 %s 已被占用，请先停止占用进程" % hint}

    log_path = log_path_for(p)
    with open(log_path, "ab") as log_file:
        log_file.write(("\n===== %s 启动 %s =====\n" %
                        (time.strftime("%Y-%m-%d %H:%M:%S"), command)).encode("utf-8"))
        log_file.flush()
        proc = subprocess.Popen(
            ["/bin/zsh"] + shell_args() + [command],
            cwd=path, env=project_process_env(p),
            stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    runtime[p["id"]] = {"pid": proc.pid, "started": time.time()}
    save_runtime(runtime)
    invalidate_ports_cache()
    message = "已启动（PID %s）" % proc.pid
    if env_note:
        message += "；" + env_note
    return {"ok": True, "message": message, "pid": proc.pid}


def stop_project(p):
    runtime = load_runtime()
    entry = runtime.get(p["id"]) if isinstance(runtime.get(p["id"]), dict) else {}
    mpid = entry.get("pid")
    ps = ps_table()
    listeners = list_listeners(force=True)
    cwds = pids_cwd([l["pid"] for l in listeners])
    path = os.path.expanduser(p.get("path") or "")
    me, parent = os.getpid(), os.getppid()

    pids = set()
    if mpid:
        pids |= {pid for pid, info in ps.items() if info["pgid"] == mpid}
        if pid_alive(mpid):
            pids.add(mpid)
    for l in listeners:
        # 不要误杀本面板自身（例如项目目录正好是 aihub 所在目录）
        if l["port"] == PORT or l["pid"] in (me, parent):
            continue
        if is_under(cwds.get(l["pid"], ""), path):
            pids.add(l["pid"])
    pids.discard(me)
    pids.discard(parent)

    if not pids:
        runtime.pop(p["id"], None)
        save_runtime(runtime)
        return {"ok": False, "message": "没有检测到正在运行的进程"}

    # 由本页面启动的项目独占一个进程组，整组结束更彻底
    if mpid and pid_alive(mpid):
        try:
            pgid = os.getpgid(mpid)
            if pgid != os.getpgid(me):
                os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    deadline = time.time() + 5
    while time.time() < deadline and any(pid_alive(x) for x in pids):
        time.sleep(0.25)

    left = [x for x in pids if pid_alive(x)]
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if left:
        time.sleep(0.3)

    runtime.pop(p["id"], None)
    save_runtime(runtime)
    invalidate_ports_cache()
    return {"ok": True, "message": "已停止（%d 个进程）" % len(pids)}


def kill_listener_process(pid):
    """结束单个监听端口的进程。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"ok": False, "message": "无效的 PID"}
    me, parent = os.getpid(), os.getppid()
    if pid <= 1 or pid in (me, parent):
        return {"ok": False, "message": "不能结束本面板或系统进程"}
    targets = [l for l in list_listeners(force=True) if l["pid"] == pid]
    if not targets:
        return {"ok": False, "message": "进程 %d 已不在监听端口" % pid}
    if any(l["port"] == PORT for l in targets):
        return {"ok": False, "message": "不能结束本面板自身"}
    command = targets[0]["command"] or str(pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return {"ok": False, "message": "结束失败：%s" % e}
    deadline = time.time() + 3
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.2)
    if pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(0.2)
    invalidate_ports_cache()
    if pid_alive(pid):
        return {"ok": False, "message": "无法结束 %s（PID %d），可能需要更高权限" % (command, pid)}
    return {"ok": True, "message": "已结束 %s（PID %d），释放端口 %s" % (
        command, pid, ", ".join(":%d" % l["port"] for l in targets))}


def handle_project_action(payload):
    p = project_by_id(payload.get("id"))
    if not p:
        return {"ok": False, "message": "项目不存在"}
    action = payload.get("action")
    try:
        if action == "start":
            return start_project(p)
        if action == "stop":
            return stop_project(p)
        if action == "restart":
            stop_project(p)
            time.sleep(0.8)
            return start_project(p)
        if action == "prepare":
            return prepare_project(p)
    except Exception as exc:
        return {"ok": False, "message": str(exc)[:300]}
    return {"ok": False, "message": "未知操作：%s" % action}


def handle_project_save(payload):
    data = payload.get("project") or {}
    path = normalize_path(data.get("path"))
    if not path:
        return {"ok": False, "message": "请填写项目目录"}
    if not os.path.isdir(path):
        return {"ok": False, "message": "目录不存在：%s（请填写以 / 开头的完整路径）" % path}
    name = (data.get("name") or "").strip() or os.path.basename(path.rstrip("/")) or path
    start = (data.get("start") or "").strip()
    port = clean_port(data.get("port"))
    admin = normalize_admin(data.get("admin"))
    is_new = not data.get("id")
    if not admin and is_new:
        try:
            admins = detect_admin_paths(path)
            if admins:
                admin = admins[0]
        except Exception:
            pass

    projects = load_projects()
    pid = data.get("id")
    for old in projects:
        if old["id"] == pid:
            old.update({"name": name, "path": path, "start": start, "port": port,
                        "admin": admin})
            save_projects(projects)
            return {"ok": True, "message": "已保存", "id": pid}

    new = {"id": "p-" + uuid.uuid4().hex[:8], "name": name, "path": path,
           "start": start, "port": port, "admin": admin, "type": data.get("type") or ""}
    projects.append(new)
    save_projects(projects)
    return {"ok": True, "message": "已添加", "id": new["id"]}


def handle_project_delete(payload):
    pid = payload.get("id")
    if not pid:
        return {"ok": False, "message": "缺少项目 ID"}
    projects = load_projects()
    keep = [p for p in projects if p["id"] != pid]
    if len(keep) == len(projects):
        return {"ok": False, "message": "项目不存在"}
    save_projects(keep)
    runtime = load_runtime()
    runtime.pop(pid, None)
    save_runtime(runtime)
    return {"ok": True, "message": "已删除（运行中的进程未终止）"}


def read_log(p, lines=160):
    path = log_path_for(p)
    if not os.path.exists(path):
        return {"lines": [], "path": path}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 65536))
            text = f.read().decode("utf-8", "replace")
        return {"lines": text.splitlines()[-lines:], "path": path}
    except Exception as exc:
        return {"lines": ["日志读取失败：%s" % exc], "path": path}


# ---------------- 项目扫描 ----------------

SCAN_SKIP = {"node_modules", ".git", ".hg", ".svn", "dist", "build", "target", "out",
             "__pycache__", ".venv", "venv", "env", ".next", ".nuxt", ".turbo",
             ".cache", "coverage", "Pods", "vendor", ".idea", ".vscode", "logs"}


def read_text(path, limit=200000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except Exception:
        return ""


def detect_env_port(d):
    for name in (".env", ".env.local", ".env.development", ".env.dev"):
        m = re.search(r"^\s*(?:PORT|SERVER_PORT|VITE_PORT|APP_PORT)\s*=\s*[\"']?(\d{2,5})",
                      read_text(os.path.join(d, name)), re.M)
        if m:
            return int(m.group(1))
    return None


def detect_spring_port(d):
    for rel in ("src/main/resources/application.properties",
                "src/main/resources/application.yml",
                "src/main/resources/application.yaml"):
        text = read_text(os.path.join(d, rel))
        if not text:
            continue
        m = (re.search(r"server\.port\s*=\s*(\d{2,5})", text)
             or re.search(r"port\s*:\s*(\d{2,5})", text))
        if m:
            return int(m.group(1))
    return None


def detect_node(d):
    try:
        data = json.loads(read_text(os.path.join(d, "package.json")))
    except Exception:
        return None
    scripts = data.get("scripts") or {}
    key = next((k for k in ("dev", "start", "serve", "develop") if k in scripts), None)
    if not key:
        return None

    if os.path.exists(os.path.join(d, "pnpm-lock.yaml")):
        manager = "pnpm"
    elif os.path.exists(os.path.join(d, "yarn.lock")):
        manager = "yarn"
    elif os.path.exists(os.path.join(d, "bun.lockb")) or os.path.exists(os.path.join(d, "bun.lock")):
        manager = "bun"
    else:
        manager = "npm"
    command = "yarn %s" % key if manager == "yarn" else "%s run %s" % (manager, key)

    script = str(scripts.get(key) or "").lower()
    port = None
    if "vite" in script:
        for f in ("vite.config.ts", "vite.config.js", "vite.config.mjs", "vite.config.mts"):
            m = re.search(r"port\s*:\s*(\d{2,5})", read_text(os.path.join(d, f)))
            if m:
                port = int(m.group(1))
                break
        port = port or 5173
    elif "next" in script or "nuxt" in script or "react-scripts" in script:
        port = 3000
    elif "ng serve" in script:
        port = 4200
    if not port:
        for hint in script_port_hints(d, sorted(os.listdir(d)), 3):
            m = re.search(r"(?i)(?:port\s*[=:]\s*[\"']?|listen\(\s*|PORT\s*\|\|\s*)(\d{2,5})", hint)
            if m:
                port = int(m.group(1))
                break
    return {"type": "Node", "start": command, "port": port or detect_env_port(d)}


def detect_project(d, files):
    fs = set(files)
    if "package.json" in fs:
        info = detect_node(d)
        if info:
            return info
    if "pom.xml" in fs:
        text = read_text(os.path.join(d, "pom.xml")).lower()
        if "spring-boot" in text:
            return {"type": "Spring", "start": "mvn spring-boot:run",
                    "port": detect_spring_port(d) or 8080}
    if "build.gradle" in fs or "build.gradle.kts" in fs:
        name = "build.gradle" if "build.gradle" in fs else "build.gradle.kts"
        text = read_text(os.path.join(d, name)).lower()
        if "org.springframework.boot" in text:
            gradlew = "./gradlew" if os.path.exists(os.path.join(d, "gradlew")) else "gradle"
            return {"type": "Spring", "start": gradlew + " bootRun",
                    "port": detect_spring_port(d) or 8080}
    if "manage.py" in fs:
        return {"type": "Django", "start": "python3 manage.py runserver", "port": 8000}
    if "go.mod" in fs:
        return {"type": "Go", "start": "go run .", "port": detect_env_port(d)}
    if "Cargo.toml" in fs:
        return {"type": "Rust", "start": "cargo run", "port": detect_env_port(d)}
    if "composer.json" in fs and "artisan" in fs:
        return {"type": "Laravel", "start": "php artisan serve", "port": 8000}

    if "requirements.txt" in fs:
        req = read_text(os.path.join(d, "requirements.txt")).lower()
        if "fastapi" in req or "uvicorn" in req:
            for rel in ("main.py", "app/main.py", "server.py"):
                if "FastAPI(" in read_text(os.path.join(d, rel)):
                    target = rel[:-3].replace("/", ".") + ":app"
                    return {"type": "FastAPI",
                            "start": "python3 -m uvicorn %s --reload" % target, "port": 8000}
        if "flask" in req:
            for name in [f for f in files if f.endswith(".py")][:30]:
                if "Flask(" in read_text(os.path.join(d, name)):
                    return {"type": "Flask", "start": "python3 %s" % name, "port": 5000}
        if "streamlit" in req and os.path.exists(os.path.join(d, "app.py")):
            return {"type": "Streamlit", "start": "streamlit run app.py", "port": 8501}

    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        if name in fs:
            if not shutil.which("docker"):
                return None
            m = re.search(r"-\s*[\"']?(\d{2,5}):\d{2,5}", read_text(os.path.join(d, name)))
            return {"type": "Docker", "start": "docker compose up",
                    "port": int(m.group(1)) if m else None}

    # 没有 package.json 的单文件 Node 服务（server.js / app.js）
    for name in ("server.js", "app.js", "index.js", "main.js"):
        if name in fs:
            text = read_text(os.path.join(d, name), 200000)
            if "listen(" in text or "createServer" in text:
                port = detect_env_port(d)
                if not port:
                    m = re.search(
                        r"(?:\.listen\(\s*|PORT[^\n]{0,40}\|\|\s*)(\d{2,5})", text)
                    port = int(m.group(1)) if m else None
                return {"type": "Node", "start": "node " + name, "port": port}
    return None


# ---------------- 项目环境检测 ----------------

_ENV_REQ_CACHE = {}
_LOCAL_VER_CACHE = {"at": 0.0, "data": {}}
_FNM_VERS_CACHE = {"at": 0.0, "data": []}
_VERSION_CMD = {
    "node": ["node", "-v"], "npm": ["npm", "-v"], "pnpm": ["pnpm", "-v"],
    "yarn": ["yarn", "-v"], "bun": ["bun", "-v"], "python3": ["python3", "--version"],
    "java": ["java", "-version"], "go": ["go", "version"], "rustc": ["rustc", "--version"],
}


def _ver_tuple(text):
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(text or ""))
    return tuple(int(x or 0) for x in m.groups()) if m else None


def _ver_cmp(cur, base, op):
    if op == ">":
        return cur > base
    if op == ">=":
        return cur >= base
    if op == "<":
        return cur < base
    if op == "<=":
        return cur <= base
    if op == "^":
        if cur < base:
            return False
        if base[0] > 0:
            return cur[0] == base[0]
        if base[1] > 0:
            return cur[0] == 0 and cur[1] == base[1]
        return cur[:2] == base[:2]
    if op == "~":
        return cur >= base and cur[0] == base[0] and cur[1] == base[1]
    return cur == base


def version_satisfies(version, spec):
    """判断版本是否满足 engines / packageManager 约束，无法判断时返回 None。"""
    cur = _ver_tuple(version)
    if not cur or not str(spec or "").strip():
        return None
    for alt in re.split(r"\|\|", str(spec)):
        tokens = re.findall(r"(\^|~|>=|<=|>|<|=)?\s*(\d+(?:\.\d+){0,2})", alt)
        if not tokens:
            continue
        ok = True
        for op, num in tokens:
            base = _ver_tuple(num)
            if not op or op == "=":
                # 只写到主版本/次版本时按前缀匹配，例如 .nvmrc 里写 18 或 18.20
                parts = num.count(".") + 1
                ok = cur[:parts] == base[:parts] if parts < 3 else cur == base
            else:
                ok = _ver_cmp(cur, base, op)
            if not ok:
                break
        if ok:
            return True
    return False


def local_versions(force=False):
    now = time.time()
    if not force and now - _LOCAL_VER_CACHE["at"] < 60:
        return dict(_LOCAL_VER_CACHE["data"])
    data = {}
    path_env = ENV.get("PATH")
    for name, argv in _VERSION_CMD.items():
        if not shutil.which(argv[0], path=path_env):
            data[name] = ""
            continue
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=3, env=ENV)
            # java -version 输出到 stderr，其余多数在 stdout，合并后再匹配
            out = (proc.stdout or "") + "\n" + (proc.stderr or "")
            m = re.search(r"\d+(?:\.\d+){1,2}", out)
            data[name] = m.group(0) if m else ""
        except Exception:
            data[name] = ""
    data["corepack"] = shutil.which("corepack", path=path_env) or ""
    data["fnm"] = shutil.which("fnm", path=path_env) or ""
    _LOCAL_VER_CACHE.update({"at": now, "data": data})
    return dict(data)


def project_env_requirements(d):
    """读取项目声明的 Node/npm/Python 版本约束（只读文件，不执行命令）。"""
    cached = _ENV_REQ_CACHE.get(d)
    if cached and time.time() - cached[0] < 30:
        return cached[1]

    pkg_file = os.path.join(d, "package.json")
    pkg = {}
    if os.path.exists(pkg_file):
        try:
            pkg = json.loads(read_text(pkg_file))
        except Exception:
            pkg = {}
    engines = pkg.get("engines") if isinstance(pkg.get("engines"), dict) else {}

    req = {"node": {}, "packageManager": {}, "npm": {}, "python": {},
           "java": {}, "go": {}, "rust": {}}
    node_spec = str(engines.get("node") or "").strip()
    node_src = "engines.node" if node_spec else ""
    for name in (".nvmrc", ".node-version"):
        text = read_text(os.path.join(d, name)).strip()
        if text and not text.startswith("#"):
            node_spec, node_src = text, name
            break
    if node_spec:
        req["node"] = {"spec": node_spec, "source": node_src}

    pm = str(pkg.get("packageManager") or "").strip()
    if "@" in pm:
        name, _, ver = pm.partition("@")
        if name in ("npm", "pnpm", "yarn", "bun") and ver:
            req["packageManager"] = {"name": name, "version": ver}
    npm_spec = str(engines.get("npm") or "").strip()
    if npm_spec:
        req["npm"] = {"spec": npm_spec}

    py_spec = read_text(os.path.join(d, ".python-version")).strip()
    py_src = ".python-version" if py_spec else ""
    if not py_spec:
        m = re.search(r'requires-python\s*=\s*"([^"]+)"',
                      read_text(os.path.join(d, "pyproject.toml"), 20000))
        if m:
            py_spec, py_src = m.group(1).strip(), "pyproject.toml"
    if py_spec:
        req["python"] = {"spec": py_spec, "source": py_src}

    # Java：pom.xml / build.gradle / 版本管理器声明文件
    java_spec, java_src = "", ""
    pom = read_text(os.path.join(d, "pom.xml"), 20000)
    for tag in ("maven.compiler.release", "java.version", "maven.compiler.source", "maven.compiler.target"):
        m = re.search(r"<%s>\s*([^<\s]+)" % re.escape(tag), pom)
        if m:
            java_spec, java_src = m.group(1).strip(), "pom.xml"
            break
    if not java_spec:
        for gname in ("build.gradle", "build.gradle.kts"):
            gtext = read_text(os.path.join(d, gname), 20000)
            m = re.search(r"sourceCompatibility\s*=\s*['\"]?(?:JavaVersion\.VERSION_)?(\d+(?:_\d+)?)", gtext)
            if not m:
                m = re.search(r"JavaLanguageVersion\.of\(\s*(\d+)", gtext)
            if m:
                java_spec, java_src = m.group(1).strip().replace("_", "."), gname
                break
    if not java_spec:
        for vname in (".sdkmanrc", ".jenv-version", ".tool-versions"):
            vtext = read_text(os.path.join(d, vname)).strip()
            if vname == ".tool-versions":
                m = re.search(r"(?m)^\s*java\s+([^\s#]+)", vtext)
            elif vname == ".sdkmanrc":
                m = re.search(r"(?m)^\s*java\s*=\s*([^\s#]+)", vtext)
            else:
                m = re.match(r"^([^\s#]+)", vtext)
            if m:
                java_spec, java_src = m.group(1).strip(), vname
                break
    if java_spec:
        req["java"] = {"spec": java_spec, "source": java_src}

    # Go：go.mod 的 go / toolchain 指令
    gomod = read_text(os.path.join(d, "go.mod"), 20000)
    m = re.search(r"(?m)^\s*go\s+(\d+(?:\.\d+)*)", gomod)
    if m:
        req["go"] = {"spec": m.group(1).strip(), "source": "go.mod"}
    else:
        m = re.search(r"(?m)^\s*toolchain\s+go(\d+(?:\.\d+)*)", gomod)
        if m:
            req["go"] = {"spec": m.group(1).strip(), "source": "go.mod"}

    # Rust：rust-toolchain.toml / rust-toolchain
    rust_spec, rust_src = "", ""
    rtoml = read_text(os.path.join(d, "rust-toolchain.toml"), 20000)
    m = re.search(r"(?m)^\s*channel\s*=\s*['\"]([^'\"]+)", rtoml)
    if m:
        rust_spec, rust_src = m.group(1).strip(), "rust-toolchain.toml"
    if not rust_spec:
        rplain = read_text(os.path.join(d, "rust-toolchain")).strip()
        if rplain and not rplain.startswith("["):
            rust_spec, rust_src = rplain, "rust-toolchain"
    if rust_spec:
        req["rust"] = {"spec": rust_spec, "source": rust_src}

    npmrc = read_text(os.path.join(d, ".npmrc"), 20000)
    info = {"requirements": req,
            "engineStrict": bool(re.search(r"(?m)^\s*engine-strict\s*=\s*true", npmrc))}
    _ENV_REQ_CACHE[d] = (time.time(), info)
    return info


def fnm_versions():
    """列出 fnm 已安装的 Node 版本，用于判断能否自动切换。"""
    if time.time() - _FNM_VERS_CACHE["at"] < 60:
        return list(_FNM_VERS_CACHE["data"])
    fnm = local_versions().get("fnm")
    versions = []
    if fnm:
        try:
            out = subprocess.run([fnm, "ls"], capture_output=True, text=True,
                                 timeout=5, env=ENV).stdout
            versions = re.findall(r"v(\d+\.\d+\.\d+)", out)
        except Exception:
            versions = []
    _FNM_VERS_CACHE.update({"at": time.time(), "data": versions})
    return list(versions)


def fnm_has_node(spec):
    return bool(spec) and any(version_satisfies(v, spec) is True for v in fnm_versions())


def load_env_prefs():
    data = load_json_file(ENV_FILE, {})
    return data if isinstance(data, dict) else {}


def save_node_pref(path, version):
    prefs = load_env_prefs()
    prefs[os.path.realpath(path)] = {"node": version}
    write_json_file(ENV_FILE, prefs)


def fnm_retry_candidates(current_command):
    """EBADENGINE 时可选的重试版本：排除当前大版本，优先新的。"""
    cur_major = (_ver_tuple(local_versions().get("node")) or (0,))[0]
    versions = []
    for version in fnm_versions():
        ver = _ver_tuple(version)
        if not ver or ver[0] == cur_major:
            continue
        if "fnm exec --using=%s " % version in current_command:
            continue
        versions.append((ver, version))
    versions.sort(reverse=True)
    return [v for _, v in versions[:2]]


def env_report(d):
    """返回项目环境要求、本机版本以及不匹配项，供识别结果和卡片展示。"""
    info = project_env_requirements(d)
    req = info["requirements"]
    local = local_versions()
    issues = []

    pm = req.get("packageManager") or {}
    pm_name, pm_ver = pm.get("name"), pm.get("version")
    if pm_name and pm_ver:
        cur = local.get(pm_name) or ""
        if version_satisfies(cur, pm_ver) is False:
            fix = "，启动时自动用 corepack 切换" if local.get("corepack") else ""
            issues.append("需要 %s %s（当前 %s）%s" % (pm_name, pm_ver, cur or "未安装", fix))
    elif (req.get("npm") or {}).get("spec"):
        spec = req["npm"]["spec"]
        cur = local.get("npm") or ""
        if version_satisfies(cur, spec) is False:
            issues.append("需要 npm %s（当前 %s）" % (spec, cur or "未安装"))

    node = req.get("node") or {}
    if node.get("spec"):
        cur = local.get("node") or ""
        if version_satisfies(cur, node["spec"]) is False:
            if local.get("fnm") and fnm_has_node(node["spec"]):
                hint = "，启动时自动用 fnm 切换"
            elif local.get("fnm"):
                hint = "，可运行 fnm install %s 后自动切换" % node["spec"]
            else:
                hint = "，可安装 fnm 后自动切换"
            issues.append("需要 Node %s（当前 %s）%s" % (node["spec"], cur or "未安装", hint))

    py = req.get("python") or {}
    if py.get("spec"):
        cur = local.get("python3") or ""
        if version_satisfies(cur, py["spec"]) is False:
            issues.append("需要 Python %s（当前 %s）" % (py["spec"], cur or "未安装"))

    _ENV_LABELS = (("java", "JDK", "java"), ("go", "Go", "go"), ("rust", "Rust", "rustc"))
    for key, label, local_key in _ENV_LABELS:
        item = req.get(key) or {}
        if not item.get("spec"):
            continue
        cur = local.get(local_key) or ""
        if version_satisfies(cur, item["spec"]) is False:
            src = "（声明来自 %s）" % item.get("source") if item.get("source") else ""
            issues.append("需要 %s %s（当前 %s）%s" %
                          (label, item["spec"], cur or "未安装", src))

    return {"requirements": req,
            "current": {k: local.get(k, "") for k in
                        ("node", "npm", "pnpm", "yarn", "python3", "java", "go", "rustc")},
            "issues": issues,
            "fixes": env_fix_suggestions(req, local),
            "ignored": env_ignored(d),
            "engineStrict": info.get("engineStrict")}


def env_fix_suggestions(req, local):
    """根据环境落差生成可操作的修复建议。

    每个 fix 包含：
      - detected: 本机探测到的工具（向用户明示"发现了什么"）
      - options:  所有可选方案，每项 {tool, provider, command, note, available, recommended}
      - command/note/provider: 当前推荐方案的快捷字段（兼容旧前端）
    原则：把"探测结果"和"所有选项"都摆出来，让用户知情并可纠正。
    """
    fixes = []

    def add(kind, label, version, options, detected=""):
        # options: list of (tool, provider, command, note, available)
        opts = []
        for tool, provider, command, note, available in options:
            opts.append({"tool": tool, "provider": provider, "command": command,
                         "note": note, "available": bool(available)})
        recommended_idx = 0
        for i, o in enumerate(opts):
            if o["available"]:
                recommended_idx = i
                break
        for i, o in enumerate(opts):
            o["recommended"] = (i == recommended_idx)
        best = opts[recommended_idx]
        fixes.append({"kind": kind, "label": label, "version": version,
                      "detected": detected,
                      "command": best["command"], "note": best["note"],
                      "provider": best["provider"], "autoInstall": False,
                      "options": opts})

    pm = req.get("packageManager") or {}
    if pm.get("name") and pm.get("version") and \
            version_satisfies(local.get(pm["name"]), pm["version"]) is False:
        has_cp = bool(local.get("corepack"))
        add("node", "包管理器", pm["version"], [
            ("corepack", "corepack",
             "corepack prepare %s@%s --activate" % (pm["name"], pm["version"]),
             "本机已有 corepack，可直接锁定项目要求的版本", has_cp),
            ("手动启用", "", "npm i -g corepack && corepack enable",
             "启用 corepack 后项目会自动使用锁定的包管理器版本", True),
        ], detected="corepack" if has_cp else "")

    node = req.get("node") or {}
    if node.get("spec") and version_satisfies(local.get("node"), node["spec"]) is False:
        ver = node["spec"].lstrip("v")
        has_fnm = bool(local.get("fnm"))
        add("node", "Node.js", ver, [
            ("fnm", "fnm", "fnm install %s && fnm use %s" % (ver, ver),
             "本机已有 fnm，安装后项目会自动切换到该版本", has_fnm),
            ("Homebrew", "", "brew install fnm && fnm install %s" % ver,
             "先装 fnm 版本管理器，再用它安装 Node %s" % ver, True),
        ], detected="fnm" if has_fnm else "")

    py = req.get("python") or {}
    if py.get("spec") and version_satisfies(local.get("python3"), py["spec"]) is False:
        ver = py["spec"].lstrip("v")
        has_pyenv = bool(_tool_path("pyenv"))
        add("python", "Python", ver, [
            ("pyenv", "pyenv", "pyenv install %s && pyenv local %s" % (ver, ver),
             "本机已有 pyenv，项目内锁定版本", has_pyenv),
            ("Homebrew", "", "brew install pyenv && pyenv install %s" % ver,
             "先装 pyenv，再用它安装 Python %s" % ver, True),
        ], detected="pyenv" if has_pyenv else "")

    # Java（用户最关心）：SDKMAN / jenv / 直接安装 三种方案全列出
    java = req.get("java") or {}
    if java.get("spec") and version_satisfies(local.get("java"), java["spec"]) is False:
        ver = java["spec"]
        major = "".join(ch for ch in ver if ch.isdigit())[:2] or ver
        has_sdk = bool(_tool_path("sdk"))
        has_jenv = bool(_tool_path("jenv"))
        detected = "SDKMAN" if has_sdk else ("jenv" if has_jenv else "")
        add("java", "JDK", ver, [
            ("SDKMAN", "sdkman", "sdk install java %s" % ver,
             "用 SDKMAN 安装，可在项目内切换，不污染全局", has_sdk),
            ("jenv", "jenv",
             "brew install openjdk@%s && jenv add /Library/Java/JavaVirtualMachines/openjdk-%s.jdk/Contents/Home" % (major, major),
             "用 jenv 管理已安装的 JDK", has_jenv),
            ("直接安装", "", "brew install openjdk@%s" % major,
             "用 Homebrew 装到系统，不提供项目级切换", True),
        ], detected=detected)

    # Go
    go = req.get("go") or {}
    if go.get("spec") and version_satisfies(local.get("go"), go["spec"]) is False:
        ver = go["spec"]
        has_goenv = bool(_tool_path("goenv"))
        add("go", "Go", ver, [
            ("goenv", "goenv", "goenv install %s && goenv local %s" % (ver, ver),
             "本机已有 goenv，项目内自动切换", has_goenv),
            ("Homebrew", "", "brew install go@%s" % ver,
             "用 Homebrew 安装 Go %s" % ver, True),
        ], detected="goenv" if has_goenv else "")

    # Rust
    rust = req.get("rust") or {}
    if rust.get("spec") and version_satisfies(local.get("rustc"), rust["spec"]) is False:
        ver = rust["spec"]
        has_rustup = bool(_tool_path("rustup"))
        add("rust", "Rust", ver, [
            ("rustup", "rustup", "rustup toolchain install %s" % ver,
             "本机已有 rustup，安装后项目自动使用该工具链", has_rustup),
            ("安装 rustup", "rustup",
             "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh",
             "先装 rustup，它会自动读取 rust-toolchain 文件", True),
        ], detected="rustup" if has_rustup else "")

    return fixes


def _prefix_pm(command, name):
    """把命令里每个 npm/pnpm/yarn 调用改成 corepack 前缀，复用项目锁定的版本。"""
    pattern = re.compile(
        r"(^|&&|\|\||;|\|)(\s*(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S+)\s+)*)"
        r"(" + re.escape(name) + r")(?=\s)")
    return pattern.sub(lambda m: m.group(1) + m.group(2) + "corepack " + m.group(3), command)


def _fnm_wrap(command, version=None):
    """用 fnm 指定的 Node 执行整条命令（含 && 串联时整体生效）。"""
    use = ("--using=%s " % version) if version else ""
    return "fnm exec %s-- /bin/zsh -c %s" % (use, shlex.quote(command))


def wrap_command(command, directory):
    """按项目声明自动套用 corepack / fnm，返回 (实际执行命令, 说明)。"""
    cmd = (command or "").strip()
    if not cmd:
        return cmd, ""
    req = project_env_requirements(directory)["requirements"]
    local = local_versions()
    notes = []

    pm = req.get("packageManager") or {}
    name, ver = pm.get("name"), pm.get("version")
    if name in ("npm", "pnpm", "yarn") and ver and not re.search(r"\bcorepack\b", cmd):
        if version_satisfies(local.get(name), ver) is False and local.get("corepack"):
            wrapped = _prefix_pm(cmd, name)
            if wrapped != cmd:
                cmd = wrapped
                notes.append("corepack %s@%s" % (name, ver))

    node = req.get("node") or {}
    pref = (load_env_prefs().get(os.path.realpath(directory)) or {}).get("node")
    if pref and pref in fnm_versions() and not re.search(r"\bfnm exec\b", cmd) and \
            (_ver_tuple(pref) or (0, 0, 0)) != (_ver_tuple(local.get("node")) or (0, 0, 0)):
        cmd = _fnm_wrap(cmd, pref)
        notes.append("fnm Node %s" % pref)
    pin_file = node.get("source") in (".nvmrc", ".node-version")
    if pin_file and not re.search(r"\bfnm exec\b", cmd) and \
            version_satisfies(local.get("node"), node.get("spec")) is False:
        if local.get("fnm") and fnm_has_node(node.get("spec")) and \
                not re.match(r"^\s*fnm\b", cmd):
            cmd = _fnm_wrap(cmd)
            notes.append("fnm Node %s" % node.get("spec"))

    return cmd, ("已自动使用 " + "、".join(notes) if notes else "")


def detect_admin_paths(root, max_files=120):
    """从目录结构和路由字面量中识别后台入口，返回形如 ['/admin'] 的候选。"""
    found = []

    def add(p):
        p = "/" + str(p).strip("/")
        if 2 <= len(p) <= 40 and p not in found:
            found.append(p)

    for rel in ("admin", "pages/admin", "src/pages/admin", "src/views/admin",
                "views/admin", "app/admin", "src/app/admin", "public/admin",
                "src/routes/admin", "routes/admin", "static/admin",
                "admin.html", "public/admin.html", "src/admin.html",
                "admin/index.html", "public/admin/index.html"):
        if os.path.exists(os.path.join(root, rel)):
            add("/admin")
            break

    pattern = re.compile(
        r"[\"'](/(?:admin|dashboard|manage|console|backstage)(?:[a-zA-Z0-9_/-]*))[\"']")
    scanned = 0
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SCAN_SKIP]
        if os.path.relpath(cur, root).count(os.sep) >= 4:
            dirs[:] = []
        for name in files:
            if not name.endswith((".js", ".ts", ".jsx", ".tsx", ".vue", ".py", ".go", ".java")):
                continue
            scanned += 1
            if scanned > max_files:
                return found[:5]
            text = read_text(os.path.join(cur, name), 60000)
            low = text.lower()
            if not any(k in low for k in ("admin", "dashboard", "manage", "console", "backstage")):
                continue
            for m in pattern.finditer(text):
                add(m.group(1))
            if len(found) >= 5:
                return found[:5]
    return found[:5]


def admin_reachable(port, path):
    """确认后台路径在该端口上真实存在（404 视为不存在）。"""
    for host in ("127.0.0.1", "[::1]"):
        url = "http://%s:%d%s" % (host, port, path)
        try:
            with urlopen(Request(url, method="GET"), timeout=2.5) as resp:
                if resp.status < 500:
                    return True
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code and code != 404:
                return True
    return False


def scan_projects(roots, depth=3, limit=300):
    seen, found = set(), []
    for raw in roots:
        root = normalize_path(raw)
        if not root or not os.path.isdir(root):
            continue
        base = root.rstrip(os.sep)
        for cur, dirs, files in os.walk(root):
            level = cur.rstrip(os.sep).count(os.sep) - base.count(os.sep)
            if level >= depth:
                dirs[:] = []
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in SCAN_SKIP]
            info = detect_project(cur, files)
            if not info:
                continue
            real = os.path.realpath(cur)
            if real in seen:
                continue
            seen.add(real)
            item = {"name": os.path.basename(cur) or cur, "path": cur, **info}
            admins = detect_admin_paths(cur, max_files=40)
            if admins:
                item["admin"] = admins[0]
            item["env"] = env_report(cur)
            found.append(item)
            if len(found) >= limit:
                return found
    return found


def handle_project_scan(payload):
    roots = payload.get("roots")
    if isinstance(roots, str):
        roots = roots.splitlines()
    roots = [r for r in (roots or []) if str(r).strip()]
    if not roots:
        roots = DEFAULT_SCAN_ROOTS
    try:
        depth = max(1, min(6, int(payload.get("depth") or 3)))
    except (TypeError, ValueError):
        depth = 3
    try:
        found = scan_projects(roots, depth)
    except Exception as exc:
        return {"ok": False, "message": str(exc), "projects": []}
    existing = {os.path.realpath(normalize_path(p["path"])) for p in load_projects()}
    for item in found:
        item["exists"] = os.path.realpath(item["path"]) in existing
    return {"ok": True, "projects": found}


def handle_project_import(payload):
    items = payload.get("items") or []
    projects = load_projects()
    existing = {os.path.realpath(normalize_path(p["path"])) for p in projects}
    added = 0
    for it in items:
        path = normalize_path(it.get("path"))
        if not path or not os.path.isdir(path):
            continue
        real = os.path.realpath(path)
        if real in existing:
            continue
        projects.append({
            "id": "p-" + uuid.uuid4().hex[:8],
            "name": (it.get("name") or "").strip() or os.path.basename(path.rstrip("/")),
            "path": path,
            "start": (it.get("start") or "").strip(),
            "port": clean_port(it.get("port")),
            "admin": normalize_admin(it.get("admin")),
            "type": it.get("type") or "",
        })
        existing.add(real)
        added += 1
    save_projects(projects)
    return {"ok": True, "message": "已添加 %d 个项目" % added, "added": added}


# ---------------- GitHub 导入 ----------------

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
GITHUB_CDN = "https://cdn.jsdelivr.net/gh"
GITHUB_UA = "AIHub/1.0"
GITHUB_FILE_LIMIT = 200 * 1024
GITHUB_FILES = ("README.md", "README_zh.md", "README.rst", "readme.md",
                "pyproject.toml", "setup.py", "requirements.txt", "package.json",
                "Dockerfile", "Makefile", "install.sh",
                ".python-version", ".nvmrc", ".node-version")
_GH_TOKEN = {"loaded": False, "value": ""}
_GH_JOBS = {}
_GH_JOBS_LOCK = threading.Lock()
_PY_CANDS = {"at": 0.0, "items": []}


def parse_github_url(raw):
    """支持完整 URL、git@ 形式、tree 分支路径和 owner/repo 简写。"""
    text = str(raw or "").strip()
    if not text:
        return None
    text = re.sub(r"[?#].*$", "", text).rstrip("/")
    text = re.sub(r"^git@github\.com:", "", text)
    text = re.sub(r"^(?:https?://)?(?:www\.)?github\.com/", "", text)
    text = re.sub(r"\.git$", "", text)
    parts = [p for p in text.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        return None
    ref = parts[3] if len(parts) >= 4 and parts[2] == "tree" else ""
    return {"owner": owner, "repo": repo, "ref": ref,
            "slug": "%s/%s" % (owner, repo),
            "url": "https://github.com/%s/%s" % (owner, repo)}


def default_github_dir(parsed):
    return os.path.join(HOME, "Projects", "%s__%s" % (parsed["owner"], parsed["repo"]))


def github_token():
    """优先环境变量，其次 gh 的登录态；只在本进程内使用，不落盘。"""
    if _GH_TOKEN["loaded"]:
        return _GH_TOKEN["value"]
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not token and shutil.which("gh", path=ENV.get("PATH")):
        token = exec_capture(["gh", "auth", "token"], timeout=6).strip()
    _GH_TOKEN.update({"loaded": True, "value": token})
    return token


def github_api(path, timeout=15):
    headers = {"User-Agent": GITHUB_UA, "Accept": "application/vnd.github+json"}
    token = github_token()
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urlopen(Request(GITHUB_API + path, headers=headers), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError("仓库不存在或没有访问权限（私有仓库请先 gh auth login）")
        if exc.code in (403, 429):
            raise RuntimeError("GitHub API 已限流，配置 GITHUB_TOKEN 或稍后再试")
        raise RuntimeError("GitHub 返回 %s" % exc.code)
    except Exception as exc:
        raise RuntimeError("无法访问 GitHub：%s" % str(exc)[:120])


def _http_text(url, timeout, token="", budget=10, partial_ok=False):
    """带总时长预算的读取：慢速链路上避免单个文件卡住整次分析。"""
    headers = {"User-Agent": GITHUB_UA}
    if token:
        headers["Authorization"] = "Bearer " + token
    start = time.time()
    buf = []
    size = 0
    with urlopen(Request(url, headers=headers), timeout=timeout) as resp:
        while True:
            chunk = resp.read(16384)
            if not chunk:
                break
            buf.append(chunk)
            size += len(chunk)
            if size > GITHUB_FILE_LIMIT:
                return ""
            if time.time() - start > budget:
                if not partial_ok:
                    return ""
                break
    text = b"".join(buf).decode("utf-8", "ignore")
    if text.count("```") % 2:
        text += "\n```\n"
    return text


def github_raw(owner, repo, ref, name, timeout=8, cdn=True):
    """优先 jsDelivr（快且稳），私有仓库或 CDN 不可用时回源 raw。"""
    sources = []
    if cdn:
        sources.append(("%s/%s/%s@%s/%s" % (GITHUB_CDN, owner, repo, quote(ref), name), ""))
    sources.append(("%s/%s/%s/%s/%s" % (GITHUB_RAW, owner, repo, quote(ref), name),
                    github_token()))
    partial_ok = name.lower().startswith("readme")
    for url, token in sources:
        for attempt in range(2):
            try:
                return _http_text(url, timeout, token, partial_ok=partial_ok)
            except HTTPError as exc:
                if exc.code == 404:
                    return ""
                if exc.code in (403, 429, 451):
                    break
            except Exception:
                pass
            time.sleep(0.15)
    return ""


def github_collect(parsed):
    """只读仓库：拿元信息与关键文件，不落盘。"""
    owner, repo = parsed["owner"], parsed["repo"]
    meta, warnings = {}, []
    try:
        meta = github_api("/repos/%s/%s" % (owner, repo))
    except RuntimeError as exc:
        warnings.append(str(exc))
    ref = parsed.get("ref") or meta.get("default_branch") or "main"

    files = {}
    use_cdn = not bool(meta.get("private"))

    def grab(name):
        text = github_raw(owner, repo, ref, name, cdn=use_cdn)
        if text:
            files[name] = text

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(grab, GITHUB_FILES))

    if not any(k.lower().startswith("readme") for k in files) and not parsed.get("ref"):
        for alt in ("master", "main"):
            if alt == ref:
                continue
            text = github_raw(owner, repo, alt, "README.md", cdn=use_cdn)
            if text:
                files["README.md"] = text
                break
    if not files and not meta:
        raise RuntimeError(warnings[0] if warnings else "仓库里没有可读取的文件")
    return meta, files, ref, warnings


_GH_CACHE = {}


def github_collect_cached(parsed):
    """分析结果缓存 5 分钟：导入时不必再把同样的文件重拉一遍。"""
    key = (parsed["owner"], parsed["repo"], parsed.get("ref") or "")
    hit = _GH_CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    data = github_collect(parsed)
    _GH_CACHE[key] = (time.time(), data)
    return data


def github_readme(files):
    for name in ("README.md", "README_zh.md", "readme.md", "README.rst"):
        if files.get(name):
            return files[name]
    return ""


def readme_commands(text, limit=20):
    """从 README 的代码块里提取命令行候选。"""
    out = []
    for block in re.findall(r"```[A-Za-z0-9_+.-]*\s*\n(.*?)```", text or "", re.S):
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("$"):
                line = line.lstrip("$").strip()
            line = re.sub(r"\s+#.*$", "", line).strip()
            if not line or line.startswith(("#", "//")) or len(line) > 160:
                continue
            if not re.match(r"^[A-Za-z0-9_./-]+(\s|$)", line):
                continue
            if line not in out:
                out.append(line)
            if len(out) >= limit:
                return out
    return out


def _pyproject_scripts(text):
    scripts = {}
    for header in ("project.scripts", "tool.poetry.scripts"):
        m = re.search(r"(?ms)^\[%s\](.*?)(?=^\[|\Z)" % re.escape(header), text)
        if not m:
            continue
        for line in m.group(1).splitlines():
            line = line.split("#", 1)[0].strip()
            mm = (re.match(r'([\w.-]+)\s*=\s*"([^"]+)"', line)
                  or re.match(r'([\w.-]+)\s*=\s*\{[^}]*callable\s*=\s*"([^"]+)"', line))
            if mm:
                scripts.setdefault(mm.group(1), mm.group(2))
    return scripts


def _pkg_json(text):
    try:
        data = json.loads(text) if (text or "").strip() else {}
    except Exception:
        return {}, {}, ""
    if not isinstance(data, dict):
        return {}, {}, ""
    bins = data.get("bin") or {}
    if isinstance(bins, str):
        bins = {str(data.get("name") or "cli"): bins}
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    return ({k: v for k, v in bins.items() if isinstance(v, str)},
            {k: v for k, v in scripts.items() if isinstance(v, str)},
            str(data.get("packageManager") or ""))


def python_candidates():
    """本机可用的 Python 解释器，按版本从高到低。"""
    if time.time() - _PY_CANDS["at"] < 120:
        return list(_PY_CANDS["items"])
    seen, items = set(), []
    for name in ("python3.13", "python3.12", "python3.11", "python3.10", "python3", "python"):
        path = shutil.which(name, path=ENV.get("PATH"))
        if not path:
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True,
                                 timeout=4, env=ENV).stdout
        except Exception:
            continue
        m = re.search(r"(\d+\.\d+\.\d+)", out)
        if m:
            items.append({"name": name, "path": path, "version": m.group(1)})
    items.sort(key=lambda x: _ver_tuple(x["version"]) or (0,), reverse=True)
    _PY_CANDS.update({"at": time.time(), "items": items})
    return list(items)


def pick_python(spec):
    cands = python_candidates()
    if spec:
        for c in cands:
            if version_satisfies(c["version"], spec):
                return c
    return cands[0] if cands else None


def _primary_name(candidates, *preferred):
    """优先选与包名/仓库名一致的入口，避免像 copaw 这种别名抢在前面。"""
    names = sorted(candidates or [])
    if not names:
        return ""
    norm = lambda s: re.sub(r"[-_.]+", "-", str(s or "").lower())
    for target in preferred:
        for name in names:
            if target and norm(name) == norm(target):
                return name
    return names[0]


def build_github_plan(parsed, meta, files, ref):
    """把 README 与配置文件归纳成“环境准备 / 一次性初始化 / 启动”三段。"""
    pyproject = files.get("pyproject.toml") or ""
    pkg_text = files.get("package.json") or ""
    req_text = files.get("requirements.txt") or ""
    readme = github_readme(files)
    venv_bin = ".venv/bin"

    py_name, py_spec, scripts = "", "", {}
    if pyproject:
        m = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"', pyproject)
        py_name = m.group(1) if m else ""
        m = re.search(r'requires-python\s*=\s*"([^"]+)"', pyproject)
        py_spec = m.group(1).strip() if m else ""
        scripts = _pyproject_scripts(pyproject)
    if not py_spec:
        py_spec = (files.get(".python-version") or "").strip()

    bins, node_scripts, pm = _pkg_json(pkg_text)
    is_py = bool(pyproject or req_text or files.get("setup.py"))
    is_node = bool(pkg_text)
    entry_name = (_primary_name(scripts, py_name, parsed["repo"]) if scripts
                  else _primary_name(bins, parsed["repo"]))
    entry = ("%s/%s" % (venv_bin, entry_name)) if (entry_name and is_py) else ""

    setup, init, start, warnings, notes = [], [], "", [], []
    if is_py:
        if pyproject:
            setup.append("%s/python -m pip install -e ." % venv_bin)
        else:
            setup.append("%s/python -m pip install -r requirements.txt" % venv_bin)

    pm_name = pm.split("@")[0] if "@" in pm else "npm"
    node_manager = pm_name if pm_name in ("npm", "pnpm", "yarn", "bun") else "npm"
    if is_node and not is_py:
        setup.append("%s install" % node_manager)

    # README 里同一入口的子命令：init 类只跑一次，启动类取第一个
    if entry:
        base = os.path.basename(entry).lower()
        for cmd in readme_commands(readme):
            low = cmd.lower()
            if not low.startswith(base):
                continue
            rest = cmd[len(base):].strip()
            sub = (rest.split() or [""])[0].lower()
            if not sub:
                continue
            renamed = entry + " " + rest
            if sub in ("init", "setup", "config", "configure", "bootstrap"):
                if renamed not in init:
                    init.append(renamed)
            elif sub in ("app", "start", "serve", "web", "ui", "run", "server", "gui", "console"):
                if not start:
                    start = renamed

    if not start and is_node and node_scripts:
        key = next((k for k in ("dev", "start", "serve", "develop") if k in node_scripts), "")
        if key:
            start = ("%s %s" % (node_manager, key) if key == "start"
                     else "%s run %s" % (node_manager, key))
    if not start and entry:
        start = entry

    port = None
    m = re.search(r"(?:127\.0\.0\.1|localhost):(\d{2,5})", readme)
    if m:
        port = clean_port(m.group(1))

    if is_py and is_node:
        kind = "mixed"
        notes.append("仓库里同时有 Node 工程，若启动依赖前端资源，请按 README 先构建")
    elif is_node:
        kind = "web"
    elif is_py:
        kind = "web" if port else "cli"
    else:
        kind = "unknown"

    pick = pick_python(py_spec) if is_py else None
    py_ok = None
    if is_py and py_spec and pick:
        py_ok = version_satisfies(pick["version"], py_spec)
        if py_ok is False:
            warnings.append("本机没有满足 %s 的 Python，建议先装对应版本" % py_spec)
    elif is_py and not pick:
        warnings.append("本机未找到 Python 解释器")

    engines = {}
    try:
        data = json.loads(pkg_text) if pkg_text else {}
        if isinstance(data, dict) and isinstance(data.get("engines"), dict):
            engines = data["engines"]
    except Exception:
        engines = {}
    node_spec = str(engines.get("node") or "").strip()
    for name in (".nvmrc", ".node-version"):
        if (files.get(name) or "").strip():
            node_spec = (files[name] or "").strip()
            break
    node_current = local_versions().get("node") or ""
    node_ok = None
    if node_spec and node_current:
        node_ok = version_satisfies(node_current, node_spec)
        if node_ok is False:
            warnings.append("依赖要求 Node %s，本机是 %s" % (node_spec, node_current))

    if not start:
        warnings.append("没有推断出启动命令，导入后需要手动补填")

    return {
        "name": py_name or parsed["repo"],
        "slug": parsed["slug"],
        "url": parsed["url"],
        "ref": ref,
        "kind": kind,
        "type": ("Python + Node" if (is_py and is_node)
                 else "Python" if is_py else "Node" if is_node else ""),
        "setup": setup,
        "init": init,
        "start": start,
        "entry": entry,
        "port": port,
        "python": {"spec": py_spec, "pick": pick, "ok": py_ok},
        "node": {"spec": node_spec, "current": node_current, "ok": node_ok},
        "warnings": warnings,
        "notes": notes,
        "files": sorted(files),
        "readme": readme[:6000],
        "dir": default_github_dir(parsed),
    }


def github_repo_public(meta, parsed):
    license_info = meta.get("license") if isinstance(meta.get("license"), dict) else {}
    return {
        "slug": parsed["slug"],
        "description": str(meta.get("description") or "")[:300],
        "stars": meta.get("stargazers_count") or 0,
        "language": meta.get("language") or "",
        "license": license_info.get("spdx_id") or "",
        "archived": bool(meta.get("archived")),
        "private": bool(meta.get("private")),
        "defaultBranch": meta.get("default_branch") or "",
        "pushedAt": meta.get("pushed_at") or "",
    }


def handle_github_inspect(payload):
    parsed = parse_github_url(payload.get("url"))
    if not parsed:
        return {"ok": False, "message": "无法识别仓库地址，示例：https://github.com/owner/repo"}
    try:
        meta, files, ref, warnings = github_collect_cached(parsed)
    except RuntimeError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:
        return {"ok": False, "message": "读取仓库失败：%s" % str(exc)[:160]}
    plan = build_github_plan(parsed, meta, files, ref)
    if warnings:
        plan["warnings"] = warnings + plan["warnings"]
    return {"ok": True, "repo": github_repo_public(meta, parsed), "plan": plan,
            "warnings": warnings}


# ---- 后台任务：克隆 + 环境准备 ----

def _job_new(kind, title):
    job = {"id": "j-" + uuid.uuid4().hex[:8], "kind": kind, "title": title,
           "status": "running", "step": "准备中", "message": "",
           "log": [], "result": {}, "started": time.time()}
    with _GH_JOBS_LOCK:
        _GH_JOBS[job["id"]] = job
        for old in sorted(_GH_JOBS.values(), key=lambda x: x["started"])[:-20]:
            _GH_JOBS.pop(old["id"], None)
    return job


def _job_log(job, text):
    job["log"].append(str(text)[:600])
    if len(job["log"]) > 500:
        del job["log"][:200]


def github_job_status(job_id):
    with _GH_JOBS_LOCK:
        if job_id:
            job = _GH_JOBS.get(job_id)
        else:
            running = [j for j in _GH_JOBS.values()
                       if j["status"] == "running" and j["kind"] == "import"]
            job = max(running, key=lambda x: x["started"]) if running else None
        if not job:
            return {"ok": False, "message": "没有进行中的任务"}
        return {"ok": True, "id": job["id"], "kind": job["kind"], "title": job["title"],
                "status": job["status"], "step": job["step"], "message": job["message"],
                "result": job["result"], "log": job["log"][-160:]}


def _run_step(job, args, cwd, env=None, timeout=1800):
    shown = str(args[-1]) if args and args[0] == "/bin/zsh" else " ".join(args)
    _job_log(job, "$ " + shown)
    try:
        proc = subprocess.Popen(args, cwd=cwd, env=env or ENV,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", start_new_session=True)
    except Exception as exc:
        _job_log(job, "无法执行：%s" % exc)
        return 127

    def pump():
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    _job_log(job, line)
        except Exception:
            pass

    threading.Thread(target=pump, daemon=True).start()
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _job_log(job, "超时（%d 秒），已终止" % timeout)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=8)
        except Exception:
            pass
        return 124


def _clone_matches(target, url):
    """只认完整克隆：remote 一致且 HEAD 可解析，避免把中断的 clone 当成可复用。"""
    remote = exec_capture(["git", "-C", target, "config", "--get", "remote.origin.url"],
                          timeout=6).strip()
    norm = lambda s: re.sub(r"\.git$", "", str(s or "").rstrip("/")).lower()
    if not remote or norm(remote) != norm(url):
        return False
    head = exec_capture(["git", "-C", target, "rev-parse", "--verify", "HEAD"], timeout=6).strip()
    return bool(head)


def _run_github_import(job, parsed, target):
    try:
        job["step"] = "读取仓库信息"
        meta, files, ref, warnings = github_collect_cached(parsed)
        plan = build_github_plan(parsed, meta, files, ref)
        for w in warnings:
            _job_log(job, "提示：" + w)

        os.makedirs(os.path.dirname(target) or HOME, exist_ok=True)
        if os.path.isdir(target) and os.listdir(target):
            if not _clone_matches(target, parsed["url"]):
                raise RuntimeError("目标目录已有内容：%s" % target)
            _job_log(job, "复用已有克隆：" + target)
        else:
            job["step"] = "克隆仓库"
            args = ["git", "clone", "--depth", "1"]
            if parsed.get("ref"):
                args += ["--branch", parsed["ref"]]
            clone_url = parsed["url"] + ".git"
            mirror = (os.environ.get("AIHUB_GIT_MIRROR") or "").strip().rstrip("/")
            if mirror:
                clone_url = mirror + "/" + clone_url
                _job_log(job, "使用镜像：" + mirror)
            args += [clone_url, target]
            code = _run_step(job, args, HOME, timeout=600)
            if code != 0:
                raise RuntimeError(
                    "git clone 失败（退出码 %s）。私有仓库请先 gh auth login；"
                    "网络慢时可设置 AIHUB_GIT_MIRROR（如 https://ghfast.top）后重试" % code)
        sha = exec_capture(["git", "-C", target, "rev-parse", "--short", "HEAD"], timeout=10).strip()
        branch = exec_capture(["git", "-C", target, "rev-parse", "--abbrev-ref", "HEAD"], timeout=10).strip()
        if not branch or branch == "HEAD":
            branch = parsed.get("ref") or ""

        venv_dir = os.path.join(target, ".venv")
        runtime = {"venv": "", "python": "", "pythonVersion": "",
                   "node": shutil.which("node", path=ENV.get("PATH")) or ""}
        if plan.get("setup"):
            job["step"] = "创建虚拟环境"
            pick = (plan.get("python") or {}).get("pick") or {}
            py = pick.get("path") or shutil.which("python3", path=ENV.get("PATH"))
            if not py:
                raise RuntimeError("本机没有可用的 Python 解释器")
            _job_log(job, "解释器：" + py + ("（%s）" % pick.get("version") if pick.get("version") else ""))
            code = _run_step(job, [py, "-m", "venv", venv_dir], target, timeout=300)
            if code != 0:
                raise RuntimeError("创建虚拟环境失败（退出码 %s）" % code)
            runtime.update({"venv": venv_dir,
                            "python": os.path.join(venv_dir, "bin", "python"),
                            "pythonVersion": pick.get("version") or ""})
        env = project_process_env({"runtime": runtime})
        for cmd in plan.get("setup") or []:
            job["step"] = "安装依赖"
            code = _run_step(job, ["/bin/zsh"] + shell_args() + [cmd], target, env)
            if code != 0:
                raise RuntimeError("依赖安装失败（退出码 %s）：%s" % (code, cmd))

        for cmd in plan.get("init") or []:
            job["step"] = "首次初始化"
            code = _run_step(job, ["/bin/zsh"] + shell_args() + [cmd], target, env, timeout=600)
            if code != 0:
                _job_log(job, "初始化命令未成功，可稍后手动执行：" + cmd)

        entry = plan.get("entry") or ""
        if entry and not os.path.exists(os.path.join(target, entry)):
            _job_log(job, "警告：没有生成入口 %s" % entry)
            plan["warnings"].append("入口 %s 未生成，请检查启动命令" % entry)

        job["step"] = "试运行探测端口"
        port, verified, verify_message = clean_port(plan.get("port")), False, ""
        if plan.get("start"):
            os.makedirs(LOGS_DIR, exist_ok=True)
            log_file = os.path.join(LOGS_DIR, "import-%s.log" % job["id"])
            res = _verify_once(target, plan["start"], 25, log_file, env)
            if res.get("ports"):
                port, verified = res["ports"][0], True
                verify_message = "试运行成功，监听端口 %s" % port
            elif res.get("exited") and res.get("code") == 0:
                verified, verify_message = True, "命令执行完成，未监听端口"
            else:
                verify_message = ("试运行未通过" if not res.get("exited")
                                  else "试运行退出码 %s" % res.get("code"))
            _job_log(job, verify_message)
            for line in (res.get("output") or [])[-12:]:
                _job_log(job, "  " + line)

        project = {
            "id": "p-" + uuid.uuid4().hex[:8],
            "name": plan.get("name") or parsed["repo"],
            "path": target,
            "start": plan.get("start") or "",
            "port": port,
            "admin": "",
            "type": plan.get("type") or "",
            "kind": plan.get("kind") or "",
            "source": parsed["url"],
            "commit": sha,
            "branch": branch,
            "setup": plan.get("setup") or [],
            "init": plan.get("init") or [],
            "runtime": runtime,
        }
        projects = load_projects()
        real = os.path.realpath(target)
        projects = [p for p in projects
                    if os.path.realpath(normalize_path(p.get("path") or "")) != real]
        projects.append(project)
        save_projects(projects)
        _RUNTIME_CHECK.pop(project["id"], None)
        invalidate_ports_cache()
        job["result"] = {"projectId": project["id"], "port": port, "verified": verified,
                         "verifyMessage": verify_message,
                         "message": "已导入 %s%s" % (project["name"],
                                                   "（端口 %s）" % port if port else "")}
        job["step"] = "完成"
        job["status"] = "done"
    except Exception as exc:
        job["message"] = str(exc)[:300]
        job["step"] = "失败"
        job["status"] = "error"
        _job_log(job, "失败：" + job["message"])


def _git_run(path, args, timeout=120):
    """执行 git 子命令，返回 (退出码, stdout, stderr)。"""
    try:
        proc = subprocess.run(["git", "-C", path] + args, capture_output=True,
                              text=True, errors="replace", timeout=timeout, env=ENV)
    except subprocess.TimeoutExpired:
        return 124, "", "超时（%d 秒）" % timeout
    except Exception as exc:
        return 127, "", str(exc)[:200]
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _git_branch(path, fallback=""):
    if fallback:
        return fallback
    code, out, _ = _git_run(path, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    return out if code == 0 and out and out != "HEAD" else ""


def _imported_project(payload):
    """校验项目存在且来自 GitHub 导入，返回 (项目, 错误响应)。"""
    p = project_by_id(payload.get("id"))
    if not p:
        return None, {"ok": False, "message": "项目不存在"}
    path = normalize_path(p.get("path") or "")
    if not p.get("source") or not os.path.isdir(os.path.join(path, ".git")):
        return None, {"ok": False, "message": "该项目不是从 GitHub 导入的，无法检查更新"}
    return p, None


def github_check_update(payload):
    """用 git ls-remote 比对远端分支尖端，只读 refs，速度快且不受 API 限流影响。"""
    p, err = _imported_project(payload)
    if err:
        return err
    path = normalize_path(p.get("path") or "")
    parsed = parse_github_url(p.get("source"))
    if not parsed:
        return {"ok": False, "message": "无法解析仓库地址"}
    branch = _git_branch(path, p.get("branch")) or parsed.get("ref") or ""
    head = _git_run(path, ["rev-parse", "HEAD"])[1]
    if not head:
        return {"ok": False, "message": "本地仓库没有提交，无法检查更新"}
    if branch:
        args = ["ls-remote", "--heads", "origin", branch]
    else:
        args = ["ls-remote", "origin", "HEAD"]
    code, out, err_text = _git_run(path, args, timeout=25)
    if code != 0 or not out.strip():
        tail = (err_text or "").splitlines()[-1] if err_text else "退出码 %s" % code
        return {"ok": False, "message": "无法读取远端：" + tail[:160]}
    remote = out.split()[0]
    return {"ok": True, "branch": branch or "HEAD", "head": head[:7],
            "remote": remote[:7], "behind": 0 if remote == head else 1, "dirty": -1}


SETUP_TRIGGERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
                  "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                  "uv.lock", "poetry.lock")


def _run_github_update(job, p):
    try:
        path = normalize_path(p.get("path") or "")
        branch = _git_branch(path, p.get("branch"))
        if not branch:
            raise RuntimeError("无法确定当前分支")
        job["step"] = "拉取远端"
        fetch_from = "origin"
        mirror = (os.environ.get("AIHUB_GIT_MIRROR") or "").strip().rstrip("/")
        if mirror and p.get("source"):
            fetch_from = mirror + "/" + str(p["source"]).rstrip("/") + ".git"
            _job_log(job, "使用镜像：" + mirror)
        code, _, err = _git_run(path, ["fetch", "--depth", "1", fetch_from, branch], timeout=600)
        if code != 0:
            lines = (err or "").splitlines() or ["退出码 %s" % code]
            raise RuntimeError("git fetch 失败：" + lines[-1][:200])
        old = _git_run(path, ["rev-parse", "HEAD"])[1]
        remote = _git_run(path, ["rev-parse", "FETCH_HEAD"])[1]
        st_code, st_out, _ = _git_run(path, ["status", "--porcelain", "-uno"], timeout=20)
        if st_code == 0:
            dirty = [l for l in st_out.splitlines() if l.strip()]
            if dirty:
                raise RuntimeError("本地有 %d 个未提交改动，已停止更新（请先提交或还原）" % len(dirty))
        else:
            _job_log(job, "本地改动检查跳过（超时或不可用），直接以远端覆盖")
        if not remote or remote == old:
            job["result"] = {"message": "已是最新（%s）" % old[:7]}
            job["step"] = "完成"
            job["status"] = "done"
            return
        job["step"] = "切换到 " + remote[:7]
        code, _, err = _git_run(path, ["reset", "--hard", "FETCH_HEAD"], timeout=120)
        if code != 0:
            raise RuntimeError("git reset 失败：%s" % (err or "")[:160])
        _job_log(job, "已更新 %s → %s" % (old[:7], remote[:7]))
        changed = _git_run(path, ["diff", "--name-only", old, remote])[1].splitlines()
        need_setup = [c for c in changed if os.path.basename(c) in SETUP_TRIGGERS]
        if need_setup and p.get("setup"):
            job["step"] = "重装依赖"
            _job_log(job, "依赖文件有变化：" + ", ".join(need_setup[:6]))
            for cmd in p["setup"]:
                code = _run_step(job, ["/bin/zsh"] + shell_args() + [cmd], path, timeout=1200)
                if code != 0:
                    raise RuntimeError("依赖重装失败（退出码 %s），可点「修复环境」重试" % code)
        projects = load_projects()
        for item in projects:
            if item.get("id") == p.get("id"):
                item["commit"] = remote[:7]
                item["branch"] = branch
        save_projects(projects)
        job["result"] = {"message": "已更新到 %s，重启项目后生效" % remote[:7]}
        job["step"] = "完成"
        job["status"] = "done"
    except Exception as exc:
        job["message"] = str(exc)[:300]
        job["step"] = "失败"
        job["status"] = "error"
        _job_log(job, "失败：" + job["message"])


def handle_github_update(payload):
    p, err = _imported_project(payload)
    if err:
        return err
    with _GH_JOBS_LOCK:
        if any(j["status"] == "running" and j["kind"] == "update"
               and j.get("projectId") == p.get("id") for j in _GH_JOBS.values()):
            return {"ok": False, "message": "该项目正在更新，请稍候"}
    job = _job_new("update", "更新 " + str(p.get("name") or p.get("id")))
    job["projectId"] = p.get("id")
    threading.Thread(target=_run_github_update, args=(job, p), daemon=True).start()
    return {"ok": True, "id": job["id"]}


def handle_github_import(payload):
    parsed = parse_github_url(payload.get("url"))
    if not parsed:
        return {"ok": False, "message": "无法识别仓库地址"}
    raw_dir = str(payload.get("dir") or "").strip()
    if raw_dir:
        target = normalize_path(raw_dir)
        if not target or not os.path.isabs(target):
            return {"ok": False, "message": "安装目录需要是绝对路径，例如 ~/Projects/owner__repo"}
    else:
        target = default_github_dir(parsed)
    if target in ("/", HOME):
        return {"ok": False, "message": "安装目录不能是主目录或根目录"}
    if os.path.exists(target) and not os.path.isdir(target):
        return {"ok": False, "message": "目标路径已存在且不是目录"}
    if os.path.isdir(target) and os.listdir(target) and not _clone_matches(target, parsed["url"]):
        return {"ok": False, "message": "目标目录已有内容（可能是不完整的克隆），请先删除该目录再重试"}
    with _GH_JOBS_LOCK:
        if any(j["status"] == "running" and j["kind"] == "import"
               for j in _GH_JOBS.values()):
            return {"ok": False, "message": "已有导入任务在进行，请稍候"}
    job = _job_new("import", "导入 " + parsed["slug"])
    threading.Thread(target=_run_github_import, args=(job, parsed, target),
                     daemon=True).start()
    return {"ok": True, "job": job["id"], "dir": target, "message": "已开始导入"}


def pick_folder(payload):
    """调起 macOS 原生目录选择框（可在其中新建文件夹）。"""
    if sys.platform != "darwin":
        return {"ok": False, "message": "原生目录选择仅支持 macOS，请手动填写路径"}
    prompt = str(payload.get("title") or "选择文件夹")[:60]
    default = normalize_path(payload.get("default") or "")
    if not default or not os.path.isdir(default):
        default = HOME
    script = (
        "on run argv\n"
        "  set thePrompt to item 1 of argv\n"
        "  set theDefault to item 2 of argv\n"
        "  set theFolder to choose folder with prompt thePrompt "
        "default location (POSIX file theDefault)\n"
        "  return POSIX path of theFolder\n"
        "end run"
    )
    try:
        proc = subprocess.run(["osascript", "-e", script, "--", prompt, default],
                              capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"ok": False, "canceled": True}
    except Exception as exc:
        return {"ok": False, "message": "无法打开选择窗口：%s" % str(exc)[:120]}
    if proc.returncode != 0:
        if "User canceled" in (proc.stderr or ""):
            return {"ok": False, "canceled": True}
        return {"ok": False, "message": (proc.stderr or "选择失败").strip()[:160]}
    path = (proc.stdout or "").strip().rstrip("/") or "/"
    if not path.startswith("/"):
        return {"ok": False, "message": "选择窗口没有返回有效路径"}
    empty = True
    try:
        empty = not os.listdir(path)
    except OSError:
        pass
    return {"ok": True, "path": path, "empty": empty}


def prepare_project(p):
    """重装依赖：venv 丢失或 Python 升级后使用。"""
    setup = p.get("setup") or []
    rt = p.get("runtime") if isinstance(p.get("runtime"), dict) else {}
    venv_dir = rt.get("venv") or os.path.join(os.path.expanduser(p.get("path") or ""), ".venv")
    if not setup and not os.path.exists(os.path.join(venv_dir, "bin", "python")):
        return {"ok": False, "message": "该项目没有记录环境准备步骤"}
    job = _job_new("prepare", "准备 " + (p.get("name") or ""))
    threading.Thread(target=_run_prepare, args=(job, p, venv_dir), daemon=True).start()
    return {"ok": True, "job": job["id"], "message": "正在重新准备环境…"}


def _run_prepare(job, p, venv_dir):
    try:
        target = os.path.expanduser(p.get("path") or "")
        if not os.path.isdir(target):
            raise RuntimeError("项目目录不存在：%s" % target)
        rt = p.get("runtime") if isinstance(p.get("runtime"), dict) else {}
        if not os.path.exists(os.path.join(venv_dir, "bin", "python")):
            job["step"] = "重建虚拟环境"
            want = str(rt.get("pythonVersion") or "")
            major = ".".join(want.split(".")[:2])
            pick = None
            if major:
                pick = next((c for c in python_candidates()
                             if c["version"].startswith(major + ".")), None)
            pick = pick or pick_python("") or {}
            py = pick.get("path") or shutil.which("python3", path=ENV.get("PATH"))
            if not py:
                raise RuntimeError("本机没有可用的 Python 解释器")
            code = _run_step(job, [py, "-m", "venv", venv_dir], target, timeout=300)
            if code != 0:
                raise RuntimeError("创建虚拟环境失败（退出码 %s）" % code)
            rt.update({"venv": venv_dir, "python": os.path.join(venv_dir, "bin", "python"),
                       "pythonVersion": pick.get("version") or ""})
        env = project_process_env({"runtime": rt})
        for cmd in p.get("setup") or []:
            job["step"] = "安装依赖"
            code = _run_step(job, ["/bin/zsh"] + shell_args() + [cmd], target, env)
            if code != 0:
                raise RuntimeError("依赖安装失败（退出码 %s）：%s" % (code, cmd))
        for cmd in p.get("init") or []:
            job["step"] = "首次初始化"
            code = _run_step(job, ["/bin/zsh"] + shell_args() + [cmd], target, env, timeout=600)
            if code != 0:
                _job_log(job, "初始化命令未成功：" + cmd)

        projects = load_projects()
        for item in projects:
            if item.get("id") == p.get("id"):
                item["runtime"] = rt
                break
        save_projects(projects)
        _RUNTIME_CHECK.pop(p.get("id"), None)
        job["result"] = {"message": "环境已就绪"}
        job["step"] = "完成"
        job["status"] = "done"
    except Exception as exc:
        job["message"] = str(exc)[:300]
        job["step"] = "失败"
        job["status"] = "error"
        _job_log(job, "失败：" + job["message"])


# ---------------- AI 辅助识别 ----------------

AI_TIMEOUT = 90
AI_PROMPT = """你是本地开发项目的启动配置识别助手。根据下面的项目证据，推断在终端直接启动所需的命令和端口。
要求：
1. 只依据证据，不要编造文件；命令必须是能在对应目录直接执行的单行命令。
2. 支持 monorepo / 多服务：如果项目里包含多个可独立启动的服务（例如 api、frontend），services 中分别列出，不要只给一个。
3. 优先给出本地命令（npm/pnpm/yarn/mvn/go/cargo/python 等）。若证据里有版本声明（.nvmrc、java.version、go.mod 的 go 指令、rust-toolchain 等），命令应搭配对应的版本切换方式：Node 用 fnm exec --using=<版本> -- <命令>；Java 优先 ./mvnw 或 ./gradlew（其次 jenv/sdkman 切换到声明的 JDK）；有 rust-toolchain 时直接 cargo run 即可；Go 直接 go run 即可。若证据里的「本机可用版本」不满足要求且无版本管理工具，可在 start 里先写准备步骤（用 && 连接），例如先 source 版本管理器初始化脚本再执行命令。只有证据表明必须依赖 Docker 时才用 docker compose；存在本地启动方式时不要用 Docker。
4. 忽略没有端口的工具进程（如 stdio 型 MCP），除非它确实监听端口。
5. 每个服务的 path 使用绝对路径（都在给定项目目录内），start 在该 path 下执行。
6. 若入口脚本依赖环境变量（如 DATA_DIR），而仓库内有对应目录，请把必要变量写进启动命令，例如 `DATA_DIR=../data npm start`。
7. 端口无法确定时 port 返回 null。
8. 若证据表明该服务有管理后台（如路由里出现 /admin、存在 admin 目录等），在 admin 字段给出入口路径（以 / 开头，如 "/admin"）；没有证据则返回空字符串，不要猜测。
9. 若证据里 packageManager 指定了包管理器版本（如 npm@11.6.2），命令改用 corepack 前缀，例如 `corepack npm run dev`，避免 engine-strict 校验失败；有 .nvmrc/.node-version 时同理优先用 fnm 切换。
10. 只输出 JSON，不要 Markdown，不要解释。格式：
{"services":[{"name":"api","path":"/abs/project/api","start":"npm start","port":3000,"admin":"","reason":"20字以内"}],"reason":"总体判断"}

项目证据：
%s
"""


def script_port_hints(directory, files, limit=6):
    """从入口脚本里提取端口线索，返回形如 'server.js: const PORT = 3000'。"""
    priority = []
    pkg = os.path.join(directory, "package.json")
    if os.path.exists(pkg):
        try:
            main = json.loads(read_text(pkg)).get("main")
            if main and os.path.basename(main) in files:
                priority.append(os.path.basename(main))
        except Exception:
            pass
    for name in ("server.js", "index.js", "app.js", "main.js",
                 "app.py", "main.py", "manage.py", "main.go", "main.ts"):
        if name in files:
            priority.append(name)

    candidates = []
    for name in priority + [f for f in files
                            if f.endswith((".js", ".mjs", ".cjs", ".ts", ".py", ".go", ".java"))]:
        if name not in candidates:
            candidates.append(name)

    pattern = re.compile(r"(?i)(port\s*[=:]\s*[\"']?\d{2,5}"
                         r"|listen\(\s*(?:\d{2,5}|PORT)"
                         r"|PORT\s*\|\|\s*\d{2,5})")
    hints = []
    for name in candidates[:limit * 2]:
        f = os.path.join(directory, name)
        try:
            if not os.path.isfile(f) or os.path.getsize(f) > 300000:
                continue
        except OSError:
            continue
        for line in read_text(f, 40000).splitlines():
            line = line.strip()
            if len(line) > 160 or not pattern.search(line):
                continue
            hints.append("%s: %s" % (name, line))
            break
        if len(hints) >= limit:
            break
    return hints[:limit]


def env_names(directory, files, limit=15):
    """提取入口脚本引用的环境变量名（只返回名称，不读取值）。"""
    pattern = re.compile(
        r"process\.env\.([A-Z][A-Z0-9_]*)"
        r"|\bgetenv\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']"
        r"|environ(?:\.get)?\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']"
        r"|environ\[\s*[\"']([A-Z][A-Z0-9_]*)[\"']"
    )
    names = []
    for name in [f for f in files if f.endswith((".js", ".mjs", ".cjs", ".ts", ".py", ".go"))][:15]:
        f = os.path.join(directory, name)
        try:
            if not os.path.isfile(f) or os.path.getsize(f) > 300000:
                continue
        except OSError:
            continue
        for m in pattern.finditer(read_text(f, 40000)):
            value = next((g for g in m.groups() if g), "")
            if value and value not in names:
                names.append(value)
            if len(names) >= limit:
                return names
    return names


def ollama_model():
    try:
        with urlopen(OLLAMA + "/api/tags", timeout=2) as r:
            models = [m.get("name") or "" for m in json.load(r).get("models", [])]
    except Exception:
        return None
    models = [m for m in models if m]
    preferred = DEFAULT_MODELS.get("ollama", "")
    if preferred in models:
        return preferred
    for m in models:
        if m.startswith("qwen"):
            return m
    return models[0] if models else None


def project_evidence(path):
    """收集用于 AI 判断的精简项目证据，避免发送无关内容和敏感变量。"""
    try:
        entries = sorted(os.listdir(path))
    except Exception:
        return ""
    parts = ["项目目录: %s" % path, "根目录条目: " + ", ".join(entries[:80])]

    def has_marker(name):
        d = os.path.join(path, name)
        return any(os.path.exists(os.path.join(d, m)) for m in
                   ("package.json", "pom.xml", "build.gradle", "build.gradle.kts",
                    "manage.py", "go.mod", "Cargo.toml", "composer.json"))

    subdirs = [e for e in entries if not e.startswith(".") and e not in SCAN_SKIP
               and os.path.isdir(os.path.join(path, e))][:40]
    subdirs.sort(key=lambda e: (not has_marker(e), e))
    if subdirs:
        parts.append("一级子目录: " + ", ".join(subdirs[:20]))

    pkg = os.path.join(path, "package.json")
    if os.path.exists(pkg):
        try:
            data = json.loads(read_text(pkg))
            parts.append("package.json name: %s" % data.get("name"))
            parts.append("scripts: " + json.dumps(data.get("scripts") or {}, ensure_ascii=False))
            deps = list((data.get("dependencies") or {}).keys())[:30]
            dev = list((data.get("devDependencies") or {}).keys())[:30]
            if deps:
                parts.append("dependencies: " + ", ".join(deps))
            if dev:
                parts.append("devDependencies: " + ", ".join(dev))
        except Exception:
            pass
        for lock in ("pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock", "package-lock.json"):
            if os.path.exists(os.path.join(path, lock)):
                parts.append("lockfile: " + lock)
        for rel in (".nvmrc", ".node-version", ".python-version"):
            text = read_text(os.path.join(path, rel)).strip()
            if text:
                parts.append("%s: %s" % (rel, text[:40]))
        env_req = project_env_requirements(path)["requirements"]
        if env_req.get("node") or env_req.get("npm") or env_req.get("packageManager"):
            parts.append("版本约束: " + json.dumps(
                {k: v for k, v in env_req.items() if v}, ensure_ascii=False))
        if "engine-strict" in read_text(os.path.join(path, ".npmrc")).lower():
            parts.append(".npmrc: engine-strict=true")

    for name in ("requirements.txt", "pyproject.toml", "go.mod", "Cargo.toml"):
        f = os.path.join(path, name)
        if os.path.exists(f):
            parts.append("--- %s ---\n%s" % (name, read_text(f, 2000).strip()))

    for name in ("pom.xml", "build.gradle", "build.gradle.kts"):
        f = os.path.join(path, name)
        if os.path.exists(f):
            keys = [ln.strip() for ln in read_text(f, 5000).splitlines()
                    if any(k in ln for k in ("artifactId", "spring-boot", "com.android", "server.port"))][:20]
            parts.append("--- %s 关键行 ---\n%s" % (name, "\n".join(keys)))

    for rel in ("application.properties", "application.yml", "application.yaml",
                "src/main/resources/application.properties",
                "src/main/resources/application.yml",
                "src/main/resources/application.yaml"):
        f = os.path.join(path, rel)
        if os.path.exists(f):
            keep = [ln.strip() for ln in read_text(f, 1500).splitlines() if "port" in ln.lower()][:5]
            if keep:
                parts.append("%s: %s" % (rel, " | ".join(keep)))

    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        f = os.path.join(path, name)
        if os.path.exists(f):
            keep = [ln.strip() for ln in read_text(f, 4000).splitlines()
                    if any(k in ln for k in ("services:", "image:", "ports:", "command:", "container_name:"))
                    or re.match(r"^\s{2}[A-Za-z0-9_-]+:\s*$", ln)][:40]
            parts.append("--- %s 摘要 ---\n%s" % (name, "\n".join(keep)))

    env = os.path.join(path, ".env")
    if os.path.exists(env):
        env_text = read_text(env, 4000)
        keep = [ln.strip() for ln in env_text.splitlines()
                if re.match(r"^\s*[A-Za-z0-9_]*PORT[A-Za-z0-9_]*\s*=", ln)]
        if keep:
            parts.append(".env 端口配置: " + " | ".join(keep[:5]))
        keys = [m.group(1) for ln in env_text.splitlines()
                for m in [re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", ln)] if m]
        if keys:
            parts.append(".env 键名（值已省略）: " + ", ".join(keys[:20]))

    for name in ("README.md", "README.rst", "readme.md"):
        f = os.path.join(path, name)
        if os.path.exists(f):
            parts.append("--- %s 摘要 ---\n%s" %
                         (name, "\n".join(read_text(f, 3000).splitlines()[:40])))
            break

    # 根目录入口脚本的端口线索
    root_hints = script_port_hints(path, entries, 12)
    if root_hints:
        parts.append("根目录端口线索: " + " | ".join(root_hints))
    root_envs = env_names(path, entries)
    if root_envs:
        parts.append("根目录脚本引用的环境变量: " + ", ".join(root_envs))
    root_admins = detect_admin_paths(path)
    if root_admins:
        parts.append("后台入口线索（来自目录/路由）: " + ", ".join(root_admins))

    # monorepo：一级子项目的启动脚本、环境变量与端口
    for sub in subdirs[:10]:
        sd = os.path.join(path, sub)
        try:
            sfiles = sorted(os.listdir(sd))
        except Exception:
            continue
        hint = detect_project(sd, sfiles)
        if not hint:
            continue
        seg = ["[子项目 %s] 类型=%s 初判命令=%s 初判端口=%s" %
               (sub, hint.get("type") or "?", hint.get("start") or "?", hint.get("port"))]
        sub_pkg = os.path.join(sd, "package.json")
        if os.path.exists(sub_pkg):
            try:
                pdata = json.loads(read_text(sub_pkg))
                seg.append("scripts=" + json.dumps(pdata.get("scripts") or {}, ensure_ascii=False)[:500])
            except Exception:
                pass
        for envname in (".env", ".env.local"):
            ef = os.path.join(sd, envname)
            if os.path.exists(ef):
                keep = [ln.strip() for ln in read_text(ef, 3000).splitlines()
                        if re.match(r"^\s*[A-Za-z0-9_]*(PORT|HOST)[A-Za-z0-9_]*\s*=", ln)]
                if keep:
                    seg.append("env=" + " | ".join(keep[:5]))
        sub_hints = script_port_hints(sd, sfiles, 4)
        if sub_hints:
            seg.append("端口线索=" + " | ".join(sub_hints))
        sub_envs = env_names(sd, sfiles)
        if sub_envs:
            seg.append("环境变量=" + ", ".join(sub_envs))
        sub_admins = detect_admin_paths(sd)
        if sub_admins:
            seg.append("后台线索=" + ", ".join(sub_admins))
        parts.append("\n".join(seg))

    makefile = os.path.join(path, "Makefile")
    if os.path.exists(makefile):
        targets = [ln.strip() for ln in read_text(makefile, 3000).splitlines()
                   if re.match(r"^[A-Za-z0-9_.-]+:\s*$", ln)][:15]
        if targets:
            parts.append("Makefile targets: " + ", ".join(targets))

    hint = detect_project(path, entries)
    if hint:
        parts.append("本地规则初判: " + json.dumps(hint, ensure_ascii=False))

    # 环境要求 + 本机可用工具链：让 AI 知道项目要什么、本机有什么，避免给出跑不通的命令
    try:
        env_req = project_env_requirements(path)["requirements"]
        declared = {k: v for k, v in env_req.items() if v}
        if declared:
            parts.append("项目环境要求: " + json.dumps(declared, ensure_ascii=False))
        local_ver = local_versions()
        available = {k: local_ver.get(k) for k in
                     ("node", "java", "go", "rustc", "python3") if local_ver.get(k)}
        mgrs = [m for m in ("fnm", "corepack", "sdk", "jenv", "rustup", "pyenv")
                if shutil.which(m, path=ENV.get("PATH"))]
        if available:
            parts.append("本机可用版本: " + json.dumps(available, ensure_ascii=False))
        if mgrs:
            parts.append("本机版本管理工具: " + ", ".join(mgrs))
    except Exception:
        pass

    return "\n".join(parts)[:8000]


def parse_ai_result(text, root):
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None

    def clean_start(value):
        raw = str(value or "").strip()
        return next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")[:200]

    services = []
    raw_services = data.get("services")
    if isinstance(raw_services, list):
        for item in raw_services[:10]:
            if not isinstance(item, dict):
                continue
            spath = str(item.get("path") or "").strip()
            if spath and not os.path.isabs(spath):
                spath = os.path.join(root, spath)
            spath = normalize_path(spath or root)
            if not is_under(spath, root):
                spath = root
            start = clean_start(item.get("start"))
            if not start:
                continue
            services.append({
                "name": str(item.get("name") or "").strip()[:40]
                        or os.path.basename(spath.rstrip("/")) or spath,
                "path": spath,
                "start": start,
                "port": clean_port(item.get("port")),
                "admin": normalize_admin(item.get("admin")),
                "reason": str(item.get("reason") or "").strip()[:100],
            })

    if not services:
        start = clean_start(data.get("start"))
        if not start:
            return None
        services = [{
            "name": str(data.get("name") or "").strip()[:40]
                    or os.path.basename(root.rstrip("/")) or root,
            "path": root,
            "start": start,
            "port": clean_port(data.get("port")),
            "admin": normalize_admin(data.get("admin")),
            "reason": str(data.get("reason") or "").strip()[:100],
        }]

    for s in services:
        try:
            s["type"] = (detect_project(s["path"], os.listdir(s["path"])) or {}).get("type", "")
        except Exception:
            s["type"] = ""
        s["env"] = env_report(s["path"])
        if not s.get("admin"):
            try:
                admins = detect_admin_paths(s["path"])
                if admins:
                    s["admin"] = admins[0]
            except Exception:
                pass

    first = services[0]
    return {
        "name": first["name"],
        "start": first["start"],
        "port": first["port"],
        "admin": first.get("admin") or "",
        "reason": str(data.get("reason") or first["reason"] or "").strip()[:120],
        "services": services,
    }


def mark_ai_services(data):
    existing = {os.path.realpath(normalize_path(p["path"])) for p in load_projects()}
    for s in data.get("services", []):
        s["exists"] = os.path.realpath(s["path"]) in existing
    return data


def call_ollama_text(model, prompt):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }).encode()
    req = Request(OLLAMA + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=AI_TIMEOUT) as r:
        data = json.load(r)
    return (data.get("message") or {}).get("content") or ""


# ---------------- AI 工具探测 ----------------

AI_TOOLS = [
    {"id": "ollama", "label": "Ollama", "kind": "http", "files": False,
     "hint": "启动 Ollama 服务后可用"},
    {"id": "claude", "label": "Claude Code", "kind": "cli", "files": True,
     "hint": "安装并登录 claude CLI 后可用"},
    {"id": "openclaw", "label": "OpenClaw", "kind": "cli", "files": True,
     "hint": "安装 OpenClaw 后可用"},
]

CUSTOM_TOOL_TIMEOUT = 300


def load_custom_tools():
    """读取用户添加的 CLI 工具；只保留结构完整的条目。"""
    data = load_json_file(AI_TOOLS_FILE, {})
    items = data if isinstance(data, list) else (data.get("custom") or [])
    out = []
    for t in items:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "").strip()
        command = str(t.get("command") or "").strip()
        if not tid or not command:
            continue
        try:
            timeout = int(t.get("timeout") or CUSTOM_TOOL_TIMEOUT)
        except (TypeError, ValueError):
            timeout = CUSTOM_TOOL_TIMEOUT
        out.append({
            "id": tid,
            "label": str(t.get("label") or tid).strip(),
            "command": command,
            "input": "stdin" if t.get("input") == "stdin" else "arg",
            "timeout": max(10, min(1800, timeout)),
            "enabled": t.get("enabled") is not False,
        })
    return out


def save_custom_tools(tools):
    write_json_file(AI_TOOLS_FILE, {"custom": tools})


def _command_program(command):
    try:
        parts = shlex.split(command)
    except ValueError:
        return ""
    return parts[0] if parts else ""


def custom_tool_status():
    """探测自定义 CLI 是否可执行（只看命令首段是否存在）。"""
    tools = []
    for t in load_custom_tools():
        entry = dict(t)
        entry["kind"] = "custom"
        entry["files"] = False
        entry["id"] = "custom:" + t["id"]
        entry["tool_id"] = t["id"]
        entry["hint"] = "在「工具」页检查命令模板"
        program = _command_program(t["command"])
        path = shutil.which(program) if program else None
        if not t["enabled"]:
            entry["available"] = False
            entry["detail"] = "已停用"
        elif path:
            entry["available"] = True
            entry["detail"] = path
        else:
            entry["available"] = False
            entry["detail"] = "未找到命令：%s" % (program or "（模板为空）")
        tools.append(entry)
    return tools


def ai_tool_status():
    """探测本机可用的 AI 工具；只读检查，不发起模型请求。"""
    tools = []
    for t in AI_TOOLS:
        entry = dict(t)
        if t["kind"] == "cli":
            path = shutil.which(t["id"])
            entry["available"] = bool(path)
            entry["detail"] = path or "未安装"
        else:
            entry["available"] = port_open(11434)
            if entry["available"]:
                model = ollama_model()
                entry["detail"] = "运行中 · %s" % model if model else "运行中"
            else:
                entry["detail"] = "未启动"
        tools.append(entry)
    return tools


def all_ai_tools():
    """内置工具 + 自定义 CLI，供识别/诊断共用。"""
    return ai_tool_status() + custom_tool_status()


def _tool_path(cmd, *extras):
    """在 PATH 中找命令，找不到时再查常见安装路径（只读探测）。"""
    p = shutil.which(cmd, path=ENV.get("PATH"))
    if p:
        return p
    for e in extras:
        full = os.path.expanduser(e)
        if os.path.isfile(full) or os.path.islink(full):
            return full
    return ""


def env_tools_payload():
    """探测本机版本管理工具与运行时版本，供「工具」页一目了然展示。"""
    manager_defs = (
        ("fnm", "fnm", "Node 版本管理", ("~/.fnm/bin/fnm",)),
        ("corepack", "corepack", "Node 包管理器版本锁定", ()),
        ("sdk", "SDKMAN!", "Java / 多语言版本管理", ("~/.sdkman/bin/sdk",)),
        ("jenv", "jenv", "JDK 切换", ("~/.jenv/bin/jenv",)),
        ("goenv", "goenv", "Go 版本管理", ("~/.goenv/bin/goenv",)),
        ("rustup", "rustup", "Rust 工具链管理", ("~/.cargo/bin/rustup",)),
        ("pyenv", "pyenv", "Python 版本管理", ("~/.pyenv/bin/pyenv",)),
    )
    managers = []
    for key, label, desc, extras in manager_defs:
        path = _tool_path(key, *extras)
        entry = {"id": key, "label": label, "desc": desc,
                 "available": bool(path), "detail": path or "未安装", "versions": []}
        if key == "fnm" and path:
            try:
                entry["versions"] = fnm_versions()
            except Exception:
                pass
        managers.append(entry)

    local = local_versions()
    runtime_defs = (
        ("node", "Node.js", "node"),
        ("java", "JDK", "java"),
        ("go", "Go", "go"),
        ("rustc", "Rust", "rustc"),
        ("python3", "Python", "python3"),
    )
    runtimes = [{"id": key, "label": label, "version": local.get(local_key) or ""}
                for key, label, local_key in runtime_defs]
    return {"managers": managers, "runtimes": runtimes}


def handle_env_detect(payload):
    """检测指定项目的环境要求、本机版本与可用管理工具。"""
    path = normalize_path(payload.get("path"))
    if not path or not os.path.isdir(path):
        return {"ok": False, "message": "目录不存在：%s" % path}
    report = env_report(path)
    info = env_tools_payload()
    return {"ok": True, "path": path,
            "requirements": report["requirements"],
            "current": report["current"],
            "issues": report["issues"],
            "fixes": report.get("fixes") or [],
            "managers": info["managers"]}


def handle_env_ignore(payload):
    """忽略/恢复某项目的环境提示（写入 envs.json 的 _envIgnored 列表）。"""
    path = normalize_path(payload.get("path"))
    if not path or not os.path.isdir(path):
        return {"ok": False, "message": "目录不存在：%s" % path}
    ignored = bool(payload.get("ignored", True))
    prefs = load_env_prefs()
    key = os.path.realpath(path)
    lst = prefs.get("_envIgnored")
    lst = list(lst) if isinstance(lst, list) else []
    if ignored and key not in lst:
        lst.append(key)
    elif not ignored and key in lst:
        lst.remove(key)
    prefs["_envIgnored"] = lst
    write_json_file(ENV_FILE, prefs)
    return {"ok": True, "ignored": ignored,
            "message": "已忽略该项目的环境提示" if ignored else "已恢复环境提示"}


def env_ignored(path):
    key = os.path.realpath(normalize_path(path) or "")
    lst = load_env_prefs().get("_envIgnored")
    return bool(key and isinstance(lst, list) and key in lst)


def handle_env_switch(payload):
    """为项目绑定某个运行时版本，写入 envs.json（按 realpath 键控，不改全局环境）。"""
    path = normalize_path(payload.get("path"))
    kind = str(payload.get("kind") or "").strip()
    version = str(payload.get("version") or "").strip()
    if not path or not os.path.isdir(path):
        return {"ok": False, "message": "目录不存在：%s" % path}
    if kind not in ("node", "java", "go", "rust", "python"):
        return {"ok": False, "message": "不支持的环境类型：%s" % kind}
    if not version:
        return {"ok": False, "message": "请指定版本"}
    prefs = load_env_prefs()
    key = os.path.realpath(path)
    entry = prefs.get(key) if isinstance(prefs.get(key), dict) else {}
    entry[kind] = version
    provider = str(payload.get("provider") or "").strip()
    if provider:
        entry["provider"] = provider
    toolchain = str(payload.get("toolchainPath") or "").strip()
    if toolchain:
        entry[kind + "Path"] = toolchain
    entry["updatedAt"] = int(time.time())
    prefs[key] = entry
    write_json_file(ENV_FILE, prefs)
    _ENV_REQ_CACHE.pop(path, None)
    return {"ok": True, "message": "已为该项目绑定 %s %s" % (kind, version),
            "binding": {kind: version}}


def _install_init_prefix(provider):
    """为安装命令加版本管理器的初始化（登录 shell 未加载时也能用）。"""
    inits = {
        "sdkman": 'source "$HOME/.sdkman/bin/sdkman-init.sh" 2>/dev/null || true; ',
        "jenv": 'export PATH="$HOME/.jenv/bin:$PATH" 2>/dev/null || true; ',
        "fnm": 'eval "$(fnm env --shell bash)" 2>/dev/null || true; ',
        "pyenv": 'eval "$(pyenv init -)" 2>/dev/null || true; ',
        "goenv": 'eval "$(goenv init -)" 2>/dev/null || true; ',
        "rustup": 'export PATH="$HOME/.cargo/bin:$PATH"; ',
        "corepack": '',
    }
    return inits.get(provider or "", "")


def handle_env_auto_install(payload):
    """自动安装环境工具：只接受服务端生成的选项，绝不执行前端传入的任意命令。"""
    path = normalize_path(payload.get("path"))
    kind = str(payload.get("kind") or "").strip()
    tool = str(payload.get("tool") or "").strip()
    if not path or not os.path.isdir(path):
        return {"ok": False, "message": "目录不存在：%s" % path}

    # 从服务端当前检测结果里按 kind + tool 找回命令，防止命令注入
    fixes = env_report(path).get("fixes") or []
    fix = next((f for f in fixes if f.get("kind") == kind), None)
    if not fix:
        return {"ok": False, "message": "该项目当前没有需要安装的 %s 环境" % kind}
    opt = next((o for o in fix.get("options") or [] if o.get("tool") == tool), None)
    if not opt or not opt.get("command"):
        return {"ok": False, "message": "无效的安装方式：%s" % tool}

    # 防重：同一项目同一 kind 的安装任务在跑就不重复建
    with _GH_JOBS_LOCK:
        for j in _GH_JOBS.values():
            if j.get("status") == "running" and j.get("kind") == "env-install" \
                    and j.get("_path") == os.path.realpath(path) and j.get("_kind") == kind:
                return {"ok": False, "message": "该项目正在安装 %s，请稍候" % kind}

    version = str(fix.get("version") or "")
    provider = str(opt.get("provider") or "")
    command = _install_init_prefix(provider) + opt["command"]
    job = _job_new("env-install", "安装 %s %s（%s）" % (fix.get("label") or kind, version, tool))
    job["_path"] = os.path.realpath(path)
    job["_kind"] = kind
    threading.Thread(target=_run_env_auto_install,
                     args=(job, path, kind, version, provider, command, tool),
                     daemon=True).start()
    return {"ok": True, "job": job["id"],
            "message": "正在安装 %s %s…" % (fix.get("label") or kind, version)}


def _run_env_auto_install(job, path, kind, version, provider, command, tool):
    try:
        job["step"] = "安装 %s %s" % (kind, version)
        _job_log(job, "$ " + command)
        proc = subprocess.Popen(
            ["/bin/zsh"] + shell_args() + [command],
            cwd=path, env=ENV, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", start_new_session=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                _job_log(job, line)
        code = proc.wait(timeout=1800)
        if code != 0:
            raise RuntimeError("安装命令退出码 %s" % code)

        # 安装成功：自动写入项目级绑定（provider 决定后续如何解析路径）
        job["step"] = "写入项目绑定"
        prefs = load_env_prefs()
        key = os.path.realpath(path)
        entry = prefs.get(key) if isinstance(prefs.get(key), dict) else {}
        entry[kind] = version
        if provider:
            entry["provider"] = provider
        entry["updatedAt"] = int(time.time())
        prefs[key] = entry
        write_json_file(ENV_FILE, prefs)
        _ENV_REQ_CACHE.pop(path, None)
        _LOCAL_VER_CACHE["at"] = 0.0  # 让下次检测读到新装的版本

        job["result"] = {"message": "已安装并绑定 %s %s（%s）" % (kind, version, tool),
                         "provider": provider}
        job["step"] = "完成"
        job["status"] = "done"
        _job_log(job, "完成：已安装并绑定 %s %s" % (kind, version))
    except Exception as exc:
        job["message"] = str(exc)[:300]
        job["step"] = "失败"
        job["status"] = "error"
        _job_log(job, "安装失败：" + job["message"])


def http_tool_accounts():
    """模型页里可直接用于对话的 OpenAI / Anthropic 兼容账号（已启用、有密钥、有模型）。"""
    out = []
    for a in load_models():
        if a.get("enabled") is False:
            continue
        base = str(a.get("base_url") or "").strip().rstrip("/")
        if not base or not get_secret(a.get("key_ref")):
            continue
        models = []
        for m in a.get("models") or []:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("model_id") or m.get("name") or "").strip()
            if mid:
                models.append({"name": str(m.get("name") or mid).strip(), "model_id": mid})
        if not models:
            continue
        out.append({
            "account": a["id"],
            "vendor": a.get("vendor") or "未命名平台",
            "protocol": "anthropic" if a.get("protocol") == "anthropic" else "openai",
            "models": models,
        })
    return out


def ai_tools_payload():
    tools = all_ai_tools()
    return {
        "tools": tools,
        "available": [t["id"] for t in tools if t["available"]],
        "http": http_tool_accounts(),
        "clis": clis_payload().get("clis") or [],
    }


# ---------------- 自定义 CLI 工具 ----------------

def call_custom_tool(tool, prompt, cwd=None):
    """按命令模板执行自定义 CLI，返回 (文本, 错误)。

    arg 模式：模板里必须有一个独立的 {prompt} token，原样替换成提示词；
    stdin 模式：提示词从标准输入写入。全程不经过 shell。
    """
    try:
        parts = shlex.split(tool.get("command") or "")
    except ValueError as exc:
        return "", "命令模板无法解析：%s" % exc
    if not parts:
        return "", "命令模板为空"
    data = None
    if tool.get("input") == "stdin":
        data = prompt.encode("utf-8")
    else:
        if "{prompt}" not in parts:
            return "", "命令模板里需要一个独立的 {prompt} 占位符"
        parts = [prompt if p == "{prompt}" else p for p in parts]
    try:
        proc = subprocess.run(parts, input=data, capture_output=True, timeout=tool.get("timeout") or CUSTOM_TOOL_TIMEOUT,
                              env=ENV, cwd=cwd or HOME)
    except subprocess.TimeoutExpired:
        return "", "%s 执行超时" % tool.get("label", "工具")
    except FileNotFoundError:
        return "", "未找到命令：%s" % parts[0]
    except Exception as exc:
        return "", str(exc)[:200]
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0 and not out:
        return "", (err or "退出码 %s" % proc.returncode)[-400:]
    return out, ""


def call_http_model(account_id, model, prompt, system="", messages=None, timeout=AI_TIMEOUT):
    """直接调用模型页里的兼容账号，返回 (文本, 来源, 错误)。"""
    acc = next((a for a in load_models() if a["id"] == account_id), None)
    if not acc:
        return "", "", "模型账号不存在：%s" % account_id
    vendor = acc.get("vendor") or "兼容账号"
    if acc.get("enabled") is False:
        return "", "", "%s 已停用" % vendor
    base = str(acc.get("base_url") or "").strip().rstrip("/")
    key = get_secret(acc.get("key_ref"))
    if not base or not key:
        return "", "", "%s 缺少 Base URL 或 API Key" % vendor
    protocol = "anthropic" if acc.get("protocol") == "anthropic" else "openai"
    if not model:
        first = next((str(m.get("model_id") or m.get("name") or "").strip()
                      for m in acc.get("models") or [] if isinstance(m, dict)), "")
        model = first
    if not model:
        return "", "", "%s 还没有配置模型" % vendor

    msgs = list(messages or [])
    if system:
        msgs.insert(0, {"role": "system", "content": system})
    if not msgs:
        msgs.append({"role": "user", "content": prompt})
    elif prompt and all(m.get("role") != "user" for m in msgs):
        msgs.append({"role": "user", "content": prompt})

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if protocol == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        sys_parts = [m["content"] for m in msgs if m.get("role") == "system"]
        body = {
            "model": model,
            "max_tokens": 2048,
            "messages": [m for m in msgs if m.get("role") != "system"],
        }
        if sys_parts:
            body["system"] = "\n\n".join(sys_parts)
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    else:
        headers["Authorization"] = "Bearer " + key
        body = {"model": model, "messages": msgs, "temperature": 0, "stream": False}
        url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")

    try:
        req = Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        return "", "", "%s 返回 HTTP %d %s" % (vendor, exc.code, detail)
    except Exception as exc:
        return "", "", "%s 无法连接：%s" % (vendor, str(exc)[:160])

    text = ""
    if protocol == "anthropic":
        blocks = data.get("content") if isinstance(data, dict) else None
        if isinstance(blocks, list):
            text = "\n".join(b.get("text") or "" for b in blocks if isinstance(b, dict))
    else:
        choices = data.get("choices") if isinstance(data, dict) else None
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") or {}
            text = message.get("content") or ""
            if isinstance(text, list):
                text = "\n".join(b.get("text") or "" for b in text if isinstance(b, dict))
    text = (text or "").strip()
    if not text:
        return "", "", "%s 没有返回内容" % vendor
    return text, "%s · %s" % (vendor, model), ""


def handle_tool_save(payload):
    """新增或修改自定义 CLI 工具。"""
    tools = load_custom_tools()
    tid = str(payload.get("id") or "").strip()
    label = str(payload.get("label") or "").strip()
    command = str(payload.get("command") or "").strip()
    mode = "stdin" if payload.get("input") == "stdin" else "arg"
    try:
        timeout = int(payload.get("timeout") or CUSTOM_TOOL_TIMEOUT)
    except (TypeError, ValueError):
        timeout = CUSTOM_TOOL_TIMEOUT
    timeout = max(10, min(1800, timeout))

    if not command:
        return {"ok": False, "message": "请填写命令模板"}
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return {"ok": False, "message": "命令模板无法解析：%s" % exc}
    if not parts:
        return {"ok": False, "message": "命令模板为空"}
    if mode == "arg" and "{prompt}" not in parts:
        return {"ok": False,
                "message": "arg 模式需要在命令里加一个独立的 {prompt}，例如：codex exec \"{prompt}\""}
    if not shutil.which(parts[0]):
        return {"ok": False, "message": "本机找不到命令 %s，请先安装或填写绝对路径" % parts[0]}

    entry = {
        "id": tid or ("t" + uuid.uuid4().hex[:8]),
        "label": label or parts[0],
        "command": command,
        "input": mode,
        "timeout": timeout,
        "enabled": payload.get("enabled") is not False,
    }
    if tid:
        idx = next((i for i, t in enumerate(tools) if t["id"] == tid), -1)
        if idx < 0:
            return {"ok": False, "message": "工具不存在：%s" % tid}
        tools[idx] = entry
    else:
        tools.append(entry)
    save_custom_tools(tools)
    return {"ok": True, "tool": entry, "message": "已保存 %s" % entry["label"]}


def handle_tool_delete(payload):
    tid = str(payload.get("id") or "").strip()
    tools = load_custom_tools()
    left = [t for t in tools if t["id"] != tid]
    if len(left) == len(tools):
        return {"ok": False, "message": "工具不存在：%s" % tid}
    save_custom_tools(left)
    return {"ok": True, "message": "已删除"}


def handle_tool_test(payload):
    """用一句最小提示实际跑一次，确认命令模板可用。"""
    tid = str(payload.get("id") or "").strip()
    tool = next((t for t in load_custom_tools() if t["id"] == tid), None)
    if not tool:
        return {"ok": False, "message": "工具不存在：%s" % tid}
    if not tool["enabled"]:
        return {"ok": False, "message": "%s 已停用" % tool["label"]}
    started = time.time()
    text, err = call_custom_tool(tool, "只回复 OK", HOME)
    elapsed = int((time.time() - started) * 1000)
    if err:
        return {"ok": False, "message": err, "latency": elapsed}
    return {"ok": True, "latency": elapsed,
            "output": text[-500:], "message": "命令可用，耗时 %dms" % elapsed}


def rules_analyze(path, verify=True):
    """不依赖 AI：按目录文件推断启动方式，可选试运行确认。"""
    try:
        info = detect_project(path, os.listdir(path))
    except OSError as exc:
        return {"ok": False, "message": "无法读取目录：%s" % exc}
    if not info or not info.get("start"):
        return {"ok": False, "message": "没有识别出可启动入口，请手动填写启动命令"}
    try:
        admins = detect_admin_paths(path)
    except Exception:
        admins = []
    service = {
        "name": os.path.basename(path.rstrip("/")) or path,
        "path": path,
        "start": info["start"],
        "port": info.get("port"),
        "type": info.get("type") or "",
        "admin": admins[0] if admins else "",
        "env": env_report(path),
        "reason": "按目录文件推断",
    }
    data = {"ok": True, "name": service["name"], "start": service["start"],
            "port": service["port"], "admin": service["admin"],
            "type": service["type"], "services": [service],
            "source": "规则识别"}
    if verify:
        auto_verify_services(data, path, "rules")
    return mark_ai_services(data)


def normalize_provider(payload):
    """统一 provider 取值：支持 http:<account>，自定义工具保持 custom:<id>。"""
    raw = str(payload.get("provider") or "auto").strip()
    account = str(payload.get("account") or "").strip()
    model = str(payload.get("model") or "").strip()
    low = raw.lower()
    if low.startswith("http:"):
        account = account or raw.split(":", 1)[1]
        return "http", account, model
    if raw.startswith("custom:"):
        return raw, account, model
    return low, account, model


def analyze_project_with_ai(payload):
    path = normalize_path(payload.get("path"))
    if not os.path.isdir(path):
        return {"ok": False, "message": "目录不存在：%s（请填写以 / 开头的完整路径）" % path}
    provider, account_id, model_id = normalize_provider(payload)
    if provider == "rules":
        return rules_analyze(path, payload.get("verify", True))

    evidence = project_evidence(path)
    if not evidence:
        return {"ok": False, "message": "无法读取项目目录"}

    prompt = AI_PROMPT % evidence
    tools = {t["id"]: t for t in all_ai_tools()}
    avail = {k for k, t in tools.items() if t["available"]}
    http_accs = {a["account"]: a for a in http_tool_accounts()}

    if provider == "http":
        if account_id not in http_accs:
            return {"ok": False,
                    "message": "模型页里没有可用的兼容账号（需要启用、填好 Base URL、API Key 和模型）"}
    elif provider not in ("auto",) and provider not in avail:
        t = tools.get(provider) or {}
        return {"ok": False,
                "message": "未检测到 %s（%s）。%s，或改用「仅规则识别」" %
                           (t.get("label") or provider, t.get("detail") or "不可用",
                            t.get("hint") or "请检查安装")}
    errors = []

    def accept(data, source):
        data.update({"ok": True, "source": source})
        if payload.get("verify", True):
            auto_verify_services(data, path, provider, account_id, model_id)
        return mark_ai_services(data)

    if provider in ("auto", "ollama") and "ollama" in avail:
        model = ollama_model()
        if not model:
            errors.append("Ollama 没有可用模型")
        else:
            try:
                data = parse_ai_result(call_ollama_text(model, prompt), path)
                if data:
                    return accept(data, "Ollama · " + model)
                errors.append("Ollama 返回无法解析")
            except Exception as exc:
                errors.append("Ollama：%s" % str(exc)[:120])

    if provider == "openclaw" and "openclaw" in avail:
        try:
            text, _, err = call_openclaw(prompt, "aihub-" + uuid.uuid4().hex[:8])
            if err:
                errors.append("OpenClaw：%s" % err[-120:])
            else:
                data = parse_ai_result(text, path)
                if data:
                    return accept(data, "OpenClaw")
                errors.append("OpenClaw 返回无法解析")
        except Exception as exc:
            errors.append("OpenClaw：%s" % str(exc)[:120])

    if provider in ("auto", "claude") and "claude" in avail:
        claude_model, claude_env = claude_call_config()
        try:
            text, _, err = call_claude(claude_model, prompt, "", claude_env)
            if err:
                errors.append("Claude：%s" % err[-120:])
            else:
                data = parse_ai_result(text, path)
                if data:
                    return accept(data, "Claude · " + (claude_model or "default"))
                errors.append("Claude 返回无法解析")
        except Exception as exc:
            errors.append("Claude：%s" % str(exc)[:120])

    if provider.startswith("custom:") and provider in avail:
        tool = tools[provider]
        text, err = call_custom_tool(tool, prompt, path)
        if err:
            errors.append("%s：%s" % (tool.get("label") or provider, err[-120:]))
        else:
            data = parse_ai_result(text, path)
            if data:
                return accept(data, tool.get("label") or provider)
            errors.append("%s 返回无法解析" % (tool.get("label") or provider))

    if provider == "http":
        text, source, err = call_http_model(account_id, model_id, prompt)
        if err:
            errors.append(err[-160:])
        else:
            data = parse_ai_result(text, path)
            if data:
                return accept(data, source)
            errors.append("兼容账号返回无法解析")

    # 自动模式在本地工具都失败时，再试一次自定义 CLI / 兼容账号
    if provider == "auto" and errors:
        for t in custom_tool_status():
            if not t["available"]:
                continue
            text, err = call_custom_tool(t, prompt, path)
            if not err:
                data = parse_ai_result(text, path)
                if data:
                    return accept(data, t.get("label") or t["id"])
        for acc in http_accs.values():
            text, source, err = call_http_model(acc["account"], "", prompt)
            if not err:
                data = parse_ai_result(text, path)
                if data:
                    return accept(data, source)

    if not errors:
        return {"ok": False,
                "message": "未检测到可用的 AI 工具，可改用「仅规则识别」（不需要安装 AI）"}
    return {"ok": False, "message": "AI 识别失败。" + "；".join(errors[-2:])}


# ---------------- 试运行验证 ----------------

VERIFY_TIMEOUT = 25
AUTO_VERIFY_TIMEOUT = 12
_VERIFY_LOCK = threading.Lock()


def tail_text_file(path, limit=65536):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _pid_exists(pid):
    """只判断进程是否存在，不回收僵尸进程（避免抢走 Popen 的退出码）。"""
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def _verify_once(path, command, timeout, log_path, env=None):
    """执行一次命令并观察端口，返回执行结果。"""
    started = time.time()
    with open(log_path, "ab") as log_file:
        log_file.write(("\n===== %s 试运行 %s =====\n" %
                        (time.strftime("%Y-%m-%d %H:%M:%S"), command)).encode("utf-8"))
        log_file.flush()
        proc = subprocess.Popen(
            ["/bin/zsh"] + shell_args() + [command],
            cwd=path, env=env or ENV,
            stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    ports, exited, tree, code = set(), False, {proc.pid}, None
    deadline = time.time() + timeout
    while time.time() < deadline:
        code = proc.poll()
        if code is not None:
            exited = True
            break
        listeners = list_listeners(force=True)
        ps = ps_table()
        tree = {pid for pid, info in ps.items() if info["pgid"] == proc.pid}
        if _pid_exists(proc.pid):
            tree.add(proc.pid)
        ports = {l["port"] for l in listeners if l["pid"] in tree}
        if ports:
            break
        time.sleep(1.0)

    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            pass
    for pid in tree:
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    wait_until = time.time() + 4
    while time.time() < wait_until and any(pid_alive(p) for p in tree):
        time.sleep(0.2)
    for pid in [p for p in tree if pid_alive(p)]:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    invalidate_ports_cache()
    if code is None:
        code = proc.returncode
    return {"ports": sorted(ports), "exited": exited, "code": code,
            "elapsed": int(time.time() - started),
            "output": tail_text_file(log_path).splitlines()[-30:]}


def verify_start_command(payload, timeout=VERIFY_TIMEOUT):
    """实际执行一次启动命令，检测是否监听端口，然后自动停止。"""
    path = normalize_path(payload.get("path"))
    command = (payload.get("start") or "").strip()
    admin = normalize_admin(payload.get("admin"))
    if not os.path.isdir(path):
        return {"ok": False, "message": "目录不存在：%s" % path}
    if not command:
        return {"ok": False, "message": "请先填写启动命令"}
    env = env_report(path)
    effective, env_note = wrap_command(command, path)
    if not _VERIFY_LOCK.acquire(blocking=False):
        return {"ok": False, "busy": True, "message": "已有试运行在进行，请稍候"}

    try:
        hint = clean_port(payload.get("port"))
        if hint and port_open(hint):
            listeners = [l for l in list_listeners(force=True) if l["port"] == hint]
            cwds = pids_cwd([l["pid"] for l in listeners])
            if any(is_under(cwds.get(l["pid"], ""), path) for l in listeners):
                return {"ok": True, "verified": True, "ports": [hint], "elapsed": 0,
                        "adminOk": admin_reachable(hint, admin) if admin else None,
                        "message": "服务已在运行（端口 %s）" % hint, "log": [], "logPath": "",
                        "env": env, "command": effective}
            return {"ok": False, "message": "端口 %s 已被占用，可能已有实例在运行" % hint,
                    "env": env, "command": effective}

        os.makedirs(LOGS_DIR, exist_ok=True)
        log_path = os.path.join(LOGS_DIR, "verify-%s.log" % uuid.uuid4().hex[:8])
        res = _verify_once(path, effective, timeout, log_path)
        switched = ""

        # Node 版本与依赖 engines 不兼容时，自动换 fnm 已装的其他大版本重试一次
        if not res["ports"] and not (res["exited"] and res["code"] == 0):
            text = "\n".join(res["output"])
            if "EBADENGINE" in text or "Unsupported engine" in text:
                for version in fnm_retry_candidates(effective):
                    retry = _verify_once(
                        path, _fnm_wrap(effective, version), timeout, log_path)
                    if retry["ports"] or (retry["exited"] and retry["code"] == 0):
                        res, switched = retry, version
                        save_node_pref(path, version)
                        break

        elapsed, output = res["elapsed"], res["output"]
        ports = res["ports"]
        admin_ok = None
        if ports and admin:
            admin_ok = admin_reachable(ports[0], admin)
        if switched:
            note = "检测到 Node 版本不兼容，已用 fnm 切换到 Node %s 并重试，后续启动将自动使用该版本" % switched
            env_note = (env_note + "；" if env_note else "") + note

        if ports:
            message = "试运行成功，监听端口 %s（已自动停止）" % \
                      ", ".join(str(p) for p in ports)
            if admin and admin_ok is True:
                message += "；后台 %s 可访问" % admin
            elif admin and admin_ok is False:
                message += "；后台 %s 未验证通过" % admin
            if env_note:
                message += "；" + env_note
            return {"ok": True, "verified": True, "ports": ports, "elapsed": elapsed,
                    "adminOk": admin_ok, "message": message,
                    "log": output, "logPath": log_path, "env": env,
                    "command": res.get("command") or effective}
        if res["exited"] and res["code"] == 0:
            return {"ok": True, "verified": True, "ports": [], "elapsed": elapsed,
                    "message": "命令执行完成，未监听端口（已退出）" + ("；" + env_note if env_note else ""),
                    "log": output, "logPath": log_path, "env": env, "command": effective}
        engine_hint = ""
        if any("EBADENGINE" in ln or "Unsupported engine" in ln for ln in output):
            engine_hint = "；依赖要求的 Node 版本与本机不兼容，可安装对应 LTS（fnm install 24）后重试"
        if res["exited"]:
            return {"ok": False, "message": "启动失败，退出码 %s%s%s" %
                    (res["code"], engine_hint, ("；" + env_note) if env_note else ""),
                    "elapsed": elapsed, "log": output, "logPath": log_path,
                    "env": env, "command": effective}
        return {"ok": False, "elapsed": elapsed,
                "message": "%d 秒内未检测到监听端口（进程已停止）%s" % (timeout, engine_hint),
                "log": output, "logPath": log_path, "env": env, "command": effective}
    except Exception as exc:
        return {"ok": False, "message": "试运行失败：%s" % str(exc)[:200]}
    finally:
        _VERIFY_LOCK.release()


FIX_PROMPT = """下面的启动命令在本机实际执行失败。请根据目录信息和报错给出修正后的启动命令。
要求：
1. 只输出 JSON，不要 Markdown：{"fixes":[{"name":"api","start":"DATA_DIR=../data npm start","reason":"20字以内"}]}
2. start 必须是单行、在对应目录可直接执行的命令；无法修复就给空字符串。
3. 不要编造不存在的文件或依赖。

项目目录: %s

失败信息:
%s
"""


def call_ai_text(provider, prompt, account="", model=""):
    provider = (provider or "auto").lower()
    avail = {t["id"] for t in all_ai_tools() if t["available"]}
    if provider in ("auto", "ollama") and "ollama" in avail:
        model_name = ollama_model()
        if model_name:
            try:
                return call_ollama_text(model_name, prompt), "Ollama · " + model_name, ""
            except Exception as exc:
                if provider == "ollama":
                    return "", "", "Ollama：%s" % str(exc)[:120]
    if provider in ("auto", "claude") and "claude" in avail:
        claude_model, claude_env = claude_call_config()
        try:
            text, _, err = call_claude(claude_model, prompt, "", claude_env)
            if not err:
                return text, "Claude", ""
            if provider == "claude":
                return "", "", "Claude：%s" % err[-120:]
        except Exception as exc:
            if provider == "claude":
                return "", "", "Claude：%s" % str(exc)[:120]
    if provider == "openclaw" and "openclaw" in avail:
        try:
            text, _, err = call_openclaw(prompt, "aihub-" + uuid.uuid4().hex[:8])
            if not err:
                return text, "OpenClaw", ""
            return "", "", "OpenClaw：%s" % err[-120:]
        except Exception as exc:
            return "", "", "OpenClaw：%s" % str(exc)[:120]
    if provider.startswith("custom:"):
        tool = next((t for t in custom_tool_status() if t["id"] == provider), None)
        if not tool:
            return "", "", "自定义工具不存在：%s" % provider
        if not tool["available"]:
            return "", "", "%s 不可用（%s）" % (tool["label"], tool["detail"])
        text, err = call_custom_tool(tool, prompt, HOME)
        return (text, tool["label"], "") if not err else ("", "", err)
    if provider == "http":
        return call_http_model(account, model, prompt)
    if provider in ("ollama", "claude", "openclaw"):
        label = next((t["label"] for t in AI_TOOLS if t["id"] == provider), provider)
        return "", "", "未检测到可用的 %s" % label
    return "", "", "无可用 AI 后端"


def suggest_fix_commands(provider, failures, root, account="", model=""):
    if not failures:
        return {}
    blocks = []
    for f in failures[:3]:
        blocks.append("服务: %s\n目录: %s\n命令: %s\n报错:\n%s" %
                      (f["name"], f["path"], f["start"], f["error"][-700:]))
    text, _, _ = call_ai_text(provider, FIX_PROMPT % (root, "\n\n".join(blocks)), account, model)
    if not text:
        return {}
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {}
    fixes = {}
    for item in data.get("fixes") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        raw = str(item.get("start") or "").strip()
        start = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")[:200]
        if name and start:
            fixes[name] = start
    return fixes


def auto_verify_services(data, root, provider, account="", model=""):
    """AI 识别后逐个试运行，成功则记录端口，失败则让 AI 给一版修正命令。"""
    services = (data.get("services") or [])[:3]
    failures = []
    for s in services:
        if not s.get("start"):
            continue
        res = verify_start_command({"path": s["path"], "start": s["start"], "port": s["port"],
                                    "admin": s.get("admin") or ""},
                                   timeout=AUTO_VERIFY_TIMEOUT)
        message = res.get("message") or "试运行失败"
        if res.get("env"):
            s["env"] = res["env"]
        if res.get("ok"):
            s["verified"] = True
            s["verifyMessage"] = message
            if res.get("ports"):
                s["port"] = res["ports"][0]
            if s.get("admin") and res.get("adminOk") is False:
                s["admin"] = ""
        elif res.get("busy"):
            s["verified"] = False
            s["verifySkipped"] = True
            s["verifyMessage"] = "已有试运行在进行，未自动验证"
        else:
            s["verified"] = False
            s["verifyMessage"] = message
            failures.append({"name": s["name"], "path": s["path"], "start": s["start"],
                             "error": "\n".join((res.get("log") or [])[-20:]) or message})

    if failures:
        fixes = suggest_fix_commands(provider, failures, root, account, model)
        for s in services:
            if s.get("verified") or not fixes.get(s["name"]):
                continue
            s["start"] = fixes[s["name"]]
            s["suggested"] = True
            s["verifyMessage"] = (s.get("verifyMessage") or "") + \
                "；已按报错给出建议命令，请点「试运行」确认"

    if services:
        first = services[0]
        data["name"] = first.get("name") or data.get("name")
        data["start"] = first.get("start") or data.get("start")
        data["port"] = first.get("port") or data.get("port")
        data["admin"] = first.get("admin") or ""
    return data


# ---------------- 诊断上下文 ----------------

DIAG_LOG_LINES = 80
DIAG_LOG_CHARS = 6000


def project_snapshot(pid):
    """取项目实时快照（运行状态、端口、进程、环境），供诊断面板使用。"""
    for x in collect_projects().get("projects") or []:
        if x.get("id") == pid:
            return x
    return None


def project_context_block(p):
    """把项目快照压成一段上下文，避免模型臆测启动方式、端口与依赖。"""
    issues = (p.get("env") or {}).get("issues") or []
    lines = [
        "[项目诊断上下文]",
        "名称: %s" % (p.get("name") or ""),
        "目录: %s" % (p.get("path") or ""),
        "启动命令: %s" % (p.get("start") or "未配置"),
        "技术类型: %s" % (p.get("type") or "未知"),
    ]
    status = "启动中" if p.get("starting") else ("运行中" if p.get("running") else "未运行")
    if p.get("running") and not p.get("managed"):
        status += "（不是由 AI Hub 启动的）"
    lines.append("状态: " + status)
    if p.get("ports"):
        lines.append("监听端口: " + ", ".join(str(n) for n in p["ports"]))
    elif p.get("port"):
        lines.append("配置端口: %s（当前未监听）" % p["port"])
    if p.get("pids"):
        lines.append("PID: " + ", ".join(str(n) for n in p["pids"]))
    if p.get("admin"):
        lines.append("后台路径: " + p["admin"])
    if issues:
        lines.append("环境问题: " + "；".join(issues[:6]))
    log = read_log(p, DIAG_LOG_LINES).get("lines") or []
    if log:
        lines += ["最近日志（末尾 %d 行）:" % len(log), "\n".join(log)[-DIAG_LOG_CHARS:]]
    lines += [
        "[/项目诊断上下文]",
        "回答规则：只依据上面的信息与该目录里能读到的文件判断；缺少信息时先读文件，"
        "不要编造启动命令、端口或依赖。",
    ]
    return "\n".join(lines)


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _guard(self, write=False):
        """仅接受本机回环访问；写接口额外校验自定义头、Content-Type 与请求体大小。"""
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in ALLOWED_HOSTS:
            self._json({"error": "非法访问来源"}, 403)
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and origin not in ALLOWED_ORIGINS:
            self._json({"error": "非法访问来源"}, 403)
            return False
        if not write:
            return True
        if (self.headers.get(CSRF_HEADER) or "").strip() != "1":
            self._json({"error": "缺少请求头 %s" % CSRF_HEADER}, 403)
            return False
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._json({"error": "仅接受 application/json"}, 415)
            return False
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json({"error": "非法请求体"}, 400)
            return False
        if length > MAX_BODY_BYTES:
            self._json({"error": "请求体过大"}, 413)
            return False
        return True

    def do_OPTIONS(self):
        # 不返回任何 CORS 头，跨站预检直接失败
        self._json({"error": "不支持跨域请求"}, 403)

    def _send(self, body, ctype, code=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", code)

    def do_GET(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/projects", "/projects.html"):
            self._send(PROJECTS_PAGE, "text/html; charset=utf-8")
        elif path in ("/models", "/models.html"):
            self._send(MODELS_PAGE, "text/html; charset=utf-8")
        elif path in ("/tools", "/tools.html"):
            self._send(TOOLS_PAGE, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(collect_status())
        elif path == "/api/ai/tools":
            self._json(ai_tools_payload())
        elif path == "/api/env/status":
            self._json(env_tools_payload())
        elif path == "/api/models":
            self._json(collect_models())
        elif path == "/api/claude/models":
            self._json(claude_models_payload())
        elif path == "/api/projects":
            self._json(collect_projects())
        elif path == "/api/workspaces":
            self._json({"workspaces": workspace_list()})
        elif path == "/api/projects/log":
            q = parse_qs(urlparse(self.path).query)
            p = project_by_id((q.get("id") or [""])[0])
            if p:
                self._json(read_log(p))
            else:
                self._json({"error": "项目不存在"}, 404)
        elif path == "/api/github/job":
            q = parse_qs(urlparse(self.path).query)
            self._json(github_job_status((q.get("id") or [""])[0]))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._guard(write=True):
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            payload = {}

        if self.path == "/api/service":
            self._json(handle_service(payload))
        elif self.path == "/api/models/save":
            self._json(handle_model_save(payload))
        elif self.path == "/api/models/delete":
            self._json(handle_model_delete(payload))
        elif self.path == "/api/models/copy":
            self._json(handle_model_copy(payload))
        elif self.path == "/api/models/fetch":
            self._json(handle_model_fetch(payload))
        elif self.path == "/api/models/balance":
            self._json(handle_model_balance(payload))
        elif self.path == "/api/models/test":
            self._json(handle_model_test(payload))
        elif self.path == "/api/ai/tools/save":
            self._json(handle_tool_save(payload))
        elif self.path == "/api/ai/tools/delete":
            self._json(handle_tool_delete(payload))
        elif self.path == "/api/ai/tools/test":
            self._json(handle_tool_test(payload))
        elif self.path == "/api/env/detect":
            self._json(handle_env_detect(payload))
        elif self.path == "/api/env/switch":
            self._json(handle_env_switch(payload))
        elif self.path == "/api/env/ignore":
            self._json(handle_env_ignore(payload))
        elif self.path == "/api/env/auto-install":
            self._json(handle_env_auto_install(payload))
        elif self.path == "/api/clis/bind":
            self._json(handle_cli_bind(payload))
        elif self.path == "/api/clis/restore":
            self._json(handle_cli_restore(payload))
        elif self.path == "/api/clis/verify":
            self._json(handle_cli_verify(payload))
        elif self.path == "/api/openapp":
            self._json(handle_openapp(payload))
        elif self.path == "/api/projects/save":
            self._json(handle_project_save(payload))
        elif self.path == "/api/projects/delete":
            self._json(handle_project_delete(payload))
        elif self.path == "/api/projects/action":
            self._json(handle_project_action(payload))
        elif self.path == "/api/ports/kill":
            self._json(kill_listener_process(payload.get("pid")))
        elif self.path == "/api/projects/scan":
            self._json(handle_project_scan(payload))
        elif self.path == "/api/projects/import":
            self._json(handle_project_import(payload))
        elif self.path == "/api/github/inspect":
            self._json(handle_github_inspect(payload))
        elif self.path == "/api/github/import":
            self._json(handle_github_import(payload))
        elif self.path == "/api/github/check-update":
            self._json(github_check_update(payload))
        elif self.path == "/api/github/update":
            self._json(handle_github_update(payload))
        elif self.path == "/api/system/pick-folder":
            self._json(pick_folder(payload))
        elif self.path == "/api/projects/analyze":
            self._json(analyze_project_with_ai(payload))
        elif self.path == "/api/projects/verify":
            self._json(verify_start_command(payload))
        elif self.path == "/api/chat":
            self.handle_chat(payload)
        else:
            self._json({"error": "not found"}, 404)

    # SSE：先发头，再逐条推送
    def handle_chat(self, payload):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

        def emit(event, data):
            chunk = "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()

        provider, account_id, model_id = normalize_provider(payload)
        message = payload.get("message", "")
        history = payload.get("history") or []
        model = model_id or payload.get("model") or DEFAULT_MODELS.get(provider, "")
        multi = bool(payload.get("multi"))
        session = payload.get("session") or ""

        # 诊断面板必须绑定项目：没有项目就不接受对话
        pid = str(payload.get("project") or "").strip()
        raw = project_by_id(pid) if pid else None
        if not raw:
            emit("error", {"message": "诊断面板需要绑定项目：请从项目卡片的「诊断」进入"})
            return
        project = project_snapshot(pid)
        if not project:
            emit("error", {"message": "项目不存在：%s" % pid})
            return

        # 同一分组（如 api + frontend）共用仓库根目录，让模型能读到完整代码
        workdir = project["path"]
        wid = str(payload.get("workspace") or "").strip()
        ws = workspace_by_id(wid) if wid else None
        if ws and os.path.isdir(ws.get("path") or ""):
            workdir = ws["path"]
        if not os.path.isdir(workdir):
            emit("error", {"message": "项目目录不存在：%s" % workdir})
            return
        readonly = True
        context = project_context_block(project)
        emit("context", {
            "id": project["id"],
            "name": project["name"],
            "path": workdir,
            "status": "运行中" if project.get("running") else "已停止",
            "ports": project.get("ports") or [],
            "issues": (project.get("env") or {}).get("issues") or [],
        })

        # 工具没装就给出明确原因，而不是让调用直接失败
        tools = {t["id"]: t for t in all_ai_tools()}
        avail = {k for k, t in tools.items() if t["available"]}
        if provider == "http":
            if account_id not in {a["account"] for a in http_tool_accounts()}:
                emit("error", {"message": "模型页里没有可用的兼容账号（需要启用、填好 Base URL、API Key 和模型）"})
                return
        elif provider not in avail:
            tool = tools.get(provider) or {}
            emit("error", {"message": "未检测到 %s（%s）" %
                                      (tool.get("label") or provider,
                                       tool.get("hint") or "请检查安装")})
            return

        # 模型来源：手动指定账号优先；否则使用「工具」页的全局绑定
        env_extra = None
        account_id = account_id or str(payload.get("claudeAccount") or "").strip()
        if provider == "claude":
            if account_id:
                env_extra, model_err = claude_account_env(account_id)
                if model_err:
                    emit("error", {"message": model_err})
                    return
            elif claude_binding():
                bound_env, bound_model, bound_err = claude_binding_env()
                if bound_err:
                    emit("error", {"message": "全局绑定不可用：%s。可到「工具」页重新选择模型" % bound_err})
                    return
                env_extra = bound_env
                if not model_id and not payload.get("model"):
                    model = bound_model

        # 图片：Ollama 直接随消息发 base64，Claude / OpenClaw 落盘后在提示词里给路径
        images, img_err = parse_images(payload)
        if img_err:
            emit("error", {"message": img_err})
            return

        # 告知前端这次实际调用的后端，用于在 AI 回复前标注工具来源
        if provider == "http":
            acc = next((a for a in http_tool_accounts() if a["account"] == account_id), None)
            backend = {"id": "http", "label": (acc or {}).get("vendor") or "兼容账号"}
        else:
            backend = {"id": provider,
                       "label": (tools.get(provider) or {}).get("label") or provider}
        emit("meta", {"backend": backend})

        try:
            if provider == "ollama":
                msgs = [{"role": "system", "content": context}]
                if multi:
                    for h in history if isinstance(history, list) else []:
                        if not isinstance(h, dict):
                            continue
                        m = {"role": h.get("role") or "user", "content": h.get("content") or ""}
                        imgs = history_b64_images(h)
                        if imgs:
                            m["images"] = imgs
                        msgs.append(m)
                m = {"role": "user",
                     "content": message or ("请描述这张图片" if images else message)}
                if images:
                    m["images"] = [img["b64"] for img in images]
                msgs.append(m)
                stream_ollama(model, msgs, emit)
                emit("done", {"session": ""})

            elif provider == "claude":
                sid, err = stream_claude(model, prompt_with_images(context + "\n\n" + message, images),
                                         session if multi else "",
                                         workdir, readonly, emit, env_extra)
                if err:
                    emit("error", {"message": err})
                else:
                    emit("done", {"session": sid or ""})

            elif provider == "openclaw":
                sid = session if (multi and session) else "aihub-" + uuid.uuid4().hex[:8]
                text, sid, err = call_openclaw(
                    prompt_with_images(context + "\n\n" + message, images), sid)
                if err:
                    emit("error", {"message": err})
                else:
                    emit("delta", {"text": text})
                    emit("done", {"session": sid})

            elif provider.startswith("custom:"):
                tool = tools.get(provider) or {}
                text, err = call_custom_tool(
                    tool, prompt_with_images(context + "\n\n" + message, images), workdir)
                if err:
                    emit("error", {"message": err})
                else:
                    emit("delta", {"text": text})
                    emit("done", {"session": ""})

            elif provider == "http":
                msgs = []
                if multi:
                    for h in history if isinstance(history, list) else []:
                        if not isinstance(h, dict):
                            continue
                        msgs.append({"role": h.get("role") or "user",
                                     "content": h.get("content") or ""})
                msgs.append({"role": "user", "content": message or "请说明这个项目当前的问题"})
                text, _, err = call_http_model(account_id, model_id or model, "",
                                               system=context, messages=msgs, timeout=180)
                if err:
                    emit("error", {"message": err})
                else:
                    emit("delta", {"text": text})
                    emit("done", {"session": ""})

            else:
                emit("error", {"message": "未知后端：%s" % provider})
        except Exception as exc:
            try:
                emit("error", {"message": str(exc)[:400]})
            except Exception:
                pass


def handle_service(payload):
    name = payload.get("name")
    action = payload.get("action")
    cmds = {
        ("ollama", "start"): ["brew", "services", "start", "ollama"],
        ("ollama", "stop"): ["brew", "services", "stop", "ollama"],
        ("ollama", "restart"): ["brew", "services", "restart", "ollama"],
        ("openclaw", "start"): ["openclaw", "gateway", "start"],
        ("openclaw", "stop"): ["openclaw", "gateway", "stop"],
        ("openclaw", "restart"): ["openclaw", "gateway", "restart"],
    }
    args = cmds.get((name, action))
    if not args:
        return {"ok": False, "message": "不支持的操作：%s %s" % (name, action)}
    try:
        code, out, err = run_cmd(args, timeout=120)
        return {"ok": code == 0, "message": (out or err)[-300:]}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def handle_openapp(payload):
    name = payload.get("name")
    try:
        if name == "openclaw":
            subprocess.Popen(["openclaw", "dashboard"], env=ENV, cwd=HOME,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            cmd = "ollama run qwen3:8b" if name == "ollama" else "claude"
            subprocess.Popen(
                ["osascript",
                 "-e", 'tell application "Terminal" to do script "%s"' % cmd,
                 "-e", 'tell application "Terminal" to activate'],
                env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "message": "已打开"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


PROJECTS_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Hub · 项目</title>
<style>
  :root{
    --bg:#F5F4F1; --panel:#FFFFFF; --surface:#F0EFEA; --text:#17160F; --muted:#6E6B63;
    --line:rgba(24,22,18,.12); --line-strong:rgba(24,22,18,.24);
    --brand:#0AB861; --brand-ink:#06743F; --brand-soft:#E5FBF0;
    --danger:#C43A2E; --warn:#A2610A;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
    --ease:cubic-bezier(.2,.8,.2,1);
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#0D0D0F; --panel:#16161A; --surface:#1F1F24; --text:#ECECEE; --muted:#98979A;
           --line:rgba(236,236,238,.12); --line-strong:rgba(236,236,238,.26);
           --brand:#32F08C; --brand-ink:#32F08C; --brand-soft:#0A2A1B;
           --danger:#FF6B60; --warn:#E8A34A; }
  }
  *{ box-sizing:border-box; }
  html,body{ margin:0; min-height:100%; }
  body{
    background:var(--bg); color:var(--text);
    font:14px/1.5 -apple-system,"PingFang SC","Helvetica Neue",sans-serif;
  }
  .app{ display:flex; height:100vh; }
  .rail{
    width:236px; flex:0 0 236px; background:var(--panel); border-right:1px solid var(--line);
    display:flex; flex-direction:column; padding:14px 12px 12px; gap:6px; min-height:0;
  }
  .brand{
    display:flex; align-items:center; gap:9px; padding:4px 8px 14px;
    font-size:14.5px; font-weight:650; letter-spacing:-.01em;
  }
  .mark{ width:18px; height:18px; border-radius:5px; background:var(--text); position:relative; flex:0 0 auto; }
  .mark::before{
    content:""; position:absolute; inset:4px; border-radius:2px;
    border:1.5px solid var(--panel); border-right-color:transparent; border-bottom-color:transparent;
  }
  .mark::after{
    content:""; position:absolute; right:4px; bottom:4px; width:3px; height:3px;
    border-radius:1px; background:var(--brand);
  }
  .rail-nav{ display:flex; flex-direction:column; gap:2px; }
  .nav-btn{
    color:var(--muted); text-decoration:none; font-size:13px; font-weight:500;
    display:flex; align-items:center; gap:10px; padding:8px 10px; border-radius:8px;
    position:relative; transition:background 150ms var(--ease), color 150ms var(--ease);
  }
  .nav-btn svg{ width:15px; height:15px; flex:0 0 auto; opacity:.8; }
  .nav-btn:hover{ background:var(--surface); color:var(--text); }
  .nav-btn.is-active{ background:var(--surface); color:var(--text); font-weight:600; }
  .nav-btn.is-active svg{ opacity:1; }
  .nav-btn.is-active::before{
    content:""; position:absolute; left:0; top:9px; bottom:9px; width:2px;
    border-radius:2px; background:var(--brand);
  }
  .nav-btn .kbd{ margin-left:auto; font-family:var(--mono); font-size:9.5px; color:var(--faint); letter-spacing:.04em; }
  .rail-sep{ height:1px; background:var(--line); margin:12px 8px 10px; }
  .sec{
    font-family:var(--mono); font-size:9.5px; letter-spacing:.1em; color:var(--faint);
    text-transform:uppercase; padding:0 10px 7px;
  }
  #groups{ display:flex; flex-direction:column; gap:2px; flex:1; min-height:0; overflow:auto; }
  .row{
    display:flex; align-items:center; gap:8px; padding:7px 10px; border-radius:7px;
    font-size:12.5px; color:var(--muted); cursor:pointer;
    transition:background 140ms var(--ease), color 140ms var(--ease);
  }
  .row:hover{ background:var(--surface); color:var(--text); }
  .row.on{ background:var(--surface); color:var(--text); font-weight:600; }
  .row .n{ min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .row .m{ margin-left:auto; font-family:var(--mono); font-size:10px; color:var(--faint); font-weight:400; }
  .rail-foot{ margin-top:auto; padding-top:11px; border-top:1px solid var(--line); }
  .foot-line{ font-family:var(--mono); font-size:10px; color:var(--faint); padding:0 2px; }
  .main{ flex:1; min-width:0; display:flex; flex-direction:column; min-height:0; }
  .topbar{
    display:flex; align-items:center; gap:12px; padding:13px 22px;
    border-bottom:1px solid var(--line); flex-wrap:wrap;
  }
  .topbar h1{ margin:0; font-size:15px; font-weight:600; letter-spacing:-.01em; }
  .summary{ font-size:12px; color:var(--muted); }
  .tools{ margin-left:auto; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .btn{
    appearance:none; border:1px solid var(--line); background:var(--panel); color:var(--text);
    font:inherit; font-size:12.5px; padding:7px 12px; border-radius:8px; cursor:pointer;
    text-decoration:none; display:inline-flex; align-items:center; gap:4px;
    transition:background 150ms var(--ease), border-color 150ms var(--ease),
               color 150ms var(--ease), transform 150ms var(--ease);
  }
  .btn:hover{ background:var(--surface); border-color:var(--line-strong); transform:translateY(-1px); }
  .btn:active{ transform:translateY(1px) scale(.985); transition-duration:90ms; }
  .btn.primary{ background:var(--text); border-color:var(--text); color:var(--bg); font-weight:600; }
  .btn.primary:hover{ background:var(--text); border-color:var(--text); }
  .btn.ghost{ border-color:transparent; background:transparent; color:var(--muted); }
  .btn.ghost:hover{ color:var(--text); background:var(--surface); border-color:transparent; }
  .btn.subtle{ border-color:transparent; background:transparent; color:var(--muted); padding:7px 9px; }
  .btn.subtle:hover{ color:var(--text); background:var(--surface); border-color:transparent; }
  .btn.danger:hover{ color:var(--danger); }
  .btn.tiny{ padding:3px 9px; font-size:11.5px; border-radius:6px; }
  .btn:disabled{ opacity:.55; cursor:default; transform:none; }
  .btn:focus-visible, .chip:focus-visible, .navlink:focus-visible{
    outline:2px solid var(--brand-ink); outline-offset:2px;
  }
  @media (prefers-reduced-motion: reduce){
    .btn, .card, .chip{ transition-duration:1ms; }
    .btn:hover, .btn:active{ transform:none; }
  }
  .page{ flex:1; overflow:auto; padding:22px 22px 48px; }
  .page > *{ max-width:1240px; margin-left:auto; margin-right:auto; }
  .grid{ display:flex; flex-direction:column; gap:16px; }
  .group-grid{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; }
  @media (max-width: 1120px){ .group-grid{ grid-template-columns:repeat(2,minmax(0,1fr)); } }
  @media (max-width: 760px){ .group-grid{ grid-template-columns:1fr; } }
  .card{
    position:relative; display:flex; flex-direction:column; gap:11px;
    padding:16px 16px 12px; border-radius:16px;
    background:var(--panel); border:1px solid var(--line);
    box-shadow:inset 0 1px 0 rgba(255,255,255,.7);
    transition:border-color 180ms var(--ease), box-shadow 180ms var(--ease);
  }
  @media (prefers-color-scheme: dark){
    .card{ box-shadow:inset 0 1px 0 rgba(255,255,255,.045); }
  }
  @media (hover:hover) and (pointer:fine){
    .card:hover{
      border-color:var(--line-strong);
      box-shadow:0 22px 44px -30px rgba(20,18,14,.5), inset 0 1px 0 rgba(255,255,255,.7);
    }
  }
  .card-top{ display:flex; align-items:flex-start; gap:10px; }
  .dot{
    width:8px; height:8px; border-radius:50%; flex:0 0 auto; margin-top:5px;
    border:1.5px solid color-mix(in srgb, var(--muted) 55%, transparent);
    background:transparent;
  }
  .dot.on{
    background:var(--brand); border-color:var(--brand);
    box-shadow:0 0 0 3px color-mix(in srgb, var(--brand) 18%, transparent);
  }
  .names{ min-width:0; flex:1; display:flex; flex-direction:column; gap:3px; }
  .task-link{
    align-self:flex-start; border:none; background:transparent; color:var(--brand-ink);
    font:inherit; font-size:11px; padding:0; cursor:pointer;
  }
  .task-link:hover{ text-decoration:underline; }
  .name{
    font-size:14.5px; font-weight:600; line-height:1.3; letter-spacing:-.01em;
    display:flex; align-items:center; gap:7px; min-width:0;
  }
  .group{
    font-family:var(--mono); font-size:9.5px; font-weight:500; letter-spacing:.1em;
    text-transform:uppercase; color:var(--muted); flex:0 0 auto;
  }
  .name .sep{ display:none; }
  .name em, .cand-name em{
    font-style:normal; font-family:var(--mono); font-size:9.5px; font-weight:500;
    letter-spacing:.06em; text-transform:uppercase; color:var(--muted);
    border:1px solid var(--line); border-radius:5px; padding:1px 5px; flex:0 0 auto;
  }
  .status{ font-size:11px; color:var(--muted); margin-top:1px; }
  .status.ok{ color:var(--text); }
  .status.warn{ color:var(--warn); }
  .ports{ display:flex; gap:5px; flex-wrap:wrap; justify-content:flex-end; flex:0 0 auto; max-width:54%; }
  .chip{
    font-family:var(--mono); font-size:11px; font-variant-numeric:tabular-nums;
    padding:2px 7px; border-radius:6px; background:transparent; color:var(--text);
    border:1px solid var(--line); text-decoration:none; white-space:nowrap;
    transition:color 140ms var(--ease), border-color 140ms var(--ease), background 140ms var(--ease);
  }
  .chip.port:hover{
    color:var(--brand-ink); border-color:color-mix(in srgb, var(--brand-ink) 45%, transparent);
    background:color-mix(in srgb, var(--brand) 8%, transparent);
  }
  .chip.muted{ color:var(--muted); }
  .path{
    font-family:var(--mono); font-size:11px; color:var(--muted);
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  }
  .cmd{
    display:flex; gap:9px; align-items:baseline; min-width:0;
    border-top:1px solid var(--line); padding-top:10px;
  }
  .cmd span{
    color:var(--muted); font-family:var(--mono); font-size:9.5px; letter-spacing:.1em;
    text-transform:uppercase; flex:0 0 auto; padding-top:1px;
  }
  .cmd code{
    font-family:var(--mono); font-size:11.5px; color:var(--text);
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-width:0;
  }
  .meta{
    display:flex; flex-wrap:wrap; align-items:baseline; color:var(--muted);
    font-family:var(--mono); font-size:10.5px; font-variant-numeric:tabular-nums;
  }
  .meta span + span::before{ content:"·"; margin:0 8px; color:var(--line-strong); }
  .env-warn{
    display:flex; gap:5px; align-items:baseline; flex-wrap:wrap;
    font-size:11px; line-height:1.5; color:var(--warn);
  }
  .env-badges{
    display:flex; gap:5px; align-items:center; flex-wrap:wrap; margin-top:2px;
  }
  .env-badges .chip{ font-size:10px; padding:2px 7px; }
  .env-fix{
    border:1px solid var(--warn); border-radius:9px; padding:9px 10px; margin-top:6px;
    background:rgba(255,159,10,.05);
  }
  .env-fix-head{ font-size:11.5px; line-height:1.5; color:var(--warn); font-weight:600; }
  .env-fix-more{ font-weight:400; opacity:.8; margin-left:4px; }
  .env-fix-row{ margin-top:8px; padding-top:8px; border-top:1px solid var(--line); }
  .env-fix-row:first-of-type{ border-top:0; padding-top:0; }
  .env-fix-info{ display:flex; gap:7px; align-items:center; flex-wrap:wrap; }
  .env-fix-note{ font-size:10.5px; color:var(--muted); }
  .env-fix-cmd{
    display:block; font-family:var(--mono); font-size:11px; line-height:1.5;
    color:var(--text); background:var(--bg); border:1px solid var(--line);
    border-radius:6px; padding:5px 8px; margin-top:6px; word-break:break-all;
  }
  .env-detected{ font-size:10.5px; color:var(--muted); margin-top:7px; }
  .env-detected b{ color:var(--brand); }
  .muted-detect{ opacity:.75; }
  .env-opts{ display:flex; flex-direction:column; gap:5px; margin-top:7px; }
  .env-opt{
    display:block; border:1px solid var(--line); border-radius:7px; padding:7px 9px;
    cursor:pointer; transition:border-color .15s, background .15s;
  }
  .env-opt:hover{ border-color:var(--line-strong); }
  .env-opt.sel{ border-color:var(--brand); background:rgba(59,130,246,.05); }
  .env-opt input[type=radio]{ margin-right:6px; accent-color:var(--brand); }
  .env-opt-name{ font-size:11.5px; font-weight:600; }
  .env-opt-cmd{
    display:block; font-family:var(--mono); font-size:10.5px; color:var(--muted);
    margin:5px 0 0 20px; word-break:break-all;
  }
  .env-opt-note{ display:block; font-size:10px; color:var(--muted); margin:3px 0 0 20px; opacity:.85; }
  .env-fix-btns{ display:flex; gap:6px; margin-top:6px; flex-wrap:wrap; }
  .env-fix-btns .btn, .env-fix-foot .btn{ font-size:11px; padding:4px 9px; }
  .env-fix-foot{ display:flex; gap:6px; margin-top:8px; }
  .env-ignored{ font-size:10.5px; color:var(--muted); margin-top:5px; }
  .env-ignored .task-link{ font-size:10.5px; margin-left:4px; }
  .card-actions{
    display:flex; gap:6px; align-items:center; flex-wrap:nowrap;
    border-top:1px solid var(--line); padding-top:10px; margin-top:auto;
  }
  .acts{ display:flex; gap:6px; align-items:center; flex:0 0 auto; }
  .more{ display:flex; gap:2px; align-items:center; margin-left:auto; flex:0 0 auto; }
  .card-actions .btn{ padding:6px 10px; font-size:12px; border-radius:8px; white-space:nowrap; }
  .card-actions .more .btn{ padding:6px 7px; }
  .empty{
    text-align:center; color:var(--muted); padding:70px 20px 40px;
    display:flex; flex-direction:column; align-items:center; gap:10px;
  }
  .empty[hidden]{ display:none; }
  [hidden]{ display:none !important; }
  .empty-title{ font-size:16px; color:var(--text); font-weight:600; }
  .empty-sub{ font-size:12.5px; max-width:430px; line-height:1.7; }
  .empty-actions{ display:flex; gap:9px; margin-top:8px; }
  .ports-box{
    margin-top:26px; border:1px solid var(--line); border-radius:12px;
    background:var(--panel); overflow:hidden;
  }
  .ports-box summary{
    cursor:pointer; padding:12px 16px; font-size:12.5px; color:var(--muted); user-select:none;
  }
  .ports-box summary:hover{ color:var(--text); }
  .table-wrap{ overflow:auto; border-top:1px solid var(--line); }
  table{ border-collapse:collapse; width:100%; font-size:12px; }
  th, td{ text-align:left; padding:8px 14px; border-bottom:1px solid var(--line); white-space:nowrap; }
  th{ font-weight:500; color:var(--muted); font-size:11px; background:var(--surface); }
  tbody tr:last-child td{ border-bottom:none; }
  .mono{ font-family:var(--mono); font-size:11.5px; }
  .pathcell{ max-width:420px; overflow:hidden; text-overflow:ellipsis; color:var(--muted); }
  .muted{ color:var(--muted); }
  .modal{
    position:fixed; inset:0; background:rgba(10,10,12,.42); display:none;
    align-items:center; justify-content:center; padding:22px; z-index:50;
  }
  .modal.show{ display:flex; }
  .sheet{
    background:var(--panel); border:1px solid var(--line); border-radius:16px;
    width:100%; max-width:460px; max-height:88vh; display:flex; flex-direction:column;
    box-shadow:0 24px 70px rgba(0,0,0,.28);
  }
  .sheet.wide{ max-width:780px; }
  .sheet-head{ display:flex; align-items:center; padding:15px 18px; border-bottom:1px solid var(--line); }
  .sheet-head h3{ margin:0; font-size:14px; font-weight:600; flex:1; }
  .x{
    border:none; background:transparent; color:var(--muted); font-size:14px;
    cursor:pointer; padding:2px 6px; border-radius:6px;
  }
  .x:hover{ background:var(--surface); color:var(--text); }
  .sheet-body{ padding:16px 18px; display:flex; flex-direction:column; gap:12px; overflow:auto; }
  .sheet-foot{
    display:flex; justify-content:flex-end; gap:8px; padding:13px 18px;
    border-top:1px solid var(--line);
  }
  .field{ display:flex; flex-direction:column; gap:5px; font-size:12px; color:var(--muted); }
  .field input, .field textarea{
    border:1px solid var(--line); background:var(--surface); color:var(--text);
    border-radius:9px; padding:8px 10px; font:inherit; font-size:12.5px;
    outline:none; width:100%; resize:vertical;
  }
  .field input:focus, .field textarea:focus{ border-color:var(--brand); }
  .input-row{ display:flex; gap:8px; align-items:center; }
  .input-row input{ flex:1; min-width:0; width:auto; }
  .input-row .btn{ flex:0 0 auto; }
  .hint{ font-size:11.5px; color:var(--muted); line-height:1.6; }
  .ai-row{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .ai-row .hint{ flex:1; min-width:160px; }
  .sel{
    border:1px solid var(--line); background:var(--surface); color:var(--text);
    border-radius:9px; padding:7px 8px; font:inherit; font-size:12px; outline:none;
  }
  .sel:focus{ border-color:var(--brand); }
  .auto-check{ display:flex; align-items:center; gap:4px; font-size:11.5px; color:var(--muted); cursor:pointer; }
  .auto-check input{ accent-color:var(--brand); }
  .scan-bar{ display:flex; align-items:center; gap:10px; }
  .check{ display:flex; align-items:center; gap:5px; font-size:12px; color:var(--muted); cursor:pointer; margin-left:auto; }
  .scan-list{
    display:flex; flex-direction:column; gap:7px; max-height:44vh; overflow:auto;
    border:1px solid var(--line); border-radius:10px; padding:8px; background:var(--bg);
  }
  .cand{
    display:flex; gap:9px; padding:8px 9px; border:1px solid var(--line);
    border-radius:9px; background:var(--panel); cursor:pointer;
  }
  .cand:hover{ border-color:color-mix(in srgb, var(--brand) 45%, transparent); }
  .cand.exists{ opacity:.62; }
  .cand input{ margin-top:3px; accent-color:var(--brand); }
  .cand-main{ min-width:0; flex:1; display:flex; flex-direction:column; gap:2px; }
  .cand-ops{ margin-left:auto; display:flex; flex-direction:column; gap:4px; align-self:center; flex:0 0 auto; }
  .aimini{ font-size:11px; padding:3px 9px; white-space:nowrap; }
  .cand-status{ font-size:11px; line-height:1.5; color:var(--muted); }
  .cand-status.ok{ color:var(--brand); }
  .cand-status.err{ color:var(--danger); }
  .cand-env{ font-size:11px; line-height:1.5; color:var(--warn); }
  .cand-name{
    font-size:12.5px; font-weight:600; display:flex; align-items:center; gap:6px; flex-wrap:wrap;
  }
  .cand-path{
    font-family:var(--mono); font-size:11px; color:var(--muted);
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }
  .cand-cmd{ font-size:11px; color:var(--muted); display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
  .cand-cmd code{ font-family:var(--mono); font-size:11px; color:var(--text); }
  .gh-repo{
    display:flex; flex-direction:column; gap:6px; padding:12px 14px;
    border:1px solid var(--line); border-radius:12px; background:var(--surface);
  }
  .gh-repo .top{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .gh-repo .slug{ font-family:var(--mono); font-size:12px; color:var(--text); }
  .gh-repo .desc{ font-size:11.5px; line-height:1.6; color:var(--muted); }
  .gh-flow{ display:flex; flex-direction:column; gap:8px; }
  .gh-flow .row{ display:flex; gap:10px; align-items:flex-start; font-size:11.5px; line-height:1.6; }
  .gh-flow .k{ flex:0 0 58px; color:var(--muted); }
  .gh-flow .v{ flex:1; min-width:0; }
  .gh-flow code{ font-family:var(--mono); font-size:11px; color:var(--text); word-break:break-all; }
  .gh-flow .v span{ display:block; color:var(--muted); }
  .gh-warn{ font-size:11.5px; line-height:1.6; color:var(--warn); }
  .gh-note{ font-size:11.5px; line-height:1.6; color:var(--muted); }
  .gh-readme summary{ font-size:11.5px; color:var(--muted); cursor:pointer; }
  .gh-readme pre{
    margin:6px 0 0; max-height:220px; overflow:auto; white-space:pre-wrap;
    font-family:var(--mono); font-size:11px; line-height:1.6; color:var(--muted);
    background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:10px 12px;
  }
  #ghLog{ height:240px; }
  .log{
    margin:0; background:var(--bg); border:1px solid var(--line); border-radius:10px;
    padding:12px; font-family:var(--mono); font-size:11.5px; line-height:1.6;
    height:46vh; overflow:auto; white-space:pre-wrap; word-break:break-all; color:var(--text);
  }
  /* 项目诊断面板 */
  .diag{ position:fixed; inset:0; z-index:70; display:flex; justify-content:flex-end; }
  .diag-mask{ position:absolute; inset:0; background:rgba(10,10,12,.36); }
  .diag-panel{
    position:relative; width:min(480px,100%); height:100%; background:var(--panel);
    border-left:1px solid var(--line); display:flex; flex-direction:column; min-height:0;
    box-shadow:-30px 0 70px rgba(0,0,0,.22); animation:diagIn 180ms var(--ease);
  }
  @keyframes diagIn{ from{ transform:translateX(20px); opacity:0; } to{ transform:none; opacity:1; } }
  @media (prefers-reduced-motion: reduce){ .diag-panel{ animation:none; } }
  .diag-head{ display:flex; align-items:flex-start; gap:10px; padding:15px 18px 12px; border-bottom:1px solid var(--line); }
  .diag-title{ display:flex; align-items:flex-start; gap:9px; min-width:0; flex:1; }
  .diag-main{ min-width:0; }
  .diag-name{ font-size:14.5px; font-weight:600; letter-spacing:-.01em; min-width:0; }
  .diag-sub{ font-family:var(--mono); font-size:11px; color:var(--muted); margin-top:3px; }
  .diag-ctx{ display:flex; flex-wrap:wrap; gap:6px; padding:10px 18px; border-bottom:1px solid var(--line); }
  .dchip{
    font-family:var(--mono); font-size:10.5px; color:var(--muted);
    border:1px solid var(--line); border-radius:6px; padding:2px 7px;
    max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }
  .dchip.warn{ color:var(--warn); border-color:color-mix(in srgb, var(--warn) 40%, transparent); }
  .diag-log{
    flex:1; min-height:0; overflow:auto; padding:16px 18px; background:var(--bg);
    display:flex; flex-direction:column; gap:14px;
  }
  .msg{ display:flex; flex-direction:column; gap:5px; }
  .msg-role{
    font-family:var(--mono); font-size:9.5px; letter-spacing:.1em;
    text-transform:uppercase; color:var(--muted);
    display:flex; align-items:center; gap:6px;
  }
  .tool-ico{ display:inline-flex; width:13px; height:13px; flex:0 0 auto; }
  .tool-ico svg{ width:13px; height:13px; display:block; }
  .tool-ico.ollama{ color:var(--brand); }
  .tool-ico.claude{ color:#D97757; }
  .tool-ico.openclaw{ color:#6C8DF5; }
  .tool-ico.http, .tool-ico.custom, .tool-ico.default{ color:var(--muted); }
  .msg-body{ font-size:13px; line-height:1.65; white-space:pre-wrap; word-break:break-word; }
  .msg.user .msg-body{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:8px 11px; }
  .msg.error .msg-body{ color:var(--danger); }
  .diag-quick{ display:flex; flex-wrap:wrap; gap:6px; padding:10px 18px 0; }
  .dq{
    border:1px solid var(--line); background:var(--panel); color:var(--muted);
    border-radius:999px; padding:4px 10px; font:inherit; font-size:11px; cursor:pointer;
  }
  .dq:hover{ color:var(--text); border-color:var(--line-strong); }
  .diag-form{ border-top:1px solid var(--line); padding:12px 18px 16px; display:flex; flex-direction:column; gap:8px; }
  .diag-row{ display:flex; gap:6px; align-items:center; }
  .diag-row .sel{ min-width:0; flex:1; }
  .diag-row .btn{ flex:0 0 auto; }
  .diag-form textarea{
    width:100%; border:1px solid var(--line); background:var(--surface); color:var(--text);
    border-radius:9px; padding:8px 10px; font:inherit; font-size:12.5px; line-height:1.6;
    resize:vertical; outline:none;
  }
  .diag-form textarea:focus{ border-color:var(--brand); }
  .diag-status{ font-size:11px; color:var(--muted); min-height:14px; }
  .toast{
    position:fixed; left:50%; bottom:28px; transform:translate(-50%,14px);
    background:var(--text); color:var(--bg); padding:9px 16px; border-radius:999px;
    font-size:12.5px; opacity:0; pointer-events:none; transition:opacity .2s, transform .2s;
    z-index:80; max-width:80vw; text-align:center;
  }
  .toast.show{ opacity:1; transform:translate(-50%,0); }
  @media (max-width:900px){
    .rail{ width:64px; flex-basis:64px; padding:12px 9px; }
    .brand{ padding:4px 0 12px; justify-content:center; }
    .brand span:last-child, .nav-btn span:not(.kbd), .nav-btn .kbd, .sec, .rail-foot,
    .row .n, .row .m{ display:none; }
    .nav-btn{ justify-content:center; padding:9px 0; }
    .row{ justify-content:center; padding:8px 0; }
    .topbar{ padding:11px 16px; }
    .page{ padding:16px 16px 40px; }
  }
  @media (max-width:640px){
    .topbar{ padding:10px 14px; }
    .page{ padding:14px 14px 40px; }
    .card{ padding:15px 14px 11px; border-radius:14px; }
    .card-actions .btn{ padding:8px 11px; min-height:38px; }
    .card-actions .more .btn{ padding:8px 9px; }
  }



  /* == MINIMAL-TOPNAV v2 == */
  /* 极简灰阶（Tailwind Zinc）：保留原变量名，仅覆盖色值，确保组件样式自动继承 */
  :root{
    --bg:#FAFAFA; --panel:#FFFFFF; --surface:#F4F4F5;
    --text:#18181B; --muted:#71717A; --faint:#A1A1AA;
    --line:#E4E4E7; --line-strong:#D4D4D8;
    --brand:#15803D; --brand-ink:#15803D; --brand-soft:#E8F5EC;
    --ok:#15803D; --ok-soft:#E8F5EC; --off:#A1A1AA;
    --danger:#B42318; --warn:#A2610A;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#09090B; --panel:#18181B; --surface:#27272A;
      --text:#FAFAFA; --muted:#A1A1AA; --faint:#71717A;
      --line:#3F3F46; --line-strong:#52525B;
      --brand:#34D399; --brand-ink:#34D399; --brand-soft:#12261F;
      --ok:#34D399; --ok-soft:#12261F; --off:#71717A;
      --danger:#F87171; --warn:#E8A34A;
    }
  }
  /* 顶栏：把原左侧 rail 翻成横向 header，分组/筛选并排主区 */
  html,body{ background:var(--bg); }
  .app{ display:block; height:auto; min-height:100vh; }
  .rail{
    width:100%; flex:none; height:auto; min-height:56px;
    flex-direction:row; align-items:center; gap:8px 16px; flex-wrap:wrap;
    padding:9px 22px; border-right:0; border-bottom:1px solid var(--line);
    position:sticky; top:0; z-index:20; background:var(--panel);
  }
  .brand{ padding:0 16px 0 0; font-size:14px; }
  .mark{ width:18px; height:18px; background:var(--text); }
  .rail-nav{ flex-direction:row; gap:4px; }
  .nav-btn{ padding:7px 12px; font-size:12.5px; gap:7px; }
  .nav-btn svg{ width:14px; height:14px; }
  .nav-btn.is-active{ background:var(--surface); }
  .nav-btn.is-active::before{ display:none; }
  .nav-btn .kbd{ display:none; }
  .rail-sep{ width:1px; height:20px; margin:0 2px; }
  .sec{ font-size:10px; padding:0 6px 0 0; }
  #groups, #filters{
    flex:0 1 auto; flex-direction:row; flex-wrap:wrap; gap:4px;
    overflow:visible; min-height:0; align-items:center;
  }
  .row{
    display:inline-flex; align-items:center; gap:6px;
    padding:3px 10px; font-size:11.5px; white-space:nowrap;
    border:1px solid var(--line); border-radius:999px; background:transparent;
  }
  .row.on{ background:var(--text); color:var(--panel); border-color:var(--text); font-weight:600; }
  .row .dot{ width:6px; height:6px; margin-top:0; }
  .dot{ box-shadow:none !important; }
  .rail-foot{
    margin:0 0 0 auto; padding:0; border:0; display:flex;
    align-items:center; gap:10px; text-align:left;
  }
  .foot-line{ padding:0; }
  /* 主区：顶栏已占头部，收掉一级内边距 */
  .main, .page{ min-height:0; }
  .topbar h1{ font-size:19px; }
  /* 去装饰：卡片无阴影、按钮无位移 */
  .card{ box-shadow:none; border-radius:11px; }
  .card:hover{ box-shadow:none; }
  .btn:hover{ transform:none; }
  .btn.primary{ background:var(--text); border-color:var(--text); color:var(--panel); }
  /* v2 新增：卡片网格改为自适应列宽——卡片少时自动变宽铺满，卡片多时自然分列 */
  .group-grid{ grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:14px; align-items:stretch; }
  .group-grid > .card{ width:100%; min-width:0; }
  /* 窄屏：顶栏自然换行，不做图标栏收缩 */
  @media (max-width:900px){
    .rail{ width:100% !important; flex-basis:auto !important; padding:9px 14px; }
    .brand span:last-child, .nav-btn span:not(.kbd), .nav-btn .kbd, .sec, .rail-foot,
    .row .n, .row .m{ display:inline !important; }
    .nav-btn{ justify-content:flex-start; padding:7px 12px; }
    .row{ justify-content:flex-start; padding:3px 10px; }
  }
  /* == END MINIMAL-TOPNAV v2 == */
  </style>
</head>
<body>
<div class="app">
  <aside class="rail">
    <div class="brand"><span class="mark"></span><span>AI Hub</span></div>
    <div class="rail-nav">
      <a class="nav-btn is-active" href="/projects">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1.2"/><rect x="9" y="2.5" width="4.5" height="4.5" rx="1.2"/><rect x="2.5" y="9" width="4.5" height="4.5" rx="1.2"/><rect x="9" y="9" width="4.5" height="4.5" rx="1.2"/></svg>
        <span>项目</span><span class="kbd">⌘1</span>
      </a>
      <a class="nav-btn" href="/models">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="5.8" cy="5.8" r="3"/><path d="M8 8l5 5M10.8 10.8l1.4-1.4M12 12l1.4-1.4" stroke-linecap="round"/></svg>
        <span>模型</span><span class="kbd">⌘2</span>
      </a>
      <a class="nav-btn" href="/tools">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M10.5 2.5l3 3-2 2-3-3zM8.5 4.5L2.5 10.5v3h3l6-6" stroke-linecap="round" stroke-linejoin="round"/></svg>
        <span>工具</span><span class="kbd">⌘3</span>
      </a>
    </div>
    <div class="rail-sep"></div>
    <div class="sec">分组</div>
    <div id="groups"></div>
    <div class="rail-foot">
      <div class="foot-line">127.0.0.1:8799</div>
    </div>
  </aside>
  <main class="main">
    <header class="topbar">
      <h1>项目</h1>
      <div class="summary" id="summary">加载中…</div>
      <div class="tools">
        <button class="btn" id="ghBtn">从 GitHub 导入</button>
        <button class="btn" id="scanBtn">扫描项目</button>
        <button class="btn primary" id="addBtn">＋ 新增项目</button>
      </div>
    </header>
    <div class="page">
      <div class="empty" id="empty" hidden>
        <div class="empty-title">还没有项目</div>
        <div class="empty-sub">自动扫描本机目录，或手动添加项目目录与启动命令。启动后会自动检测端口、PID、内存与日志。</div>
        <div class="empty-actions">
          <button class="btn primary" id="emptyScan">扫描项目</button>
          <button class="btn" id="emptyAdd">手动添加</button>
        </div>
      </div>
      <div class="grid" id="grid"></div>
      <details class="ports-box" id="portsBox">
        <summary>本机监听端口<span id="portCount"></span></summary>
        <div class="table-wrap"><table id="portsTable"></table></div>
      </details>
    </div>
  </main>
</div>

<div class="toast" id="toast"></div>

<div class="modal" id="editModal">
  <div class="sheet">
    <div class="sheet-head"><h3 id="editTitle">新增项目</h3><button class="x" data-close="editModal">✕</button></div>
    <div class="sheet-body">
      <input type="hidden" id="pID">
      <label class="field">项目名称<input id="pName" placeholder="如：约球吧"></label>
      <div class="field">项目目录<div class="input-row"><input id="pPath" placeholder="/Users/you/code/project"><button class="btn subtle" type="button" id="pPathPick">选择…</button></div></div>
      <div class="ai-row">
        <select class="sel" id="aiProvider" title="AI 后端"></select>
        <button class="btn" id="aiBtn" type="button">✦ AI 识别</button>
        <label class="auto-check"><input type="checkbox" id="autoVerify" checked>识别后自动试运行</label>
        <span class="hint" id="aiStatus">AI 只给建议，试运行确认后再保存</span>
      </div>
      <div class="field"><span>启动命令</span>
        <div class="input-row">
          <input id="pStart" placeholder="npm run dev">
          <button class="btn" id="verifyBtn" type="button">▶ 试运行</button>
        </div>
      </div>
      <label class="field">端口（可选）<input id="pPort" placeholder="3000"></label>
      <label class="field">后台路径（可选）<input id="pAdmin" placeholder="/admin"></label>
      <div class="hint">端口仅用于启动前检查冲突，实际端口自动检测；填写后台路径后，卡片会多一个「后台」入口。</div>
    </div>
    <div class="sheet-foot">
      <button class="btn" data-close="editModal">取消</button>
      <button class="btn primary" id="saveBtn">保存</button>
    </div>
  </div>
</div>

<div class="modal" id="scanModal">
  <div class="sheet wide">
    <div class="sheet-head"><h3 id="scanTitle">扫描项目</h3><button class="x" data-close="scanModal">✕</button></div>
    <div class="sheet-body">
      <label class="field">扫描目录（每行一个）<textarea id="scanRoots" rows="3" placeholder="~/Documents"></textarea></label>
      <div class="scan-bar">
        <button class="btn" id="scanRun">开始扫描</button>
        <span class="hint" id="scanStatus">支持 Node / Spring / Django / Go / Rust / Docker 等常见项目</span>
        <label class="check"><input type="checkbox" id="scanAll"> 全选</label>
      </div>
      <div class="scan-list" id="scanList"></div>
    </div>
    <div class="sheet-foot">
      <button class="btn" data-close="scanModal">取消</button>
      <button class="btn primary" id="scanImport">添加所选</button>
    </div>
  </div>
</div>

<div class="modal" id="ghModal">
  <div class="sheet wide">
    <div class="sheet-head"><h3 id="ghTitle">从 GitHub 导入</h3><button class="x" data-close="ghModal">✕</button></div>
    <div class="sheet-body">
      <label class="field">仓库地址<input id="ghUrl" placeholder="https://github.com/owner/repo 或 owner/repo"></label>
      <div class="scan-bar">
        <button class="btn" id="ghInspect">分析仓库</button>
        <span class="hint" id="ghStatus">只读取 README 与配置文件，不下载代码</span>
      </div>
      <div id="ghPlan" hidden>
        <div class="gh-repo" id="ghRepo"></div>
        <div class="field">安装目录<div class="input-row"><input id="ghDir" placeholder="~/Projects/owner__repo"><button class="btn subtle" type="button" id="ghDirPick">选择…</button></div></div>
        <div class="gh-flow" id="ghFlow"></div>
        <div id="ghWarn"></div>
        <details class="gh-readme" id="ghReadmeBox" hidden>
          <summary>README 摘要</summary>
          <pre id="ghReadme"></pre>
        </details>
      </div>
      <pre class="log" id="ghLog" hidden></pre>
    </div>
    <div class="sheet-foot">
      <button class="btn" data-close="ghModal">取消</button>
      <button class="btn primary" id="ghImport" disabled>克隆并准备环境</button>
    </div>
  </div>
</div>

<div class="modal" id="logModal">
  <div class="sheet wide">
    <div class="sheet-head"><h3 id="logTitle">日志</h3><button class="x" data-close="logModal">✕</button></div>
    <div class="sheet-body">
      <pre class="log" id="logBody"></pre>
      <div class="hint mono" id="logPath"></div>
    </div>
    <div class="sheet-foot">
      <button class="btn" id="logRefresh">刷新</button>
      <button class="btn" data-close="logModal">关闭</button>
    </div>
  </div>
</div>

<div class="diag" id="diag" hidden>
  <div class="diag-mask" id="diagMask"></div>
  <aside class="diag-panel" role="dialog" aria-modal="true" aria-labelledby="diagName">
    <header class="diag-head">
      <div class="diag-title">
        <span class="dot" id="diagDot"></span>
        <div class="diag-main">
          <div class="diag-name" id="diagName">诊断</div>
          <div class="diag-sub" id="diagSub"></div>
        </div>
      </div>
      <button class="x" id="diagClose" type="button" aria-label="关闭诊断">✕</button>
    </header>
    <div class="diag-ctx" id="diagCtx"></div>
    <div class="diag-log" id="diagLog"></div>
    <div class="diag-quick" id="diagQuick">
      <button class="dq" type="button" data-q="为什么起不来？先看最近日志，给出最可能的原因和下一步排查命令。">为什么起不来</button>
      <button class="dq" type="button" data-q="这个项目现在跑在哪个端口？端口有没有被别的进程占用？">端口被占了吗</button>
      <button class="dq" type="button" data-q="检查这个项目的环境依赖和启动命令是否正确，缺什么告诉我怎么装。">环境缺什么</button>
    </div>
    <form class="diag-form" id="diagForm">
      <div class="diag-row">
        <select class="sel" id="diagProvider" title="诊断后端">
          <option value="claude">Claude Code</option>
          <option value="ollama">Ollama</option>
        </select>
        <select class="sel" id="diagModel" title="模型"></select>
        <button class="btn primary" id="diagSend" type="submit">发送</button>
      </div>
      <textarea id="diagInput" rows="3" placeholder="问这个项目的问题，⌘ + Enter 发送"></textarea>
      <div class="diag-status" id="diagStatus"></div>
    </form>
  </aside>
</div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }[c]));

let projects = [], listeners = [], scanResults = [], pending = {};
let logId = null, logTimer = null, toastTimer = null, activeGroup = null;

async function api(url, body){
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-AIHub": "1" }, body: JSON.stringify(body)
  };
  const res = await fetch(url, opt);
  return await res.json();
}

function fmtUptime(sec){
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return h + "小时" + m + "分";
  if (m) return m + "分" + s + "秒";
  return s + "秒";
}

function toast(msg){
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

async function refresh(){
  try{
    const data = await api("/api/projects");
    projects = data.projects || [];
    projects.sort((a, b) => (a.group || "").localeCompare(b.group || ""));
    listeners = data.listeners || [];
    render();
    if (diag.id && !$("#diag").hidden){
      const fresh = projects.find(x => x.id === diag.id);
      if (fresh){
        diag.project = fresh;
        renderDiagHead();
        renderDiagCtx();
      }
    }
  }catch(err){
    $("#summary").textContent = "服务未连接";
  }
}

function renderGroups(){
  const counts = new Map();
  for (const p of projects){
    const k = p.group || "";
    counts.set(k, (counts.get(k) || 0) + 1);
  }
  if (activeGroup !== null && !counts.has(activeGroup)) activeGroup = null;
  const row = (key, label, n) =>
    '<div class="row ' + (activeGroup === key ? "on" : "") + '" data-g="' +
      (key === null ? "__all__" : key === "" ? "__none__" : esc(key)) + '" title="' + esc(label) + ' · ' + n + '">' +
      '<span class="dot ' + (activeGroup === key ? "on" : "") + '"></span>' +
      '<span class="n">' + esc(label) + '</span><span class="m">' + n + '</span></div>';
  const html = row(null, "全部", projects.length) +
    [...counts.entries()].map(([k, n]) => row(k, k || "未分组", n)).join("");
  const el = $("#groups");
  el.innerHTML = html;
  el.querySelectorAll("[data-g]").forEach(r => r.onclick = () => {
    const v = r.dataset.g;
    activeGroup = v === "__all__" ? null : v === "__none__" ? "" : v;
    render();
  });
}

function render(){
  const running = projects.filter(p => p.running).length;
  $("#summary").textContent = projects.length
    ? projects.length + " 个项目 · " + running + " 个运行中"
    : "暂无项目";
  $("#empty").hidden = projects.length > 0;
  renderGroups();
  const list = activeGroup === null ? projects : projects.filter(p => (p.group || "") === activeGroup);
  const sections = [];
  for (const p of list){
    const key = p.group || ("#" + p.id);
    let sec = sections[sections.length - 1];
    if (!sec || sec.key !== key){
      sec = { key, items: [] };
      sections.push(sec);
    }
    sec.items.push(p);
  }
  $("#grid").innerHTML = sections.map(sec =>
    '<div class="group-grid">' + sec.items.map(card).join("") + "</div>"
  ).join("");
  bindCards();
  renderPorts();
}

function envLine(env){
  const issues = (env && env.issues) || [];
  if (!issues.length) return "";
  return '<div class="env-warn" title="' + esc(issues.join("；")) + '">⚠ ' + esc(issues[0]) +
    (issues.length > 1 ? "（共 " + issues.length + " 项）" : "") + "</div>";
}

function envFixCard(env, p){
  const issues = (env && env.issues) || [];
  if (!issues.length) return "";
  if (env && env.ignored){
    return '<div class="env-ignored">环境提示已忽略 ' +
      '<button class="task-link" data-act="env-restore" data-id="' + esc(p.id) + '">恢复</button></div>';
  }
  const fixes = (env && env.fixes) || [];
  const head = '<div class="env-fix">' +
    '<div class="env-fix-head">⚠ ' + esc(issues[0]) +
      (issues.length > 1 ? '<span class="env-fix-more">（共 ' + issues.length + ' 项）</span>' : "") +
    "</div>";
  if (!fixes.length) return head + "</div>";
  const rows = fixes.map((f, i) => {
    const detected = f.detected
      ? '<div class="env-detected">🔍 检测到本机已装 <b>' + esc(f.detected) + '</b></div>'
      : '<div class="env-detected muted-detect">🔍 未检测到版本管理工具，可选下方方式</div>';
    const opts = (f.options || []).map((o, j) =>
      '<label class="env-opt' + (o.recommended ? " rec" : "") + '" data-fix="' + i + '" data-opt="' + j + '">' +
        '<input type="radio" name="envfix-' + esc(p.id) + '-' + i + '"' +
          (o.recommended ? " checked" : "") +
          ' data-act="env-opt" data-fix="' + i + '" data-opt="' + j + '">' +
        '<span class="env-opt-name">' + esc(o.tool) +
          (o.available ? ' <span class="tag brand">已装</span>' : ' <span class="tag">未装</span>') +
          (o.recommended ? ' <span class="tag warn">推荐</span>' : "") +
        "</span>" +
        '<code class="env-opt-cmd">' + esc(o.command) + "</code>" +
        '<span class="env-opt-note">' + esc(o.note) + "</span>" +
      "</label>").join("");
    const best = (f.options || []).find(o => o.recommended) || {};
    return '<div class="env-fix-row">' +
        '<div class="env-fix-info"><span class="tag warn">' + esc(f.label) + " " + esc(f.version) + "</span></div>" +
        detected +
        '<div class="env-opts">' + opts + "</div>" +
        '<code class="env-fix-cmd" data-cmd-for="' + i + '">' + esc(best.command || f.command) + "</code>" +
        '<div class="env-fix-btns">' +
          '<button class="btn subtle" data-act="env-copy" data-id="' + esc(p.id) +
            '" data-fix="' + i + '">复制命令</button>' +
          '<button class="btn subtle" data-act="env-bind" data-id="' + esc(p.id) +
            '" data-fix="' + i + '" title="只记录所选方式，不执行命令">记住选择</button>' +
          '<button class="btn subtle primary" data-act="env-auto" data-id="' + esc(p.id) +
            '" data-fix="' + i + '" title="自动执行安装命令并绑定项目">自动安装</button>' +
        "</div>" +
      "</div>";
  }).join("");
  return head + rows +
    '<div class="env-fix-foot">' +
      '<button class="btn subtle" data-act="env-recheck" data-id="' + esc(p.id) + '">重新检测</button>' +
      '<button class="btn subtle" data-act="env-ignore" data-id="' + esc(p.id) + '">忽略</button>' +
    "</div></div>";
}

function envBadges(env){
  const req = (env && env.requirements) || {};
  const labels = { node: "Node", java: "JDK", go: "Go", rust: "Rust", python: "Python" };
  const out = [];
  ["node", "java", "go", "rust", "python"].forEach(k => {
    const spec = req[k] && req[k].spec;
    if (spec) out.push('<span class="chip muted" title="项目声明的 ' + labels[k] + ' 版本">' +
      labels[k] + " " + esc(spec) + "</span>");
  });
  return out.length ? '<div class="env-badges">' + out.join("") + "</div>" : "";
}

function card(p){
  const linkFor = n => (p.portUrls && p.portUrls[String(n)]) || ("http://localhost:" + n);
  const baseUrl = p.ports.length ? linkFor(p.ports[0]) : "";
  const ports = p.ports.length
    ? p.ports.map(n => '<a class="chip port" href="' + linkFor(n) + '" target="_blank" rel="noreferrer">:' + n + "</a>").join("")
    : (p.port
        ? '<span class="chip muted" title="配置端口（未运行）">:' + p.port + "</span>"
        : '<span class="chip muted">未检测到端口</span>');
  const status = p.starting ? "启动中…" : (p.running ? (p.managed ? "运行中" : "外部运行") : "已停止");
  const statusCls = p.starting ? "warn" : (p.running ? "ok" : "off");
  const meta = [];
  if (p.pids.length) meta.push("PID " + p.pids.join(","));
  if (p.uptime) meta.push(fmtUptime(p.uptime));
  if (p.running) meta.push("CPU " + p.cpu + "%");
  if (p.running && p.mem) meta.push(p.mem + "MB");
  if (p.commit) meta.push("commit " + p.commit);

  let actions;
  if (p.running){
    actions =
      '<button class="btn" data-act="stop" data-id="' + p.id + '">停止</button>' +
      '<button class="btn" data-act="restart" data-id="' + p.id + '">重启</button>' +
      (baseUrl ? '<button class="btn" data-act="open" data-url="' + baseUrl + '">打开</button>' : "") +
      (baseUrl && p.admin
        ? '<button class="btn" data-act="open" data-url="' + baseUrl + esc(p.admin) + '">后台</button>'
        : "");
  }else{
    actions = '<button class="btn primary" data-act="start" data-id="' + p.id + '"' +
      (pending[p.id] ? " disabled" : "") + ">" + (pending[p.id] ? "启动中…" : "启动") + "</button>";
  }

  return '<article class="card ' + (p.running ? "live" : "") + '">' +
    '<div class="card-top">' +
      '<span class="dot ' + (p.running ? "on" : "") + '"></span>' +
      '<div class="names">' +
        '<div class="name">' +
          (p.group ? '<span class="group">' + esc(p.group) + '</span>' : "") +
          esc(p.name) + (p.type ? "<em>" + esc(p.type) + "</em>" : "") +
        "</div>" +
        '<div class="status ' + statusCls + '">' + status + "</div>" +
        '<button class="task-link" data-act="diag" data-id="' + p.id + '">诊断 →</button>' +
      "</div>" +
      '<div class="ports">' + ports + "</div>" +
    "</div>" +
    '<div class="path" title="' + esc(p.path) + '">' + esc(p.path) + "</div>" +
    '<div class="cmd"><span>启动命令</span><code title="' + esc(p.start || "") + '">' +
      esc(p.start || "未配置") + "</code></div>" +
    envBadges(p.env) +
    envFixCard(p.env, p) +
    (p.envBroken ? '<div class="env-warn">⚠ ' + esc(p.envBroken) + "</div>" : "") +
    (meta.length ? '<div class="meta">' + meta.map(x => "<span>" + esc(x) + "</span>").join("") + "</div>" : "") +
    '<div class="card-actions">' +
      '<div class="acts">' + actions + "</div>" +
      '<div class="more">' +
        '<button class="btn subtle" data-act="log" data-id="' + p.id + '">日志</button>' +
        (p.source ? '<button class="btn subtle" data-act="update" data-id="' + p.id + '">更新</button>' : "") +
        (p.envBroken ? '<button class="btn subtle" data-act="prepare" data-id="' + p.id + '">修复环境</button>' : "") +
        '<button class="btn subtle" data-act="edit" data-id="' + p.id + '">编辑</button>' +
        '<button class="btn subtle danger" data-act="del" data-id="' + p.id + '">删除</button>' +
      "</div>" +
    "</div>" +
  "</article>";
}

function bindCards(){
  document.querySelectorAll("#grid [data-act]").forEach(el => {
    el.onclick = () => {
      const id = el.dataset.id, act = el.dataset.act;
      if (act === "open") return window.open(el.dataset.url, "_blank");
      if (act === "diag") return openDiag(id);
      if (act === "log") return openLog(id);
      if (act === "edit") return openEdit(id);
      if (act === "del") return delProject(id);
      if (act === "update") return projectUpdate(id);
      if (act === "env-opt") return envOptChanged(el);
      if (act === "env-copy") return envCopyCmd(id, el.dataset.fix);
      if (act === "env-bind") return envBind(id, el.dataset.fix);
      if (act === "env-recheck") return envRecheck(id);
      if (act === "env-ignore") return envIgnore(id, true);
      if (act === "env-restore") return envIgnore(id, false);
      if (act === "env-auto") return envAuto(id, el.dataset.fix);
      return doAction(id, act);
    };
  });
}

function copyText(text){
  if (navigator.clipboard && navigator.clipboard.writeText){
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve, reject) => {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try{ document.execCommand("copy"); resolve(); }
    catch(e){ reject(e); }
    finally{ document.body.removeChild(ta); }
  });
}

function selectedOption(id, fixIndex){
  const p = projects.find(x => x.id === id);
  if (!p) return null;
  const f = (p.env && p.env.fixes || [])[Number(fixIndex)];
  if (!f || !(f.options || []).length) return null;
  // 在页面里找到该项目卡片内、该 fix 下被选中的 radio
  const radios = document.querySelectorAll('input[data-act="env-opt"][data-fix="' + fixIndex + '"]');
  for (const r of radios){
    const root = r.closest('.env-fix');
    const copyBtn = root && root.querySelector('[data-act="env-copy"]');
    if (copyBtn && copyBtn.dataset.id === id && r.checked){
      return (f.options || [])[Number(r.dataset.opt)] || null;
    }
  }
  const rec = (f.options || []).find(o => o.recommended);
  return rec || f.options[0] || null;
}

function envOptChanged(el){
  const fixIndex = el.dataset.fix;
  const root = el.closest('.env-fix');
  if (!root) return;
  const copyBtn = root.querySelector('[data-act="env-copy"]');
  if (!copyBtn) return;
  const p = projects.find(x => x.id === copyBtn.dataset.id);
  if (!p) return;
  const f = (p.env && p.env.fixes || [])[Number(fixIndex)];
  const o = f && (f.options || [])[Number(el.dataset.opt)];
  if (!o) return;
  const codeEl = root.querySelector('[data-cmd-for="' + fixIndex + '"]');
  if (codeEl) codeEl.textContent = o.command;
  root.querySelectorAll('.env-opt[data-fix="' + fixIndex + '"]').forEach(l => l.classList.remove('sel'));
  const label = root.querySelector('.env-opt[data-fix="' + fixIndex + '"][data-opt="' + el.dataset.opt + '"]');
  if (label) label.classList.add('sel');
}

function envCopyCmd(id, fixIndex){
  const p = projects.find(x => x.id === id);
  if (!p) return;
  const o = selectedOption(id, fixIndex);
  const f = (p.env && p.env.fixes || [])[Number(fixIndex)];
  const cmd = (o && o.command) || (f && f.command);
  if (!cmd){ toast("没有可复制的命令"); return; }
  copyText(cmd).then(() => toast("已复制：" + cmd))
    .catch(() => toast("复制失败，请手动选择命令"));
}

async function envBind(id, fixIndex){
  const p = projects.find(x => x.id === id);
  if (!p) return;
  const o = selectedOption(id, fixIndex);
  const f = (p.env && p.env.fixes || [])[Number(fixIndex)];
  if (!f || !o){ toast("请先选择一种方式"); return; }
  try{
    const res = await api("/api/env/switch", {
      path: p.path, kind: f.kind, version: f.version, provider: o.provider || ""
    });
    toast(res.ok ? ("已记住：" + o.tool + "（" + f.label + " " + f.version + "）") : (res.message || "保存失败"));
    await refresh();
  }catch(err){
    toast("保存失败：" + err.message);
  }
}

async function envAuto(id, fixIndex){
  const p = projects.find(x => x.id === id);
  if (!p) return;
  const o = selectedOption(id, fixIndex);
  const f = (p.env && p.env.fixes || [])[Number(fixIndex)];
  if (!f || !o){ toast("请先选择一种安装方式"); return; }
  if (!confirm("自动执行：" + o.command + "\n\n安装并绑定 " + f.label + " " + f.version +
               " 到该项目，可能需要几分钟。继续？")) return;
  try{
    const res = await api("/api/env/auto-install", {
      path: p.path, kind: f.kind, tool: o.tool
    });
    if (!res.ok){ toast(res.message || "启动安装失败"); return; }
    toast("已开始安装：" + o.tool + " · " + f.label + " " + f.version);
    pollEnvJob(res.job, id);
  }catch(err){
    toast("启动失败：" + err.message);
  }
}

function pollEnvJob(jobId, projectId){
  const timer = setInterval(async () => {
    try{
      const res = await api("/api/github/job?id=" + encodeURIComponent(jobId));
      if (!res.ok || res.status === "running") return;
      clearInterval(timer);
      if (res.status === "done"){
        toast((res.result && res.result.message) || "安装完成");
        await refresh();
        await envRecheck(projectId);
      }else{
        toast("安装失败：" + (res.message || "未知错误"));
        await refresh();
      }
    }catch(err){
      // 网络抖动不中止轮询，下次继续
    }
  }, 1500);
}

async function envRecheck(id){
  const p = projects.find(x => x.id === id);
  if (!p) return;
  try{
    const res = await api("/api/env/detect", { path: p.path });
    toast(res.ok ? (res.issues && res.issues.length ? "仍有 " + res.issues.length + " 项待处理" : "环境已就绪") : (res.message || "检测失败"));
    await refresh();
  }catch(err){
    toast("检测失败：" + err.message);
  }
}

async function envIgnore(id, ignored){
  const p = projects.find(x => x.id === id);
  if (!p) return;
  try{
    const res = await api("/api/env/ignore", { path: p.path, ignored: ignored });
    toast(res.message || "已更新");
    await refresh();
  }catch(err){
    toast("操作失败：" + err.message);
  }
}

async function doAction(id, action){
  const p = projects.find(x => x.id === id);
  if (!p) return;
  if (action === "prepare"){
    try{
      const res = await api("/api/projects/action", { id, action });
      toast(res.message || "");
      if (res.job) watchJobQuiet(res.job);
    }catch(err){
      toast("请求失败：" + err.message);
    }
    return;
  }
  if (action === "start"){ pending[id] = true; render(); }
  if (action === "stop" || action === "restart"){
    if (!confirm("停止「" + p.name + "」？\n若进程是在终端里启动的，将终止该目录下正在监听的进程。")) return;
  }
  try{
    const res = await api("/api/projects/action", { id, action });
    toast(res.message || (res.ok ? "完成" : "操作失败"));
    setTimeout(refresh, 900);
  }catch(err){
    toast("请求失败：" + err.message);
  }finally{
    if (action === "start"){
      setTimeout(() => { delete pending[id]; render(); }, 2500);
    }
  }
}

async function projectUpdate(id){
  const p = projects.find(x => x.id === id);
  if (!p) return;
  toast("正在检查更新…");
  let res;
  try{ res = await api("/api/github/check-update", { id }); }
  catch(err){ return toast("检查失败：" + err.message); }
  if (!res.ok) return toast(res.message || "检查失败");
  if (!res.behind){
    return toast("已是最新（" + (res.branch || "") + " " + res.head + "）");
  }
  const lines = [
    "「" + p.name + "」远端有新提交",
    res.head + " → " + res.remote + "（" + res.branch + "）",
  ];
  if (res.dirty > 0) lines.push("注意：本地有 " + res.dirty + " 个未提交改动，更新会中止");
  lines.push("", "更新会用远端代码覆盖本地，依赖有变化时自动重装。继续？");
  if (!confirm(lines.join("\n"))) return;
  try{
    const r = await api("/api/github/update", { id });
    if (!r.ok) return toast(r.message || "更新失败");
    toast("开始更新…");
    watchJobQuiet(r.id);
  }catch(err){
    toast("更新失败：" + err.message);
  }
}

async function delProject(id){
  const p = projects.find(x => x.id === id);
  if (!p || !confirm("从列表移除「" + p.name + "」？\n不会删除磁盘文件，也不会终止正在运行的进程。")) return;
  const res = await api("/api/projects/delete", { id });
  toast(res.message || "已删除");
  refresh();
}

async function killListener(btn){
  const pid = Number(btn.dataset.kill);
  const name = btn.dataset.name || ("PID " + pid);
  const port = btn.dataset.port;
  if (!confirm("结束进程「" + name + "」 (PID " + pid + ")？\n端口 :" + port + " 将释放。")) return;
  btn.disabled = true;
  btn.textContent = "结束中…";
  try{
    const res = await api("/api/ports/kill", { pid });
    toast(res.message || (res.ok ? "已结束" : "结束失败"));
  }catch(err){
    toast("请求失败：" + err.message);
  }finally{
    setTimeout(refresh, 600);
  }
}

function renderPorts(){
  const rows = listeners.map(l =>
    '<tr><td class="mono">:' + l.port + "</td><td>" + esc(l.command) +
    '</td><td class="mono">' + l.pid + "</td><td>" +
    (l.project ? esc(l.project) : '<span class="muted">—</span>') +
    '</td><td class="mono pathcell">' + (l.cwd ? esc(l.cwd) : '<span class="muted">—</span>') +
    '</td><td><button class="btn subtle danger tiny" data-kill="' + l.pid +
    '" data-name="' + esc(l.command) + '" data-port="' + l.port + '">结束</button></td></tr>').join("");
  $("#portsTable").innerHTML =
    "<thead><tr><th>端口</th><th>进程</th><th>PID</th><th>项目</th><th>工作目录</th><th>操作</th></tr></thead><tbody>" +
    (rows || '<tr><td colspan="6" class="muted">暂无监听端口</td></tr>') + "</tbody>";
  $("#portsTable").querySelectorAll("[data-kill]").forEach(b => b.onclick = () => killListener(b));
  $("#portCount").textContent = listeners.length ? "（" + listeners.length + "）" : "";
}

function openModal(id){ $("#" + id).classList.add("show"); }
function closeModal(id){
  $("#" + id).classList.remove("show");
  if (id === "logModal" && logTimer){ clearInterval(logTimer); logTimer = null; logId = null; }
  if (id === "ghModal" && ghTimer){ clearInterval(ghTimer); ghTimer = null; }
}

let aiTools = null;

async function ensureAiTools(){
  if (aiTools) return aiTools;
  try{ aiTools = await api("/api/ai/tools"); }
  catch(err){ aiTools = { tools: [] }; }
  if (!Array.isArray(aiTools.tools)) aiTools.tools = [];
  return aiTools;
}

function aiToolAvailable(){
  return ((aiTools && aiTools.tools) || []).filter(t => t.available);
}

function updateAiHint(){
  const avail = aiToolAvailable();
  const http = (aiTools && aiTools.http) || [];
  const btn = $("#aiBtn");
  if (!avail.length && !http.length){
    $("#aiStatus").textContent = "未检测到 AI 工具，将用规则识别 + 试运行（不需要安装 AI）";
    btn.textContent = "✦ 规则识别";
    return;
  }
  const names = avail.map(t => t.label).concat(http.map(a => a.vendor + "（兼容）"));
  $("#aiStatus").textContent = "可用：" + names.join(" / ") + "；试运行确认后再保存";
  btn.textContent = "✦ AI 识别";
}

async function loadAiTools(){
  await ensureAiTools();
  const sel = $("#aiProvider");
  const avail = aiToolAvailable();
  const http = (aiTools && aiTools.http) || [];
  const prev = localStorage.getItem("aihub.aiProvider") || "auto";
  let html = "";
  if (avail.length || http.length) html += '<option value="auto">自动（优先本地）</option>';
  avail.forEach(t => { html += '<option value="' + esc(t.id) + '">' + esc(t.label) + "</option>"; });
  http.forEach(a => {
    html += '<option value="http:' + esc(a.account) + '" title="兼容账号：只能读取注入的项目上下文">' +
      esc(a.vendor) + "（兼容账号）</option>";
  });
  html += '<option value="rules">仅规则识别（无需 AI）</option>';
  sel.innerHTML = html;
  sel.value = Array.from(sel.options).some(o => o.value === prev)
    ? prev
    : (avail.length || http.length ? "auto" : "rules");
  updateAiHint();
}

function openEdit(id){
  const p = id ? projects.find(x => x.id === id) : null;
  $("#editTitle").textContent = p ? "编辑项目" : "新增项目";
  $("#pID").value = p ? p.id : "";
  $("#pName").value = p ? p.name : "";
  $("#pPath").value = p ? p.path : "";
  $("#pStart").value = p ? p.start : "";
  $("#pPort").value = p && p.port ? p.port : "";
  $("#pAdmin").value = p && p.admin ? p.admin : "";
  updateAiHint();
  openModal("editModal");
  setTimeout(() => $("#pPath").focus(), 60);
}

async function aiFill(){
  const path = $("#pPath").value.trim();
  if (!path){ $("#aiStatus").textContent = "请先填写项目目录"; return; }
  const btn = $("#aiBtn");
  const verify = $("#autoVerify").checked;
  btn.disabled = true;
  const provider = $("#aiProvider").value;
  const rulesOnly = provider === "rules";
  $("#aiStatus").textContent = rulesOnly
    ? (verify ? "规则识别并自动试运行，可能需要 1 分钟…" : "规则识别中…")
    : (verify ? "AI 识别并自动试运行，可能需要 1-2 分钟…" : "AI 识别中，可能需要几十秒…");
  try{
    const res = await api("/api/projects/analyze", {
      path, provider, verify
    });
    if (!res.ok){
      $("#aiStatus").textContent = res.message || "识别失败";
      return;
    }
    const list = res.services && res.services.length
      ? res.services
      : [{ name: res.name, path, start: res.start, port: res.port, type: "",
           verified: false, verifyMessage: "" }];
    closeModal("editModal");
    scanResults = list;
    $("#scanTitle").textContent = "AI 识别结果";
    $("#scanRoots").closest(".field").hidden = true;
    $("#scanRun").hidden = true;
    const passed = list.filter(c => c.verified).length;
    $("#scanStatus").textContent = "识别到 " + list.length + " 个服务（" + res.source + "）" +
      (verify ? "，" + passed + " 个通过试运行" : "，未自动验证") +
      "；可手动试运行后勾选添加";
    renderScan();
    openModal("scanModal");
    toast(verify && passed === list.length ? "识别完成，已全部通过试运行" : "识别完成，请检查后添加");
  }catch(err){
    $("#aiStatus").textContent = "识别失败：" + err.message;
  }finally{
    btn.disabled = false;
  }
}

async function verifyEdit(){
  const path = $("#pPath").value.trim();
  const start = $("#pStart").value.trim();
  if (!path){ $("#aiStatus").textContent = "请先填写项目目录"; return; }
  if (!start){ $("#aiStatus").textContent = "请先填写启动命令"; return; }
  const btn = $("#verifyBtn");
  btn.disabled = true;
  $("#aiStatus").textContent = "试运行中（最多 25 秒），成功后会自动停止…";
  try{
    const res = await api("/api/projects/verify", {
      path, start, port: $("#pPort").value, admin: $("#pAdmin").value
    });
    if (res.ok){
      if (res.ports && res.ports.length) $("#pPort").value = res.ports[0];
      $("#aiStatus").textContent = "✓ " + (res.message || "试运行成功");
      toast("试运行成功，可保存");
    }else{
      $("#aiStatus").textContent = "✗ " + (res.message || "试运行失败");
      if (res.log && res.log.length) showLogText("试运行日志", res.log, res.logPath || "");
    }
  }catch(err){
    $("#aiStatus").textContent = "✗ 试运行失败：" + err.message;
  }finally{
    btn.disabled = false;
  }
}

function showLogText(title, lines, path){
  if (logTimer){ clearInterval(logTimer); logTimer = null; }
  logId = null;
  $("#logTitle").textContent = title;
  $("#logBody").textContent = (lines && lines.length) ? lines.join("\n") : "(暂无输出)";
  $("#logPath").textContent = path || "";
  openModal("logModal");
  setTimeout(() => {
    const box = $("#logBody");
    box.scrollTop = box.scrollHeight;
  }, 30);
}

async function saveProject(){
  const res = await api("/api/projects/save", { project: {
    id: $("#pID").value || undefined,
    name: $("#pName").value,
    path: $("#pPath").value,
    start: $("#pStart").value,
    port: $("#pPort").value,
    admin: $("#pAdmin").value
  }});
  toast(res.message || "");
  if (res.ok){ closeModal("editModal"); refresh(); }
}

function openScan(){
  $("#scanTitle").textContent = "扫描项目";
  $("#scanRoots").closest(".field").hidden = false;
  $("#scanRun").hidden = false;
  if (!$("#scanRoots").value) $("#scanRoots").value = "~/Documents\n~/Projects\n~/code\n~/Desktop";
  openModal("scanModal");
}

async function runScan(){
  const roots = $("#scanRoots").value.split("\n").map(s => s.trim()).filter(Boolean);
  $("#scanStatus").textContent = "扫描中，请稍候…";
  $("#scanList").innerHTML = "";
  try{
    const res = await api("/api/projects/scan", { roots, depth: 3 });
    scanResults = res.projects || [];
    $("#scanStatus").textContent = scanResults.length
      ? "发现 " + scanResults.length + " 个可识别项目"
      : "未发现项目，可调整扫描目录";
    renderScan();
  }catch(err){
    $("#scanStatus").textContent = "扫描失败：" + err.message;
  }
}

function renderScan(){
  if (!scanResults.length){
    $("#scanList").innerHTML = '<div class="hint">没有发现带 package.json / pom.xml / manage.py / go.mod 等标记的目录。</div>';
    return;
  }
  $("#scanList").innerHTML = scanResults.map((c, i) =>
    '<div class="cand' + (c.exists ? " exists" : "") + '">' +
      '<input type="checkbox" data-i="' + i + '"' + (c.exists ? " disabled" : "") + ">" +
      '<div class="cand-main">' +
        '<div class="cand-name">' + esc(c.name) +
          (c.type ? "<em>" + esc(c.type) + "</em>" : "") +
          (c.exists ? ' <span class="chip muted">已添加</span>' : "") +
        "</div>" +
        '<div class="cand-path">' + esc(c.path) + "</div>" +
        '<div class="cand-cmd"><code>' + esc(c.start || "未识别启动命令") + "</code>" +
          (c.port ? '<span class="chip">:' + c.port + "</span>" : "") +
          (c.admin ? '<span class="chip muted">后台 ' + esc(c.admin) + "</span>" : "") +
          (c.verified ? ' <span class="chip muted">已验证</span>' : "") +
          (c.suggested ? ' <span class="chip muted">AI 建议</span>' : "") + "</div>" +
        envLine(c.env) +
        '<div class="cand-status ' + (c.verified ? "ok" : (c.verifyMessage ? "err" : "")) + '">' +
          (c.verifyMessage ? (c.verified ? "✓ " : "✗ ") + esc(c.verifyMessage) : "") + "</div>" +
      "</div>" +
      '<div class="cand-ops">' +
        '<button class="btn subtle aimini" data-ai="' + i + '" title="用 AI 复核启动命令和端口">AI</button>' +
        '<button class="btn subtle aimini" data-verify="' + i + '" title="实际启动一次，成功后再添加">试运行</button>' +
      "</div>" +
    "</div>").join("");
  document.querySelectorAll("#scanList [data-ai]").forEach(btn => {
    btn.onclick = () => refineCandidate(+btn.dataset.ai, btn);
  });
  document.querySelectorAll("#scanList [data-verify]").forEach(btn => {
    btn.onclick = () => verifyCandidate(+btn.dataset.verify, btn);
  });
  document.querySelectorAll("#scanList .cand-main").forEach(el => {
    el.style.cursor = "pointer";
    el.onclick = () => {
      const box = el.parentElement.querySelector("input");
      if (box && !box.disabled) box.checked = !box.checked;
    };
  });
}

function updateEnvLine(row, env){
  const old = row.querySelector(".env-warn");
  if (old) old.remove();
  const html = envLine(env);
  if (html) row.querySelector(".cand-status").insertAdjacentHTML("beforebegin", html);
}

async function refineCandidate(i, btn){
  const c = scanResults[i];
  if (!c) return;
  btn.disabled = true;
  btn.textContent = "…";
  try{
    const res = await api("/api/projects/analyze", {
      path: c.path, provider: $("#aiProvider").value, verify: $("#autoVerify").checked
    });
    if (!res.ok) return toast(res.message || "AI 识别失败");
    const svc = (res.services && res.services[0]) || res;
    if (svc.name) c.name = svc.name;
    if (svc.start) c.start = svc.start;
    if (svc.port) c.port = svc.port;
    c.verified = !!svc.verified;
    c.verifyMessage = svc.verifyMessage || "";
    c.suggested = !!svc.suggested;
    if (svc.env) c.env = svc.env;
    const row = btn.closest(".cand");
    row.querySelector(".cand-name").innerHTML =
      esc(c.name) + (c.type ? "<em>" + esc(c.type) + "</em>" : "") +
      (c.exists ? ' <span class="chip muted">已添加</span>' : "");
    row.querySelector(".cand-cmd").innerHTML =
      "<code>" + esc(c.start || "未识别启动命令") + "</code>" +
      (c.port ? '<span class="chip">:' + c.port + "</span>" : "") +
      (c.verified ? ' <span class="chip muted">已验证</span>' : "");
    const status = row.querySelector(".cand-status");
    status.className = "cand-status " + (c.verified ? "ok" : (c.verifyMessage ? "err" : ""));
    status.textContent = c.verifyMessage ? (c.verified ? "✓ " : "✗ ") + c.verifyMessage : "";
    updateEnvLine(row, c.env);
    toast(c.verified ? "AI 识别并试运行成功" : "已更新建议，请试运行确认");
  }catch(err){
    toast("识别失败：" + err.message);
  }finally{
    btn.disabled = false;
    btn.textContent = "AI";
  }
}

async function verifyCandidate(i, btn){
  const c = scanResults[i];
  if (!c) return;
  const row = btn.closest(".cand");
  const status = row.querySelector(".cand-status");
  if (!c.start) return toast("该服务没有可用的启动命令");
  btn.disabled = true;
  btn.textContent = "…";
  status.className = "cand-status";
  status.textContent = "试运行中（最多 25 秒），成功后自动停止…";
  try{
    const res = await api("/api/projects/verify", {
      path: c.path, start: c.start, port: c.port, admin: c.admin || ""
    });
    if (res.env) c.env = res.env;
    updateEnvLine(row, c.env);
    if (res.ok){
      if (res.adminOk === false) c.admin = "";
      if (res.ports && res.ports.length) c.port = res.ports[0];
      c.verified = true;
      status.className = "cand-status ok";
      status.textContent = "✓ " + (res.message || "试运行成功");
      row.querySelector(".cand-cmd").innerHTML =
        "<code>" + esc(c.start) + "</code>" +
        (c.port ? '<span class="chip">:' + c.port + "</span>" : "") +
        ' <span class="chip muted">已验证</span>';
      toast(c.name + " 试运行成功");
    }else{
      c.verified = false;
      status.className = "cand-status err";
      status.textContent = "✗ " + (res.message || "试运行失败");
      if (res.log && res.log.length) showLogText("试运行日志 · " + c.name, res.log, res.logPath || "");
    }
  }catch(err){
    c.verified = false;
    status.className = "cand-status err";
    status.textContent = "✗ " + err.message;
  }finally{
    btn.disabled = false;
    btn.textContent = "试运行";
  }
}

async function importSelected(){
  const items = [...document.querySelectorAll("#scanList input:checked")]
    .map(el => scanResults[+el.dataset.i]);
  if (!items.length) return toast("请先勾选要添加的项目");
  const res = await api("/api/projects/import", { items });
  toast(res.message || "");
  closeModal("scanModal");
  refresh();
}

/* ---------------- 从 GitHub 导入 ---------------- */

let ghPlan = null, ghTimer = null;

function openGithub(){
  clearInterval(ghTimer);
  ghTimer = null;
  ghPlan = null;
  $("#ghUrl").value = "";
  $("#ghStatus").textContent = "只读取 README 与配置文件，不下载代码";
  $("#ghPlan").hidden = true;
  $("#ghFlow").innerHTML = "";
  $("#ghRepo").innerHTML = "";
  $("#ghWarn").innerHTML = "";
  $("#ghReadmeBox").hidden = true;
  $("#ghLog").hidden = true;
  $("#ghLog").textContent = "";
  $("#ghImport").disabled = true;
  $("#ghImport").textContent = "克隆并准备环境";
  openModal("ghModal");
  resumeGithubJob();
}

async function resumeGithubJob(){
  let res;
  try{ res = await api("/api/github/job"); }
  catch(err){ return; }
  if (!res.ok || res.status !== "running") return;
  $("#ghPlan").hidden = true;
  $("#ghLog").hidden = false;
  $("#ghLog").textContent = (res.log || []).join("\n") || "任务进行中…";
  $("#ghStatus").textContent = "已有导入任务在进行";
  $("#ghImport").disabled = true;
  $("#ghImport").textContent = "导入中…";
  watchGithubJob(res.id);
}

function ghChip(text, muted){
  return '<span class="chip' + (muted ? " muted" : "") + '">' + esc(text) + "</span>";
}

function renderGithubPlan(res){
  const p = res.plan, r = res.repo || {};
  const top = [
    '<span class="slug">' + esc(r.slug || p.slug) + "</span>",
    r.language ? ghChip(r.language, true) : "",
    r.stars ? ghChip("★ " + r.stars, true) : "",
    r.license ? ghChip(r.license, true) : "",
    r.archived ? ghChip("已归档") : "",
    p.ref ? ghChip("分支 " + p.ref, true) : ""
  ].filter(Boolean).join("");
  $("#ghRepo").innerHTML =
    '<div class="top">' + top + "</div>" +
    (r.description ? '<div class="desc">' + esc(r.description) + "</div>" : "") +
    '<div class="desc">依据：' + esc((p.files || []).join(" · ") || "仅 README") + "</div>";

  const rows = [], py = p.python || {}, node = p.node || {};
  if (py.spec || (py.pick && py.pick.version)){
    const judge = py.spec
      ? (py.ok === false ? "不满足" : (py.ok === true ? "已满足" : "未校验"))
      : "";
    rows.push(["Python", "<code>" + esc(py.spec || "未声明") + "</code>" +
      (py.pick ? "<span>本机 " + esc(py.pick.version) + (judge ? "（" + judge + "）" : "") + "</span>" : "")]);
  }
  if (node.spec || node.current){
    rows.push(["Node", "<code>" + esc(node.spec || "未声明") + "</code>" +
      (node.current ? "<span>本机 " + esc(node.current) + "</span>" : "")]);
  }
  const step = (label, list) => {
    if (!list || !list.length) return;
    rows.push([label, list.map(c => "<code>" + esc(c) + "</code>").join("")]);
  };
  step("准备", p.setup);
  step("初始化", p.init);
  step("启动", p.start ? [p.start] : []);
  if (p.port) rows.push(["端口", "<code>:" + p.port + "</code><span>从 README 推测，启动后以实际监听为准</span>"]);

  $("#ghFlow").innerHTML = rows.map(([k, v]) =>
    '<div class="row"><div class="k">' + esc(k) + '</div><div class="v">' + v + "</div></div>").join("");

  const warns = [...(p.warnings || []), ...(p.notes || [])];
  $("#ghWarn").innerHTML = warns.length
    ? '<div class="gh-warn">⚠ ' + warns.map(esc).join("<br>") + "</div>" : "";

  if (p.readme){
    $("#ghReadmeBox").hidden = false;
    $("#ghReadme").textContent = p.readme;
  }
  $("#ghDir").value = p.dir || "";
  $("#ghPlan").hidden = false;
  $("#ghImport").disabled = false;
}

async function inspectGithub(){
  const url = $("#ghUrl").value.trim();
  if (!url) return toast("请填写仓库地址");
  $("#ghInspect").disabled = true;
  $("#ghPlan").hidden = true;
  $("#ghStatus").textContent = "分析中，需要访问 GitHub…";
  try{
    const res = await api("/api/github/inspect", { url });
    if (!res.ok){
      $("#ghStatus").textContent = res.message || "分析失败";
      return;
    }
    ghPlan = res.plan;
    renderGithubPlan(res);
    $("#ghStatus").textContent = "分析完成，确认后开始下载并准备环境";
  }catch(err){
    $("#ghStatus").textContent = "分析失败：" + err.message;
  }finally{
    $("#ghInspect").disabled = false;
  }
}

async function startGithubImport(){
  if (!ghPlan) return;
  const url = $("#ghUrl").value.trim(), dir = $("#ghDir").value.trim();
  $("#ghImport").disabled = true;
  $("#ghImport").textContent = "准备中…";
  $("#ghLog").hidden = false;
  $("#ghLog").textContent = "等待任务开始…";
  try{
    const res = await api("/api/github/import", { url, dir });
    if (!res.ok){
      toast(res.message || "无法开始导入");
      $("#ghLog").hidden = false;
      $("#ghLog").textContent = res.message || "无法开始导入";
      $("#ghImport").disabled = false;
      $("#ghImport").textContent = "克隆并准备环境";
      resumeGithubJob();
      return;
    }
    $("#ghStatus").textContent = "正在导入…";
    watchGithubJob(res.job);
  }catch(err){
    toast("请求失败：" + err.message);
    $("#ghImport").disabled = false;
    $("#ghImport").textContent = "克隆并准备环境";
  }
}

function watchGithubJob(id){
  clearInterval(ghTimer);
  ghTimer = setInterval(async () => {
    let res;
    try{ res = await api("/api/github/job?id=" + encodeURIComponent(id)); }
    catch(err){ return; }
    if (!res.ok) return;
    const box = $("#ghLog");
    const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
    box.textContent = (res.log || []).join("\n") || "…";
    if (stick) box.scrollTop = box.scrollHeight;
    if (res.status === "running"){
      $("#ghStatus").textContent = res.step + "…";
      return;
    }
    clearInterval(ghTimer);
    ghTimer = null;
    refresh();
    if (res.status === "done"){
      $("#ghStatus").textContent = "已完成";
      $("#ghImport").textContent = "已导入";
      toast((res.result && res.result.message) || "导入完成");
    }else{
      $("#ghStatus").textContent = "失败：" + (res.message || "查看日志");
      $("#ghImport").disabled = false;
      $("#ghImport").textContent = "重试准备";
      toast("导入失败：" + (res.message || "查看日志"));
    }
  }, 1200);
}

function watchJobQuiet(id){
  const timer = setInterval(async () => {
    let res;
    try{ res = await api("/api/github/job?id=" + encodeURIComponent(id)); }
    catch(err){ clearInterval(timer); return; }
    if (!res.ok || res.status === "running") return;
    clearInterval(timer);
    toast(res.status === "done"
      ? ((res.result && res.result.message) || "环境已就绪")
      : ("环境准备失败：" + (res.message || "")));
    refresh();
  }, 1500);
}

async function pickFolder(inputSel, title){
  const input = $(inputSel);
  try{
    const res = await api("/api/system/pick-folder", { title, default: input.value.trim() });
    if (res.ok && res.path){ input.value = res.path; return res; }
    if (!res.canceled) toast(res.message || "选择失败");
  }catch(err){
    toast("打开选择窗口失败：" + err.message);
  }
  return null;
}

function openLog(id){
  const p = projects.find(x => x.id === id);
  if (!p) return;
  logId = id;
  $("#logTitle").textContent = "日志 · " + p.name;
  $("#logBody").textContent = "加载中…";
  openModal("logModal");
  loadLog();
  if (logTimer) clearInterval(logTimer);
  logTimer = setInterval(loadLog, 2000);
}

async function loadLog(){
  if (!logId) return;
  try{
    const res = await api("/api/projects/log?id=" + encodeURIComponent(logId));
    const box = $("#logBody");
    const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
    box.textContent = (res.lines && res.lines.length) ? res.lines.join("\n") : "(暂无日志)";
    $("#logPath").textContent = res.path || "";
    if (stick) box.scrollTop = box.scrollHeight;
  }catch(err){ /* 日志读取失败时保留当前内容 */ }
}

/* ---------------- 项目诊断面板 ---------------- */
const DIAG_PROVIDERS = {
  claude: { label: "Claude Code", model: "" },
  ollama: { label: "Ollama", model: "qwen3:8b" }
};
// 各后端的来源图标：内联 SVG，颜色由 .tool-ico.* 控制，跟随明暗模式
const TOOL_ICONS = {
  ollama: { name: "Ollama", svg: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7.4 9.6V5.4M16.6 9.6V5.4"/><path d="M6.6 12.7a5.4 5.4 0 0 1 10.8 0v3.5a3.8 3.8 0 0 1-3.8 3.8h-3.2a3.8 3.8 0 0 1-3.8-3.8z"/><circle cx="9.8" cy="12.9" r=".95" fill="currentColor" stroke="none"/><circle cx="14.2" cy="12.9" r=".95" fill="currentColor" stroke="none"/></svg>' },
  claude: { name: "Claude Code", svg: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 3.4v17.2M4.6 7.7l14.8 8.6M19.4 7.7L4.6 16.3"/></svg>' },
  openclaw: { name: "OpenClaw", svg: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M7.6 4.4c1.4 4.4 1.4 10.6 0 15.2"/><path d="M12 3.8c1.4 4.6 1.4 11 0 15.8"/><path d="M16.4 4.4c1.4 4.4 1.4 10.6 0 15.2"/></svg>' },
  http: { name: "兼容账号", svg: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="8.2"/><path d="M3.8 12h16.4M12 3.8c3.1 3.6 3.1 12.8 0 16.4M12 3.8c-3.1 3.6-3.1 12.8 0 16.4"/></svg>' },
  custom: { name: "自定义工具", svg: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3.6" y="5" width="16.8" height="14" rx="2.4"/><path d="M8 9.6l2.6 2.4L8 14.4"/><path d="M13.4 14.8h3"/></svg>' },
  default: { name: "AI", svg: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.6l1.9 5.3 5.3 1.9-5.3 1.9L12 18l-1.9-5.3L4.8 10.8l5.3-1.9z"/></svg>' }
};
let diag = { id: null, project: null, messages: [], session: "", busy: false, streamId: null };
let diagModels = null;

function diagStore(id){
  try{ return JSON.parse(localStorage.getItem("aihub.diag." + id) || "null") || {}; }
  catch(err){ return {}; }
}

function diagPersist(){
  if (!diag.id) return;
  const keep = diag.messages
    .filter(m => m.role === "user" || m.role === "assistant")
    .slice(-40);
  localStorage.setItem("aihub.diag." + diag.id, JSON.stringify({
    messages: keep,
    session: diag.session,
    provider: $("#diagProvider").value,
    model: $("#diagModel").value
  }));
}

function shortPath(p){
  const parts = String(p || "").split("/").filter(Boolean);
  return parts.length > 3 ? "…/" + parts.slice(-2).join("/") : String(p || "");
}

function renderDiagHead(){
  const p = diag.project;
  if (!p) return;
  $("#diagName").textContent = p.name;
  const bits = [];
  if (p.type) bits.push(p.type);
  bits.push(p.running ? (p.pids.length ? "PID " + p.pids.join(",") : "运行中") : "已停止");
  if (p.ports.length) bits.push(":" + p.ports.join(" :"));
  $("#diagSub").textContent = bits.join(" · ");
  $("#diagDot").className = "dot" + (p.running ? " on" : "");
}

function renderDiagCtx(){
  const p = diag.project;
  if (!p) return;
  const ctx = ['<span class="dchip" title="' + esc(p.path) + '">' + esc(shortPath(p.path)) + "</span>"];
  if (p.start) ctx.push('<span class="dchip" title="' + esc(p.start) + '">' + esc(p.start) + "</span>");
  const issues = (p.env && p.env.issues) || [];
  issues.slice(0, 2).forEach(x =>
    ctx.push('<span class="dchip warn" title="' + esc(x) + '">⚠ ' + esc(x) + "</span>"));
  $("#diagCtx").innerHTML = ctx.join("");
}

/* 消息实际使用的后端：优先用记录下来的，旧记录回退到当前选择 */
function diagBackend(m){
  if (m && m.backend) return m.backend;
  const sel = $("#diagProvider");
  const opt = sel && sel.selectedOptions[0];
  return { id: sel ? sel.value : "", label: opt ? opt.textContent : "" };
}

function toolIcon(id){
  const key = !id ? "default" : (String(id).indexOf("custom:") === 0 ? "custom" : String(id));
  const ico = TOOL_ICONS[key] || TOOL_ICONS.default;
  return '<span class="tool-ico ' + key + '" title="' + esc(ico.name) + '">' + ico.svg + "</span>";
}

function renderDiagLog(){
  const box = $("#diagLog");
  if (!diag.messages.length){
    box.innerHTML = '<div class="msg"><div class="msg-role">诊断</div>' +
      '<div class="msg-body muted">上下文会自动带上这个项目的启动命令、端口、环境依赖和最近日志。</div></div>';
    return;
  }
  box.innerHTML = diag.messages.map((m, i) => {
    const backend = m.role === "assistant" ? diagBackend(m) : null;
    const role = m.role === "user" ? "你"
      : m.role === "error" ? "错误"
      : "AI Hub" + (backend && backend.label ? " · " + esc(backend.label) : "");
    return '<div class="msg ' + m.role + '" data-i="' + i + '">' +
      '<div class="msg-role">' + (backend ? toolIcon(backend.id) : "") + role + "</div>" +
      '<div class="msg-body">' + esc(m.text) + "</div></div>";
  }).join("");
  box.scrollTop = box.scrollHeight;
}

function updateDiagMsg(i, text){
  const node = document.querySelector("#diagLog .msg[data-i='" + i + "'] .msg-body");
  if (!node) return;
  node.textContent = text;
  const box = $("#diagLog");
  box.scrollTop = box.scrollHeight;
}

function httpAccount(accountId){
  return ((aiTools && aiTools.http) || []).find(a => a.account === accountId) || null;
}

function fillDiagModels(){
  const provider = $("#diagProvider").value;
  const sel = $("#diagModel");
  let opts;
  if (provider === "claude"){
    const list = (diagModels && diagModels.claude) || [];
    opts = list.length
      ? list.map(m => ({ v: m.model, a: m.account, l: m.vendor + " · " + m.label }))
      : [{ v: "", a: "", l: "未配置 Anthropic 兼容账号（去「模型」页添加）" }];
  }else if (provider.indexOf("http:") === 0){
    const acc = httpAccount(provider.slice(5));
    opts = ((acc && acc.models) || []).map(m => ({ v: m.model_id, a: "", l: m.name + " · " + m.model_id }));
    if (!opts.length) opts = [{ v: "", a: "", l: "该账号没有可用模型" }];
  }else if (provider === "ollama"){
    const saved = localStorage.getItem("aihub.diag.ollama") || DIAG_PROVIDERS.ollama.model;
    opts = [{ v: saved, a: "", l: saved + " · 本地" }];
  }else{
    // OpenClaw / 自定义 CLI 不需要选模型
    sel.innerHTML = "";
    sel.hidden = true;
    return;
  }
  sel.hidden = false;
  sel.innerHTML = opts.map(o =>
    '<option value="' + esc(o.v) + '" data-account="' + esc(o.a) + '">' + esc(o.l) + "</option>").join("");
  const saved = localStorage.getItem("aihub.diag.model." + provider);
  if (saved && opts.some(o => o.v === saved)) sel.value = saved;
  else if (provider === "claude" && diagModels.binding){
    // 没有手动选过模型时，跟随「工具」页的全局绑定
    const target = diagModels.binding;
    const hit = [...sel.options].findIndex(o => o.dataset.account === target.account && o.value === target.model);
    if (hit >= 0) sel.selectedIndex = hit;
  }
}

async function loadDiagModels(){
  if (diagModels) return;
  diagModels = { claude: [], binding: null };
  try{
    const res = await api("/api/claude/models");
    diagModels.claude = (res && res.models) || [];
    diagModels.binding = (res && res.binding) || null;
  }catch(err){ diagModels.claude = []; }
}

function diagProviderOptions(){
  const out = aiToolAvailable().map(t => ({ v: t.id, l: t.label }));
  ((aiTools && aiTools.http) || []).forEach(a => {
    out.push({ v: "http:" + a.account, l: a.vendor + "（兼容账号）" });
  });
  return out;
}

function fillDiagProviders(preferred){
  const sel = $("#diagProvider");
  const opts = diagProviderOptions();
  if (!opts.length){
    sel.innerHTML = '<option value="">无可用 AI 工具</option>';
    sel.disabled = true;
    $("#diagInput").disabled = true;
    $("#diagInput").placeholder = "未检测到 AI 工具，可在「工具」页添加自定义 CLI";
    $("#diagSend").disabled = true;
    $("#diagStatus").textContent =
      "没有可用后端：装 Claude Code / OpenClaw、启动 Ollama，或在「工具」页添加 CLI";
    return;
  }
  sel.disabled = false;
  $("#diagInput").disabled = false;
  $("#diagSend").disabled = false;
  $("#diagInput").placeholder = "问这个项目的问题，⌘ + Enter 发送";
  sel.innerHTML = opts.map(o =>
    '<option value="' + esc(o.v) + '">' + esc(o.l) + "</option>").join("");
  sel.value = opts.some(o => o.v === preferred) ? preferred : opts[0].v;
}

async function openDiag(id){
  const p = projects.find(x => x.id === id);
  if (!p) return;
  diag.id = id;
  diag.project = p;
  const saved = diagStore(id);
  diag.messages = Array.isArray(saved.messages) ? saved.messages : [];
  diag.session = saved.session || "";
  const provider = saved.provider || localStorage.getItem("aihub.diag.provider") || "claude";
  renderDiagHead();
  renderDiagCtx();
  renderDiagLog();
  $("#diagStatus").textContent = "";
  $("#diagInput").value = "";
  $("#diag").hidden = false;
  await ensureAiTools();
  fillDiagProviders(provider);
  await loadDiagModels();
  fillDiagModels();
  if (saved.model){
    const opt = [...$("#diagModel").options].find(o => o.value === saved.model);
    if (opt) $("#diagModel").value = saved.model;
  }
  setTimeout(() => $("#diagInput").focus(), 80);
}

function closeDiag(){
  diagPersist();
  $("#diag").hidden = true;
}

async function readDiagStream(res, onEvent){
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true){
    const step = await reader.read();
    if (step.done) break;
    buf += decoder.decode(step.value, { stream: true });
    let cut;
    while ((cut = buf.indexOf("\n\n")) >= 0){
      const chunk = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      let event = "message", data = "";
      chunk.split("\n").forEach(line => {
        if (line.indexOf("event:") === 0) event = line.slice(6).trim();
        else if (line.indexOf("data:") === 0) data += line.slice(5).trim();
      });
      if (!data) continue;
      try{ onEvent(event, JSON.parse(data)); }catch(err){ /* 忽略坏事件 */ }
    }
  }
}

async function sendDiag(e){
  if (e) e.preventDefault();
  if (diag.busy || !diag.id) return;
  const text = $("#diagInput").value.trim();
  if (!text) return;
  const selected = $("#diagProvider").value;
  if (!selected) return toast("没有可用的 AI 后端，先到「工具」页添加");
  let provider = selected, account = "";
  if (selected.indexOf("http:") === 0){
    provider = "http";
    account = selected.slice(5);
  }
  const opt = $("#diagModel").selectedOptions[0];
  const model = opt ? opt.value : "";
  if (provider === "claude") account = opt ? (opt.dataset.account || "") : "";
  const needModel = provider === "claude" || provider === "ollama" || provider === "http";
  if (needModel && !model){
    return toast(provider === "claude"
      ? "先在「模型」页配置一个 Anthropic 兼容账号"
      : "该后端还没有可用模型");
  }
  const target = diag.project || {};
  const streamId = diag.id;
  const history = diag.messages
    .filter(m => m.role === "user" || m.role === "assistant")
    .map(m => ({ role: m.role, content: m.text }));

  const psel = $("#diagProvider").selectedOptions[0];
  const backend = { id: provider, label: psel ? psel.textContent : provider };
  diag.messages.push({ role: "user", text: text });
  diag.messages.push({ role: "assistant", text: "", backend: backend });
  const idx = diag.messages.length - 1;
  renderDiagLog();
  diag.busy = true;
  diag.streamId = streamId;
  $("#diagSend").disabled = true;
  $("#diagSend").textContent = "思考中…";
  $("#diagStatus").textContent = "正在读取项目上下文…";

  try{
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-AIHub": "1" },
      body: JSON.stringify({
        provider, model, message: text, multi: true,
        history: (provider === "ollama" || provider === "http") ? history : [],
        session: diag.session,
        project: diag.id,
        workspace: target.workspace_id || target.id || diag.id,
        account: account,
        claudeAccount: provider === "claude" ? account : ""
      })
    });
    if (!res.ok || !res.body) throw new Error("服务返回 " + res.status);
    await readDiagStream(res, (event, data) => {
      if (diag.streamId !== streamId) return;
      if (event === "delta"){
        diag.messages[idx].text += data.text || "";
        updateDiagMsg(idx, diag.messages[idx].text);
        $("#diagStatus").textContent = "";
      }else if (event === "meta"){
        // 服务端回报实际调用的后端，覆盖发送时的预估
        if (data.backend) diag.messages[idx].backend = data.backend;
        renderDiagLog();
      }else if (event === "tool"){
        $("#diagStatus").textContent = "调用工具：" + (data.name || "工具");
      }else if (event === "error"){
        diag.messages[idx] = { role: "error", text: data.message || "执行失败" };
        renderDiagLog();
      }else if (event === "done"){
        if (data.session) diag.session = data.session;
        if (!diag.messages[idx].text){
          diag.messages[idx].text = "（没有返回内容）";
          updateDiagMsg(idx, diag.messages[idx].text);
        }
      }
    });
  }catch(err){
    diag.messages[idx] = { role: "error", text: "请求失败：" + err.message };
    renderDiagLog();
  }finally{
    if (diag.streamId === streamId){
      diag.busy = false;
      diag.streamId = null;
      $("#diagSend").disabled = false;
      $("#diagSend").textContent = "发送";
      $("#diagStatus").textContent = "";
      diagPersist();
    }
  }
}

$("#diagClose").onclick = closeDiag;
$("#diagMask").onclick = closeDiag;
$("#diagForm").onsubmit = sendDiag;
$("#diagProvider").onchange = () => {
  localStorage.setItem("aihub.diag.provider", $("#diagProvider").value);
  fillDiagModels();
};
$("#diagModel").onchange = () => {
  localStorage.setItem("aihub.diag.model." + $("#diagProvider").value, $("#diagModel").value);
};
document.querySelectorAll("#diagQuick .dq").forEach(b => {
  b.onclick = () => { $("#diagInput").value = b.dataset.q || ""; sendDiag(); };
});
$("#diagInput").addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter"){ e.preventDefault(); sendDiag(); }
});

document.querySelectorAll("[data-close]").forEach(el => {
  el.onclick = () => closeModal(el.dataset.close);
});
document.querySelectorAll(".modal").forEach(m => {
  m.addEventListener("click", e => { if (e.target === m) closeModal(m.id); });
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape"){
    if (!$("#diag").hidden) closeDiag();
    document.querySelectorAll(".modal.show").forEach(m => closeModal(m.id));
  }
  if ((e.metaKey || e.ctrlKey) && !e.altKey && ["1", "2", "3"].includes(e.key)){
    e.preventDefault();
    location.href = { "1": "/projects", "2": "/models", "3": "/tools" }[e.key];
  }
});

$("#addBtn").onclick = () => openEdit();
$("#emptyAdd").onclick = () => openEdit();
$("#scanBtn").onclick = openScan;
$("#emptyScan").onclick = openScan;
$("#saveBtn").onclick = saveProject;
$("#aiBtn").onclick = aiFill;
$("#verifyBtn").onclick = verifyEdit;
const aiProviderSel = $("#aiProvider");
aiProviderSel.onchange = () => localStorage.setItem("aihub.aiProvider", aiProviderSel.value);
loadAiTools();
const autoVerifyBox = $("#autoVerify");
autoVerifyBox.checked = localStorage.getItem("aihub.autoVerify") !== "0";
autoVerifyBox.onchange = () => localStorage.setItem("aihub.autoVerify", autoVerifyBox.checked ? "1" : "0");
$("#scanRun").onclick = runScan;
$("#scanImport").onclick = importSelected;
$("#ghBtn").onclick = openGithub;
$("#ghInspect").onclick = inspectGithub;
$("#ghImport").onclick = startGithubImport;
$("#ghUrl").addEventListener("keydown", e => {
  if (e.key === "Enter"){ e.preventDefault(); inspectGithub(); }
});
$("#ghDir").addEventListener("keydown", e => {
  if (e.key === "Enter"){ e.preventDefault(); startGithubImport(); }
});
$("#pPathPick").onclick = () => pickFolder("#pPath", "选择项目目录");
$("#ghDirPick").onclick = async () => {
  const res = await pickFolder("#ghDir", "选择安装目录");
  if (!res || !res.path) return;
  if (!res.empty && ghPlan){
    const name = String(ghPlan.slug || "").replace("/", "__");
    if (name){
      $("#ghDir").value = res.path.replace(/\/+$/, "") + "/" + name;
      toast("该目录非空，已改为在其中新建 " + name);
    }
  }
};
$("#logRefresh").onclick = loadLog;
$("#scanAll").onchange = e => {
  document.querySelectorAll("#scanList input[type=checkbox]:not(:disabled)")
    .forEach(el => el.checked = e.target.checked);
};
$("#scanRoots").value = "~/Documents\n~/Projects\n~/code\n~/Desktop";

refresh();
setInterval(refresh, 4000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
</script>
</body>
</html>
"""


MODELS_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Hub · 模型</title>
<style>
  :root{
    --bg:#F5F4F1; --panel:#FFFFFF; --surface:#F0EFEA; --text:#17160F; --muted:#6E6B63;
    --faint:#9A968C; --line:rgba(24,22,18,.12); --line-strong:rgba(24,22,18,.24);
    --brand:#0AB861; --brand-ink:#06743F; --brand-soft:#E5FBF0;
    --danger:#C43A2E; --warn:#A2610A;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
    --ease:cubic-bezier(.2,.8,.2,1);
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#0D0D0F; --panel:#16161A; --surface:#1F1F24; --text:#ECECEE; --muted:#98979A;
           --faint:#6F6E72; --line:rgba(236,236,238,.12); --line-strong:rgba(236,236,238,.26);
           --brand:#32F08C; --brand-ink:#32F08C; --brand-soft:#0A2A1B;
           --danger:#FF6B60; --warn:#E8A34A; }
  }
  *{ box-sizing:border-box; }
  html,body{ margin:0; min-height:100%; }
  body{ background:var(--bg); color:var(--text);
    font:14px/1.5 -apple-system,"PingFang SC","Helvetica Neue",sans-serif; }
  .app{ display:flex; height:100vh; }
  .rail{ width:236px; flex:0 0 236px; background:var(--panel); border-right:1px solid var(--line);
    display:flex; flex-direction:column; padding:14px 12px 12px; gap:6px; min-height:0; }
  .brand{ display:flex; align-items:center; gap:9px; padding:4px 8px 14px;
    font-size:14.5px; font-weight:650; letter-spacing:-.01em; }
  .mark{ width:18px; height:18px; border-radius:5px; background:var(--text); position:relative; flex:0 0 auto; }
  .mark::before{ content:""; position:absolute; inset:4px; border-radius:2px;
    border:1.5px solid var(--panel); border-right-color:transparent; border-bottom-color:transparent; }
  .mark::after{ content:""; position:absolute; right:4px; bottom:4px; width:3px; height:3px;
    border-radius:1px; background:var(--brand); }
  .rail-nav{ display:flex; flex-direction:column; gap:2px; }
  .nav-btn{ color:var(--muted); text-decoration:none; font-size:13px; font-weight:500;
    display:flex; align-items:center; gap:10px; padding:8px 10px; border-radius:8px;
    position:relative; transition:background 150ms var(--ease), color 150ms var(--ease); }
  .nav-btn svg{ width:15px; height:15px; flex:0 0 auto; opacity:.8; }
  .nav-btn:hover{ background:var(--surface); color:var(--text); }
  .nav-btn.is-active{ background:var(--surface); color:var(--text); font-weight:600; }
  .nav-btn.is-active svg{ opacity:1; }
  .nav-btn.is-active::before{ content:""; position:absolute; left:0; top:9px; bottom:9px; width:2px;
    border-radius:2px; background:var(--brand); }
  .nav-btn .kbd{ margin-left:auto; font-family:var(--mono); font-size:9.5px; color:var(--faint); letter-spacing:.04em; }
  .rail-sep{ height:1px; background:var(--line); margin:12px 8px 10px; }
  .sec{ font-family:var(--mono); font-size:9.5px; letter-spacing:.1em; color:var(--faint);
    text-transform:uppercase; padding:0 10px 7px; }
  #filters{ display:flex; flex-direction:column; gap:2px; flex:1; min-height:0; overflow:auto; }
  .row{ display:flex; align-items:center; gap:8px; padding:7px 10px; border-radius:7px;
    font-size:12.5px; color:var(--muted); cursor:pointer;
    transition:background 140ms var(--ease), color 140ms var(--ease); }
  .row:hover{ background:var(--surface); color:var(--text); }
  .row.on{ background:var(--surface); color:var(--text); font-weight:600; }
  .row .n{ min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .row .m{ margin-left:auto; font-family:var(--mono); font-size:10px; color:var(--faint); font-weight:400; }
  .rail-foot{ margin-top:auto; padding-top:11px; border-top:1px solid var(--line); }
  .foot-line{ font-family:var(--mono); font-size:10px; color:var(--faint); padding:0 2px; line-height:1.6; }
  .main{ flex:1; min-width:0; display:flex; flex-direction:column; min-height:0; }
  .topbar{ display:flex; align-items:center; gap:12px; padding:13px 22px;
    border-bottom:1px solid var(--line); flex-wrap:wrap; }
  .topbar h1{ margin:0; font-size:15px; font-weight:600; letter-spacing:-.01em; }
  .summary{ font-size:12px; color:var(--muted); }
  .tools{ margin-left:auto; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .btn{ appearance:none; border:1px solid var(--line); background:var(--panel); color:var(--text);
    font:inherit; font-size:12.5px; padding:7px 12px; border-radius:8px; cursor:pointer;
    text-decoration:none; display:inline-flex; align-items:center; gap:4px;
    transition:background 150ms var(--ease), border-color 150ms var(--ease),
               color 150ms var(--ease), transform 150ms var(--ease); }
  .btn:hover{ background:var(--surface); border-color:var(--line-strong); transform:translateY(-1px); }
  .btn:active{ transform:translateY(1px) scale(.985); transition-duration:90ms; }
  .btn.primary{ background:var(--text); border-color:var(--text); color:var(--bg); font-weight:600; }
  .btn.ghost{ border-color:transparent; background:transparent; color:var(--muted); }
  .btn.ghost:hover{ color:var(--text); background:var(--surface); border-color:transparent; }
  .btn.subtle{ border-color:transparent; background:transparent; color:var(--muted); padding:6px 9px; }
  .btn.subtle:hover{ color:var(--text); background:var(--surface); border-color:transparent; }
  .btn.danger:hover{ color:var(--danger); }
  .btn:disabled{ opacity:.55; cursor:default; transform:none; }
  .btn:focus-visible{ outline:2px solid var(--brand-ink); outline-offset:2px; }
  @media (prefers-reduced-motion: reduce){ .btn{ transition-duration:1ms; } .btn:hover,.btn:active{ transform:none; } }
  .page{ flex:1; overflow:auto; padding:22px 22px 48px; }
  .page > *{ max-width:1240px; margin-left:auto; margin-right:auto; }
  .panel{ background:var(--panel); border:1px solid var(--line); border-radius:16px; overflow:hidden;
    box-shadow:inset 0 1px 0 rgba(255,255,255,.7); }
  @media (prefers-color-scheme: dark){ .panel{ box-shadow:inset 0 1px 0 rgba(255,255,255,.045); } }
  .table-wrap{ overflow:auto; }
  table{ border-collapse:collapse; width:100%; font-size:12.5px; }
  th, td{ text-align:left; padding:11px 14px; border-bottom:1px solid var(--line); vertical-align:middle; }
  th{ font-weight:500; color:var(--muted); font-size:11px; background:var(--surface); white-space:nowrap; }
  tbody tr:last-child td{ border-bottom:none; }
  tbody tr:hover{ background:color-mix(in srgb, var(--surface) 55%, transparent); }
  .mono{ font-family:var(--mono); font-size:11.5px; }
  .muted{ color:var(--muted); }
  .faint{ color:var(--faint); }
  .vendor{ font-weight:600; display:flex; align-items:center; gap:6px; }
  .proto{ font-family:var(--mono); font-size:9.5px; color:var(--faint); border:1px solid var(--line);
    border-radius:4px; padding:0 4px; line-height:15px; }
  .url{ font-family:var(--mono); font-size:10.5px; color:var(--faint); max-width:230px;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .chips{ display:flex; flex-wrap:wrap; gap:4px; max-width:320px; }
  .mchip{ font-family:var(--mono); font-size:10.5px; background:var(--surface); color:var(--muted);
    border:1px solid var(--line); border-radius:5px; padding:1px 6px; white-space:nowrap; }
  .mtag{ font-size:9px; color:var(--brand-ink); border:1px solid var(--line);
    border-radius:4px; padding:0 3px; margin-left:4px; }
  .labrow{ display:flex; align-items:center; justify-content:space-between; gap:8px; }
  .labrow .btn{ padding:3px 9px; font-size:11px; }
  .bar{ width:130px; height:5px; border-radius:999px; background:var(--surface); overflow:hidden; margin-bottom:5px; }
  .bar i{ display:block; height:100%; border-radius:999px; background:var(--brand); }
  .bar i.warn{ background:var(--warn); }
  .bar i.full{ background:var(--danger); }
  .qtext{ font-size:11px; color:var(--muted); font-family:var(--mono); white-space:nowrap; }
  .st{ display:inline-flex; align-items:center; gap:6px; font-size:11.5px; white-space:nowrap; }
  .st .d{ width:7px; height:7px; border-radius:50%; background:var(--brand); flex:0 0 auto; }
  .st.low .d{ background:var(--warn); }
  .st.empty .d, .st.expired .d{ background:var(--danger); }
  .st.off .d{ background:var(--faint); }
  .st.low{ color:var(--warn); } .st.empty, .st.expired{ color:var(--danger); } .st.off{ color:var(--muted); }
  .ops{ display:flex; gap:2px; justify-content:flex-end; }
  .ops .btn{ white-space:nowrap; }
  .empty{ text-align:center; color:var(--muted); padding:70px 20px 40px;
    display:flex; flex-direction:column; align-items:center; gap:10px; }
  .empty[hidden]{ display:none; }
  [hidden]{ display:none !important; }
  .empty-title{ font-size:16px; color:var(--text); font-weight:600; }
  .empty-sub{ font-size:12.5px; max-width:460px; line-height:1.7; }
  .empty-actions{ display:flex; gap:9px; margin-top:8px; }
  .tip{ margin:16px auto 0; font-size:11.5px; color:var(--faint); line-height:1.7; }
  .modal{ position:fixed; inset:0; background:rgba(10,10,12,.42); display:none;
    align-items:center; justify-content:center; padding:22px; z-index:50; }
  .modal.show{ display:flex; }
  .sheet{ background:var(--panel); border:1px solid var(--line); border-radius:16px;
    width:100%; max-width:560px; max-height:88vh; display:flex; flex-direction:column;
    box-shadow:0 24px 70px rgba(0,0,0,.28); }
  .sheet.wide{ max-width:780px; }
  .caps{ border:1px solid var(--line); border-radius:9px; overflow:hidden; }
  .caps-head, .caps-row{ display:grid; align-items:center; gap:6px; padding:5px 9px;
    grid-template-columns:minmax(110px,1fr) 38px 68px 62px 62px 62px 38px; }
  .caps-head{ background:var(--surface); color:var(--faint); font-size:10.5px; }
  .caps-head span{ text-align:center; }
  .caps-head span:first-child{ text-align:left; }
  .caps-row{ border-top:1px solid var(--line); }
  .caps-row .nm{ font-family:var(--mono); font-size:10.5px; color:var(--muted);
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .caps-row select, .caps-row input[type="number"]{
    border:1px solid var(--line); background:var(--panel); color:var(--text);
    border-radius:6px; padding:2px 4px; font-size:11px; width:100%; }
  .caps-row input[type="checkbox"]{ accent-color:var(--brand); justify-self:center; margin:0; }
  .caps-empty{ padding:10px; font-size:11px; color:var(--faint); text-align:center; }
  @media (max-width:640px){ .caps-head, .caps-row{ grid-template-columns:1fr 34px 58px 54px 54px 54px 34px; } }
  .sheet-head{ display:flex; align-items:center; padding:15px 18px; border-bottom:1px solid var(--line); }
  .sheet-head h3{ margin:0; font-size:14px; font-weight:600; flex:1; }
  .x{ border:none; background:transparent; color:var(--muted); font-size:14px;
    cursor:pointer; padding:2px 6px; border-radius:6px; }
  .x:hover{ background:var(--surface); color:var(--text); }
  .sheet-body{ padding:16px 18px; display:flex; flex-direction:column; gap:12px; overflow:auto; }
  .sheet-foot{ display:flex; justify-content:flex-end; gap:8px; padding:13px 18px; border-top:1px solid var(--line); }
  .field{ display:flex; flex-direction:column; gap:5px; font-size:12px; color:var(--muted); }
  .field input, .field textarea, .field select{
    border:1px solid var(--line); background:var(--surface); color:var(--text);
    border-radius:9px; padding:8px 10px; font:inherit; font-size:12.5px;
    outline:none; width:100%; resize:vertical; }
  .field input:focus, .field textarea:focus, .field select:focus{ border-color:var(--brand); }
  .f2{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  .f4{ display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:10px; }
  @media (max-width:600px){ .f2, .f4{ grid-template-columns:1fr; } }
  .hint{ font-size:11px; color:var(--faint); line-height:1.6; }
  .hint.ok{ color:var(--brand-ink); }
  .hint.err{ color:var(--danger); }
  .stack{ display:flex; flex-direction:column; gap:10px; }
  .checkline{ display:flex; align-items:center; gap:6px; font-size:12px; color:var(--muted); cursor:pointer; }
  .checkline input{ accent-color:var(--brand); }
  .toast{ position:fixed; left:50%; bottom:28px; transform:translate(-50%,14px);
    background:var(--text); color:var(--bg); padding:9px 16px; border-radius:999px;
    font-size:12.5px; opacity:0; pointer-events:none; transition:opacity .2s, transform .2s;
    z-index:80; max-width:80vw; text-align:center; }
  .toast.show{ opacity:1; transform:translate(-50%,0); }
  @media (max-width:900px){
    .rail{ width:64px; flex-basis:64px; padding:12px 9px; }
    .brand{ padding:4px 0 12px; justify-content:center; }
    .brand span:last-child, .nav-btn span:not(.kbd), .nav-btn .kbd, .sec, .rail-foot, #filters{ display:none; }
    .nav-btn{ justify-content:center; padding:9px 0; }
  }



  /* == MINIMAL-TOPNAV v2 == */
  /* 极简灰阶（Tailwind Zinc）：保留原变量名，仅覆盖色值，确保组件样式自动继承 */
  :root{
    --bg:#FAFAFA; --panel:#FFFFFF; --surface:#F4F4F5;
    --text:#18181B; --muted:#71717A; --faint:#A1A1AA;
    --line:#E4E4E7; --line-strong:#D4D4D8;
    --brand:#15803D; --brand-ink:#15803D; --brand-soft:#E8F5EC;
    --ok:#15803D; --ok-soft:#E8F5EC; --off:#A1A1AA;
    --danger:#B42318; --warn:#A2610A;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#09090B; --panel:#18181B; --surface:#27272A;
      --text:#FAFAFA; --muted:#A1A1AA; --faint:#71717A;
      --line:#3F3F46; --line-strong:#52525B;
      --brand:#34D399; --brand-ink:#34D399; --brand-soft:#12261F;
      --ok:#34D399; --ok-soft:#12261F; --off:#71717A;
      --danger:#F87171; --warn:#E8A34A;
    }
  }
  /* 顶栏：把原左侧 rail 翻成横向 header，分组/筛选并排主区 */
  html,body{ background:var(--bg); }
  .app{ display:block; height:auto; min-height:100vh; }
  .rail{
    width:100%; flex:none; height:auto; min-height:56px;
    flex-direction:row; align-items:center; gap:8px 16px; flex-wrap:wrap;
    padding:9px 22px; border-right:0; border-bottom:1px solid var(--line);
    position:sticky; top:0; z-index:20; background:var(--panel);
  }
  .brand{ padding:0 16px 0 0; font-size:14px; }
  .mark{ width:18px; height:18px; background:var(--text); }
  .rail-nav{ flex-direction:row; gap:4px; }
  .nav-btn{ padding:7px 12px; font-size:12.5px; gap:7px; }
  .nav-btn svg{ width:14px; height:14px; }
  .nav-btn.is-active{ background:var(--surface); }
  .nav-btn.is-active::before{ display:none; }
  .nav-btn .kbd{ display:none; }
  .rail-sep{ width:1px; height:20px; margin:0 2px; }
  .sec{ font-size:10px; padding:0 6px 0 0; }
  #groups, #filters{
    flex:0 1 auto; flex-direction:row; flex-wrap:wrap; gap:4px;
    overflow:visible; min-height:0; align-items:center;
  }
  .row{
    display:inline-flex; align-items:center; gap:6px;
    padding:3px 10px; font-size:11.5px; white-space:nowrap;
    border:1px solid var(--line); border-radius:999px; background:transparent;
  }
  .row.on{ background:var(--text); color:var(--panel); border-color:var(--text); font-weight:600; }
  .row .dot{ width:6px; height:6px; margin-top:0; }
  .dot{ box-shadow:none !important; }
  .rail-foot{
    margin:0 0 0 auto; padding:0; border:0; display:flex;
    align-items:center; gap:10px; text-align:left;
  }
  .foot-line{ padding:0; }
  /* 主区：顶栏已占头部，收掉一级内边距 */
  .main, .page{ min-height:0; }
  .topbar h1{ font-size:19px; }
  /* 去装饰：卡片无阴影、按钮无位移 */
  .card{ box-shadow:none; border-radius:11px; }
  .card:hover{ box-shadow:none; }
  .btn:hover{ transform:none; }
  .btn.primary{ background:var(--text); border-color:var(--text); color:var(--panel); }
  /* v2 新增：卡片网格改为自适应列宽——卡片少时自动变宽铺满，卡片多时自然分列 */
  .group-grid{ grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:14px; align-items:stretch; }
  .group-grid > .card{ width:100%; min-width:0; }
  /* 窄屏：顶栏自然换行，不做图标栏收缩 */
  @media (max-width:900px){
    .rail{ width:100% !important; flex-basis:auto !important; padding:9px 14px; }
    .brand span:last-child, .nav-btn span:not(.kbd), .nav-btn .kbd, .sec, .rail-foot,
    .row .n, .row .m{ display:inline !important; }
    .nav-btn{ justify-content:flex-start; padding:7px 12px; }
    .row{ justify-content:flex-start; padding:3px 10px; }
  }
  /* == END MINIMAL-TOPNAV v2 == */
  </style>
</head>
<body>
<div class="app">
  <aside class="rail">
    <div class="brand"><span class="mark"></span><span>AI Hub</span></div>
    <div class="rail-nav">
      <a class="nav-btn" href="/projects">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1.2"/><rect x="9" y="2.5" width="4.5" height="4.5" rx="1.2"/><rect x="2.5" y="9" width="4.5" height="4.5" rx="1.2"/><rect x="9" y="9" width="4.5" height="4.5" rx="1.2"/></svg>
        <span>项目</span><span class="kbd">⌘1</span>
      </a>
      <a class="nav-btn is-active" href="/models">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="5.8" cy="5.8" r="3"/><path d="M8 8l5 5M10.8 10.8l1.4-1.4M12 12l1.4-1.4" stroke-linecap="round"/></svg>
        <span>模型</span><span class="kbd">⌘2</span>
      </a>
      <a class="nav-btn" href="/tools">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M10.5 2.5l3 3-2 2-3-3zM8.5 4.5L2.5 10.5v3h3l6-6" stroke-linecap="round" stroke-linejoin="round"/></svg>
        <span>工具</span><span class="kbd">⌘3</span>
      </a>
    </div>
    <div class="rail-sep"></div>
    <div class="sec">筛选</div>
    <div id="filters"></div>
    <div class="rail-foot">
      <div class="foot-line">127.0.0.1:8799<br>密钥存于系统钥匙串</div>
    </div>
  </aside>
  <main class="main">
    <header class="topbar">
      <h1>模型与密钥</h1>
      <div class="summary" id="summary">加载中…</div>
      <div class="tools">
        <button class="btn" id="balanceBtn">刷新余额</button>
        <button class="btn primary" id="addBtn">添加平台</button>
      </div>
    </header>
    <div class="page">
      <div class="panel" id="panel"><div class="table-wrap"><table>
        <thead><tr>
          <th>平台</th><th>模型</th><th>额度</th><th>Key</th><th>有效期</th><th>状态</th><th></th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table></div></div>
      <div class="empty" id="empty" hidden>
        <div class="empty-title">还没有记录任何平台</div>
        <div class="empty-sub">添加平台后可以集中管理各家的模型、额度与密钥。额度既可以是免费额度，也可以是充值余额、代金券。密钥写入 macOS 钥匙串，models.json 只保存引用名和掩码。</div>
        <div class="empty-actions"><button class="btn primary" id="emptyAdd">添加第一个平台</button></div>
      </div>
      <div class="tip">密钥保存在 macOS 钥匙串（服务名 aihub.models.*），models.json 不落明文。余额查询在添加平台时配置：可选内置预设或自定义接口路径与字段。</div>
    </div>
  </main>
</div>

<div class="modal" id="editModal">
  <div class="sheet wide">
    <div class="sheet-head"><h3 id="editTitle">添加平台</h3><button class="x" data-close="editModal">✕</button></div>
    <div class="sheet-body">
      <div class="field">
        <label>平台模板</label>
        <select id="mTemplate"></select>
        <div class="hint">选择后自动填好接口地址与额度单位，地址仍可手动修改。</div>
      </div>
      <div class="f2">
        <div class="field"><label>平台名称</label><input id="mVendor" placeholder="如 智谱 AI"></div>
        <div class="field"><label>协议</label>
          <select id="mProtocol">
            <option value="openai">OpenAI 兼容</option>
            <option value="anthropic">Anthropic 兼容</option>
            <option value="custom">其他</option>
          </select>
        </div>
      </div>
      <div class="field"><label>Base URL</label><input id="mBaseUrl" placeholder="https://..."></div>
      <div class="field">
        <div class="labrow">
          <label>API Key</label>
          <button class="btn subtle" id="testKeyBtn" type="button">测试连接</button>
        </div>
        <input id="mKey" type="password" autocomplete="off" placeholder="粘贴 key，写入钥匙串">
        <div class="hint" id="keyHint">编辑时留空表示不修改已保存的 key。</div>
      </div>
      <div class="field">
        <div class="labrow">
          <label>模型</label>
          <button class="btn subtle" id="fetchBtn" type="button">从接口获取</button>
        </div>
        <textarea id="mModels" rows="4" placeholder="GLM-4-Flash|glm-4-flash&#10;每行一个，格式：显示名|模型ID，只写一个值时两者相同"></textarea>
        <div class="hint">一个 key 支持多个模型时，一行一个；也可点右上角按钮按 key 自动拉取。选填。</div>
      </div>
      <div class="field">
        <div class="labrow"><label>模型能力</label><button class="btn subtle" id="guessCapsBtn" type="button">按名称推测</button></div>
        <div class="caps" id="capsBox"><div class="caps-empty">先填写上面的模型列表</div></div>
        <div class="hint">视觉=能看图；代码=编码强度；上下文填 K；速度/价格用于挑快而便宜的模型；工具=tool calling 稳定。不确定就保持默认，只影响自动路由。</div>
      </div>
      <div class="f4">
        <div class="field"><label>总额度</label><input id="mQuotaLimit" type="number" min="0" step="any" placeholder="0 表示未填写"></div>
        <div class="field"><label>已用</label><input id="mQuotaUsed" type="number" min="0" step="any" placeholder="0"></div>
        <div class="field"><label>单位</label>
          <select id="mQuotaUnit"><option value="tokens">tokens</option><option value="次">次</option><option value="请求">请求</option><option value="元">元</option><option value="美元">美元</option></select>
        </div>
        <div class="field"><label>周期</label>
          <select id="mQuotaPeriod">
            <option value="day">每天</option><option value="month">每月</option>
            <option value="5h">每 5 小时</option><option value="total">一次性</option><option value="none">不限</option>
          </select>
        </div>
      </div>
      <div class="hint">免费额度填上限；付费账号填充值总额或代金券面额，单位选「元」/「美元」。</div>
      <div class="field">
        <label>余额查询</label>
        <select id="mBalPreset">
          <option value="none">不支持（手动填写额度）</option>
          <option value="deepseek">DeepSeek</option>
          <option value="moonshot">Kimi / Moonshot</option>
          <option value="siliconflow">硅基流动</option>
          <option value="openrouter">OpenRouter</option>
          <option value="custom">自定义接口</option>
        </select>
      </div>
      <div class="stack" id="balCustom" hidden>
        <div class="f2">
          <div class="field"><label>接口路径</label><input id="mBalPath" placeholder="/users/me/balance"></div>
          <div class="field"><label>余额字段</label><input id="mBalField" placeholder="data.available_balance"></div>
        </div>
        <div class="f2">
          <div class="field"><label>扣减字段（可选）</label><input id="mBalField2" placeholder="data.total_usage"></div>
          <div class="field"><label>单位</label><select id="mBalUnit"><option value="元">元</option><option value="美元">美元</option></select></div>
        </div>
        <div class="hint">路径相对 Base URL；字段支持 data.balance、balance_infos[0].total_balance。扣减字段用于「总额减已用」型接口。</div>
      </div>
      <div class="f2">
        <div class="field"><label>有效期至</label><input id="mExpire" type="date"></div>
        <div class="field"><label>备注</label><input id="mNote" placeholder="可选"></div>
      </div>
      <label class="checkline"><input type="checkbox" id="mEnabled" checked> 启用该平台</label>
    </div>
    <div class="sheet-foot">
      <button class="btn" data-close="editModal">取消</button>
      <button class="btn primary" id="saveBtn">保存</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }[c]));

const TEMPLATES = [
  { name:"自定义", vendor:"", base_url:"", unit:"tokens", period:"month" },
  { name:"DeepSeek", vendor:"DeepSeek", base_url:"https://api.deepseek.com/v1", unit:"元", period:"total", bal:"deepseek" },
  { name:"智谱 AI", vendor:"智谱 AI", base_url:"https://open.bigmodel.cn/api/paas/v4", unit:"tokens", period:"month" },
  { name:"讯飞星火", vendor:"讯飞星火", base_url:"https://spark-api-open.xf-yun.com/v1", unit:"tokens", period:"total" },
  { name:"美团 LongCat", vendor:"美团 LongCat", base_url:"https://api.longcat.chat/openai/v1", unit:"tokens", period:"day" },
  { name:"硅基流动", vendor:"硅基流动", base_url:"https://api.siliconflow.cn/v1", unit:"元", period:"total", bal:"siliconflow" },
  { name:"商汤 SenseNova", vendor:"商汤 SenseNova", base_url:"https://api.sensenova.cn/compatible-mode/v1", unit:"次", period:"5h" },
  { name:"魔搭 ModelScope", vendor:"魔搭 ModelScope", base_url:"https://api-inference.modelscope.cn/v1", unit:"请求", period:"day" },
  { name:"阿里百炼", vendor:"阿里百炼", base_url:"https://dashscope.aliyuncs.com/compatible-mode/v1", unit:"tokens", period:"total" },
  { name:"火山方舟", vendor:"火山方舟", base_url:"https://ark.cn-beijing.volces.com/api/v3", unit:"tokens", period:"total" },
  { name:"百度千帆", vendor:"百度千帆", base_url:"https://qianfan.baidubce.com/v2", unit:"tokens", period:"total" },
  { name:"Kimi 开放平台", vendor:"Kimi", base_url:"https://api.moonshot.cn/v1", unit:"元", period:"total", bal:"moonshot" },
  { name:"MiniMax", vendor:"MiniMax", base_url:"https://api.minimax.chat/v1", unit:"元", period:"total" },
  { name:"百川智能", vendor:"百川智能", base_url:"https://api.baichuan-ai.com/v1", unit:"元", period:"total" },
  { name:"OpenRouter", vendor:"OpenRouter", base_url:"https://openrouter.ai/api/v1", unit:"请求", period:"day", bal:"openrouter" },
  { name:"Groq", vendor:"Groq", base_url:"https://api.groq.com/openai/v1", unit:"请求", period:"day" }
];

const BAL_PRESETS = {
  deepseek:    { base:"origin", path:"/user/balance", field:"balance_infos[0].total_balance", unit:"元" },
  moonshot:    { base:"root", path:"/users/me/balance", field:"data.available_balance", unit:"元" },
  siliconflow: { base:"root", path:"/user/info", field:"data.balance", unit:"元" },
  openrouter:  { base:"root", path:"/credits", field:"data.total_credits", field2:"data.total_usage", unit:"美元" }
};

function balPresetOf(cfg){
  if (!cfg) return "none";
  for (const key in BAL_PRESETS){
    const p = BAL_PRESETS[key];
    if (p.path === cfg.path && p.field === cfg.field && (p.field2 || "") === (cfg.field2 || "")) return key;
  }
  return "custom";
}

function syncBalFields(){
  $("#balCustom").hidden = $("#mBalPreset").value !== "custom";
}

const STATUS = {
  ok:      { label:"可用",     cls:"ok" },
  low:     { label:"额度紧张", cls:"low" },
  empty:   { label:"已用尽",   cls:"empty" },
  expired: { label:"已过期",   cls:"expired" },
  off:     { label:"已停用",   cls:"off" }
};
const PERIOD = { day:"每天", month:"每月", "5h":"每 5 小时", total:"一次性", none:"不限" };

let accounts = [], filter = "all", editing = null, toastTimer = null;

async function api(url, body){
  const opt = body === undefined ? {} : {
    method:"POST", headers:{ "Content-Type":"application/json", "X-AIHub":"1" }, body: JSON.stringify(body)
  };
  const res = await fetch(url, opt);
  return await res.json();
}

function toast(msg){
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

function fmtNum(n){
  n = Number(n) || 0;
  if (n >= 100000000) return (n / 100000000).toFixed(n % 100000000 ? 1 : 0) + " 亿";
  if (n >= 10000) return (n / 10000).toFixed(n % 10000 ? 1 : 0) + " 万";
  return String(Math.round(n));
}

async function refresh(){
  try{
    const data = await api("/api/models");
    accounts = data.accounts || [];
    render();
  }catch(err){
    $("#summary").textContent = "服务未连接";
  }
}

function renderFilters(){
  const count = st => accounts.filter(a => st === "all" || a.status === st).length;
  const items = [["all","全部"],["ok","可用"],["low","额度紧张"],["empty","已用尽"],["expired","已过期"],["off","已停用"]];
  $("#filters").innerHTML = items.map(([k, label]) =>
    '<div class="row ' + (filter === k ? "on" : "") + '" data-f="' + k + '">' +
      '<span class="dot"></span><span class="n">' + label + '</span><span class="m">' + count(k) + '</span></div>'
  ).join("");
  $("#filters").querySelectorAll("[data-f]").forEach(el => {
    el.onclick = () => { filter = el.dataset.f; render(); };
  });
}

function fmtQuota(n, unit){
  n = Number(n) || 0;
  if (unit === "元") return "¥" + (n % 1 ? n.toFixed(2) : String(n));
  if (unit === "美元") return "$" + (n % 1 ? n.toFixed(2) : String(n));
  return fmtNum(n);
}

function quotaCell(a){
  const q = a.quota || {}, limit = Number(q.limit) || 0, used = Number(q.used) || 0;
  const unit = q.unit || "", money = unit === "元" || unit === "美元";
  const hasBal = a.balance !== null && a.balance !== undefined && a.balance !== "";
  const balLine = hasBal
    ? '<div class="qtext">余额 ' + fmtQuota(a.balance, a.balance_unit || unit) +
      (a.balance_at ? ' · ' + esc(String(a.balance_at).slice(5, 16)) : '') + '</div>'
    : "";
  if (!limit) return '<span class="qtext faint">未填写</span>' + balLine;
  const pct = Math.min(100, used / limit * 100);
  const cls = pct >= 100 ? "full" : pct >= 80 ? "warn" : "";
  const tail = money ? "" : " " + esc(unit);
  const period = (money && q.period === "total") ? "" : QPERIOD(q.period);
  return '<div class="bar"><i class="' + cls + '" style="width:' + pct.toFixed(1) + '%"></i></div>' +
    '<div class="qtext">' + fmtQuota(used, unit) + ' / ' + fmtQuota(limit, unit) + tail +
    (period ? ' · ' + period : "") + '</div>' + balLine;
}
function QPERIOD(p){ return PERIOD[p] || ""; }

function capTags(m){
  const c = m.caps;
  if (!c) return "";
  let out = "";
  if (c.vision) out += '<span class="mtag">视觉</span>';
  if (c.tools) out += '<span class="mtag">工具</span>';
  if (c.code === "strong") out += '<span class="mtag">强代码</span>';
  else if (c.code === "weak") out += '<span class="mtag">弱代码</span>';
  if (Number(c.context) > 0) out += '<span class="mtag">' + Number(c.context) + 'K</span>';
  return out;
}

function row(a){
  const st = STATUS[a.status] || STATUS.ok;
  const chips = (a.models || []).length
    ? a.models.map(m => '<span class="mchip" title="' + esc(m.model_id) + '">' + esc(m.name) + capTags(m) + '</span>').join("")
    : '<span class="faint">未填写</span>';
  return '<tr data-id="' + esc(a.id) + '">' +
    '<td><div class="vendor">' + esc(a.vendor) + '<span class="proto">' + esc(a.protocol || "openai") + '</span></div>' +
      '<div class="url" title="' + esc(a.base_url || "") + '">' + esc(a.base_url || "未填写地址") + '</div></td>' +
    '<td><div class="chips">' + chips + '</div></td>' +
    '<td>' + quotaCell(a) + '</td>' +
    '<td><span class="mono">' + esc(a.key_hint || "未配置") + '</span></td>' +
    '<td class="mono muted">' + (a.expire_at ? esc(a.expire_at) : "长期") + '</td>' +
    '<td><span class="st ' + st.cls + '"><span class="d"></span>' + st.label + '</span></td>' +
    '<td><div class="ops">' +
      '<button class="btn subtle" data-act="copy" data-id="' + esc(a.id) + '">复制 Key</button>' +
      '<button class="btn subtle" data-act="edit" data-id="' + esc(a.id) + '">编辑</button>' +
      '<button class="btn subtle danger" data-act="del" data-id="' + esc(a.id) + '">删除</button>' +
    '</div></td></tr>';
}

function render(){
  const list = filter === "all" ? accounts : accounts.filter(a => a.status === filter);
  const modelCount = accounts.reduce((n, a) => n + (a.models || []).length, 0);
  const warn = accounts.filter(a => a.status === "expired" || a.status === "empty").length;
  $("#summary").textContent = accounts.length
    ? accounts.length + " 个平台 · " + modelCount + " 个模型" + (warn ? " · " + warn + " 个需处理" : "")
    : "暂无记录";
  $("#empty").hidden = accounts.length > 0;
  $("#panel").hidden = accounts.length === 0 || list.length === 0;
  $("#rows").innerHTML = list.map(row).join("");
  if (accounts.length && !list.length){
    $("#rows").innerHTML = '<tr><td colspan="7" class="muted" style="padding:28px;text-align:center">该筛选下没有记录</td></tr>';
    $("#panel").hidden = false;
  }
  renderFilters();
  bindOps();
}

function bindOps(){
  document.querySelectorAll("[data-act]").forEach(btn => {
    btn.onclick = () => {
      const a = accounts.find(x => x.id === btn.dataset.id);
      if (!a) return;
      if (btn.dataset.act === "edit") openEdit(a);
      if (btn.dataset.act === "copy") copyKey(a);
      if (btn.dataset.act === "del") removeAccount(a);
    };
  });
}

async function copyKey(a){
  const r = await api("/api/models/copy", { id: a.id });
  toast(r.ok ? "已复制到剪贴板" : (r.message || "复制失败"));
}

function mergeModels(existing, ids){
  const lines = String(existing || "").split("\n").map(s => s.trim()).filter(Boolean);
  const seen = new Set(lines.map(l => (l.includes("|") ? l.split("|")[1] : l).trim()));
  for (const id of ids){
    if (!seen.has(id)){ lines.push(id); seen.add(id); }
  }
  return lines.join("\n");
}

// ---------------- 模型能力标签 ----------------

let capsMap = {};
const DEFAULT_CAPS = { vision:false, tools:false, code:"normal", speed:"normal", price:"normal", context:0 };

function parseModelRows(text){
  return String(text || "").split("\n").map(s => s.trim()).filter(Boolean).map(line => {
    if (!line.includes("|")) return { name:line, model_id:line };
    const parts = line.split("|");
    const mid = (parts[1] || "").trim();
    return { name:(parts[0] || "").trim() || mid, model_id:mid };
  }).filter(m => m.model_id);
}

function capOpts(pairs, cur){
  return pairs.map(([v, t]) => '<option value="' + v + '"' + (v === cur ? " selected" : "") + '>' + t + '</option>').join("");
}

function readCapsRow(row){
  const c = {};
  row.querySelectorAll("[data-k]").forEach(el => {
    c[el.dataset.k] = el.type === "checkbox" ? el.checked
      : el.dataset.k === "context" ? Number(el.value) || 0 : el.value;
  });
  capsMap[row.dataset.mid] = c;
}

function collectCaps(){
  $("#capsBox").querySelectorAll(".caps-row").forEach(readCapsRow);
  return capsMap;
}

function renderCaps(skipCollect){
  const box = $("#capsBox");
  if (!skipCollect && box.querySelector(".caps-row")) collectCaps();
  const rows = parseModelRows($("#mModels").value);
  if (!rows.length){
    box.innerHTML = '<div class="caps-empty">先填写上面的模型列表</div>';
    return;
  }
  box.innerHTML =
    '<div class="caps-head"><span>模型</span><span>视觉</span><span>代码</span><span>上下文</span><span>速度</span><span>价格</span><span>工具</span></div>' +
    rows.map(m => {
      const c = Object.assign({}, DEFAULT_CAPS, capsMap[m.model_id] || {});
      return '<div class="caps-row" data-mid="' + esc(m.model_id) + '">' +
        '<span class="nm" title="' + esc(m.model_id) + '">' + esc(m.name) + '</span>' +
        '<input type="checkbox" data-k="vision"' + (c.vision ? " checked" : "") + '>' +
        '<select data-k="code">' + capOpts([["strong","强"],["normal","中"],["weak","弱"]], c.code) + '</select>' +
        '<input type="number" min="0" max="10000" step="1" data-k="context" value="' + (Number(c.context) || 0) + '">' +
        '<select data-k="speed">' + capOpts([["fast","快"],["normal","中"],["slow","慢"]], c.speed) + '</select>' +
        '<select data-k="price">' + capOpts([["cheap","低"],["normal","中"],["high","高"]], c.price) + '</select>' +
        '<input type="checkbox" data-k="tools"' + (c.tools ? " checked" : "") + '>' +
      '</div>';
    }).join("");
  box.querySelectorAll(".caps-row").forEach(row => {
    row.querySelectorAll("[data-k]").forEach(el => el.addEventListener("change", () => readCapsRow(row)));
  });
}

// 按模型名推测能力，仅作为初始建议，保存前可逐项修改
function guessCaps(mid){
  const id = String(mid || "").toLowerCase();
  const c = Object.assign({}, DEFAULT_CAPS);
  if (/glm-[\d.]+v\b|vision|(^|[-_])vl([-_]|$)|qwen.*vl|llava|omni|claude|gemini|gpt-4o|gpt-4\.1|gpt-5/.test(id)) c.vision = true;
  if (/coder|code|claude|gpt-5|o[34]|deepseek-(v3|r1)|glm-(4\.6|4\.7|5)|kimi-k(2|3)|qwen.*(max|coder)|sonnet|opus/.test(id)) c.code = "strong";
  else if (/flash|air|mini|lite|turbo|small|[1-8]b\b/.test(id)) c.code = "weak";
  if (/flash|air|mini|lite|turbo|highspeed|haiku/.test(id)) c.speed = "fast";
  if (/flash|free|air|lite|mini|turbo/.test(id)) c.price = "cheap";
  if (/claude|gpt|gemini|glm|kimi|qwen|deepseek|mistral|llama/.test(id)) c.tools = true;
  return c;
}

function applyGuessCaps(){
  const rows = parseModelRows($("#mModels").value);
  if (!rows.length){ toast("先填写模型列表"); return; }
  if (!confirm("将按模型名称推测能力，并覆盖当前已填内容，继续？")) return;
  rows.forEach(m => { capsMap[m.model_id] = guessCaps(m.model_id); });
  renderCaps(true);
  toast("已按名称推测，请核对后保存");
}

async function fetchForModal(){
  const btn = $("#fetchBtn");
  btn.disabled = true;
  btn.textContent = "获取中…";
  try{
    const r = await api("/api/models/fetch", {
      id: editing ? editing.id : "",
      base_url: $("#mBaseUrl").value.trim(),
      protocol: $("#mProtocol").value,
      key: $("#mKey").value.trim()
    });
    if (!r.ok){ toast(r.message || "获取失败"); return; }
    $("#mModels").value = mergeModels($("#mModels").value, r.models || []);
    renderCaps();
    toast("获取到 " + r.models.length + " 个模型，保存后生效");
  }catch(err){
    toast("获取失败：" + err.message);
  }finally{
    btn.disabled = false;
    btn.textContent = "从接口获取";
  }
}

async function testKey(){
  const btn = $("#testKeyBtn"), hint = $("#keyHint");
  btn.disabled = true;
  btn.textContent = "测试中…";
  try{
    const r = await api("/api/models/test", {
      id: editing ? editing.id : "",
      base_url: $("#mBaseUrl").value.trim(),
      protocol: $("#mProtocol").value,
      key: $("#mKey").value.trim(),
      models: $("#mModels").value
    });
    hint.className = "hint " + (r.ok ? "ok" : "err");
    hint.textContent = (r.ok ? "✓ " : "✕ ") + r.message + (r.latency ? " · " + r.latency + "ms" : "");
  }catch(err){
    hint.className = "hint err";
    hint.textContent = "✕ 测试失败：" + err.message;
  }finally{
    btn.disabled = false;
    btn.textContent = "测试连接";
  }
}

async function removeAccount(a){
  if (!confirm("删除「" + a.vendor + "」？钥匙串中的 key 也会一并删除。")) return;
  const r = await api("/api/models/delete", { id: a.id });
  toast(r.ok ? "已删除" : (r.message || "删除失败"));
  refresh();
}

async function refreshBalances(){
  const target = accounts.filter(a => a.balance_supported);
  if (!target.length){ toast("当前没有支持自动查余额的平台"); return; }
  const btn = $("#balanceBtn");
  btn.disabled = true;
  btn.textContent = "查询中…";
  let ok = 0, failed = 0, lastMsg = "";
  try{
    for (const a of target){
      const r = await api("/api/models/balance", { id: a.id });
      if (r.ok) ok++; else { failed++; lastMsg = r.message || lastMsg; }
    }
  }catch(err){
    lastMsg = err.message;
  }finally{
    btn.disabled = false;
    btn.textContent = "刷新余额";
  }
  toast(ok ? "已更新 " + ok + " 个平台余额" + (failed ? "，" + failed + " 个失败" : "")
           : ("查询失败：" + (lastMsg || "请检查网络")));
  refresh();
}

function fillTemplates(){
  $("#mTemplate").innerHTML = TEMPLATES.map((t, i) =>
    '<option value="' + i + '">' + esc(t.name) + '</option>').join("");
  $("#mTemplate").onchange = () => {
    const t = TEMPLATES[Number($("#mTemplate").value)] || TEMPLATES[0];
    $("#mVendor").value = t.vendor;
    $("#mBaseUrl").value = t.base_url;
    $("#mQuotaUnit").value = t.unit;
    $("#mQuotaPeriod").value = t.period;
    $("#mBalPreset").value = t.bal || "none";
    syncBalFields();
  };
}

function openEdit(a){
  editing = a || null;
  $("#editTitle").textContent = a ? "编辑平台" : "添加平台";
  $("#mTemplate").value = "0";
  $("#mVendor").value = a ? a.vendor || "" : "";
  $("#mBaseUrl").value = a ? a.base_url || "" : "";
  $("#mProtocol").value = a ? a.protocol || "openai" : "openai";
  $("#mKey").value = "";
  $("#mKey").placeholder = a && a.key_hint && a.key_hint !== "未配置"
    ? "已保存 " + a.key_hint + "，留空则不修改" : "粘贴 key，写入钥匙串";
  $("#keyHint").className = "hint";
  $("#keyHint").textContent = "编辑时留空表示不修改已保存的 key。";
  $("#mModels").value = a ? (a.models || []).map(m => m.name === m.model_id ? m.model_id : m.name + "|" + m.model_id).join("\n") : "";
  $("#capsBox").innerHTML = "";
  capsMap = {};
  (a ? a.models || [] : []).forEach(m => {
    if (m && m.caps) capsMap[m.model_id] = Object.assign({}, m.caps);
  });
  renderCaps();
  const q = a ? a.quota || {} : {};
  $("#mQuotaLimit").value = Number(q.limit) || "";
  $("#mQuotaUsed").value = Number(q.used) || "";
  $("#mQuotaUnit").value = q.unit || "tokens";
  $("#mQuotaPeriod").value = q.period || "month";
  const cfg = a ? a.balance_cfg : null;
  $("#mBalPreset").value = a ? balPresetOf(cfg) : "none";
  $("#mBalPath").value = cfg ? cfg.path || "" : "";
  $("#mBalField").value = cfg ? cfg.field || "" : "";
  $("#mBalField2").value = cfg ? cfg.field2 || "" : "";
  $("#mBalUnit").value = cfg && cfg.unit ? cfg.unit : "元";
  syncBalFields();
  $("#mExpire").value = a ? a.expire_at || "" : "";
  $("#mNote").value = a ? a.note || "" : "";
  $("#mEnabled").checked = a ? a.enabled !== false : true;
  $("#editModal").classList.add("show");
  setTimeout(() => $("#mVendor").focus(), 30);
}

function closeEdit(){ $("#editModal").classList.remove("show"); }

async function save(){
  const rows = parseModelRows($("#mModels").value);
  const caps = collectCaps(), capsOut = {};
  rows.forEach(m => { if (caps[m.model_id]) capsOut[m.model_id] = caps[m.model_id]; });
  const payload = {
    id: editing ? editing.id : "",
    vendor: $("#mVendor").value.trim(),
    base_url: $("#mBaseUrl").value.trim(),
    protocol: $("#mProtocol").value,
    key: $("#mKey").value.trim(),
    models: $("#mModels").value,
    caps: capsOut,
    quota: {
      limit: $("#mQuotaLimit").value || 0,
      used: $("#mQuotaUsed").value || 0,
      unit: $("#mQuotaUnit").value,
      period: $("#mQuotaPeriod").value
    },
    expire_at: $("#mExpire").value,
    note: $("#mNote").value.trim(),
    enabled: $("#mEnabled").checked
  };
  if (!payload.vendor){ toast("请填写平台名称"); return; }
  const bp = $("#mBalPreset").value;
  if (bp === "custom"){
    const path = $("#mBalPath").value.trim(), field = $("#mBalField").value.trim();
    if (!path || !field){ toast("自定义余额接口需要填写路径和字段"); return; }
    payload.balance_cfg = { base:"root", path, field, field2:$("#mBalField2").value.trim(), unit:$("#mBalUnit").value };
  } else {
    payload.balance_cfg = bp === "none" ? null : Object.assign({}, BAL_PRESETS[bp]);
  }
  const btn = $("#saveBtn");
  btn.disabled = true;
  try{
    const r = await api("/api/models/save", payload);
    if (r.ok){ closeEdit(); toast("已保存"); refresh(); }
    else toast(r.message || "保存失败");
  }catch(err){
    toast("保存失败：" + err.message);
  }finally{
    btn.disabled = false;
  }
}

document.querySelectorAll("[data-close]").forEach(el => { el.onclick = closeEdit; });
$("#editModal").addEventListener("click", e => { if (e.target.id === "editModal") closeEdit(); });
document.addEventListener("keydown", e => {
  if (e.key === "Escape") closeEdit();
  if ((e.metaKey || e.ctrlKey) && !e.altKey && ["1", "2", "3"].includes(e.key)){
    e.preventDefault();
    location.href = { "1": "/projects", "2": "/models", "3": "/tools" }[e.key];
  }
});
$("#addBtn").onclick = () => openEdit(null);
$("#emptyAdd").onclick = () => openEdit(null);
$("#saveBtn").onclick = save;
$("#fetchBtn").onclick = fetchForModal;
$("#mModels").addEventListener("input", () => renderCaps());
$("#guessCapsBtn").onclick = applyGuessCaps;
$("#balanceBtn").onclick = refreshBalances;
$("#mBalPreset").onchange = syncBalFields;
$("#testKeyBtn").onclick = testKey;

fillTemplates();
refresh();
setInterval(refresh, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
</script>
</body>
</html>
"""


TOOLS_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Hub · 工具</title>
<style>
  :root{
    --bg:#F5F4F1; --panel:#FFFFFF; --surface:#F0EFEA; --text:#17160F; --muted:#6E6B63;
    --faint:#9C998F; --line:rgba(24,22,18,.12); --line-strong:rgba(24,22,18,.24);
    --brand:#0AB861; --brand-ink:#06743F; --brand-soft:#E5FBF0;
    --danger:#C43A2E; --warn:#A2610A;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#0D0D0F; --panel:#16161A; --surface:#1F1F24; --text:#ECECEE; --muted:#98979A;
           --faint:#6E6D72; --line:rgba(236,236,238,.12); --line-strong:rgba(236,236,238,.26);
           --brand:#2BD98A; --brand-ink:#7BE8B6; --brand-soft:rgba(43,217,138,.12);
           --danger:#E06A5E; --warn:#D89B45; }
  }
  *{ box-sizing:border-box; }
  html,body{ margin:0; height:100%; }
  body{ background:var(--bg); color:var(--text);
        font:13px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",sans-serif;
        -webkit-font-smoothing:antialiased; }
  .app{ display:grid; grid-template-columns:208px 1fr; min-height:100vh; }
  .rail{ border-right:1px solid var(--line); padding:18px 12px; display:flex; flex-direction:column; gap:4px;
         background:var(--bg); position:sticky; top:0; height:100vh; }
  .brand{ display:flex; align-items:center; gap:9px; font-weight:700; font-size:14px; padding:4px 8px 14px; }
  .brand .mark{ width:18px; height:18px; border-radius:5px; background:var(--brand); }
  .rail-nav{ display:flex; flex-direction:column; gap:2px; }
  .nav-btn{ position:relative; display:flex; align-items:center; gap:9px; padding:7px 9px; border-radius:7px;
            color:var(--muted); text-decoration:none; font-size:13px; font-weight:500; }
  .nav-btn svg{ width:15px; height:15px; flex:0 0 auto; opacity:.8; }
  .nav-btn:hover{ background:var(--surface); color:var(--text); }
  .nav-btn.is-active{ background:var(--surface); color:var(--text); font-weight:600; }
  .nav-btn.is-active svg{ opacity:1; }
  .nav-btn.is-active::before{ content:""; position:absolute; left:0; top:9px; bottom:9px; width:2px;
            border-radius:2px; background:var(--brand); }
  .nav-btn .kbd{ margin-left:auto; font-family:var(--mono); font-size:9.5px; color:var(--faint); }
  .rail-sep{ height:1px; background:var(--line); margin:12px 4px; }
  .sec{ font-size:10.5px; letter-spacing:.08em; text-transform:uppercase; color:var(--faint);
        padding:2px 9px 6px; }
  .rail-foot{ margin-top:auto; padding:10px 9px; font-size:10.5px; color:var(--faint);
              font-family:var(--mono); line-height:1.7; }
  .main{ min-width:0; padding:0 28px 40px; }
  .topbar{ display:flex; align-items:center; gap:14px; padding:22px 0 18px; }
  .topbar h1{ font-size:18px; margin:0; font-weight:650; letter-spacing:-.01em; }
  .summary{ color:var(--muted); font-size:12px; }
  .tools{ margin-left:auto; display:flex; gap:8px; }
  .btn{ appearance:none; border:1px solid var(--line-strong); background:var(--panel); color:var(--text);
        font:inherit; font-size:12px; padding:6px 11px; border-radius:7px; cursor:pointer; }
  .btn:hover{ background:var(--surface); }
  .btn.primary{ background:var(--brand); border-color:var(--brand); color:#fff; font-weight:600; }
  .btn.subtle{ border-color:var(--line); color:var(--muted); }
  .btn.danger{ color:var(--danger); border-color:var(--line); }
  .btn:disabled{ opacity:.5; cursor:not-allowed; }
  .page{ display:flex; flex-direction:column; gap:20px; max-width:1080px; }
  .block{ background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  .block-head{ display:flex; align-items:center; gap:10px; padding:14px 16px 12px; border-bottom:1px solid var(--line); }
  .block-head h2{ font-size:13.5px; margin:0; font-weight:650; }
  .block-head .hint{ color:var(--muted); font-size:11.5px; }
  .block-head .right{ margin-left:auto; display:flex; gap:8px; align-items:center; }
  .list{ display:flex; flex-direction:column; }
  .row{ display:flex; align-items:center; gap:12px; padding:11px 16px; border-top:1px solid var(--line); }
  .row:first-child{ border-top:0; }
  .row .dot{ width:7px; height:7px; border-radius:50%; background:var(--faint); flex:0 0 auto; }
  .row .dot.on{ background:var(--brand); box-shadow:0 0 0 3px var(--brand-soft); }
  .row .name{ font-weight:600; font-size:12.5px; min-width:120px; }
  .row .detail{ color:var(--muted); font-size:11.5px; font-family:var(--mono); overflow:hidden;
                text-overflow:ellipsis; white-space:nowrap; }
  .row .spacer{ margin-left:auto; }
  .row .ops{ display:flex; gap:6px; flex:0 0 auto; }
  .tag{ font-size:10px; padding:1px 6px; border-radius:5px; background:var(--surface);
        color:var(--muted); border:1px solid var(--line); white-space:nowrap; }
  .tag.brand{ color:var(--brand-ink); background:var(--brand-soft); border-color:transparent; }
  .tag.warn{ color:var(--warn); }
  .empty-row{ padding:22px 16px; color:var(--muted); font-size:12px; text-align:center; }
  .bind{ padding:14px 16px; display:flex; flex-direction:column; gap:11px; }
  .bind-row{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .bind-row .name{ font-weight:600; font-size:12.5px; min-width:120px; }
  .bind-row .detail{ color:var(--muted); font-size:11.5px; font-family:var(--mono); }
  .bind-row .spacer{ margin-left:auto; }
  .bind-row select{ width:auto; min-width:190px; max-width:100%; }
  .bind-hint{ font-size:11.5px; color:var(--muted); line-height:1.6; word-break:break-all; }
  .bind-hint.warn{ color:var(--warn); }
  .bind-hint.bad{ color:var(--danger); }
  .bind-hint.ok{ color:var(--brand-ink); }
  .cmd{ font-family:var(--mono); font-size:11.5px; color:var(--muted); background:var(--surface);
        padding:2px 6px; border-radius:5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
        max-width:420px; }
  .modal{ position:fixed; inset:0; background:rgba(12,12,12,.42); display:none; align-items:center;
          justify-content:center; padding:24px; z-index:30; }
  .modal.show{ display:flex; }
  .sheet{ background:var(--panel); border-radius:14px; width:min(540px,100%); border:1px solid var(--line);
          box-shadow:0 24px 60px rgba(0,0,0,.22); }
  .sheet-head{ display:flex; align-items:center; gap:10px; padding:16px 18px 12px; border-bottom:1px solid var(--line); }
  .sheet-head h3{ margin:0; font-size:14px; }
  .sheet-head .x{ margin-left:auto; border:0; background:transparent; color:var(--muted);
                  font-size:15px; cursor:pointer; }
  .sheet-body{ padding:16px 18px; display:flex; flex-direction:column; gap:13px; }
  .field{ display:flex; flex-direction:column; gap:6px; font-size:12px; }
  .field > label, .field > span{ color:var(--muted); font-weight:500; }
  input[type=text], input:not([type]), select, textarea{
    font:inherit; font-size:12.5px; padding:7px 9px; border-radius:7px; border:1px solid var(--line-strong);
    background:var(--bg); color:var(--text); width:100%; }
  input:focus, select:focus, textarea:focus{ outline:2px solid var(--brand-soft); border-color:var(--brand); }
  .hint{ color:var(--faint); font-size:11px; line-height:1.6; }
  .f2{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .check{ display:flex; align-items:center; gap:7px; font-size:12px; color:var(--muted); }
  .sheet-foot{ display:flex; justify-content:flex-end; gap:8px; padding:12px 18px 16px; border-top:1px solid var(--line); }
  .toast{ position:fixed; left:50%; bottom:26px; transform:translate(-50%,10px); background:#17160F; color:#fff;
          padding:8px 14px; border-radius:8px; font-size:12px; opacity:0; pointer-events:none;
          transition:.22s ease; z-index:40; }
  .toast.show{ opacity:1; transform:translate(-50%,0); }
  @media (max-width:860px){
    .app{ grid-template-columns:64px 1fr; }
    .brand span:last-child, .nav-btn span:not(.kbd), .nav-btn .kbd, .sec, .rail-foot{ display:none; }
    .nav-btn{ justify-content:center; padding:9px 0; }
    .main{ padding:0 16px 32px; }
    .row{ flex-wrap:wrap; }
  }



  /* == MINIMAL-TOPNAV v2 == */
  /* 极简灰阶（Tailwind Zinc）：保留原变量名，仅覆盖色值，确保组件样式自动继承 */
  :root{
    --bg:#FAFAFA; --panel:#FFFFFF; --surface:#F4F4F5;
    --text:#18181B; --muted:#71717A; --faint:#A1A1AA;
    --line:#E4E4E7; --line-strong:#D4D4D8;
    --brand:#15803D; --brand-ink:#15803D; --brand-soft:#E8F5EC;
    --ok:#15803D; --ok-soft:#E8F5EC; --off:#A1A1AA;
    --danger:#B42318; --warn:#A2610A;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#09090B; --panel:#18181B; --surface:#27272A;
      --text:#FAFAFA; --muted:#A1A1AA; --faint:#71717A;
      --line:#3F3F46; --line-strong:#52525B;
      --brand:#34D399; --brand-ink:#34D399; --brand-soft:#12261F;
      --ok:#34D399; --ok-soft:#12261F; --off:#71717A;
      --danger:#F87171; --warn:#E8A34A;
    }
  }
  /* 顶栏：把原左侧 rail 翻成横向 header，分组/筛选并排主区 */
  html,body{ background:var(--bg); }
  .app{ display:block; height:auto; min-height:100vh; }
  .rail{
    width:100%; flex:none; height:auto; min-height:56px;
    flex-direction:row; align-items:center; gap:8px 16px; flex-wrap:wrap;
    padding:9px 22px; border-right:0; border-bottom:1px solid var(--line);
    position:sticky; top:0; z-index:20; background:var(--panel);
  }
  .brand{ padding:0 16px 0 0; font-size:14px; }
  .mark{ width:18px; height:18px; background:var(--text); }
  .rail-nav{ flex-direction:row; gap:4px; }
  .nav-btn{ padding:7px 12px; font-size:12.5px; gap:7px; }
  .nav-btn svg{ width:14px; height:14px; }
  .nav-btn.is-active{ background:var(--surface); }
  .nav-btn.is-active::before{ display:none; }
  .nav-btn .kbd{ display:none; }
  .rail-sep{ width:1px; height:20px; margin:0 2px; }
  .sec{ font-size:10px; padding:0 6px 0 0; }
  #groups, #filters{
    flex:0 1 auto; flex-direction:row; flex-wrap:wrap; gap:4px;
    overflow:visible; min-height:0; align-items:center;
  }
  .row{
    display:inline-flex; align-items:center; gap:6px;
    padding:3px 10px; font-size:11.5px; white-space:nowrap;
    border:1px solid var(--line); border-radius:999px; background:transparent;
  }
  .row.on{ background:var(--text); color:var(--panel); border-color:var(--text); font-weight:600; }
  .row .dot{ width:6px; height:6px; margin-top:0; }
  .dot{ box-shadow:none !important; }
  .rail-foot{
    margin:0 0 0 auto; padding:0; border:0; display:flex;
    align-items:center; gap:10px; text-align:left;
  }
  .foot-line{ padding:0; }
  /* 主区：顶栏已占头部，收掉一级内边距 */
  .main, .page{ min-height:0; }
  .topbar h1{ font-size:19px; }
  /* 去装饰：卡片无阴影、按钮无位移 */
  .card{ box-shadow:none; border-radius:11px; }
  .card:hover{ box-shadow:none; }
  .btn:hover{ transform:none; }
  .btn.primary{ background:var(--text); border-color:var(--text); color:var(--panel); }
  /* v2 新增：卡片网格改为自适应列宽——卡片少时自动变宽铺满，卡片多时自然分列 */
  .group-grid{ grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:14px; align-items:stretch; }
  .group-grid > .card{ width:100%; min-width:0; }
  /* 窄屏：顶栏自然换行，不做图标栏收缩 */
  @media (max-width:900px){
    .rail{ width:100% !important; flex-basis:auto !important; padding:9px 14px; }
    .brand span:last-child, .nav-btn span:not(.kbd), .nav-btn .kbd, .sec, .rail-foot,
    .row .n, .row .m{ display:inline !important; }
    .nav-btn{ justify-content:flex-start; padding:7px 12px; }
    .row{ justify-content:flex-start; padding:3px 10px; }
  }
  /* == END MINIMAL-TOPNAV v2 == */
  </style>
</head>
<body>
<div class="app">
  <aside class="rail">
    <div class="brand"><span class="mark"></span><span>AI Hub</span></div>
    <div class="rail-nav">
      <a class="nav-btn" href="/projects">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1.2"/><rect x="9" y="2.5" width="4.5" height="4.5" rx="1.2"/><rect x="2.5" y="9" width="4.5" height="4.5" rx="1.2"/><rect x="9" y="9" width="4.5" height="4.5" rx="1.2"/></svg>
        <span>项目</span><span class="kbd">⌘1</span>
      </a>
      <a class="nav-btn" href="/models">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="5.8" cy="5.8" r="3"/><path d="M8 8l5 5M10.8 10.8l1.4-1.4M12 12l1.4-1.4" stroke-linecap="round"/></svg>
        <span>模型</span><span class="kbd">⌘2</span>
      </a>
      <a class="nav-btn is-active" href="/tools">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M10.5 2.5l3 3-2 2-3-3zM8.5 4.5L2.5 10.5v3h3l6-6" stroke-linecap="round" stroke-linejoin="round"/></svg>
        <span>工具</span><span class="kbd">⌘3</span>
      </a>
    </div>
    <div class="rail-sep"></div>
    <div class="sec">说明</div>
    <div class="rail-foot">AI 只给建议<br>规则识别始终可用</div>
    <div class="rail-foot">127.0.0.1:8799</div>
  </aside>
  <main class="main">
    <header class="topbar">
      <h1>工具与后端</h1>
      <div class="summary" id="summary">加载中…</div>
      <div class="tools">
        <button class="btn" id="refreshBtn">刷新状态</button>
        <button class="btn primary" id="addBtn">＋ 添加 CLI</button>
      </div>
    </header>
    <div class="page">
      <section class="block">
        <div class="block-head">
          <h2>CLI 模型绑定</h2>
          <span class="hint">切换后写入 Claude Code 配置，新开的终端会话生效</span>
          <div class="right">
            <button class="btn subtle" id="verifyClaudeBtn">验证</button>
            <button class="btn subtle" id="restoreClaudeBtn">恢复默认</button>
          </div>
        </div>
        <div class="bind" id="claudeBind"></div>
      </section>

      <section class="block">
        <div class="block-head">
          <h2>本机 AI 工具</h2>
          <span class="hint">自动探测，无需配置</span>
        </div>
        <div class="list" id="builtinList"></div>
      </section>

      <section class="block">
        <div class="block-head">
          <h2>环境工具</h2>
          <span class="hint">版本管理工具与本机运行时，自动探测</span>
        </div>
        <div class="list" id="envList"></div>
      </section>

      <section class="block">
        <div class="block-head">
          <h2>兼容账号</h2>
          <span class="hint">来自「模型」页的 OpenAI / Anthropic 接口账号</span>
          <div class="right"><a class="btn subtle" href="/models">管理账号</a></div>
        </div>
        <div class="list" id="httpList"></div>
      </section>

      <section class="block">
        <div class="block-head">
          <h2>自定义 CLI</h2>
          <span class="hint">codex、gemini-cli 等，命令模板里用 {prompt} 占位</span>
          <div class="right"><button class="btn subtle" id="addBtn2">＋ 添加</button></div>
        </div>
        <div class="list" id="customList"></div>
      </section>
    </div>
  </main>
</div>

<div class="toast" id="toast"></div>

<div class="modal" id="toolModal">
  <div class="sheet">
    <div class="sheet-head"><h3 id="toolTitle">添加 CLI 工具</h3><button class="x" data-close="toolModal">✕</button></div>
    <div class="sheet-body">
      <input type="hidden" id="tID">
      <div class="f2">
        <label class="field">名称<input id="tLabel" placeholder="如 Codex"></label>
        <label class="field">超时（秒）<input id="tTimeout" placeholder="300"></label>
      </div>
      <label class="field">命令模板
        <input id="tCommand" placeholder="codex exec &quot;{prompt}&quot;">
        <div class="hint">按空格分词后原样执行，不经过 shell。arg 模式必须有一个独立的 {prompt}；stdin 模式把提示词写进标准输入。</div>
      </label>
      <div class="f2">
        <label class="field">提示词传入方式
          <select id="tInput">
            <option value="arg">命令参数（{prompt}）</option>
            <option value="stdin">标准输入 stdin</option>
          </select>
        </label>
        <label class="check"><input type="checkbox" id="tEnabled" checked> 启用</label>
      </div>
      <div class="hint">示例：codex exec "{prompt}" · gemini -p "{prompt}" · 自定义脚本 /path/to/run.sh（stdin 模式）<br>
        自定义 CLI 只能拿到注入的项目上下文，不能自行读文件；需要读代码请用 Claude Code / OpenClaw。</div>
    </div>
    <div class="sheet-foot">
      <button class="btn" data-close="toolModal">取消</button>
      <button class="btn primary" id="saveBtn">保存并测试</button>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }[c]));
let data = { tools: [], http: [], clis: [], env: { managers: [], runtimes: [] } }, toastTimer = null;

async function api(url, body){
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-AIHub": "1" }, body: JSON.stringify(body)
  };
  const res = await fetch(url, opt);
  return await res.json();
}

function toast(msg){
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

function timeAgo(ts){
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + " 秒前";
  if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  if (s < 86400) return Math.floor(s / 3600) + " 小时前";
  return Math.floor(s / 86400) + " 天前";
}

function claudeCli(){
  return (data.clis || []).find(c => c.id === "claude") || null;
}

function claudeBindRows(){
  const c = claudeCli();
  if (!c) return '<div class="empty-row">加载中…</div>';
  const b = c.binding;
  const st = c.settings || {};
  const verify = c.verify || {};
  const models = c.models || [];

  let stateTag = '<span class="tag">未绑定</span>';
  if (b){
    if (st.state === "synced") stateTag = '<span class="tag brand">已同步</span>';
    else if (st.state === "drift") stateTag = '<span class="tag warn">配置被外部修改</span>';
    else if (st.state === "missing") stateTag = '<span class="tag warn">配置缺失</span>';
    else if (st.state === "invalid") stateTag = '<span class="tag warn">配置无法解析</span>';
  }
  const verifyTag = verify.at
    ? '<span class="tag ' + (verify.ok ? "brand" : "warn") + '" title="' + esc(verify.message || "") + '">' +
      (verify.ok ? "已验证" : "验证失败") + " · " + timeAgo(verify.at) + "</span>"
    : '<span class="tag">未验证</span>';

  const opts = models.map(m => {
    const selected = b && b.account === m.account && b.model === m.model;
    return '<option value="' + esc(m.account + "|" + m.model) + '"' + (selected ? " selected" : "") + ">" +
      esc(m.vendor + " · " + m.label + "（" + m.model + "）") + "</option>";
  }).join("");
  const selectHtml = opts
    ? '<select id="claudeModelSel">' + opts + "</select>"
    : '<select id="claudeModelSel" disabled><option value="">模型页里没有可用的 Anthropic 兼容账号</option></select>';
  const keyMode = b ? (b.key_mode === "plain" ? "plain" : "helper") : "helper";
  const keyHtml = '<select id="claudeKeyMode">' +
    '<option value="helper"' + (keyMode === "helper" ? " selected" : "") + ">密钥：钥匙串动态读取</option>" +
    '<option value="plain"' + (keyMode === "plain" ? " selected" : "") + ">密钥：写入配置（兼容性最好）</option>" +
    "</select>";

  let hints = "";
  if (!c.available){
    hints += '<div class="bind-hint warn">没有检测到 claude 命令，安装后刷新即可。</div>';
  }
  if (b){
    hints += '<div class="bind-hint">当前：' + esc((b.vendor || "") + " · " + b.model) +
      '；配置文件：' + esc(c.config_path) + "</div>";
  }else{
    hints += '<div class="bind-hint">还没有绑定模型。选择后会写入 ' + esc(c.config_path) +
      "，终端新开的 claude 会话自动使用该模型。</div>";
  }
  if (st.state === "drift"){
    hints += '<div class="bind-hint warn">' + esc(st.detail || "") + "，点「切换模型」可重新写入。</div>";
  }else if (st.state === "missing" || st.state === "invalid"){
    hints += '<div class="bind-hint bad">' + esc(st.detail || "") + "</div>";
  }
  if (verify.at) hints += '<div class="bind-hint ' + (verify.ok ? "ok" : "bad") + '">最近验证：' + esc(verify.message || "") + "</div>";
  (c.conflicts || []).forEach(x => {
    hints += '<div class="bind-hint warn">' + esc(x.file) + " 里设置了 " + esc(x.keys.join("、")) +
      "，可能覆盖这里的配置。</div>";
  });

  return '<div class="bind-row">' +
      '<span class="dot' + (c.available ? " on" : "") + '"></span>' +
      '<span class="name">Claude Code</span>' +
      '<span class="tag">' + (c.available ? esc(c.version || "已安装") : "未安装") + "</span>" +
      stateTag + verifyTag +
      '<span class="spacer"></span>' +
      '<span class="detail" title="' + esc(c.path || "") + '">' + esc(c.path || "") + "</span>" +
    "</div>" +
    '<div class="bind-row">' + selectHtml + keyHtml +
      '<button class="btn primary" id="bindClaudeBtn"' +
      (c.available && models.length ? "" : " disabled") + ">切换模型</button>" +
    "</div>" + hints;
}

function wireClaudeBind(){
  const btn = $("#bindClaudeBtn");
  if (!btn) return;
  btn.onclick = async () => {
    const raw = $("#claudeModelSel").value || "";
    const cut = raw.indexOf("|");
    if (cut < 0){ toast("请先选择一个模型"); return; }
    btn.disabled = true;
    btn.textContent = "写入中…";
    try{
      const res = await api("/api/clis/bind", {
        cli: "claude",
        account: raw.slice(0, cut),
        model: raw.slice(cut + 1),
        key_mode: $("#claudeKeyMode").value
      });
      toast(res.message || (res.ok ? "已切换" : "切换失败"));
      await refresh();
    }catch(err){
      toast("切换失败：" + err.message);
    }finally{
      btn.disabled = false;
      btn.textContent = "切换模型";
    }
  };
}

function builtinRows(){
  const rows = data.tools.filter(t => t.kind !== "custom");
  return rows.map(t => {
    const caps = t.files ? '<span class="tag brand">可读代码</span>' : '<span class="tag">仅上下文</span>';
    return '<div class="row">' +
      '<span class="dot' + (t.available ? " on" : "") + '"></span>' +
      '<span class="name">' + esc(t.label) + "</span>" + caps +
      '<span class="detail" title="' + esc(t.detail) + '">' + esc(t.detail) + "</span>" +
      '<span class="spacer"></span>' +
      '<span class="tag">' + (t.available ? "可用" : "不可用") + "</span>" +
      "</div>";
  }).join("");
}

function envRows(){
  const env = data.env || { managers: [], runtimes: [] };
  const parts = [];
  (env.runtimes || []).forEach(r => {
    if (!r.version) return;
    parts.push('<div class="row">' +
      '<span class="dot on"></span>' +
      '<span class="name">' + esc(r.label) + "</span>" +
      '<span class="tag">' + esc(r.id) + "</span>" +
      '<span class="spacer"></span>' +
      '<span class="tag brand">' + esc(r.version) + "</span>" +
      "</div>");
  });
  (env.managers || []).forEach(m => {
    let detail = m.detail || "";
    if (m.versions && m.versions.length) detail += " · 已装 " + m.versions.length + " 个版本";
    parts.push('<div class="row">' +
      '<span class="dot' + (m.available ? " on" : "") + '"></span>' +
      '<span class="name">' + esc(m.label) + "</span>" +
      '<span class="tag">' + esc(m.desc) + "</span>" +
      '<span class="detail" title="' + esc(detail) + '">' + esc(detail) + "</span>" +
      '<span class="spacer"></span>' +
      '<span class="tag' + (m.available ? " brand" : "") + '">' +
        (m.available ? "已装" : "未装") + "</span>" +
      "</div>");
  });
  return parts.length ? parts.join("") :
    '<div class="empty-row">未探测到环境工具</div>';
}

function httpRows(){
  const list = data.http || [];
  if (!list.length){
    return '<div class="empty-row">还没有可用账号。到「模型」页添加 OpenAI / Anthropic 兼容平台，填好 Base URL、Key 和模型后会自动出现在这里。</div>';
  }
  return list.map(a => {
    const names = (a.models || []).map(m => m.model_id).join("、");
    return '<div class="row">' +
      '<span class="dot on"></span>' +
      '<span class="name">' + esc(a.vendor) + "</span>" +
      '<span class="tag brand">' + (a.protocol === "anthropic" ? "Anthropic" : "OpenAI") + "</span>" +
      '<span class="tag">仅上下文</span>' +
      '<span class="detail" title="' + esc(names) + '">' + esc(names) + "</span>" +
      '<span class="spacer"></span>' +
      '<span class="tag">' + (a.models || []).length + " 个模型</span>" +
      "</div>";
  }).join("");
}

function customRows(){
  const list = data.tools.filter(t => t.kind === "custom");
  if (!list.length){
    return '<div class="empty-row">还没有自定义 CLI。点右上角「添加 CLI」，填一条命令模板即可接入 codex、gemini-cli 等工具。</div>';
  }
  return list.map(t => {
    const mode = t.input === "stdin" ? "stdin" : "arg";
    return '<div class="row">' +
      '<span class="dot' + (t.available ? " on" : "") + '"></span>' +
      '<span class="name">' + esc(t.label) + "</span>" +
      '<span class="tag">' + mode + "</span>" +
      '<span class="cmd" title="' + esc(t.command) + '">' + esc(t.command) + "</span>" +
      '<span class="spacer"></span>' +
      '<span class="tag' + (t.available ? "" : " warn") + '">' + (t.enabled ? (t.available ? "可用" : "未找到命令") : "已停用") + "</span>" +
      '<span class="ops">' +
        '<button class="btn subtle" data-test="' + esc(t.tool_id) + '">测试</button>' +
        '<button class="btn subtle" data-edit="' + esc(t.tool_id) + '">编辑</button>' +
        '<button class="btn danger" data-del="' + esc(t.tool_id) + '">删除</button>' +
      "</span></div>";
  }).join("");
}

function render(){
  $("#claudeBind").innerHTML = claudeBindRows();
  wireClaudeBind();
  $("#builtinList").innerHTML = builtinRows();
  $("#envList").innerHTML = envRows();
  $("#httpList").innerHTML = httpRows();
  $("#customList").innerHTML = customRows();
  const avail = data.tools.filter(t => t.available).length;
  const custom = data.tools.filter(t => t.kind === "custom").length;
  const claude = claudeCli();
  const bound = claude && claude.binding ? " · Claude 已绑定 " + claude.binding.model : "";
  $("#restoreClaudeBtn").disabled = !(claude && (claude.binding || claude.has_backup));
  $("#summary").textContent = avail + " 个可用后端 · " + custom + " 个自定义 CLI · " +
    ((data.http || []).length) + " 个兼容账号" + bound;
  document.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => openEdit(b.dataset.edit));
  document.querySelectorAll("[data-del]").forEach(b => b.onclick = () => delTool(b.dataset.del));
  document.querySelectorAll("[data-test]").forEach(b => b.onclick = () => testTool(b.dataset.test, b));
}

async function refresh(){
  try{
    const res = await api("/api/ai/tools");
    data = { tools: res.tools || [], http: res.http || [], clis: res.clis || [], env: data.env || { managers: [], runtimes: [] } };
    render();
  }catch(err){
    $("#summary").textContent = "服务未连接";
  }
  try{
    const env = await api("/api/env/status");
    data.env = env;
    render();
  }catch(err){}
}

function openEdit(id){
  const tool = (data.tools || []).find(t => t.kind === "custom" && t.tool_id === id);
  $("#toolTitle").textContent = tool ? "编辑 CLI 工具" : "添加 CLI 工具";
  $("#tID").value = tool ? tool.tool_id : "";
  $("#tLabel").value = tool ? tool.label : "";
  $("#tCommand").value = tool ? tool.command : "";
  $("#tInput").value = tool ? (tool.input || "arg") : "arg";
  $("#tTimeout").value = tool ? tool.timeout : 300;
  $("#tEnabled").checked = tool ? tool.enabled !== false : true;
  $("#toolModal").classList.add("show");
  setTimeout(() => $("#tLabel").focus(), 40);
}

function closeEdit(){ $("#toolModal").classList.remove("show"); }

async function saveTool(){
  const payload = {
    id: $("#tID").value,
    label: $("#tLabel").value.trim(),
    command: $("#tCommand").value.trim(),
    input: $("#tInput").value,
    timeout: $("#tTimeout").value || 300,
    enabled: $("#tEnabled").checked
  };
  if (!payload.command){ toast("请填写命令模板"); return; }
  const btn = $("#saveBtn");
  btn.disabled = true;
  try{
    const res = await api("/api/ai/tools/save", payload);
    if (!res.ok){ toast(res.message || "保存失败"); return; }
    $("#tID").value = res.tool.id;
    toast(res.message || "已保存");
    await refresh();
    testTool(res.tool.id);
  }catch(err){
    toast("保存失败：" + err.message);
  }finally{
    btn.disabled = false;
  }
}

async function delTool(id){
  const tool = (data.tools || []).find(t => t.kind === "custom" && t.tool_id === id);
  if (!confirm("删除自定义工具「" + (tool ? tool.label : id) + "」？")) return;
  const res = await api("/api/ai/tools/delete", { id });
  if (res.ok){ toast("已删除"); refresh(); }
  else toast(res.message || "删除失败");
}

async function testTool(id, btn){
  if (btn){ btn.disabled = true; btn.textContent = "测试中…"; }
  try{
    const res = await api("/api/ai/tools/test", { id });
    toast(res.message || (res.ok ? "命令可用" : "测试失败"));
    if (res.ok && res.output) $("#summary").textContent = "测试输出：" + res.output.slice(0, 80);
  }catch(err){
    toast("测试失败：" + err.message);
  }finally{
    if (btn){ btn.disabled = false; btn.textContent = "测试"; }
  }
}

document.querySelectorAll("[data-close]").forEach(el => el.onclick = closeEdit);
$("#toolModal").addEventListener("click", e => { if (e.target.id === "toolModal") closeEdit(); });
document.addEventListener("keydown", e => {
  if (e.key === "Escape") closeEdit();
  if ((e.metaKey || e.ctrlKey) && !e.altKey && ["1", "2", "3"].includes(e.key)){
    e.preventDefault();
    location.href = { "1": "/projects", "2": "/models", "3": "/tools" }[e.key];
  }
});
$("#addBtn").onclick = () => openEdit("");
$("#addBtn2").onclick = () => openEdit("");
$("#saveBtn").onclick = saveTool;
$("#refreshBtn").onclick = refresh;

$("#verifyClaudeBtn").onclick = async () => {
  const btn = $("#verifyClaudeBtn");
  btn.disabled = true;
  btn.textContent = "验证中…";
  try{
    const res = await api("/api/clis/verify", { cli: "claude", deep: true });
    toast(res.message || (res.ok ? "验证通过" : "验证失败"));
    await refresh();
  }catch(err){
    toast("验证失败：" + err.message);
  }finally{
    btn.disabled = false;
    btn.textContent = "验证";
  }
};

$("#restoreClaudeBtn").onclick = async () => {
  if (!confirm("恢复切换前的 Claude Code 配置？当前模型绑定会被清除。")) return;
  const res = await api("/api/clis/restore", { cli: "claude" });
  toast(res.message || (res.ok ? "已恢复" : "恢复失败"));
  if (res.ok) refresh();
};

refresh();
setInterval(refresh, 6000);
</script>
</body>
</html>
"""


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = "http://127.0.0.1:%d" % PORT
    print("AI Hub 已启动：%s" % url)
    print("按 Ctrl+C 退出")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
