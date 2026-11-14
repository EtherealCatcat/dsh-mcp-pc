# Changelog

本项目的控制端接入方式在 0.2.0 起统一为**网关模式**。旧的“一台被控端一个
`@deepseek-ai/dsh-mcp-client` bundle”方案已移除，不再提供生成、安装或回滚脚本。

## 0.2.0 — 2026-11-15

### 新增

- `gateway/` 控制端 bundle：一个插件卡片、一个 `pc_*` 工具命名空间，覆盖多台被控端。
  - `pc_list`：列出已配置的机器；`probe: true` 时并行探测在线状态。
  - `pc_sysinfo`、`pc_run`、`pc_screenshot`、`pc_input`、`pc_sleep`、`pc_features`：
    分别对应被控端原有的 6 个 MCP 工具，统一用 `target` 选择机器。
  - 直连被控端现有的 Streamable HTTP `/mcp`，不增加代理进程；启动探测失败时可用
    `gateway/tools.json` 快照离线提供 schema。
- `gateway/pcs.json` 作为唯一的机器清单（alias / displayName / url / token），
  加机器只改这个文件并重载插件，不需要重新生成或安装控制端插件。
- 被控端安装器新增 `-Alias` 参数，并生成机器可读的 `gateway-entry.json` 与便于人读的
  `PAIRING.txt`，供控制端合并进 `pcs.json`。
- 插件页本地化标题改为“远程电脑”，描述说明网关、`target` 参数和 7 个工具。

### 变更

- `deploy/install.ps1` 不再生成 `controller-bundle/` 或 `dsh-pc-remote-*.zip`；
  改为输出网关条目。安装目录不再复制 `bundle/`。
- 控制端文档统一为网关接入：先安装一次 `gateway/`，之后每台被控端只增加一个条目。
- token 轮换、端口或地址变化时，只更新 `gateway/pcs.json` 中对应条目并重载网关，
  不再需要重装控制端插件。
- 网关版本号提升到 `0.2.0`，补齐插件页图标与中英文描述。

### 移除

- `bundle/` 控制端插件模板。
- `deploy/make-bundle.ps1` 及 zip/PAIRING 生成流程。
- 每台机器一个 `serverName`、`mcp__<serverName>__*` 工具前缀的控制端方案。
- 仓库内旧的 `dist/controller-bundle/`、`dist/dsh-pc-remote-*.zip`、`dist/PAIRING.txt`
  生成物（不再作为回滚材料保留）。

### 升级说明

1. 被控端重新运行 `deploy/install.ps1`（必要时加 `-RotateToken`），取得
   `<数据目录>\gateway-entry.json`。
2. 控制端安装 `gateway/`，把该条目合并进 `gateway/pcs.json` 的 `pcs` 数组。
3. 重载 `dsh-pc-gateway` 插件（或重启 DSH），调用 `pc_list` 与 `pc_sysinfo` 验证。
4. 移除旧的 `dsh-remote-pc-*` 插件；旧的控制端 bundle 目录可以删除。

`target` 只是选择机器的参数，不是安全边界；真正的边界仍是被控端的 Bearer token、
Tailscale ACL、防火墙、功能开关和本机远程管理锁。
