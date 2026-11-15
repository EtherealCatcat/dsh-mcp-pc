# Changelog

本项目的控制端接入方式在 0.2.0 起统一为**网关模式**。旧的“一台被控端一个
`@deepseek-ai/dsh-mcp-client` bundle”方案已移除，不再提供生成、安装或回滚脚本。

## 0.2.2 — 2026-11-15

### 修复

- 去掉仓库里的个人环境值：示例配置与文档改用 `100.x.y.z` 占位地址，`verify_deployed.py`
  的地址改由被控端 `config.json` 的 `public_host`/`port` 推导，`gateway/README.md` 不再写
  开发者本机路径。
- 版本与常量改为单一来源：安装脚本的 `VERSION` 直接读 `server/dshpc_version.py`，不再写死
  `1.0.1`；`gateway-entry.json` 不再复制网关的 `requestTimeoutMs`；端口、请求体上限、日志
  轮转、命令超时/输出上限、截图保留数量全部集中在 `server/dshpc_state.py`，MCP schema、
  安装脚本与网关与之对齐。
- `tools/list` 的 `timeout_sec.maximum` 现在跟随被控端 `shell.max_timeout_sec`，配置调低上限时
  不再向控制端承诺一个服务端会拒绝的值。

### 变更

- 新增 `deploy/common.ps1`：安装与卸载共享任务名、Run 键名、防火墙规则前缀、默认安装/数据
  目录、默认端口与健康检查窗口，卸载不会漏删安装时创建的对象。
- `mode_serve` / `mode_stdio` 共用 `_build_runtime()`，新增限制只需接一次线。
- 新增配置项 `shell.max_output_bytes`（默认 4 MiB）与 `log_command_chars`（默认 500）。
- `tests/*.mjs` 通过 `tests/mcp-client.mjs` 三级探测 DSH 自带的 MCP SDK，`test_mcp.mjs` 支持
  `--config` / `--url` / `--port`；非 Windows 平台的数据目录回退改为 `~/.dshpc`。
- 新增 `tests/test_no_baked_values.py`：阻止个人 IP/路径回流，并校验端口、超时上限、安装脚本
  常量仍与 Python 单一来源一致。
- 远程模式的 `test_http_keepalive.py` 默认把客户端超时放宽到 60 秒（可用 `DSHPC_TEST_TIMEOUT`
  覆盖），避免经 Tailscale/DERP 上传 2 MB 超限请求时误报失败。

## 0.2.1 — 2026-11-15

### 修复

- 网关按 `pc_run` 的 `timeout_sec` 自动放宽 HTTP 超时（该值 +30 秒，上限 600 秒），
  不再用固定 120 秒掐断被控端允许跑 600 秒的命令。此前网关先超时、命令仍在被控端
  继续执行，用户重试就会重复执行同一条命令。
- 被控端 HTTP 层在 401/413/404/411 等早期返回前读取并丢弃未解析的请求体，超限或
  无法解析时显式关闭连接。此前未读的请求体会被当成下一条请求行解析，同一 keep-alive
  连接上先发错 token 再发正常请求会得到 `501 Unsupported method`，大请求体则直接
  `BrokenPipe`。超过 `max_request_bytes` 4 倍的上传不再读取，直接断开。
- `ScreenshotStore.save()` 改用每次唯一的临时文件名，`cleanup()` 只回收超过 300 秒的
  残留 `.tmp`，两者并发时不再出现 `os.replace()` 的 `FileNotFoundError`。

### 新增

- 跨平台回归测试（不需要 Windows）：`tests/test_http_keepalive.py`、
  `tests/test_screenshot_store.py`、`tests/test_gateway_contract.py`、
  `tests/test_gateway_timeout.mjs`。最后一个不依赖 Host 直接测超时策略；契约测试同时校验
  `gateway/tools.json` 快照与 `server/dshpc_tools.TOOL_DEFS` 一致。
- 超时策略抽到无依赖的 `gateway/timeout.js`（`callTimeoutMs`），便于单独测试。

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
