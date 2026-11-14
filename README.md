# DSH Remote PC（Tailscale 远程控制）

把一台 Windows 电脑变成 DeepSeek Harness（DSH）可远程控制的“被控端”。控制端只安装**一个网关插件**
`dsh-pc-gateway`，就能通过 Tailscale 管理多台被控端：执行命令、获取系统信息、截屏、操作键鼠、
阻止/释放睡眠，并在本机或远程随时关闭/开启各项功能。

- 被控端：Windows 10/11，Python 3.10+（仅用标准库；Pillow 可选，用于更好的截图）
- 控制端：任意平台的 DSH；一个 `dsh-pc-gateway` 插件、7 个 `pc_*` 工具
- 传输：Tailscale（WireGuard 加密）+ 每台被控端独立的 Bearer token
- 部署：被控端一条 PowerShell 安装命令；控制端装一次网关，之后加机器只改 `gateway/pcs.json`

> 这是一个**真实远程控制工具**，不是演示。请只在你拥有或获授权管理的电脑上部署，并妥善保管
> 被控端的 `config.json` 和控制端的 `gateway/pcs.json`（见 [SECURITY.md](SECURITY.md)）。

## 架构

```text
                         Tailscale (WireGuard)
┌──────────────────────────────┐       http://100.x.y.z:8765/mcp       ┌─────────────────────────────┐
│ 控制端（任意系统）            │ ────────────────────────────────────> │ 被控端 A（Windows）          │
│                              │       Authorization: Bearer <token>   │ dshpc_server.py              │
│ DSH + dsh-pc-gateway         │                                       │  ├ MCP Streamable HTTP       │
│  ├ pc_list / pc_sysinfo      │ <──────────────────────────────────── │  ├ 功能开关 state.json       │
│  ├ pc_run / pc_screenshot    │        JSON-RPC / 截图 / 结果          │  ├ Win32: 截图/键鼠/睡眠      │
│  ├ pc_input / pc_sleep       │                                       │  └ 本机 CLI + 计划任务        │
│  ├ pc_features               │                                       └─────────────────────────────┘
│  └ gateway/pcs.json          │                     ... 更多被控端（B、C ...）
└──────────────────────────────┘
```

网关是一个纯 Host 插件：它读取 `gateway/pcs.json`，把每台机器的 6 个 MCP 工具合并成一组
`pc_*` 工具，用 `target` 参数选择机器；它直接向被控端现有的 `/mcp` 发 JSON-RPC，不额外启动代理进程。

被控端以**当前登录用户的普通权限**运行（登录时自启），因此截图和键鼠注入发生在真实的交互桌面会话中；
这也是不使用 Windows 服务（session 0 看不到桌面）的原因。

## 控制端工具

| 工具 | 功能 |
|---|---|
| `pc_list` | 列出 `pcs.json` 中的机器；`probe: true` 时并行探测在线状态和工具数 |
| `pc_sysinfo` | 主机名/OS/CPU/内存/磁盘/GPU、显示器与 DPI、会话与锁屏、电源、网络与 Tailscale、功能开关、服务状态。**每个控制任务必须首先调用** |
| `pc_run` | PowerShell（优先 pwsh，缺失时 Windows PowerShell）、cmd、Python；退出码、stdout/stderr、超时与进程树终止 |
| `pc_screenshot` | 全屏（所有显示器）或指定显示器，PNG/JPEG，返回图像内容 + 可访问 URL + 本机路径 |
| `pc_input` | 批量鼠标移动/点击/滚轮、输入文本（含中文）、组合键（ctrl+c 等）、等待 |
| `pc_sleep` | 阻止/释放空闲系统睡眠（可选保持显示器常亮） |
| `pc_features` | 查看状态、单项/批量开关、一键全开/全关、重置首次调用门禁 |

`pc_*` 工具都接受 `target`（`pcs.json` 里的 `alias`）。除 `pc_list` 外，参数与被控端原有的
`get_system_info`、`run_command`、`capture_screen`、`input_control`、`set_sleep_block`、
`control_features` 一致，只是多了 `target`。

