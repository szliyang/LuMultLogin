#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ── 变更记录 ─────────────────────────────────────────────
# 20261007-1148 双工作流改造：新增 ACCOUNT_INDEX 按槽位选账号，配套 login-acc1/login-acc2
#             两个错峰定时 workflow，删除原单工作流 login.yml；未设置时保持跑全部账号的旧行为
# 20261007-1350 环境变量编号统一：LUNES_EMAIL1/2、LUNES_PASSWORD1/2、NODE_LINK1/2 与槽位一一对应；
#             节点改按槽位领取（删 assign_node），旧多行 NODE_LINK 保留为兼容回退
# 20261007-1355 配置校验加固：账号凭据 strip() 防误粘空白；配置不齐/ACCOUNT_INDEX 非法
#             由静默 return 改为 sys.exit(1)，workflow 显式红叉并触发 GitHub 失败邮件
# ─────────────────────────────────────────────────────────

import os
import sys
import json
import time
import socket
import subprocess
import requests
import re
import random
from seleniumbase import SB

# 从环境变量获取账号密码和 TG 配置
# 🆕 修改：环境变量编号与 ACCOUNT_INDEX 槽位统一——账号1 用 LUNES_EMAIL1/LUNES_PASSWORD1/NODE_LINK1，
#          账号2 用 LUNES_EMAIL2/LUNES_PASSWORD2/NODE_LINK2（旧命名 LUNES_EMAIL/LUNES_EMAIL1 弃用）
# 🆕 修改：凭据统一 strip()，secret 误粘首尾空白时按「未配置」处理而不是带垃圾值去登录
#          （注意：密码本身含首尾空格的极端情况不受支持）
EMAIL1       = (os.environ.get("LUNES_EMAIL1") or "").strip()    # 登录邮箱（账号1）
PASSWORD1    = (os.environ.get("LUNES_PASSWORD1") or "").strip() # 登录密码（账号1）
TG_CHAT_ID   = (os.environ.get("TG_CHAT_ID") or "").strip()      # chat id,可选
TG_BOT_TOKEN = (os.environ.get("TG_BOT_TOKEN") or "").strip()    # bot token,可选
EMAIL2       = (os.environ.get("LUNES_EMAIL2") or "").strip()    # 登录邮箱（账号2，可选；不配则跳过）
PASSWORD2    = (os.environ.get("LUNES_PASSWORD2") or "").strip() # 登录密码（账号2，可选；不配则跳过）

# 账号列表——只收集「邮箱+密码都齐全」的账号，main 里依次执行
# 条目带 slot 字段（1/2）供 ACCOUNT_INDEX 按槽位选择，与「实际配了几个账号」解耦：
# 双工作流模式下，acc2 的 run 只传账号2凭据也能正确选中
ACCOUNTS = []
if EMAIL1 and PASSWORD1:
    ACCOUNTS.append({"slot": 1, "name": "账号1", "email": EMAIL1, "password": PASSWORD1, "shot": "acc1", "flag": "🇺🇸"})
if EMAIL2 and PASSWORD2:
    ACCOUNTS.append({"slot": 2, "name": "账号2", "email": EMAIL2, "password": PASSWORD2, "shot": "acc2", "flag": "🇩🇪"})

LOGIN_URL = "https://betadash.lunes.host/login?next=/"

PROXY_LOCAL  = "http://127.0.0.1:1081"   # sing-box 本地监听地址
PROXY_PORT   = 1081
SETUP_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_proxy.sh")

# 邮箱掩码
def mask_email(email: str) -> str:
    if '@' in email:
        name, domain = email.split('@', 1)
        if len(name) > 4:
            return f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            return f"{name}@{domain}"
    else:
        return email[:2] + '****'

#  Telegram 推送
def send_tg_message(status_icon, status_text, extra_text="", email="", name="", flag="🇺🇸"):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 Telegram 推送。")
        return

    local_time = time.gmtime(time.time() + 8 * 3600)
    current_time_str = time.strftime("%Y-%m-%d %H:%M:%S", local_time)

    masked_email = mask_email(email)

    text = (
        f"{flag} Lunes 保活通知\n\n"
        f"{status_icon} {status_text}\n"
        f"👤 登录账户: {name} {masked_email}\n"
        f"⏱️ 登录时间: {current_time_str}"
    )
    if extra_text:
        text += f"\n\n{extra_text}"

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text}

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print("📩 Telegram 通知发送成功！")
        else:
            print(f"  ⚠️ Telegram 通知发送失败: {r.text}")
    except Exception as e:
        print(f"  ⚠️ Telegram 通知发送异常: {e}")

