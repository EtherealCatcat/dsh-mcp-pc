# 远程电脑网关（dsh-pc-gateway）

一个 DSH Host 插件管理所有被控端：**一张卡片、7 个 `pc_*` 工具**，每台机器用 `target` 选择。

```text
DSH Host
└─ dsh-pc-gateway（一个插件、一张卡片、7 个工具）
   ├─ pc_list                         ← 本机生成，列出 target
   ├─ pc_sysinfo   { target: "office" } ─┐
   ├─ pc_run       { target: "office" }  │  POST /mcp  tools/call
   ├─ pc_screenshot{ target: "office" }  ├─→ 办公室 PC  (100.x.y.z:8765)
   ├─ pc_input     { target: "lab"    }  │
   ├─ pc_sleep     { target: "lab"    }  ├─→ 实验室 PC  (100.a.b.c:8765)
   └─ pc_features  { target: "lab"    } ─┘
```

没有额外的代理进程。网关直接向每台被控端现有的 `/mcp` 端点发无状态 JSON-RPC；被控端、token、
端口和部署方式都不变。

## 工具

| 工具 | 作用 |
|---|---|
| `pc_list` | 列出别名、显示名、端点；`probe: true` 时并行检查哪些机器在线 |
| `pc_sysinfo` | 主机名、OS、CPU/内存/磁盘、显示器、锁屏、功能开关、服务状态 |
| `pc_run` | PowerShell / cmd / Python，返回退出码、stdout/stderr、超时 |
| `pc_screenshot` | 全屏或单显示器截图，图片按原生 image 内容透传 |
| `pc_input` | 鼠标、滚轮、文本（含中文）、组合键、等待 |
| `pc_sleep` | 阻止/释放空闲睡眠（可选保持显示器常亮） |
| `pc_features` | 查看/切换 info、shell、screenshot、input、sleep 开关 |

除 `pc_list` 外，每个工具都有必填的 `target`，取值来自 `pcs.json` 的 `alias`。

## 配置

```jsonc
// gateway/pcs.json   （chmod 600；已在 .gitignore 中）
{
  "pcs": [
    {
      "alias": "office",                 // 工具里的 target；[A-Za-z0-9_-]{1,16}
      "displayName": "办公室 PC",         // 只用于展示
      "url": "http://100.70.184.108:8765/mcp",
      "token": "REPLACE_WITH_TOKEN",
      "requestTimeoutMs": 120000          // 可选，默认 120000
    }
  ]
}
```

- `pcs` 必须是非空数组，`alias` 必须唯一；重复或非法 alias 会让插件拒绝加载并报出具体位置。
- `token` 也可以写成 `"${env:DSH_PC_OFFICE_TOKEN}"`，由 Host 进程的环境变量提供；
  变量缺失时插件会直接报错，而不是带着空 token 启动。
- `url` 推荐使用 MagicDNS 名称，避免 IP 变化后反复修改。
- 修改 `pcs.json` 后重载插件：在插件详情里关闭再开启“远程电脑”，或重启 DSH。
- 启动时插件只花最多 3 秒拉取一次远端 `tools/list`；全部离线时回退到
  `gateway/tools.json` 的工具快照，DSH 仍能正常启动，调用时再报具体的连接错误。

## 安装

```bash
# 1. 准备控制端配置（被控端安装后生成的 gateway-entry.json 可整条复制进来）
cp gateway/pcs.example.json gateway/pcs.json
chmod 600 gateway/pcs.json
# 编辑 alias / displayName / url / token

# 2. 安装（DSH 内让助手调用 plugin_manager，或插件页“添加插件”选择 gateway/）
#    install_bundle target=/Users/EtherealCat/dsh-mcp-pc/gateway
```

安装后插件列表里会出现一张 **远程电脑** 卡片；工具名是 `pc_*`，用 `target` 选择机器。

## 加一台机器

1. 在被控端运行 `deploy\install.ps1 -Alias <别名>`，得到 `<数据目录>\gateway-entry.json`。
2. 把该 JSON 对象追加到 `gateway/pcs.json` 的 `pcs` 数组；确保 alias 唯一。
3. 重载“远程电脑”插件。
4. 调用 `pc_list` 确认出现新 alias，再 `pc_sysinfo target=<别名>` 核对主机名。

不需要重新安装控制端插件，也不需要重新生成 zip。token 轮换或地址变化时，只改对应条目并重载。

## 限制

- **`target` 不是安全边界。** 任何能调用这些工具的会话都可以操作 `pcs.json` 里的全部机器；
  要按人/按机器隔离，仍然要拆成不同插件或用不同 token 的独立部署。
- **没有运行时热加载。** 改 `pcs.json` 需要重载插件/重启 Host；运行中增删（像 mcp-manager 那样）没有做。
- **图片取决于模型能力。** 模型不支持 image 输入时，截图降级为文本诊断；原始图片仍可从
  被控端的 `url`/`local_path` 取得。
- **工具目录是快照。** 被控端升级后新增的 MCP 工具不会自动出现在网关里，需要重启时重新拉取，
  或更新 `gateway/tools.json` 作为离线回退。
- **启动探测有上限。** 启动时并行请求所有 PC 的 `tools/list`，各自 3 秒上限；机器很多时仍会占用几秒。

## 故障排查

| 现象 | 处理 |
|---|---|
| 插件加载失败 | `pcs.json` 不是合法 JSON、`pcs` 为空、`alias` 重复或不合法；看 DSH 日志里的 `pc-gateway:` 错误 |
| 改了 `pcs.json` 但工具没变 | 网关只在加载时读配置；关闭再开启插件，或重启 DSH |
| `unknown target` | 先 `pc_list`，确认 alias 拼写 |
| 某台机器调用失败 | `pc_list {probe: true}` 看 online/error；检查 Tailscale、被控端服务、token |
| `unauthorized` / 401 | token 不匹配；被控端轮换后更新对应条目 |
| 截图全黑 | 被控端锁屏、DRM 或显示器休眠 |
