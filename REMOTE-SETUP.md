# 控制端（DSH）对接指南（网关模式）

本文件面向**控制方**：在自己的电脑上使用 DSH，通过 Tailscale 控制一台或多台 Windows 被控端。
控制端只安装一个 `dsh-pc-gateway` 插件；每台被控端在 `gateway/pcs.json` 中占一个条目。

被控端安装完成后会在数据目录生成：

```text
<被控端数据目录>\gateway-entry.json   # 可直接合并进 pcs.json 的条目
<被控端数据目录>\PAIRING.txt          # 同一信息的人读版本
```

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

`requestTimeoutMs` 是普通工具的 HTTP 超时；`pc_run` 会按 `timeout_sec`
自动放宽（+30 秒，上限 600 秒），所以长时间命令不会在网关侧先超时。

本文件中的 `<...>` 都是占位符，以实际生成的文件为准。

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

## 2. 安装网关（只做一次）

1. 把本项目的 `gateway/` 目录复制到控制端的任意本地目录（也可以直接在仓库里使用）。
2. 准备机器清单：

   ```bash
   cp gateway/pcs.example.json gateway/pcs.json
   chmod 600 gateway/pcs.json
   ```

3. 在 DSH 中打开 **插件（Plugins）** → **添加插件（Add plugin）**，选择 `gateway/` 目录；
   或让助手调用 `plugin_manager` 的 `install_bundle`（target 为 `gateway/` 的绝对路径）。
4. 在插件详情里关闭再开启“远程电脑”，或重启 DSH，让网关读取 `pcs.json`。
5. 新建会话，应出现 7 个工具：`pc_list`、`pc_sysinfo`、`pc_run`、`pc_screenshot`、`pc_input`、
   `pc_sleep`、`pc_features`。

`pcs.json` 必须是非空数组，且每个 `alias` 唯一、匹配 `[A-Za-z0-9_-]{1,16}`；否则插件会拒绝加载。

## 3. 加入被控端

1. 从被控端复制 `<被控端数据目录>\gateway-entry.json` 到控制端。
2. 打开 `gateway/pcs.json`，把该对象追加到 `pcs` 数组：

   ```json
   {
     "pcs": [
       {
         "alias": "office",
         "displayName": "办公室 PC",
         "url": "http://100.70.184.108:8765/mcp",
         "token": "..."
       },
       {
         "alias": "parallels",
         "displayName": "Parallels 虚拟机",
         "url": "http://100.116.55.117:8765/mcp",
         "token": "..."
       }
     ]
   }
   ```

3. 重载“远程电脑”插件（插件详情里关闭再开启，或重启 DSH）。
4. 新会话中调用 `pc_list`，确认能看到新 alias。

添加第二台、第三台机器都只需要重复这一步；不需要重新安装网关插件，也不需要重新生成或重新安装控制端插件。

### alias 与 displayName

- `alias` 是模型在 `target` 参数里使用的名字，越短越不容易写错；建议用 `office`、`lab`、`parallels`
  这类语义名，而不是 `pc1`、`pc2`。
- `displayName` 只用于展示，可以写中文；`pc_list` 会同时返回两者。
- 所有被控端的底层工具描述相同，模型只能靠 `target` 区分；重要操作前先 `pc_sysinfo` 核对 `hostname`。

## 4. 验证

1. 让助手调用 `pc_list`（可加 `probe: true` 检查在线状态）。
2. 对目标机器调用 `pc_sysinfo`，确认返回的 `hostname` 是被控端主机名、`session.locked` 为 `false`、
   `features` 中相应功能为 `true`。
3. 再让助手截图（`pc_screenshot`）或执行一条无害命令（`pc_run`，例如 `hostname`）。
4. 若返回 `unknown target`，说明 `target` 不在 `pcs.json` 的 alias 列表里；若返回 `INFO_REQUIRED`，
   说明还没调用 `pc_sysinfo`；若返回 `FEATURE_DISABLED`，说明被控端关闭了该功能。

## 5. 使用方式与礼貌边界

服务器会在 MCP `instructions` 中要求控制端模型：

