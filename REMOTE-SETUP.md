# 控制端（DSH）对接指南

本文件面向**控制方**：你要在自己的电脑上使用 DSH，通过 Tailscale 控制被控的那台 Windows 电脑。

被控端安装脚本会生成：

```text
<被控端数据目录>\controller-bundle\        # 可直接安装的插件目录
<被控端数据目录>\dsh-pc-remote-<host>.zip  # 同一内容的压缩包
<被控端数据目录>\controller-bundle\PAIRING.txt
```

`PAIRING.txt` 里有这台机器的实际 `MCP URL`、`Bearer token`、`serverName` 和工具前缀。本文件中的
`<...>` 都是占位符，以 `PAIRING.txt` 为准。

## 1. 准备

- 控制端已经安装 DeepSeek Harness，并能正常使用插件；
- 控制端与被控端在**同一个 Tailscale tailnet**，且控制端能访问被控端的 100.x 地址；
- 被控端已登录 Windows、屏幕唤醒、服务正在运行（被控端执行 `dshpc status` 应显示 running）。

快速连通性检查（控制端）：

```bash
tailscale ping <被控端主机名>
curl http://100.x.y.z:8765/healthz
```

`/healthz` 不需要 token，只返回服务名、版本、主机名和时间；若不通，先解决 Tailscale/防火墙/服务问题。

## 2. 安装插件（推荐）

1. 把被控端生成的 `dsh-pc-remote-<host>.zip` 复制到控制端并解压，得到 `controller-bundle/` 目录
   （其中包含 `package.json`、`cordis.patch.yml`、`locale/`、`icon.svg`、`README.md`、`PAIRING.txt`）。
2. 打开 DeepSeek Harness → **插件（Plugins）** → **添加插件（Add plugin）**。
3. 选择刚解压的目录（不是 zip 文件）。
4. 安装完成后**新建一个会话**（或重载配置/重启 DSH），让 MCP 客户端连接并注册工具。
5. 新会话中应出现 6 个工具，名称形如：

```text
mcp__dshpc_ms_ftvnukxfwqav__get_system_info
mcp__dshpc_ms_ftvnukxfwqav__run_command
mcp__dshpc_ms_ftvnukxfwqav__capture_screen
mcp__dshpc_ms_ftvnukxfwqav__input_control
mcp__dshpc_ms_ftvnukxfwqav__set_sleep_block
mcp__dshpc_ms_ftvnukxfwqav__control_features
```

`serverName` 每台被控机不同，以 `PAIRING.txt` 为准。

### 备选：让 DSH 自己安装

可以在控制端会话中把解压目录的绝对路径告诉助手，让它调用 `plugin_manager` 的 `install_bundle`
（target 为该目录）。安装后同样需要新建会话验证。

### 备选：手动 MCP 配置（不用插件）

如果不想装 bundle，可在控制端 profile 的 `cordis.patch.yml` 中加入一行（token 自行替换，**不要提交**）：

```yaml
- insert:
    - id: mcp-dshpc-<host>
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: dshpc_<host>
        transport: streamable-http
        url: 'http://100.x.y.z:8765/mcp'
        headers:
          Authorization: 'Bearer <token>'
        startupTimeoutMs: 15000
        toolCallTimeoutMs: 660000
```

修改 profile 属于 DSH 配置操作，建议优先用插件方式。

## 3. 验证

1. 让助手调用 `get_system_info`（或直接说“先获取那台远程电脑的系统信息”）。
2. 确认返回的 `hostname` 是被控端主机名，`session.locked` 为 `false`，`features` 中相应功能为 `true`。
3. 再让助手截图（`capture_screen`）或执行一条无害命令（`run_command`，例如 `hostname`）。
4. 若返回 `INFO_REQUIRED`，说明还没调用 `get_system_info`；若返回 `FEATURE_DISABLED`，说明被控端关闭了该功能。

## 4. 使用方式与礼貌边界

服务器会在 MCP `instructions` 中要求控制端模型：

- **只有用户明确要求操作被控端电脑时才调用工具**；不要因为“顺便”或猜测而操作；
- 每次控制任务先调用 `get_system_info`；
- 破坏性/不可逆操作先征得用户同意；
- 不绕过 `FEATURE_DISABLED` / `REMOTE_CONTROL_LOCKED` / `SESSION_LOCKED`；
- 截图和命令输出按隐私信息处理；任务结束释放不必要的睡眠阻止。

这些是模型策略约束，不是技术强制；被控端的功能开关、门禁和本机锁才是技术边界。

示例说法：

- “控制 MS-FTVNUKXFWQAV 这台电脑，先获取系统信息。”
- “在这台远程电脑上运行 `Get-Process | Sort-Object CPU -Descending | Select-Object -First 10`。”
- “截一张远程电脑的屏幕给我看。”
- “在远程电脑上打开记事本，输入 hello，然后保存到桌面。” （需要键鼠/命令配合）
- “让这台远程电脑 30 分钟内不要睡眠，结束后释放。”
- “把远程电脑的 shell 和截图功能关掉。” （会走 `control_features`，被控端可拒绝）

## 5. 更新 / token 轮换后

被控端以下变化会使旧插件失效：

- 被控端重新运行 `deploy\install.ps1 -RotateToken` 轮换了 token；
- 端口变化；
- Tailscale IP 变化且没有用 MagicDNS 名称（`-PublicHost`）；
- 主机名变化导致 `serverName` 变化。

处理：在被控端重新运行

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\make-bundle.ps1
```

然后用新的 `controller-bundle/` 重新安装/替换控制端插件（先移除旧插件，避免同名冲突），新建会话。

## 6. 移除

- 控制端：DSH → 插件 → 找到 `Remote PC (Tailscale)`（或 `dsh-remote-pc-<host>`）→ 移除/禁用；
- 被控端如需彻底停止接受控制：运行 `deploy\uninstall.ps1`（或 `dshpc all-off` 先临时收紧）。

## 7. 故障排查

| 现象 | 原因/处理 |
|---|---|
| 插件装上了但工具不出现 | 被控端离线、端口被防火墙拦截、token 不匹配；看控制端 DSH 日志中的 MCP 连接错误；被控端 `dshpc status` |
| `unauthorized` / 401 | token 不一致：重新生成 bundle 并重装 |
| `INFO_REQUIRED` | 先 `get_system_info` |
| `FEATURE_DISABLED` | 被控端 `dshpc enable <功能>` |
| `REMOTE_CONTROL_LOCKED` | 被控端 `dshpc remote-control on` |
| `SESSION_LOCKED` | 被控端锁屏/UAC 安全桌面，需本机解锁 |
| 连接时不时断开 | 被控端睡眠/网络切换/Tailscale 退出；检查电源设置和 Tailscale |
| 截图全黑 | 锁屏、DRM、显示器休眠；让被控端解锁并唤醒屏幕 |

关于首次连接失败：插件配置了 15 秒启动超时和默认重连策略。若 DSH 启动时被控端正好离线，工具可能不会
出现；被控端上线后重载插件或重启 DSH 即可。