## 控制端：一个网关，多台机器

```bash
cp gateway/pcs.example.json gateway/pcs.json
chmod 600 gateway/pcs.json       # 填入每台机器的 alias / url / token
```

```json
{
  "pcs": [
    {
      "alias": "office",
      "displayName": "办公室 PC",
      "url": "http://100.70.184.108:8765/mcp",
      "token": "REPLACE_WITH_THE_BEARER_TOKEN",
      "requestTimeoutMs": 120000
    },
    {
      "alias": "parallels",
      "displayName": "Parallels 虚拟机",
      "url": "http://100.116.55.117:8765/mcp",
      "token": "REPLACE_WITH_THE_BEARER_TOKEN"
    }
  ]
}
```

- `alias` 是模型在 `target` 里使用的名字，规则 `[A-Za-z0-9_-]{1,16}`，必须唯一；
- `displayName` 是给人看的名字，会出现在 `pc_list` 里；
- `url` 指向被控端的 `/mcp`，推荐用 MagicDNS 名称而不是易变的 IP；
- `requestTimeoutMs` 可省略，默认 120000。

然后在 DSH 中安装网关插件：

1. 打开 DSH → **插件（Plugins）** → **添加插件（Add plugin）**，选择本项目的 `gateway/` 目录；
   或让助手调用 `plugin_manager` 的 `install_bundle`（target 为 `gateway/` 的绝对路径）。
2. 在插件详情里关闭再开启“远程电脑”，或重启 DSH，让网关读取 `pcs.json`。
3. 新建会话，让助手先调用 `pc_list`，再对某台机器调用 `pc_sysinfo`。

之后增加机器时，**不需要重新安装控制端插件**：把被控端生成的 `gateway-entry.json` 合并进
`gateway/pcs.json` 的 `pcs` 数组，重载插件即可。网关的完整说明见 [gateway/README.md](gateway/README.md)。

### 强制策略（服务器会执行）

1. **首次调用门禁**：每个被控端服务进程启动后，除 `get_system_info` 和 `control_features` 外的工具都会返回
   `INFO_REQUIRED`，直到成功调用 `get_system_info`。本机可执行 `dshpc reset-info-gate` 重新上锁。
2. **功能开关**：被关闭的功能不会执行任何操作，返回 `FEATURE_DISABLED`；`control_features` 仍可重新开启
   （除非本机锁定了远程管理，见下）。
3. **只在明确要求时控制**：通过 MCP `instructions` 和每个工具的说明，要求控制端模型只在用户明确要求操作
   该电脑时调用工具；这是策略层约束，最终仍由控制端模型/用户遵守（服务器无法技术性判断“用户意图”）。
4. **本机优先**：`dshpc remote-control off` 后，远程只能读取 `control_features` 的 status，不能修改开关；
   只有本机 CLI 能恢复。`dshpc all-off` 可一键关闭全部功能。

## 安装（被控端，本机）

前置：Windows 10/11、Python 3.10+（建议 64 位）、Tailscale 已登录并在线。
安装脚本会检测 Python；不会自动安装 Python。

```powershell
# 在项目根目录，普通 PowerShell 即可（防火墙规则/计划任务需要管理员时请用管理员 PowerShell）
powershell -ExecutionPolicy Bypass -File .\deploy\install.ps1
```

常用参数：

```powershell
.\deploy\install.ps1 -Alias office
.\deploy\install.ps1 -Port 8765 -PublicHost 100.x.y.z
.\deploy\install.ps1 -RotateToken          # 重新生成 token（控制端需更新对应条目）
.\deploy\install.ps1 -NoFirewall           # 不添加防火墙规则（需自行放行 TCP 端口）
.\deploy\install.ps1 -NoAutostart          # 不注册登录自启
.\deploy\install.ps1 -InstallDir D:\DSHRemotePC\app -DataDir D:\DSHRemotePC\data
```

安装脚本会：