- **只有用户明确要求操作某台被控端时才调用工具**；不要因为“顺便”或猜测而操作；
- 每次控制任务先调用 `pc_sysinfo`；
- 破坏性/不可逆操作先征得用户同意；
- 不绕过 `FEATURE_DISABLED` / `REMOTE_CONTROL_LOCKED` / `SESSION_LOCKED`；
- 截图和命令输出按隐私信息处理；任务结束释放不必要的睡眠阻止。

这些是模型策略约束，不是技术强制；被控端的功能开关、门禁和本机锁才是技术边界。

示例说法：

- “在 office 这台电脑上先获取系统信息。”
- “在 parallels 上运行 `Get-Process | Sort-Object CPU -Descending | Select-Object -First 10`。”
- “截一张 office 的屏幕给我看。”
- “让 parallels 30 分钟内不要睡眠，结束后释放。”
- “把 office 的 shell 和截图功能关掉。”

## 6. 更新 / token 轮换

被控端以下变化需要更新 `gateway/pcs.json` 中对应条目：

| 变化 | 处理 |
|---|---|
| 轮换 token（`install.ps1 -RotateToken`） | 用新生成的 `gateway-entry.json` 覆盖该条目的 `token`（或整条替换） |
| 端口变化 | 更新该条目的 `url` |
| Tailscale IP 变化 | 更新 `url`；推荐用 MagicDNS 名称（`-PublicHost host.tailnet.ts.net`）避免反复修改 |
| 主机名 / alias 变化 | 修改 `alias` 和 `displayName`，确认与其他机器不重复 |

改完后重载“远程电脑”插件，不需要重新安装控制端插件，也不需要重新生成 zip。

## 7. 移除

- 控制端：在 `gateway/pcs.json` 中删除对应条目并重载插件；不再需要时，从插件页面移除整个
  “远程电脑”插件。
- 被控端：如需彻底停止接受控制，运行 `deploy\uninstall.ps1`（或先 `dshpc all-off` 临时收紧）。

## 8. 故障排查

| 现象 | 原因/处理 |
|---|---|
| 插件加载失败 / 没有 `pc_*` 工具 | `pcs.json` 不是合法 JSON、`pcs` 为空、`alias` 重复或不合法；看 DSH 日志中的 `pc-gateway:` 错误 |
| 改了 `pcs.json` 但工具没变 | 网关只在插件加载时读取配置；关闭再开启插件，或重启 DSH |
| `unknown target` | `target` 不在 alias 列表；先 `pc_list` |
| 某台机器不可用，其他正常 | 该机器离线、Tailscale 未连通、服务未运行或 token 不匹配；`pc_list {probe:true}` 可看 online/error |
| `unauthorized` / 401 | token 不一致：被控端 `dshpc status` 查看端点；轮换后把新 token 更新到对应条目并重载 |
| `INFO_REQUIRED` | 先 `pc_sysinfo` |
| `FEATURE_DISABLED` | 被控端 `dshpc enable <功能>` |
| `REMOTE_CONTROL_LOCKED` | 被控端 `dshpc remote-control on` |
| `SESSION_LOCKED` | 被控端锁屏/UAC 安全桌面，需本机解锁 |
| 连接时不时断开 | 被控端睡眠/网络切换/Tailscale 退出；检查电源设置和 Tailscale |
| 截图全黑 | 锁屏、DRM、显示器休眠；让被控端解锁并唤醒屏幕 |

网关启动时只给每台机器 3 秒读取工具目录；某台离线不会阻塞其他机器。若启动时所有机器都不可达，
网关会回退到 `gateway/tools.json` 快照提供 schema，实际调用仍会失败。

## 9. 凭据

- `gateway/pcs.json` 保存所有被控端的 Bearer token，等同于这些机器的控制权；保持 `0600`
  （Windows 上用 ACL 收紧），不要提交到仓库、不要发到聊天或截图里。
- 合并完成后，被控端的 `gateway-entry.json` 可以删除；`PAIRING.txt` 也应按凭据处理。
- 怀疑泄露：在被控端 `install.ps1 -RotateToken`，再更新控制端对应条目并重载。
