# 安全说明

DSH Remote PC 把一台真实 Windows 电脑的用户级控制权暴露给 Tailscale 网络上的 DSH。部署前请明确接受
以下风险，并按本文件收紧配置。

## 威胁模型

持有 `Bearer token` 并能访问被控端端口的任何设备，都能：

- 以被控端登录用户的权限执行任意命令（安装软件、读取文件、访问浏览器会话等）；
- 截取屏幕（可能包含聊天记录、密码提示、邮件等）；
- 注入键鼠操作；
- 阻止系统空闲睡眠；
- 关闭/开启功能开关（除非被控端锁定了远程管理）。

因此 token 等同于该用户在本机的交互式控制权（但**不**等同于管理员权限）。被控端默认以普通用户权限运行；
不要为了“方便”把它改成管理员/系统服务，除非你明确接受风险。

## 已实现的防护

1. **网络范围**：默认只绑定 `127.0.0.1` 与 Tailscale IPv4 地址，不监听 Wi-Fi/LAN 地址。
2. **Bearer token**：所有 `/mcp` 请求必须携带 `Authorization: Bearer <token>`；token 为
   `secrets.token_urlsafe(32)`（约 256 位熵），比较使用常量时间。
3. **防火墙**：管理员安装时，入站规则只允许 `100.64.0.0/10`（Tailscale CGNAT 段）访问该端口。
4. **不暴露文件**：只有 `/healthz`（无凭据、仅服务名/版本/主机名）和随机文件名截图（128 位随机 ID，
   24 小时后清理）不需要 token；不存在目录浏览或任意文件读取接口。
5. **功能开关**：任何功能都可被本机立即关闭，关闭后远程调用只返回 `FEATURE_DISABLED`，不会执行。
6. **首次调用门禁**：防止控制端跳过环境确认直接操作。
7. **本机远程管理锁**：`dshpc remote-control off` 后，远程只能读取状态，不能改开关；只有本机 CLI 能解锁。
8. **进程与事务**：命令执行有超时和进程树终止；状态文件原子写入并跨进程加锁；单实例锁防止端口被第二个
   实例接管；HTTP 监听禁用 `SO_REUSEADDR`，降低本机端口劫持风险。
9. **最小权限**：服务以登录用户身份运行，不请求管理员权限；UAC 安全桌面与提权窗口不可操作。
10. **审计日志**：记录工具调用、命令（截断 500 字符）、截图尺寸与锁屏状态；不记录截图内容。
    可用 `config.json` 的 `log_commands: false` 关闭命令记录。

## 建议的 Tailscale 配置

使用 ACL/tag 把被控端和允许控制它的设备分开，例如（示意，按你的 tailnet 调整）：

```jsonc
{
  "tagOwners": {
    "tag:dsh-controlled": ["autogroup:admin"],
    "tag:dsh-controller": ["autogroup:admin"]
  },
  "grants": [
    {
      "src": ["tag:dsh-controller"],
      "dst": ["tag:dsh-controlled"],
      "ip": ["tcp:8765"]
    }
  ]
}
```

- 只给控制端设备访问被控端 **该 TCP 端口** 的权限，不要开放整个 tailnet；
- 被控端不要开启 Tailscale Funnel / subnet router 把端口暴露到公网；
- 如使用 MagicDNS，可把 `-PublicHost` 设为 `host.tailnet.ts.net`，避免 IP 变化后重新配对。

## 控制端网关的额外风险

控制端只安装一个 `dsh-pc-gateway` 插件，它在 `gateway/pcs.json` 中汇总所有被控端的地址和 token：

- 控制端被攻破或 `pcs.json` 泄露，等同于同时泄露所有已配置被控端的控制权；请按凭据目录保护它；
- `gateway/pcs.json` 应设为 `0600`（Windows 上用 ACL 收紧），且已被本项目的 `.gitignore` 排除；
- `target` 只是选择机器的参数，**不是权限边界**：能调用工具的人可以在任意已配置机器之间切换。
  重要操作前先用 `pc_sysinfo` 核对主机名，不要把“模型选错 target”当成安全隔离；
- 不再需要某台机器时，从 `pcs.json` 删除条目并重载插件，同时在那个被控端轮换 token。

## token 管理

- token 保存在被控端 `<数据目录>\config.json`，以及控制端的 `gateway/pcs.json`；
  被控端生成的 `gateway-entry.json` 和 `PAIRING.txt` 也含 token，合并后应删除或按凭据保管。
- 不要提交到 Git、不要发到聊天/截图/工单；控制端的 `gateway/` 目录应视为凭据目录。
- 怀疑泄露时：

```powershell
# 被控端：重新生成 token 并重启
.\deploy\install.ps1 -RotateToken

# 控制端：把新生成的 gateway-entry.json 合并/覆盖到 pcs.json 对应条目，重载“远程电脑”插件
```

- 建议定期轮换；删除机器时同时删除其 `pcs.json` 条目，并清理旧的 `gateway-entry.json` / `PAIRING.txt`。

## 数据与隐私

| 数据 | 位置 | 保留 |
|---|---|---|
| 配置与 token | `<数据目录>\config.json` | 直到卸载（`-Purge` 删除） |
| 功能开关 | `<数据目录>\state.json` | 同上 |
| 日志（含命令） | `<数据目录>\logs\server.log` | 按大小轮转（默认 2 MB × 3） |
| 截图 | `<数据目录>\screenshots\*.png` | 24 小时 / 最多 200 张 |

数据目录默认在 `%LOCALAPPDATA%\DSHRemotePC\data`，受用户 profile ACL 保护。若改到 `C:\ProgramData`
等共享位置，请自行收紧 ACL。

## 运行权限与 UAC

- 计划任务使用 `RunLevel=Limited`，普通管理员账户会得到 UAC 过滤后的普通令牌：能控制同一用户会话的普通窗口，
  但**不能**操作管理员窗口（UIPI）、不能点击 UAC 提示、不能读取管理员进程的内存。
- **内置 Administrator（SID -500）例外**：Windows 默认不为该账户拆分 UAC 令牌，所以即使用 Limited 计划任务，
  服务仍以提升权限运行。`get_system_info` 会在 `user.elevated=true` 时明确提示。若在意最小权限，请改用
  普通标准用户或受 UAC 过滤的管理员账户登录被控端。
- 以管理员身份运行会扩大能力，也会扩大 token 泄露后的影响；不建议手动提升。
- 锁屏/UAC 安全桌面下，Windows 会阻止截图与输入注入；这不是本工具的缺陷，也不应尝试绕过。

## 事件响应

如果怀疑被未授权控制：

1. 被控端立即执行 `dshpc all-off`（或 `dshpc stop`，再 `dshpc remote-control off`）；
2. 断开/限制该设备在 Tailscale ACL 中的访问；
3. 检查 `<数据目录>\logs\server.log` 中的工具调用与命令；
4. 轮换 token，更新或删除控制端 `gateway/pcs.json` 中的对应条目，并移除不认识的插件；
5. 检查系统是否有异常进程、计划任务、启动项与新增账户；
6. 如涉及敏感数据，按组织安全流程处理。

## 非目标

- 这不是隐蔽的远程管理工具（RAT）：没有持久化隐藏、没有反检测、不会绕过安全软件或 Windows 安全边界。
- MCP `instructions` 和工具描述只是给控制端模型的策略提示，**不是安全边界**；真正的边界是被控端的
  token、Tailscale ACL、防火墙、功能开关和本机锁。