# ============ 🆕 多节点代理管理 ============
# 🆕 修改：节点配置与槽位编号统一——
#   NODE_LINK1 → 账号1 专属节点（填多行时只取第 1 行）
#   NODE_LINK2 → 账号2 专属节点（同上）
#   兼容回退：两者都未配置时，按旧逻辑解析多行 NODE_LINK
#   （第1行→账号1，第2行→账号2，3 行及以上只取前 2 行）
def parse_nodes():
    nodes = {}
    for slot in (1, 2):
        raw = (os.environ.get(f"NODE_LINK{slot}") or "").strip()
        if raw:
            first = raw.splitlines()[0].strip()
            if first:
                nodes[slot] = first
    if nodes:
        return nodes
    raw = os.environ.get("NODE_LINK") or ""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if len(lines) > 2:
        print(f"ℹ️ NODE_LINK 共 {len(lines)} 行，只取前 2 行，其余忽略。")
    return {i + 1: line for i, line in enumerate(lines[:2])}

# setup_proxy.sh 支持的协议（2026-10 核对该脚本源码确认，不支持 ss://）
SUPPORTED_PROTOS = {"vless", "vmess", "trojan", "hysteria2", "hy2", "tuic", "anytls", "socks5", "socks"}

def node_proto(link: str) -> str:
    return link.split("://", 1)[0].split(":", 1)[0].strip().lower()

def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False

