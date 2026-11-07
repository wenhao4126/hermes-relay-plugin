# Hermes Relay

**中文**：面向 Hermes Agent 的受控 Relay 插件：登记任务资料、通过 Jev 门禁、创建空白 CLI 标签，由当前 profile 绑定的 Hermes 主控（owner）在授权范围内核实并派发任务，再跟踪通知。

**English**: A controlled Relay plugin for Hermes Agent. It registers task evidence, applies a Jev gate, creates an empty CLI tab, and lets the Hermes main agent bound as the current profile owner verify and dispatch within its authorization before tracking notifications.

语言导航 / Languages: [中文](#中文) · [English](#english)

> 示例仅供说明，实际结果以本机输出和对应版本的验收记录为准。
>
> Examples are illustrative. Actual results depend on local output and the acceptance record for the relevant version.

## 中文

### 1. 它做什么，以及适合谁

Hermes Relay 将受控的资料登记、任务门禁、空 CLI 创建、owner 核实、一次派发和状态通知串成一个流程。owner 指当前 Hermes profile 绑定的 Hermes 主控。它可以在用户授权范围内核对任务包、身份、模型、权限、工作目录和输入就绪状态，再决定是否派发；用户不必逐项查看 CLI 窗口或手填 JSON。之后可查询状态、接收后台通知并确认通知已消费。

适合由 Hermes 主控在用户授权范围内协调 Codex、OpenCode 或 Claude Code CLI 工作的本地流程。插件不会脱离当前 profile 的合法 owner 和门禁自行规划、授权或派发；Hermes 可在已有授权内自动完成常规核实并推进流程。危险、破坏性、越界或涉及新架构的决定应交由用户明确决策。它不提供跨多个 profile 的共享 owner、全局不可绕过沙箱或自动 token 硬限额，也不保证业务任务一定成功。

| 角色 | 职责 |
| --- | --- |
| Hermes 主代理 | 理解用户任务、登记资料、检查门禁结果并协调流程；主代理本身不限制模型。 |
| Relay | 绑定单一 profile owner，执行门禁与受控 CLI 生命周期记录，观察状态并投递通知。 |
| Herdr CLI | 创建标签、启动代理、读取身份与状态，并提交任务。 |
| Owner（原主控） | 当前 profile 绑定的 Hermes 主控；在用户授权内核实真实模型、权限、工作目录和就绪状态，并按门禁结果决定是否派发。 |
| 用户 | 提供任务与授权；对危险、破坏性、越界或新架构决策作明确决定，并检查最终工作结果。 |

### 2. 使用条件与已知环境

2026 年 11 月安装验证环境的版本观测值为 Linux、Hermes Agent v0.22.5（SDK 提交 bf0203a794a0）、Python 3.14.9、Herdr 0.9.5、Codex 0.167.0、OpenCode 2.2.0、Claude Code 2.1.319。这些是该环境的版本记录，不是完整兼容组合验收，也不是普遍最低版本或其他系统的兼容承诺。对应 CLI 必须已安装、登录，并能使用其配置的模型与供应商。插件安装不会替用户登录，也不附送模型额度。

`TYPESAFE_API_KEY` 用于 Relay 的 Jev 门禁请求，不是 Codex、OpenCode、Claude Code 或 Hermes 模型供应商通用的模型密钥。没有它时，门禁评估不能正常进行；状态和通知类工具仍可能可用。密钥应只配置在当前 Hermes profile 的安全配置中。平台绑定能力仅核实过 `local`、`cli`、`tui`、`desktop`，不要据此推断远程聊天渠道也受支持。

### 3. 首次原生安装（仓库已有插件源后）

确认仓库对应版本已包含 `hermes_plugin/relay/` 插件源后，在安装了 Hermes 的普通终端中执行以下命令：

```bash
# 从 GitHub 仓库的插件子目录安装并启用
hermes plugins install "wenhao4126/hermes-relay-plugin/hermes_plugin/relay" --enable
```

安装器从仓库子目录清单读取插件名 `relay`，安装目录目标是 `relay`；仓库根 README 不属于插件安装内容。安装后的预期检查标志是 Doctor 能识别该插件，随后按需启用 Relay 工具集并完成 profile 设置。不要用动态 PASS 时间、下载数或徽章替代对应版本的验收记录。

### 4. 配置、启用与安全加载

普通 Shell 是运行 Hermes 命令的终端；Hermes 聊天框是代理对话输入区。把管理命令粘贴到聊天框可能会被当作任务内容，因此在普通 Shell 中执行：

```bash
# 检查当前插件状态
hermes plugins doctor relay --ci
# 按需让 relay 工具集对 CLI 平台可用
hermes tools enable relay --platform cli
# 启用插件并明确关闭替换内置工具的授权
hermes plugins enable relay --no-allow-tool-override
# 仅在用户明确授权后台 Gateway 通知时设置注入开关
hermes config set plugins.entries.relay.allow_gateway_injection true
```

`TYPESAFE_API_KEY` 是 Relay Jev 门禁密钥，不是代理模型供应商的模型密钥。Hermes SDK 的 secret scope 会加载**当前 profile 目录**中的 `.env` 并合并已配置的 secret sources；插件自身只通过 `get_secret` 取得 `TYPESAFE_API_KEY`，不会直接读取 `.env`。不要把密钥放在任意目录、`config.yaml` 明文、聊天记录、命令参数或 Git 中。以下仅为占位示意：

```dotenv
# 在当前 Hermes profile 的 .env 中由用户安全设置；替换占位值，不要提交文件
TYPESAFE_API_KEY=<YOUR_TYPESAFE_API_KEY>
```

安装插件不会替用户登录，也不提供模型额度。`--no-allow-tool-override` 会明确关闭 Relay 替换 Hermes 内置工具的授权；`--enable` 本身不代表授予所有权限。Gateway 注入是独立的可选开关，会使插件能够向当前 Gateway 投递后台通知，必须由用户明确授权；后台通知依赖该开关。

更新文件或设置后，如需重新加载 Gateway，先保存其他活动任务，再由用户在独立普通 Shell 执行：

```bash
# 在独立普通 Shell 中执行；先保存其他活动任务
hermes gateway restart
```

不要让依赖当前 Gateway 的助手通过自身连接重启它，否则当前会话可能中断。插件启用状态与 Gateway 实际加载状态是两件事；重载后按当前 profile 再查 Doctor 和 `/relaystatus`。

### 5. 首次聊天与结果含义

在 Hermes 聊天框首次绑定并查询：

```text
/relaybind
/relaystatus
```

绑定成功后可用大白话描述目标，例如：“请核对这项任务的资料与门禁；若门禁允许，请按当前授权核实新标签的身份、模型、权限、目录和输入就绪，再派发一次。若遇到危险、破坏性、越界或新架构决定，先停下并问我。完成后报告状态，不要重复提交。” 用户不需要先手工填写复杂 JSON，也不必逐项查看窗口；Hermes owner 可在授权范围内完成常规核实和推进。

以下是流程示意，不是已执行的输出：

```text
示意：创建返回 CREATED_UNVERIFIED → Hermes owner 按授权核实实际环境 → 派发返回 SUBMITTED → 后续观察和通知 → Hermes owner / 用户按职责验收任务结果
```

这些是不同层次：创建成功只代表空 CLI 身份回读；模型与权限由 Hermes owner 在授权内核实；`SUBMITTED` 只代表提交回执被接受；通知消费只代表通知已 ACK；任务完成与最终工程验收仍需检查实际产物和证据。`/relaystatus` 可查询当前 owner 可见的摘要。

### 6. 十项工具与正常顺序

| 工具 | 用途 |
| --- | --- |
| `relay_record_document` | 登记任务使用的资料声明及来源字段。 |
| `relay_evaluate_task` | 将完整任务交给 Jev 门禁评估。 |
| `relay_adjudicate_task` | 仅在原规则允许时记录 owner 的显式裁决，需给出理由与证据引用；这不是用户人工绕过规则的豁免。 |
| `relay_create_agent` | 通过原生 Herdr 创建不带任务正文的空 CLI。 |
| `relay_refresh_agent` | 对已记录标签做只读身份和状态回读。 |
| `relay_dispatch_task` | 在门禁、owner 与核实条件满足后提交一次任务。 |
| `relay_watch_task` | 登记观察任务与实例。 |
| `relay_unwatch_task` | 显式停止对应观察记录。 |
| `relay_ack_event` | 确认当前 owner 已消费一条通知。 |
| `relay_plugin_status` | 查询插件绑定和状态摘要。 |

正常顺序是：登记资料；完成 Jev 任务明确性门禁；只有满足原规则时才由 owner 显式裁决；创建空 CLI；Hermes owner 在授权范围内核实身份、模型、effort、权限、目录和输入就绪；派发一次；观察状态；完成后 ACK 通知，再按需要显式 unwatch。派发成功后会建立观察记录；ACK 不会释放派发占位，unwatch 只停观察，不会关闭或删除 CLI。危险、破坏性、越界或新架构决定应暂停并请求用户明确决定。

`UNKNOWN`、`PREPARED`、`SENDING` 表示结果未明或正在处理，禁止换 `request_id` 盲目重试、重复创建或尝试清除占位。先按原记录核实。`REVISE` 不能由 owner 显式裁决绕过；门禁 `UNKNOWN` 也不是批准。

### 7. CLI 模型与权限核实

Hermes 主代理不被 Relay 限定模型。Codex 的 `worker` 与 `fixer` 请求固定使用 `gpt-6-luna`，effort 为 `high` 或 `max`；OpenCode 与 Claude Code 沿各自 CLI 原生默认模型运行，不由 Relay 强制 DeepSeek。`cli_default_unverified` 只是“使用 CLI 默认值，尚待核实”的标记，不是模型名或登录证明。已有代理不会因插件升级自动更换模型。

创建请求中的模型和 effort 不等于实际生效值；CLI 配置或 Shell 环境可能覆盖 effort。Hermes owner 必须核实实际模型、effort、权限、工作目录、代理身份和就绪状态，可在授权范围内通过可用状态与环境信息完成，无需用户逐项查看标签。有一项不符合预期策略就停止派发。OpenCode 与 Claude Code 的默认模型不在创建时强制指定；Claude 的 reviewer 与 auditor 使用只读工具约束和严格空 MCP 配置，仍由 owner 核实实际运行状态。

### 8. Owner、profile 与 epoch

每个 Hermes profile 只有一个 Relay owner，即当前 profile 绑定的 Hermes 主控。两个 Herdr 窗口不构成隔离边界；若要分开 owner 与状态，必须使用独立 profile。恢复原聊天时先 `/relaystatus`，必要时再普通 `/relaybind`。相同身份重复绑定可保留 epoch，但会替换当前 Runtime；运行中的工作流不要反复绑定。`/relaybind --replace` 只用于用户明确交接 owner：它会让新身份推进 epoch。

新创建的标签记录使用当前 epoch。旧创建记录保留旧 epoch，不自动迁移、停止标签或重派任务；旧请求可能因 `epoch_changed` 无法刷新。是否续用旧空标签应由新主控重新核实，并遵循未派发任务的明确接续流程；存在未结清的派发时停止处理，不换 ID 绕开保护。

### 9. 升级与已有手工安装

若本机已有手工安装的 `relay` 目录，不要把它当作首次安装成功，也不要重复首次安装。先保留现有目录、配置与账本；由维护者按备份和隔离验证流程迁入原生管理。手工目录迁移目前未验证，不要自行使用 `--force` 迁移，也不要先执行 remove、删除 plugin-data 或清账本。

原生 Git 安装后，`hermes plugins update relay` 只适用于来源可更新且未固定 SHA 的安装。固定 SHA 的安装不会因普通 update 自动移动；选择新的已验收 SHA 需要维护者显式操作，并绑定完整 SHA。没有经核实的迁移命令时不要猜命令。未来更新后仍需按上一节在独立普通 Shell 安全重新加载 Gateway，并确认其他活动任务已保存。

### 10. 效果、限制与非写入验证

后台任务通知不等于经典 Hermes CLI 前台自动展示代理回合。状态码 `0` 或 `SUBMITTED` 不等于业务端到端通过。创建历史每 profile 最多 64 条且没有自动 GC。门禁、Runtime、Dispatcher、Observer 和 Herdr 外部调用分开执行，不能保证跨文件或 Herdr 全局原子性。`UNKNOWN` 不自动重试。插件也不是绝对不可绕过的全局沙箱，没有自动 token 硬限额。

首次验证可从非写入检查开始：查看仓库文件和版本基线；安装后运行 Doctor；确认工具集与插件启用状态；在聊天中 `/relaystatus` 查看 owner 和通知摘要；确认 profile 密钥存在但不显示密钥值。需要验证真实付费模型或真实任务派发时，先取得明确授权，优先用受限、可观察、可回滚的非敏感任务，不要把真实付费业务当作默认烟雾测试。

### 11. 常见问题排查

按此顺序检查，先读状态再决定下一步：

1. 工具不存在：在普通 Shell 检查 Doctor、插件启用状态和 `relay` 工具集是否已按需启用。
2. `api_key_unavailable`：确认当前 profile 安全配置中的 `TYPESAFE_API_KEY`，不要把值贴到聊天中。
3. `not_owner`：确认当前聊天是否仍是绑定 owner；不要直接 replace。
4. `epoch_changed`：核实 owner 交接和旧记录归属，不编辑账本或猜测迁移。
5. `agent_busy` 或 `agent_busy_unknown`：查看现有派发与观察记录；未知状态不可通过新 ID 重试。
6. 模型或 effort 不符：让当前 Hermes owner 检查 CLI 实际配置、Shell 覆盖和当前标签权限，核实前停止派发；用户不需要逐项查看窗口。
7. 首次安装提示目录已存在：保留目录、配置和账本，交维护者按迁移流程处理，不 remove 重装。
8. `SUBMITTED` 但似乎没完成：这是提交回执，不是完成；查看后台标签、观察通知和实际工作产物。
9. 更新后 Gateway 未激活：保存其他活动任务后，在独立普通 Shell 执行 `hermes gateway restart`，再检查 Doctor 与状态；不要由依赖该 Gateway 的助手重启。

关闭代理、卸载插件、升级插件是不同操作。卸载不会自动关闭已有 CLI 或删除账本；保留账本以便核实未明状态。不要运行破坏性清理命令来“修复”状态。

### 12. 安全、目录与参考

不要提交密钥、认证文件、真实任务账本、会话 ID、工作区 ID 或私人本机路径。示例路径应使用通用目录，示例值应明确为占位符。Relay 的 owner 检查和 `owner_checks` 仍基于 owner 声明与当前 profile 状态，不构成全局不可绕过的权限边界。辅助 tester、reviewer、auditor 宜使用短而具体的任务包、保存完整日志到本地文件，并只在交接中提供摘要与关键差异；产品没有自动 token 上限。

发布仓库中 `hermes_plugin/relay/` 是插件源目录，根目录 `README.md` 是面向仓库用户的安装与使用说明；插件内原有中文 README 保留为内部流程材料。GitHub 仓库：<https://github.com/wenhao4126/hermes-relay-plugin>。Hermes 官方插件说明：<https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins>。Hermes v0.22.5 对应版本文档：<https://github.com/nousresearch/hermes-agent/blob/v0.22.5/website/docs/user-guide/features/plugins.md>。

## English

### 1. What it does and who it is for

Hermes Relay connects evidence registration, a task gate, empty CLI creation, owner verification, one dispatch, and status notifications. The owner is the Hermes main agent bound to the current profile. Within the user's authorization, it can verify the task package, identity, model, permissions, working directory, and input readiness before deciding whether to dispatch. Users do not need to inspect every CLI window or fill in JSON. The owner can then inspect status, receive background notifications, and acknowledge that a notification was consumed.

This is intended for local workflows where a Hermes owner coordinates Codex, OpenCode, or Claude Code CLI work within the user's authorization. Relay does not plan, authorize, or dispatch independently of the current profile's legitimate owner and gate; Hermes may complete routine checks and advance the workflow under existing authorization. Pause for an explicit user decision on dangerous, destructive, out-of-scope, or new-architecture choices. Relay does not provide shared ownership across profiles, a globally unbypassable sandbox, or automatic hard token limits. It does not guarantee that a business task will succeed.

| Role | Responsibility |
| --- | --- |
| Hermes main agent | Understands the request, registers evidence, checks gate results, and coordinates the workflow. Relay does not restrict its model. |
| Relay | Binds one profile owner, records gated CLI lifecycle actions, observes status, and delivers notifications. |
| Herdr CLI | Creates tabs, starts agents, reads identity and status, and submits task prompts. |
| Owner | The Hermes main agent bound to the current profile; verifies the real model, permissions, working directory, and readiness within authorization, then follows the gate result. |
| User | Provides the task and authorization, makes explicit decisions on dangerous, destructive, out-of-scope, or new-architecture choices, and reviews the final work. |

### 2. Requirements and verified environment

Version observations from the November 2026 installation-verification environment are Linux, Hermes Agent v0.22.5 (SDK commit bf0203a794a0), Python 3.14.9, Herdr 0.9.5, Codex 0.167.0, OpenCode 2.2.0, and Claude Code 2.1.319. These are records from that environment, not a full compatibility matrix, universal minimum versions, or a compatibility promise for other systems. The corresponding CLI must be installed and logged in, and its configured model and provider must be available. Installing this plugin does not log you in or include model credits.

`TYPESAFE_API_KEY` is used for Relay's Jev gate request. It is not a general model key for Codex, OpenCode, Claude Code, or Hermes model providers. Gate evaluation cannot proceed normally without it; status and notification tools may still work. Configure the secret only in the current Hermes profile's secure settings. The verified binding platform allowlist is `local`, `cli`, `tui`, and `desktop`; do not infer support for remote chat channels from this list.

### 3. First native installation (after the plugin source is published)

After confirming that the selected repository revision contains the `hermes_plugin/relay/` plugin source, run this command in a normal terminal where Hermes is installed:

```bash
# 从 GitHub 仓库的插件子目录安装并启用
hermes plugins install "wenhao4126/hermes-relay-plugin/hermes_plugin/relay" --enable
```

The installer reads the plugin name `relay` from the repository subdirectory manifest, so the installation directory target is `relay`; the repository-root README is not part of the installed plugin. The expected post-install check is that Doctor recognizes the plugin; then enable the Relay toolset as needed and configure the profile. Do not use dynamic PASS timestamps, download counts, or badges in place of a version-specific acceptance record.

### 4. Configure, enable, and load safely

A normal Shell is the terminal where a user runs Hermes commands. The Hermes chat box is the conversation input for the agent. Administrative commands pasted into the chat may be interpreted as task text, so the user should run these in a normal Shell:

```bash
# 检查当前插件状态
hermes plugins doctor relay --ci
# 按需让 relay 工具集对 CLI 平台可用
hermes tools enable relay --platform cli
# 启用插件并明确关闭替换内置工具的授权
hermes plugins enable relay --no-allow-tool-override
# 仅在用户明确授权后台 Gateway 通知时设置注入开关
hermes config set plugins.entries.relay.allow_gateway_injection true
```

`TYPESAFE_API_KEY` is the Relay Jev gate key, not an agent model-provider key. The Hermes SDK secret scope loads `.env` from the **current profile directory** and merges configured secret sources; the plugin itself only calls `get_secret` to obtain `TYPESAFE_API_KEY` and does not read `.env` directly. Do not put the key in an arbitrary directory, plaintext `config.yaml`, chat history, command arguments, or Git. This is placeholder-only:

```dotenv
# 在当前 Hermes profile 的 .env 中由用户安全设置；替换占位值，不要提交文件
TYPESAFE_API_KEY=<YOUR_TYPESAFE_API_KEY>
```

Plugin installation does not log you in or include model credits. `--no-allow-tool-override` explicitly disables Relay's authorization to replace Hermes built-in tools; `--enable` by itself does not grant every permission. Gateway injection is a separate optional setting that lets the plugin deliver background notifications through the current Gateway. The user must explicitly authorize it, and background notifications depend on that setting.

If a Gateway reload is needed after changing files or settings, first save other active tasks, then run this in a separate normal Shell:

```bash
# 在保存其他活动任务后，于独立普通 Shell 中执行
hermes gateway restart
```

Do not ask an assistant that depends on the current Gateway to restart it through that same connection; the session could be interrupted. Plugin enablement and actual Gateway loading are separate states. Check Doctor and `/relaystatus` again in the current profile.

### 5. First chat and what results mean

In the Hermes chat, bind and check status:

```text
/relaybind
/relaystatus
```

After binding, describe the request in plain language, for example: “Check the evidence and gate for this task. If the gate allows it, verify the new tab's identity, model, permissions, directory, and input readiness under my authorization, then dispatch once. Stop and ask me about dangerous, destructive, out-of-scope, or new-architecture decisions. Report the status when done; do not submit it again.” Users do not need to fill in complex JSON or inspect each window. Hermes can perform routine checks and advance within existing authorization.

The following is a workflow illustration, not output from an execution:

```text
Illustration: creation returns CREATED_UNVERIFIED → Hermes owner verifies the real environment within authorization → dispatch returns SUBMITTED → observation and notifications follow → owner / user accept the task result according to their roles
```

These are distinct stages. Successful creation means the empty CLI identity was read back. The Hermes owner verifies the actual model and permissions within its authorization. `SUBMITTED` means only that the submission receipt was accepted. Consuming a notification means only that it was ACKed. Task completion and final engineering acceptance still require inspection of the actual work and evidence. `/relaystatus` shows a summary visible to the current owner.

### 6. Ten tools and normal order

| Tool | Purpose |
| --- | --- |
| `relay_record_document` | Registers a task evidence statement and source fields. |
| `relay_evaluate_task` | Sends the complete task to the Jev gate for evaluation. |
| `relay_adjudicate_task` | Records explicit owner adjudication only when the original rules allow it, with a reason and evidence reference; it is not a user override of the rules. |
| `relay_create_agent` | Creates an empty CLI through native Herdr, without task content. |
| `relay_refresh_agent` | Reads identity and status for a recorded tab. |
| `relay_dispatch_task` | Submits one task after gate, owner, and verification requirements are met. |
| `relay_watch_task` | Registers a task and instance for observation. |
| `relay_unwatch_task` | Explicitly stops the corresponding observation record. |
| `relay_ack_event` | Confirms that the current owner consumed a notification. |
| `relay_plugin_status` | Reads binding and status summaries. |

The normal sequence is: register evidence; pass the Jev task-clarity gate; allow an explicit owner decision only when the original rules permit it; create an empty CLI; have the Hermes owner verify identity, model, effort, permissions, directory, and input readiness within authorization; dispatch once; observe status; ACK the notification after completion, then explicitly unwatch when appropriate. A successful dispatch starts observation tracking. ACK does not release the dispatch reservation, and unwatch stops observation without closing or deleting the CLI. Pause for user decisions on dangerous, destructive, out-of-scope, or new-architecture choices.

`UNKNOWN`, `PREPARED`, and `SENDING` mean that the outcome is unresolved or still processing. Do not blindly retry with a new `request_id`, create another agent, or try to clear the reservation. Check the original record first. Explicit owner adjudication cannot bypass `REVISE`, and a gate result of `UNKNOWN` is not approval.

### 7. CLI models and permission checks

Relay does not constrain the Hermes main agent's model. Codex `worker` and `fixer` requests use `gpt-6-luna`, with effort set to `high` or `max`. OpenCode and Claude Code use each CLI's native default model; Relay does not force DeepSeek. `cli_default_unverified` means “using the CLI default, not yet verified.” It is not a model name or proof of login. Existing agents do not switch models automatically when the plugin is upgraded.

The requested model and effort do not prove the values actually in effect; CLI settings or the Shell environment may override effort. The Hermes owner must verify the new tab's real model, effort, permissions, working directory, agent identity, and readiness, using available status and environment information within authorization. Stop dispatch if any value conflicts with the intended role policy. OpenCode and Claude Code default models are not forced at creation. Claude reviewers and auditors use read-only tool restrictions and a strict empty MCP configuration, but the owner must still verify the actual runtime state.

### 8. Owner, profile, and epoch

Each Hermes profile has one Relay owner: the Hermes main agent bound to that profile. Two Herdr windows do not create an isolation boundary. Use separate profiles when ownership and state must be separated. When returning to an old chat, run `/relaystatus` first, then use ordinary `/relaybind` if needed. Binding again as the same identity can keep the epoch, but it replaces the current Runtime. Avoid repeated binds while a workflow is running. Use `/relaybind --replace` only for an explicit owner handoff by the user; the new identity advances the epoch.

New agent creation records use the current epoch. Old creation records keep their old epoch; they are not migrated automatically, and their tabs are not stopped or their tasks redispatched. Old requests may fail to refresh with `epoch_changed`. A new owner considering an old empty tab must verify it again and follow the explicit continuation process for tasks that were never dispatched. Stop when a dispatch is unresolved; do not bypass protection with a new ID.

### 9. Upgrades and existing manual installations

If a manually installed `relay` directory already exists, do not treat it as a successful first installation and do not repeat the first-install command. Preserve the existing directory, configuration, and ledger. A maintainer should migrate it into native management using a backup and isolated verification process. Manual-directory migration has not been verified. Do not improvise a `--force` migration, remove and reinstall, delete plugin data, or clear the ledger.

After a native Git installation, `hermes plugins update relay` applies only to an installation whose source can be updated and is not pinned to a SHA. A pinned SHA does not move through an ordinary update. Selecting a newly accepted SHA requires an explicit maintainer action bound to the full SHA. Do not guess a migration command when it has not been verified. After a future update, safely reload the Gateway from a separate normal Shell as described above, after saving other active tasks.

### 10. Effects, limits, and non-writing checks

Background task notifications do not make agent turns appear automatically in the classic Hermes CLI foreground. Status code `0` or `SUBMITTED` does not mean the business workflow passed end to end. Creation history is limited to 64 records per profile and has no automatic garbage collection. The gate, Runtime, Dispatcher, Observer, and external Herdr calls execute separately; there is no guarantee of global atomicity across files or Herdr. `UNKNOWN` is not retried automatically. The plugin is not a globally unbypassable sandbox and does not enforce an automatic hard token limit.

Start first-run checks with non-writing actions: inspect repository files and the version baseline; run Doctor after installation; confirm toolset and plugin enablement; run `/relaystatus` in chat to inspect owner and notification summaries; confirm that the profile secret exists without displaying its value. Before testing a real paid model or dispatching a real task, get explicit authorization. Prefer a limited, observable, non-sensitive task with a reversible outcome; real paid business work is not a default smoke test.

### 11. Common issues and troubleshooting

Check status first, then choose the next action:

1. Tool missing: in a normal Shell, check Doctor, plugin enablement, and whether the `relay` toolset was enabled for the needed platform.
2. `api_key_unavailable`: confirm `TYPESAFE_API_KEY` in the current profile's secure settings; never paste its value into chat.
3. `not_owner`: check whether this chat is still the bound owner; do not replace the owner casually.
4. `epoch_changed`: verify the owner handoff and old record ownership; do not edit the ledger or guess a migration.
5. `agent_busy` or `agent_busy_unknown`: inspect existing dispatch and observation records. Do not retry an unknown state with a new ID.
6. Model or effort mismatch: have the current Hermes owner check actual CLI settings, Shell overrides, and tab permissions; stop dispatch until verified. The user does not need to inspect each window.
7. First install reports that the directory exists: preserve the directory, configuration, and ledger, and ask a maintainer to follow the migration process. Do not remove and reinstall.
8. `SUBMITTED` but the task seems unfinished: it is a submission receipt, not completion. Inspect the background tab, observation notifications, and actual work product.
9. Gateway is inactive after an update: save other active tasks, run `hermes gateway restart` in a separate normal Shell, then check Doctor and status. Do not have an assistant that depends on that Gateway restart it.

Closing an agent, uninstalling the plugin, and upgrading the plugin are separate actions. Uninstalling does not automatically close existing CLIs or delete the ledger. Preserve the ledger to investigate unresolved states. Do not run destructive cleanup commands to “fix” a status.

### 12. Security, repository layout, and references

Do not commit secrets, authentication files, real task ledgers, session IDs, workspace IDs, or private machine paths. Use generic paths and clearly marked placeholders in examples. Relay's owner checks and `owner_checks` are based on owner declarations and current profile state; they are not a globally unbypassable permission boundary. Keep tester, reviewer, and auditor task packages short and specific, save full logs locally, and hand off only summaries and important differences. The product has no automatic token limit.

The repository's `hermes_plugin/relay/` directory contains plugin source. The root `README.md` explains installation and use for repository users; the existing Chinese README inside the plugin remains internal workflow material. GitHub repository: <https://github.com/wenhao4126/hermes-relay-plugin>. Hermes official plugin documentation: <https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins>. Version-specific Hermes v0.22.5 documentation: <https://github.com/nousresearch/hermes-agent/blob/v0.22.5/website/docs/user-guide/features/plugins.md>.