1. 检查 Python 版本并复制 `server/`、`control/`、`deploy/` 到安装目录；
2. 在数据目录生成 `config.json`（含 256 位随机 token）和 `state.json`（全部功能默认开启）；
3. 注册登录自启：优先计划任务 `DSHRemotePC`（失败时回退 HKCU Run）；
4. 管理员运行时添加**只允许 `100.64.0.0/10`（Tailscale CGNAT 段）**访问该 TCP 端口的入站规则；
5. 启动服务并等待 `/healthz` 通过；
6. 生成 `gateway-entry.json`（控制端可直接合并的条目）和 `PAIRING.txt`（人读的配对信息）。

安装完成后会打印：

- MCP 端点（`http://100.x.y.z:8765/mcp`）
- 机器别名（`alias`）与 Bearer token
- `gateway-entry.json` 与 `PAIRING.txt` 的路径
- 本机控制命令

`gateway-entry.json` 形如：

```json
{
  "alias": "office",
  "displayName": "OFFICE-PC",
  "url": "http://100.x.y.z:8765/mcp",
  "token": "...",
  "requestTimeoutMs": 120000
}
```

`alias` 默认由主机名生成；建议用 `-Alias` 指定一个短而清晰的名字。控制端合并后请确认 `alias` 唯一。

### 本机控制 CLI

安装后使用 `control\dshpc.cmd`（或 `control\dshpc.ps1`）。为简洁，下面的示例把完整路径写成 `dshpc`；
实际路径是 `<安装目录>\control\dshpc.cmd`（默认
`%LOCALAPPDATA%\DSHRemotePC\app\control\dshpc.cmd`）。

```powershell
dshpc status                    # 服务状态 + 功能开关 + 端点
dshpc disable shell,screenshot  # 关闭指定功能（立即生效）
dshpc enable input              # 开启指定功能
dshpc all-off                   # 一键全关（并释放睡眠阻止）
dshpc all-on                    # 一键全开
dshpc sleep-hold --display      # 阻止空闲睡眠并保持显示器
dshpc sleep-release
dshpc reset-info-gate           # 要求下次远程控制先调用 get_system_info
dshpc remote-control off        # 锁定远程功能管理（只允许本机恢复）
dshpc remote-control on
dshpc start | stop | restart
dshpc url | token | logs --tail 50 | check
```

命令是直接修改共享的 `state.json`，服务在约 1 秒内应用；即使服务未运行也能改，下次启动生效。

## 控制端对接

详细步骤、多机配置、验证方法和故障排查见 [REMOTE-SETUP.md](REMOTE-SETUP.md)。

最短路径：

1. 安装一次 `gateway/`；
2. 把每台被控端的 `gateway-entry.json` 合并进 `gateway/pcs.json`；
3. 重载“远程电脑”插件；
4. 调用 `pc_list`，再对目标机器调用 `pc_sysinfo`，确认返回的主机名正确。

## 硬性限制（不能完全实现的部分）

这些是 Windows/协议层面的真实限制，已在工具返回和文档中如实说明：

1. **锁屏 / UAC 安全桌面**：Windows 禁止普通进程截取 Winlogon/UAC 桌面或向其注入输入。锁屏时截图可能
   全黑，`pc_input` 会返回 `SESSION_LOCKED`。本工具假设“已登录且屏幕唤醒”，无法绕过安全桌面。
2. **权限边界（UIPI）**：被控端默认以登录用户权限运行；普通管理员账户的计划任务使用 Limited 令牌，因此不能操作
   “以管理员身份运行”的窗口，也不能点击 UAC 提示。**例外**：Windows 内置的 `Administrator`（SID 结尾 -500）
   账户默认不做 UAC 过滤，用它登录时服务仍会以提升权限运行（`pc_sysinfo` 的 `user.elevated` 会显示
   `true` 并给出提示）。以管理员身份运行会显著扩大攻击面，默认不推荐；如需严格最小权限，请用普通标准用户
   或受 UAC 过滤的管理员账户登录。
3. **阻止睡眠的范围**：`SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)` 只能阻止**空闲超时**
   导致的系统睡眠；合盖、按电源键、开始菜单手动睡眠、电量严重不足策略、关机等仍可让机器睡眠/断电；
   也不能唤醒已经睡眠的机器（无 Wake-on-LAN 功能）。