def stop_singbox():
    # 杀掉残留 sing-box，避免上一个账号的节点串到下一个账号
    subprocess.run(["pkill", "-f", "sing-box"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 🆕 修改：等端口真正释放（最多 5 秒），避免新 sing-box 绑不上 1081
    for _ in range(10):
        if not _port_open(PROXY_PORT):
            return
        time.sleep(0.5)

# 🆕 新增：通过本地代理发真实请求，确认节点真的可用（仅端口通不算数）
def verify_proxy() -> bool:
    try:
        r = requests.get("https://api.ip.sb/ip",
                         proxies={"http": PROXY_LOCAL, "https": PROXY_LOCAL},
                         timeout=15)
        if r.status_code == 200:
            print(f"✅ 代理实测可用，出口 IP: {r.text.strip()}")
            return True
        print(f"⚠️ 代理实测返回 HTTP {r.status_code}")
    except Exception as e:
        print(f"⚠️ 代理实测失败: {e}")
    return False

def _print_file_tail(path: str, n: int = 20):
    try:
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
        for line in lines[-n:]:
            print(f"    {line.rstrip()}")
    except Exception:
        pass

# 🆕 新增：IPv4 强制重试——GitHub Actions runner 无 IPv6 出口，
#           hy2/tuic 等 UDP 节点域名若解析到 AAAA 会 dial 失败 (network is unreachable)。
#           做法：把配置里的服务器域名解析成 A 记录（IPv4 字面量）写回配置，自行重启 sing-box。
def retry_singbox_ipv4() -> bool:
    script_dir = os.path.dirname(SETUP_SCRIPT)
    cfg_path   = os.path.join(script_dir, "sing-box-config.json")
    bin_path   = os.path.join(script_dir, "sing-box")
    log_path   = os.path.join(script_dir, "sing-box.log")
    if not (os.path.exists(cfg_path) and os.path.exists(bin_path)):
        print("⚠️ 未找到 sing-box 配置或二进制，无法 IPv4 重试")
        return False
    try:
        with open(cfg_path) as f:
            conf = json.load(f)
    except Exception as e:
        print(f"⚠️ 读取 sing-box 配置失败: {e}")
        return False

    changed = False
    for ob in conf.get("outbounds", []):
        server = ob.get("server", "")
        if not server:
            continue
        # 已是 IPv4 字面量则无需改写
        try:
            socket.inet_pton(socket.AF_INET, server)
            continue
        except OSError:
            pass
        # IPv6 字面量在 Actions 上无路由，改不了
        try:
            socket.inet_pton(socket.AF_INET6, server)
            print(f"⚠️ 节点服务器是 IPv6 字面量 {server}，runner 无 IPv6 出口，无法重试")
            continue
        except OSError:
            pass
        try:
            ipv4 = socket.getaddrinfo(server, None, socket.AF_INET)[0][4][0]
        except OSError as e:
            print(f"⚠️ 解析 {server} 的 A 记录失败: {e}")
            continue
        print(f"🔧 强制 IPv4: {server} → {ipv4}")
        ob["server"] = ipv4   # tls.server_name 已在配置里是原域名/SNI，不受影响
        changed = True

    if not changed:
        print("⚠️ 配置无需改写（或改写失败），跳过重试")
        return False

    try:
        with open(cfg_path, "w") as f:
            json.dump(conf, f, indent=2)
    except Exception as e:
        print(f"⚠️ 写回配置失败: {e}")
        return False

    stop_singbox()
    print("🔧 以 IPv4 配置重启 sing-box...")
    try:
        log_file = open(log_path, "w")
        subprocess.Popen([bin_path, "run", "-c", cfg_path],
                         cwd=script_dir, stdout=log_file, stderr=subprocess.STDOUT,
                         start_new_session=True)
    except Exception as e:
        print(f"⚠️ 重启 sing-box 失败: {e}")
        return False

    for _ in range(10):
        if _port_open(PROXY_PORT):
            break
        time.sleep(1)
    else:
        print("❌ IPv4 重试后端口未监听，sing-box.log 末尾：")
        _print_file_tail(log_path)
        return False

    for _ in range(3):
        if verify_proxy():
            print("✅ IPv4 强制重试成功")
            return True
        time.sleep(2)
    print("❌ IPv4 重试后代理仍不可用，sing-box.log 末尾：")
    _print_file_tail(log_path)
    return False

def start_singbox(node_link: str) -> bool:
    # 🆕 新增：前置协议校验——setup_proxy.sh 不支持 ss:// 等协议，直接判失败，不白跑 1~3 分钟
    proto = node_proto(node_link)
    print(f"🔎 节点协议: {proto}")
    if proto not in SUPPORTED_PROTOS:
        print(f"❌ setup_proxy.sh 不支持协议 [{proto}]（支持: vless/vmess/trojan/hysteria2/tuic/anytls/socks5），本账号降级直连")
        return False

    stop_singbox()
    if not os.path.exists(SETUP_SCRIPT):
        print(f"⚠️ 未找到 {SETUP_SCRIPT}，无法启动代理")
        return False
    env = os.environ.copy()
    env["NODE_LINK"] = node_link   # 只把本账号分到的那一行传给安装脚本
    print("🔗 启动 sing-box 代理...")

    # 🆕 修改：捕获脚本输出并检查返回码——失败时把脚本输出和 sing-box.log 打出来，排查不再抓瞎
    try:
        proc = subprocess.run(["bash", SETUP_SCRIPT], env=env, timeout=240,
                              capture_output=True, text=True)
        script_out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        print("❌ setup_proxy.sh 执行超时（240s）")
        return False
    except Exception as e:
        print(f"⚠️ setup_proxy.sh 执行异常: {e}")
        return False

    if proc.returncode != 0:
        print(f"❌ setup_proxy.sh 返回码 {proc.returncode}，脚本输出如下：")
        for line in script_out.strip().splitlines()[-25:]:
            print(f"    {line}")
        log_path = os.path.join(os.path.dirname(SETUP_SCRIPT), "sing-box.log")
        if os.path.exists(log_path):
            print("  ---- sing-box.log 末尾 ----")
            _print_file_tail(log_path)
        # 🆕 新增：UDP 节点解析到 IPv6 是 Actions 上最常见的死法，强制 IPv4 重试一次
        print("🔧 尝试强制 IPv4 重试（Actions 无 IPv6 出口，UDP 节点解析到 AAAA 会 network unreachable）...")
        if retry_singbox_ipv4():
            return True
        return False

    # 轮询等待本地端口就绪（脚本返回 0 时 sing-box 已在后台运行，正常应立即可通）
    for _ in range(10):
        if _port_open(PROXY_PORT):
            print(f"✅ sing-box 已就绪 (127.0.0.1:{PROXY_PORT})")
            # 🆕 新增：端口通不代表节点可用，再发真实代理请求实测
            if verify_proxy():
                return True
            print("❌ 代理端口已监听但实测不通（节点可能已失效）")
            return False
        time.sleep(1)
    print(f"⚠️ 等待 sing-box 端口 {PROXY_PORT} 超时")
    return False
# ============ 多节点代理管理 END ============

#  js注入脚本
_EXPAND_JS = """
(function() {
    var ts = document.querySelector('input[name="cf-turnstile-response"]');
    if (!ts) return 'no-turnstile';
    var el = ts;
    for (var i = 0; i < 20; i++) {
        el = el.parentElement;
        if (!el) break;
        var s = window.getComputedStyle(el);
        if (s.overflow === 'hidden' || s.overflowX === 'hidden' || s.overflowY === 'hidden')
            el.style.overflow = 'visible';
        el.style.minWidth = 'max-content';
    }
    document.querySelectorAll('iframe').forEach(function(f){
        if (f.src && f.src.includes('challenges.cloudflare.com')) {
            f.style.width = '300px'; f.style.height = '65px';
            f.style.minWidth = '300px';
            f.style.visibility = 'visible'; f.style.opacity = '1';
        }
    });
    return 'done';
})()
"""

_EXISTS_JS = """
(function(){
    return document.querySelector('input[name="cf-turnstile-response"]') !== null;
})()
"""

_SOLVED_JS = """
(function(){
    var i = document.querySelector('input[name="cf-turnstile-response"]');
    return !!(i && i.value && i.value.length > 20);
})()
"""

_COORDS_JS = """
(function(){
    var iframes = document.querySelectorAll('iframe');
    for (var i = 0; i < iframes.length; i++) {
        var src = iframes[i].src || '';
        if (src.includes('cloudflare') || src.includes('turnstile') || src.includes('challenges')) {
            var r = iframes[i].getBoundingClientRect();
            if (r.width > 0 && r.height > 0)
                return {cx: Math.round(r.x + 30), cy: Math.round(r.y + r.height / 2)};
        }
    }
    var inp = document.querySelector('input[name="cf-turnstile-response"]');
    if (inp) {
        var p = inp.parentElement;
        for (var j = 0; j < 5; j++) {
            if (!p) break;
            var r = p.getBoundingClientRect();
            if (r.width > 100 && r.height > 30)
                return {cx: Math.round(r.x + 30), cy: Math.round(r.y + r.height / 2)};
            p = p.parentElement;
        }
    }
    return null;
})()
"""

_WININFO_JS = """
(function(){
    return {
        sx: window.screenX || 0,
        sy: window.screenY || 0,
        oh: window.outerHeight,
        ih: window.innerHeight
    };
})()
"""

def js_fill_input(sb, selector: str, text: str):
    safe_text = text.replace('\\', '\\\\').replace('"', '\\"')
    sb.execute_script(f"""
    (function(){{
        var el = document.querySelector('{selector}');
        if (!el) return;
        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
        if (nativeInputValueSetter) {{
            nativeInputValueSetter.call(el, "{safe_text}");
        }} else {{
            el.value = "{safe_text}";
        }}
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }})()
    """)

def _activate_window():
    for cls in ["chrome", "chromium", "Chromium", "Chrome", "google-chrome"]:
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", cls], capture_output=True, text=True, timeout=3)
            wids = [w for w in r.stdout.strip().split("\n") if w.strip()]
            if wids:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]], timeout=3, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
                return
        except Exception:
            pass
    try:
        subprocess.run(["xdotool", "getactivewindow", "windowactivate"], timeout=3, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _xdotool_click(x: int, y: int):
    _activate_window()
    try:
        subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y)], timeout=3, stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
    except Exception:
        os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")

