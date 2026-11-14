# Remote PC (Tailscale) — controller-side bundle

This bundle connects the local DeepSeek Harness profile to **one** controlled
Windows PC. It inserts a `@deepseek-ai/dsh-mcp-client` row whose URL and bearer
token are filled in when the controlled PC runs its installer.

The generated copy of this folder contains a live credential
(`cordis.patch.yml`). Do not commit, publish, or share it.

## Install on the controller

1. Copy the generated folder (or extract the generated `.zip`) to any local
   directory.
2. In DeepSeek Harness open **Plugins** → **Add plugin** and select that
   directory. Alternatively ask the agent to install the local bundle with
   `plugin_manager` (`install_bundle`, target = absolute directory).
3. Start a new session (or reload the profile) so the MCP client connects and
   registers the tools.
4. Ask the assistant to "call get_system_info on the remote PC" (or simply ask
   it to operate that PC) and confirm the returned hostname.

Tool names are `mcp__<serverName>__<tool>`; the generated `PAIRING.txt` in the
same folder records the exact server name and tool prefix.

## Available tools

| Tool | Capability |
|---|---|
| `get_system_info` | Required first call: OS, hardware, displays/DPI, session and lock state, power, Tailscale, feature switches |
| `run_command` | PowerShell (pwsh or Windows PowerShell), cmd, or Python with timeout and process-tree kill |
| `capture_screen` | All monitors or one monitor, PNG/JPEG, image content plus an unguessable URL |
| `input_control` | Batched mouse move/click/scroll and keyboard typing/hotkeys |
| `set_sleep_block` | Prevent/release idle system sleep (and optionally display sleep) |
| `control_features` | Status and per-feature or all-on/all-off switches |

## Policy the server asks the model to follow

- Only use these tools when the user explicitly asks to operate that PC.
- The first control call in every server session must be `get_system_info`.
- Confirm destructive or irreversible actions before running them.
- Treat screenshots and command output as private.
- Never bypass `FEATURE_DISABLED`, `REMOTE_CONTROL_LOCKED`, or
  `SESSION_LOCKED`; report them to the user.

The server injects these rules through MCP `instructions` and repeats them in
tool descriptions.

## Known limits

- Windows locked screen or UAC secure desktop: screenshots may be black and
  input injection is refused by Windows.
- The controlled process runs with the logged-in user's rights. It cannot
  operate elevated windows or click UAC prompts unless it is itself elevated
  (not recommended by default).
- Sleep prevention covers idle sleep only; lid close, power button, manual
  sleep and battery policy can still sleep the PC, and it cannot wake a
  sleeping PC.
- Screenshots do not include the mouse pointer; DRM/protected content may be
  black.
- The controller model needs image input to see screenshots directly;
  otherwise use the returned URL or ask the user to open it.
- The PC must be online in Tailscale and the DSH user logged in.

## Troubleshooting

- No tools appear: check that the controlled PC is online (`tailscale ping
  <host>`), that the server is running (`dshpc status` there), and that the
  process may bind the port. If the PC was offline when DSH started, reload
  the plugin or restart DSH after the PC comes online.
- `unauthorized`: the token in `cordis.patch.yml` no longer matches the
  controlled PC. Regenerate the bundle there (`deploy\make-bundle.ps1`) and
  reinstall it.
- `FEATURE_DISABLED`: run `dshpc enable <feature>` on the controlled PC, or ask
  its user to allow it via `control_features` when remote management is on.
- `INFO_REQUIRED`: call `get_system_info` first.
- `REMOTE_CONTROL_LOCKED`: the owner locked remote management locally; only
  `control_features` status is readable until they run `dshpc remote-control
  on`.