4. **截图**：不包含鼠标指针；DRM/受保护内容可能黑屏；锁屏时可能黑屏。多显示器按物理像素拼接，缩放
   （125%/150%）已做 DPI 感知处理。
5. **图像查看**：控制端模型必须支持图像输入才能直接“看到”截图；否则 DSH 会显示诊断文本。工具同时返回
   一个不可猜测的 URL（默认 24 小时有效），控制端用户可在浏览器打开。
6. **需要 Tailscale 在线**：机器睡眠/断网/Tailscale 退出后连接中断；IP 变化后更新 `pcs.json` 里对应
   `url`（推荐用 `-PublicHost` 填 MagicDNS 名称）并重载网关，不需要重新安装插件。
7. **单交互会话**：服务运行在安装它的用户会话中；RDP 断开/切换用户可能锁屏，从而限制输入与截图。
   多用户同时登录时，各用户的服务会争用同一端口（默认安装一个用户即可）。
8. **无 TLS**：依赖 Tailscale 的 WireGuard 加密；不要绕过 Tailscale 把端口暴露到公网。token 是 bearer
   凭据，泄露即等同于控制权；请使用 Tailscale ACL 限制谁可以访问该端口，并定期轮换。
9. **网关是单点**：控制端的 `gateway/pcs.json` 汇总了所有被控端的 token；控制端被攻破即影响所有已配置
   的机器。`target` 只是工具参数，不是权限边界；模型选错 `target` 时仍会操作到另一台机器，重要操作前
   先 `pc_sysinfo` 核对主机名。

## 安全模型

- 每台被控端只监听 `127.0.0.1` 和 Tailscale IPv4 地址（默认 `bind=tailscale`）；除非显式 `-Bind all`，
  不会监听 LAN/Wi-Fi 地址。
- 所有 `/mcp` 请求需要 `Authorization: Bearer <token>`；token 由 `secrets.token_urlsafe(32)` 生成。
- 防火墙规则（管理员安装时）把入站限制在 `100.64.0.0/10`。
- 截图 URL 使用 128 位随机文件名，不含 token；只保留 24 小时/最多 200 张。
- 安装目录与数据目录在用户 profile 下，其他本机用户无法读取；服务以普通用户权限运行。
- 控制端的 `gateway/pcs.json` 含全部 token，应设为 `0600`（Windows 上用 ACL 收紧），且不要提交到仓库；
  本项目已把 `gateway/pcs.json` 加入 `.gitignore`。
- 审计日志在 `<数据目录>\logs\server.log`，默认记录工具名与命令（截断 500 字符），不记录截图内容。
  如不希望记录命令，把 `config.json` 的 `log_commands` 设为 `false`。

详见 [SECURITY.md](SECURITY.md)。

## 可移植性 / 迁移到新电脑

项目没有第三方运行时依赖（Pillow 可选），整个目录可复制：

1. 把整个项目目录复制到新 Windows 电脑（或重新 git clone）；
2. 安装 Python 3.10+ 和 Tailscale，登录同一 tailnet；
3. 运行 `deploy\install.ps1 -Alias <新别名>`（可用 `-RotateToken` 生成新 token）；
4. 把新生成的 `gateway-entry.json` 合并进控制端的 `gateway/pcs.json`，重载网关。

不要直接复制旧机器的 `config.json` 作为新机器配置来用：token、端口、主机名都应对应新机器。

## 卸载

```powershell
# 删除程序、自启、防火墙规则；保留配置/日志/token 以便重装
powershell -ExecutionPolicy Bypass -File .\deploy\uninstall.ps1

# 连同配置、token、日志、截图一起删除
.\deploy\uninstall.ps1 -Purge
```

卸载后该电脑不再接受远程 DSH 控制。控制端请在 `gateway/pcs.json` 中删除对应条目并重载“远程电脑”插件。

## 故障排查