def _click_turnstile(sb):
    try:
        coords = sb.execute_script(_COORDS_JS)
    except Exception as e:
        print(f"⚠️ 获取 Turnstile 坐标失败: {e}")
        return
    if not coords:
        print("⚠️ 无法定位 Turnstile 坐标")
        return
    try:
        wi = sb.execute_script(_WININFO_JS)
    except Exception:
        wi = {"sx": 0, "sy": 0, "oh": 800, "ih": 768}

    bar = wi["oh"] - wi["ih"]
    ax  = coords["cx"] + wi["sx"]
    ay  = coords["cy"] + wi["sy"] + bar
    print(f"🖱️ 尝试点击 Turnstile ({ax}, {ay})")
    _xdotool_click(ax, ay)

def handle_turnstile(sb) -> bool:
    print("🔍 处理 Cloudflare Turnstile 验证...")
    time.sleep(2)

    if sb.execute_script(_SOLVED_JS):
        print("✅ 已静默通过")
        return True

    for _ in range(3):
        try: sb.execute_script(_EXPAND_JS)
        except Exception: pass
        time.sleep(0.5)

    for attempt in range(6):
        if sb.execute_script(_SOLVED_JS):
            print(f"✅ Turnstile 通过（第 {attempt + 1} 次尝试）")
            return True
        try: sb.execute_script(_EXPAND_JS)
        except Exception: pass
        time.sleep(0.3)

        _click_turnstile(sb)

        for _ in range(8):
            time.sleep(0.5)
            if sb.execute_script(_SOLVED_JS):
                print(f"✅ Turnstile 通过（第 {attempt + 1} 次尝试）")
                return True
        print(f"  ⚠️ 第 {attempt + 1} 次未通过，重试...")

    print("  ❌ Turnstile 6 次均失败")
    return False

