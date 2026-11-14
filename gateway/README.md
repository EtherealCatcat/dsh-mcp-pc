# 网关模式：一个插件管理多台远程 PC

默认的配对 bundle 是 **一台 PC 一个 mcp-client 插件**：N 台机器就有 N 个卡片、N 组
`mcp__<serverName>__*` 工具，模型要在相似的工具名之间选。网关模式换一种做法：

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

没有额外的代理进程，也不再有每台机器一个 `serverName`。网关直接向每台被控端现有的
`/mcp` 端点发无状态 JSON-RPC；被控端、token、端口、部署方式都不变。

## 工具

| 工具 | 作用 |
|---|---|
| `pc_list` | 列出别名、显示名、端点；`probe: true` 时顺带检查哪些机器在线 |
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
      "displayName": "办公室 PC",         // 只用于描述
      "url": "http://100.70.184.108:8765/mcp",
      "token": "REPLACE_WITH_TOKEN",
      "requestTimeoutMs": 120000          // 可选
    }
  ]
}
```

- `token` 也可以写成 `"${env:DSH_PC_OFFICE_TOKEN}"`，由 Host 进程的环境变量提供；
  变量缺失时插件会直接报错，而不是带着空 token 启动。
- 修改 `pcs.json` 后**重启 Host**（不是重启被控端，也不需要重新安装插件）。
- 启动时插件只花最多 3 秒拉取一次远端 `tools/list`；全部离线时回退到
  `gateway/tools.json` 的工具快照，DSH 仍能正常启动，调用时再报具体的连接错误。

## 安装

```bash
# 1. 生成被控端时已经拿到 token；这里只写控制端配置
cp gateway/pcs.example.json gateway/pcs.json
chmod 600 gateway/pcs.json
# 编辑 alias / displayName / url / token

# 2. 安装（DSH 内让助手调用 plugin_manager，或插件页“添加插件”选择 gateway/）
#    install_bundle target=/Users/EtherealCat/dsh-mcp-pc/gateway
```

安装后插件列表里会出现一张 **远程电脑（网关）** 卡片；工具名是 `pc_*`，不带
`mcp__<serverName>__` 前缀。

## 迁移：从每台一个插件到网关

1. 先装网关，确认 `pc_list` 和 `pc_sysinfo target office` 正常。
2. 禁用或移除旧的 `dsh-remote-pc-*` bundle（`plugin_manager` 的 `remove_bundle`），
   避免同一台机器出现两套工具。
3. 旧 bundle 仍然在 `dist/controller-bundle/` 里，需要回滚时重新 `install_bundle` 即可。

## 与旧方案对比

| | 每台一个 bundle | 网关 |
|---|---|---|
| 插件卡片 | N 张，标题相同 | 1 张 |
| 工具 | 6 × N，长前缀 | 7 个，短名 + `target` |
| 加一台 PC | 重新生成/安装 bundle | 改 `pcs.json` + 重启 Host |
| 单台启停 | 插件级开关 | 从 `pcs.json` 删除/注释该台 |
| 截图 | 原生 image | 原生 image（同一套 `createMcpToolDefinition` 通路） |
| 配置界面 | 无 | 无（YAML/JSON，暂无表单） |
| 安全边界 | token 决定能控制哪台 | 相同：`target` 不是权限隔离，能调用网关就能控制所有已配置机器 |

## 限制

- **`target` 不是安全边界。** 任何能调用这些工具的会话都可以操作 `pcs.json` 里的全部机器；
  要按人/按机器隔离，仍然要拆成不同插件或用不同 token 的独立部署。
- **没有热加载。** 改 `pcs.json` 需要重启 Host；运行时增删（像 mcp-manager 那样）没有做。
- **图片取决于模型能力。** 模型不支持 image 输入时，截图降级为文本诊断；原始图片仍可从
  被控端的 `url`/`local_path` 取得。
- **工具目录是快照。** 被控端升级后新增的 MCP 工具不会自动出现在网关里，需要更新
  `gateway/tools.json` 或重启时重新拉取（当前启动会优先拉取；快照只是离线回退）。
- **单进程串行启动探测。** 启动时并行请求所有 PC 的 `tools/list`，各自 3 秒上限；机器很多时
  仍会占用几秒启动时间。