| 现象 | 处理 |
|---|---|
| 控制端没有出现 `pc_*` 工具 | 确认 `gateway/pcs.json` 是合法 JSON、`alias` 唯一；重载“远程电脑”插件或重启 DSH；看 DSH 日志中的插件加载错误 |
| `unknown target` | `target` 不在 `pcs.json` 的 alias 列表里；先调用 `pc_list` |
| 某台机器不可用，其他正常 | 该机器离线、Tailscale 未连通、服务未运行或 token 不匹配；用 `pc_list {probe:true}` 区分在线状态 |
| `unauthorized` / HTTP 401 | token 不匹配：在被控端 `dshpc status` 查看端点；`-RotateToken` 后把新 token 更新到对应 `pcs.json` 条目并重载 |
| `INFO_REQUIRED` | 先调用 `pc_sysinfo`，或本机执行 `dshpc reset-info-gate` 后按提示操作 |
| `FEATURE_DISABLED` | 被控端关闭了该功能：`dshpc enable <feature>` |
| `REMOTE_CONTROL_LOCKED` | 被控端执行了 `dshpc remote-control off`；本机执行 `dshpc remote-control on` |
| `SESSION_LOCKED` | 被控端锁屏/UAC 安全桌面；在本机解锁后重试 |
| 启动失败/端口占用 | 看 `<数据目录>\logs\server.log`；`dshpc stop` 后 `dshpc start`；改端口重装 |
| 截图全黑 | 锁屏、DRM、或显示器休眠；确认屏幕唤醒并解锁 |

关于网关启动：它只等待 3 秒读取各机器的 `tools/list`；某台机器离线不会阻塞其他机器，离线机器仍会
出现在 `pc_list` 中，但调用会失败。若所有机器在启动时都不可达，网关会回退到 `gateway/tools.json`
快照提供工具 schema。

## 目录结构

```text
dsh-mcp-pc/
├─ server/                被控端 MCP 服务（Python 标准库）
│  ├─ dshpc_server.py     入口：--init / --check / --stdio / 服务模式
│  ├─ dshpc_mcp.py        MCP JSON-RPC + Streamable HTTP
│  ├─ dshpc_tools.py      工具定义、门禁、功能开关
│  ├─ dshpc_win.py        Win32 截图/键鼠/睡眠/会话/网络
│  ├─ dshpc_info.py       系统信息聚合
│  ├─ dshpc_exec.py       PowerShell/cmd/Python 执行
│  └─ dshpc_state.py      config.json / state.json 原子读写
├─ control/               本机 CLI（dshpc.cmd / dshpc.ps1 / dshpc_cli.py）
├─ gateway/               控制端唯一插件（dsh-pc-gateway）
│  ├─ index.js            7 个 pc_* 工具、target 路由、JSON-RPC 转发
│  ├─ pcs.json            机器清单（含 token，已 gitignore）
│  ├─ tools.json          离线工具 schema 快照
│  └─ README.md           网关配置与排障
├─ deploy/                install.ps1 / uninstall.ps1
├─ tests/                 本地能力与 MCP 端到端测试
├─ README.md
├─ REMOTE-SETUP.md
├─ SECURITY.md
└─ CHANGELOG.md
```

## 开发与测试

```powershell
python tests\test_local.py                       # 本机能力（执行/截图/睡眠/键鼠/开关/门禁）
python server\dshpc_server.py --init --data-dir .tmp\data --port 18765
python server\dshpc_server.py --data-dir .tmp\data --foreground
# 另开终端：
python tests\mcp_client.py --url http://127.0.0.1:18765/mcp --token <token> --initialize --list
python tests\mcp_client.py --url ... --tool get_system_info
```

`tests/test_mcp.mjs` 使用 DSH 自带的 `@modelcontextprotocol/client`（与 DSH 实际使用的版本相同）做端到端
验证：initialize、tools/list、首次调用门禁、功能开关、截图 URL 下载、睡眠开关等。

网关本身可用 `node --check gateway/index.js` 做语法检查；修改后重载插件，用 `pc_list` 和
`pc_sysinfo` 验证。

## 变更记录

见 [CHANGELOG.md](CHANGELOG.md)。0.2.0 起控制端只保留网关模式；旧的每台一个 bundle 的接入方式已移除。