def login(sb, email: str, password: str, shot: str) -> bool:
    print(f"🌐 打开登录页面: {LOGIN_URL}")
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=5)
    time.sleep(6)

    print("⏳ 等待 Cloudflare 验证通过...")
    cf_passed = False
    for i in range(30):
        page_src = sb.get_page_source() or ""
        if 'input[name="email"]' in page_src.lower() or 'name="email"' in page_src.lower():
            cf_passed = True
            print(f"✅ Cloudflare 验证已通过（{i+1}s）")
            break
        time.sleep(1)
    if not cf_passed:
        print("⚠️ Cloudflare 验证可能未通过，继续尝试...")

    try:
        sb.wait_for_element('input[name="email"]', timeout=15)
    except Exception:
        try:
            sb.wait_for_element('input[name="Email"]', timeout=5)
        except Exception:
            print("❌ 页面未加载出登录表单")
            cur_url = sb.get_current_url()
            page_title = sb.get_title() or ""
            print(f"  当前 URL: {cur_url}")
            print(f"  当前标题: {page_title}")
            sb.save_screenshot(f"login_load_fail_{shot}.png")
            return False

    print("🍪 关闭可能的 Cookie 弹窗...")
    try:
        for btn in sb.find_elements("button"):
            if "Accept" in (btn.text or ""):
                btn.click()
                time.sleep(0.5)
                break
    except Exception:
        pass

    print(f"📧 填写邮箱...")
    js_fill_input(sb, 'input[name="email"]', email)
    time.sleep(0.3)

    print("🔑 填写密码...")
    js_fill_input(sb, 'input[name="password"]', password)
    time.sleep(1)

    if sb.execute_script(_EXISTS_JS):
        if not handle_turnstile(sb):
            print("❌ 登录界面的 Turnstile 验证失败")
            sb.save_screenshot(f"login_turnstile_fail_{shot}.png")
            return False
    else:
        print("ℹ️ 未检测到 Turnstile")

    print("🖱️ 点击登录按钮提交登录...")
    sb.click('button[type="submit"]')

    print("⏳ 等待登录跳转...")
    for _ in range(12):
        time.sleep(1)
        cur_url = sb.get_current_url().split('?')[0].lower()
        page_title = sb.get_title() or ""
        if cur_url.startswith("https://betadash.lunes.host") or "Lunes host | Account page" in page_title.lower():
            break

    cur_url = sb.get_current_url().split('?')[0].lower()
    page_title = sb.get_title() or ""
    if "login" not in cur_url and "account" in page_title.lower():
        print(f"✅ 登录成功！(URL: {sb.get_current_url()}, Title: {page_title})")
        return True

    print(f"❌ 登录失败，页面未跳转到账户页。(URL: {sb.get_current_url()}, Title: {page_title})")
    sb.save_screenshot(f"login_failed_{shot}.png")
    return False

