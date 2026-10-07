## 🚀 Lunes Host 多账号自动登录续期（GitHub Actions）

这是一个基于 GitHub Actions 的多账号自动化脚本，用于定时登录自动续期[Lunes Host](https://betadash.lunes.host/) 应用。
优化列表：

1.模拟人工登录模式，在原脚本基础上增加和优化了第2账号登录方式，防止黑号。

2.**双工作流模式**：账号1、账号2 各自独立 workflow（`login-acc1.yml` / `login-acc2.yml`），cron 日期错开 5 天、时分错开，从时间维度消除两账号登录行为的相关性；app.py 通过 `ACCOUNT_INDEX` 按槽位选账号，不设置则保持单进程跑全部账号的旧行为。

3.**环境变量编号统一**：账号编号全线对齐——账号1 用 `LUNES_EMAIL1`/`LUNES_PASSWORD1`/`NODE_LINK1`，账号2 用 `LUNES_EMAIL2`/`LUNES_PASSWORD2`/`NODE_LINK2`，与 `ACCOUNT_INDEX` 的 1/2 一一对应，不再混用旧命名。

3.支持代理协议 vless:// vmess:// tuic:// hysteria2:// anttls:// socks5://， github不支持纯ipv6，增加签到前的代理检查并输出状态日志。

⚠️ 有Cloudflare盾,太垃圾的机房节点可能过不了，建议用稍微干净点的节点

━━━━━━━━━━━━━━━━━━━━━━

### 🗓️ 双工作流时间表（cron 为 UTC，北京时间 = UTC + 8）

| 工作流 | 账号 | cron（UTC） | 北京时间 | 每月日期 |
|----------------------|------|-------------|----------|----------|
| Auto-login-Lunes-Acc1 | 账号1🇺🇸 | `4 6 2,12,22 * *` | 14:04 | 2 / 12 / 22 日 |
| Auto-login-Lunes-Acc2 | 账号2🇩🇪 | `17 3 7,17,27 * *` | 11:17 | 7 / 17 / 27 日 |

两账号各自保持约 10 天的续期间隔；日期、小时、分钟三重错开，同时避免 time.txt 推送竞争。

━━━━━━━━━━━━━━━━━━━━━━

🔐 Secrets 配置说明(在原作基础上增加第2账号的保活支持，优化了签到方式防黑号)

| Secret 名称         | 是否必填 | 说明                                              |
|---------------------|----------|---------------------------------------------------|
| LUNES_EMAIL1    | ✅ 必填  | lunes 登录邮箱1🇺🇸（Acc1 工作流使用）                 |
| LUNES_PASSWORD1 | ✅ 必填  | lunes 登录密码1                                   | 
| LUNES_EMAIL2    | ❌ 可选  | lunes 登录邮箱2🇩🇪（Acc2 工作流使用）                 |
| LUNES_PASSWORD2 | ❌ 可选   | lunes 登录密码2                                  | 
| NODE_LINK1      | ❌ 可选  | 账号1 专属代理链接（Acc1 工作流；不配则账号1直连）          |
| NODE_LINK2      | ❌ 可选  | 账号2 专属代理链接（Acc2 工作流；建议与 1 出口不同，防 IP 关联）|
| TG_BOT_TOKEN    | ❌ 可选  | Telegram Bot Token（用于发送通知）                     |
| TG_CHAT_ID      | ❌ 可选  | Telegram Chat ID（接收通知的用户或群组 ID）              |

> 环境变量编号与账号槽位一一对应：`ACCOUNT_INDEX=1` ↔ `LUNES_EMAIL1`/`NODE_LINK1`，`ACCOUNT_INDEX=2` ↔ `LUNES_EMAIL2`/`NODE_LINK2`。
> 旧的 `LUNES_EMAIL`/`LUNES_PASSWORD`（无编号）及 `NODE_LINK_A`/`NODE_LINK_B` 命名已弃用；
> 旧多行 `NODE_LINK`（第1行账号1、第2行账号2）仅作手动运行时的兼容回退，工作流不再引用。

━━━━━━━━━━━━━━━━━━━━━━
### 代理格式（确认在v2rayN里使用正常的节点）

`NODE_LINK1` / `NODE_LINK2` 支持以下任意一种代理协议的完整分享链接（不配置则对应账号直连）：

- **VLESS**：`vless://uuid@server:port?security=reality&sni=...&type=ws&...`
- **VMess**：`vmess://base64encoded...`
- **Trojan**：`trojan://password@server:port?sni=...&type=ws&...`
- **tuic**：`tuic://uuid:password@server:port...`
- **anytls**：`anytls://uuid@server:port...`
- **hysteria2**：`hysteria2://base64@server:port...`
- **SOCKS5**：`socks5://user:pass@server:port` 或 `socks://user:pass@server:port`

### 🧪 节点连通性测试工作流（test-node.yml）

手动触发（`workflow_dispatch`），用于在配置/更换 `NODE_LINK1`/`NODE_LINK2` 前验证节点质量，不登录、不开浏览器：

- **输入**：`node_link` 填一条待测节点链接则只测它；留空则依次测试已配置的 `NODE_LINK1`/`NODE_LINK2`；
- **测试项**：① 出口 IP → ② Cloudflare 边缘可达性（cdn-cgi/trace）→ ③ betadash.lunes.host 登录页（HTTP 状态 + 是否返回真实登录表单）；
- **判定**：✅ 返回登录表单 = 可用；⚠️ 遇 CF 挑战页（Just a moment）= IP 信誉一般，真实浏览器可能仍能过；❌ 被拦截/超时 = 换节点；
- 注意 curl 无法执行 JS，Turnstile 交互挑战能否通过仍以真实续期流程为准；
- 日志中的出口 IP 已掩码为 `1*.2*.3*.4*` 形式（每段只留首位数字，`*` 不代表位数）；
- 测试用的节点链接会留在 run 事件记录里，工作流末尾自动清理历史记录只留最近 1 条，仓库请保持私有。

### 注意事项
- 尽量添加一个干净的节点，以免过不了Cloudflare盾