# 访问服务器页面
def visit_server(sb) -> (bool, dict):
    print("🔍 正在查找服务器卡片...")
    try:
        sb.wait_for_element('a.server-card', timeout=15)
    except Exception:
        print("❌ 未找到服务器卡片（可能没有服务器）")
        return False, {"error": "未找到服务器卡片，可能账户无服务器"}

    cards = sb.find_elements('a.server-card')
    if not cards:
        return False, {"error": "未找到服务器卡片"}

    card = cards[0]
    href = card.get_attribute('href')
    if not href:
        return False, {"error": "卡片缺少 href 属性"}

    match = re.search(r'/servers/(\d+)', href)
    if not match:
        # 🆕 修改：日志脱敏，href 细节只进 TG 推送
        return False, {"error": "无法从卡片 href 解析服务器 ID",
                       "error_tg": f"无法从 href 解析服务器 ID: {href}"}
    server_id = match.group(1)

    # 🆕 修改：日志脱敏——Actions 日志不打印服务器 ID/名称（防平台识别拉黑），TG 推送仍带完整信息
    print("🖱️ 点击服务器卡片")
    card.click()
    time.sleep(3)

    expected_url_prefix = f"https://betadash.lunes.host/servers/{server_id}"
    for _ in range(10):
        cur_url = sb.get_current_url().split('?')[0]
        if cur_url == expected_url_prefix:
            break
        time.sleep(1)
    else:
        # error 给日志用（脱敏）；error_tg 给推送用（含当前 URL，便于排查）
        return False, {"server_id": server_id,
                       "error": "跳转后 URL 不匹配",
                       "error_tg": f"跳转后 URL 不匹配，当前: {sb.get_current_url()}"}

    page_title = sb.get_title() or ""
    server_name = ""
    if "Server " in page_title:
        server_name = page_title.split("Server ", 1)[-1].strip()
    else:
        server_name = f"ID {server_id}"

    print("✅ 成功访问服务器")
    return True, {"server_id": server_id, "server_name": server_name}

# 🆕 修改：单账号流程新增 node_link / proxy_label 入参——
#           启动浏览器前先用本账号分到的节点重启 sing-box，结束后再杀掉
def run_account(base_kwargs, acc, node_link, proxy_label) -> bool:
    name = acc["name"]
    print("\n" + "#" * 25)
    print(f"   Lunes 自动登录续期 - {name}")
    print("#" * 25)

    sb_kwargs = dict(base_kwargs)   # 🆕 复制一份，避免代理配置串到其他账号

    # 🆕 新增：按节点启动本账号专属代理；启动失败则降级直连
    if node_link:
        print(f"🌐 {name} 分配出口: {proxy_label}")
        if start_singbox(node_link):
            sb_kwargs["proxy"] = PROXY_LOCAL
        else:
            proxy_label = f"{proxy_label}(启动失败,降级直连)"
            print(f"⚠️ {name} 代理启动失败，本账号走直连")
    else:
        print(f"🌐 {name} 未分配节点，直连访问")

    try:
        with SB(**sb_kwargs) as sb:
            print("✅ 浏览器已启动")
            try:
                sb.open("https://api.ip.sb/ip")
                print(f"🌐 当前出口真实 IP: {sb.get_text('body')}")
            except Exception:
                pass

            if login(sb, acc["email"], acc["password"], acc["shot"]):
                success, info = visit_server(sb)
                if success:
                    extra = (f"服务器: {info['server_name']}\n"
                             f"ID: {info['server_id']}\n"
                             f"🌐 出口: {proxy_label}")
                    send_tg_message("✅", "续期成功", extra, email=acc["email"], name=name, flag=acc["flag"])
                    return True
                else:
                    error_msg = info.get('error', '未知错误')
                    print(f"❌ 访问服务器失败: {error_msg}")   # 日志只打脱敏版
                    extra = f"错误: {info.get('error_tg', error_msg)}"   # 🆕 推送用详细版
                    if 'server_id' in info:
                        extra += f"\n服务器ID: {info['server_id']}"
                    extra += f"\n🌐 出口: {proxy_label}"
                    send_tg_message("❌", "续期失败", extra, email=acc["email"], name=name, flag=acc["flag"])
                    return False
            else:
                print(f"\n❌ {name} 登录失败，终止该账号后续续期操作。")
                send_tg_message("❌", "登录失败", f"🌐 出口: {proxy_label}",
                                email=acc["email"], name=name, flag=acc["flag"])
                return False
    except Exception as e:
        print(f"❌ {name} 执行出现异常: {e}")
        send_tg_message("❌", "执行异常", f"错误: {e}\n🌐 出口: {proxy_label}",
                        email=acc["email"], name=name, flag=acc["flag"])
        return False
    finally:
        # 🆕 新增：无论成败，杀掉本账号的 sing-box，避免节点串号
        if node_link:
            stop_singbox()

def main():
    sb_kwargs = {"uc": True, "headless": False}

    # 🆕 修改：节点按槽位从 NODE_LINK1/NODE_LINK2 领取（双工作流约定）；
    #          两者都未配置时回退旧多行 NODE_LINK 位置分配
    nodes = parse_nodes()
    if nodes:
        print(f"🔗 检测到 {len(nodes)} 个有效节点（槽位: {sorted(nodes)}）。")
    else:
        # 兼容旧逻辑：外部已自行在 1081 起好代理时，可用 IS_PROXY=true 挂载
        is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
        if is_proxy:
            print(f"🔗 未配置 NODE_LINK，沿用 IS_PROXY 旧逻辑: {PROXY_LOCAL}")
            sb_kwargs["proxy"] = PROXY_LOCAL
        else:
            print("🌐 未配置 NODE_LINK 代理，直连访问")

    # 🆕 修改：配置不齐/参数非法由静默 return 改为 exit(1)——workflow 显式红叉，
    #          GitHub 会发失败邮件，避免「绿色成功但实际空跑」被漏察觉
    if not ACCOUNTS:
        print("❌ 未配置任何账号（账号1/账号2 的邮箱密码均未配置齐全），退出。")
        sys.exit(1)

    # 🆕 新增：ACCOUNT_INDEX 按槽位选账号（双工作流模式）。
    #          "1" 只跑槽位1账号，"2" 只跑槽位2账号；未设置则跑全部（旧行为）。
    #          账号槽位、凭据、节点编号一一对应（LUNES_EMAIL{n}/NODE_LINK{n}）
    sel = (os.environ.get("ACCOUNT_INDEX") or "").strip()
    if sel and sel not in ("1", "2"):
        print(f"❌ ACCOUNT_INDEX={sel} 无效（只接受 1 或 2），退出。")
        sys.exit(1)
    selected = [acc for acc in ACCOUNTS if not sel or str(acc["slot"]) == sel]
    if sel and not selected:
        print(f"❌ ACCOUNT_INDEX={sel} 对应的账号未配置（邮箱/密码不齐全），退出。")
        sys.exit(1)

    if sel:
        print(f"📋 双工作流模式：本次只执行 slot={sel} 账号（仓库共配置 {len(ACCOUNTS)} 个）。")
    else:
        print(f"📋 共配置 {len(ACCOUNTS)} 个账号，依次执行。")

    results = []
    for pos, acc in enumerate(selected):
        node  = nodes.get(acc["slot"])          # 🆕 每个账号按槽位领自己的 NODE_LINK{n}
        label = f"节点{acc['slot']}" if node else "直连"
        ok = run_account(sb_kwargs, acc, node, label)
        results.append((acc["name"], ok))
        if pos < len(selected) - 1:
            wait_seconds = random.randint(60, 180)
            print(f"\n⏳ 随机等待 {wait_seconds} 秒（约 {wait_seconds / 60:.1f} 分钟）后执行下一账号，避免多账号行为过于规律...")
            time.sleep(wait_seconds)

    print("\n" + "=" * 40)
    print("📊 全部账号执行完毕：")
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print("=" * 40)

if __name__ == "__main__":
    main()
