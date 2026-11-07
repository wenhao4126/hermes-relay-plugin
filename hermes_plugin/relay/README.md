# Hermes Relay 插件

本目录提供 Hermes v0.22.5 Python 功能插件：原主控绑定、状态与消费回执、任务资料登记和 Jev 门禁、已存在任务的受控派发，以及在独立 Herdr 标签中创建原生空 CLI。

该插件不属于仓库历史 README 所述的 `astra-relay` CLI/服务。部署时将 `hermes_plugin/relay` 目录放入当前 Hermes profile 的 `plugins/relay` 目录，并按当前安装版的插件管理方式启用；不要把本地目录当作 `hermes plugins install` 的 identifier。此说明不修改或选择默认 profile。

## 前置条件

- Hermes Agent v0.22.5，Herdr CLI 0.9.5。
- TypeSafe 评估需要在 Hermes 的 profile secret 配置中提供 `TYPESAFE_API_KEY`。插件通过 `agent.secret_scope.get_secret("TYPESAFE_API_KEY")` 在工具调用线程读取；清单中的 secret/env 字段只引用该变量。缺少密钥只会阻止评估，不会阻止状态、绑定或回执工具加载。
- 每个 Hermes profile 使用自己的数据目录、Runtime 账本、观察账本、派发账本、创建账本和门禁记录。

## 使用顺序

1. 在本地会话执行 `/relaybind`，把当前可信会话绑定为该 profile 的主控。只有显式 `--replace` 才会换主控。
2. 用 `relay_record_document` 登记任务引用的资料；随后用 `relay_evaluate_task` 对同一份 v2 任务包和 Jev 模型执行一次门禁。
3. 只有门禁 PASS 或符合原规则的显式 ESCALATE 裁决，才能调用 `relay_create_agent` 创建**空 CLI**。该动作不会把任务正文发给新代理。
4. 原主控按已确认的角色策略检查新标签中的实际模型、effort、权限、工作目录、代理身份和就绪状态；任一项不符就停止派发。requested model/effort 只是启动请求，真实环境可能覆盖 effort，请求 `high` 不代表实际 `high`。
5. 同一主控代次内可用 `relay_refresh_agent` 只读刷新原创建记录；核实完成后再用原有 `relay_dispatch_task` 单次提交任务。更换主控后，刷新旧创建请求会返回 `epoch_changed`；如代理从未派发，可按下文由新主控重新核验原 binding 后接续。
6. Observer 按 profile 内的代理身份观察进度，通知通过原绑定主控 Gateway 投递。确认本轮任务结束后，先用原 `event_id` 调用 `relay_ack_event` 消费通知，再调用 `relay_unwatch_task`，参数严格只有 `task_id`、`instance_id`（从 `SUBMITTED` 回执或观察记录原样取得）。确认 `removed: true` 后，才能给同一代理派发新的不同 `task_id`；ACK 本身不停止观察或释放占位。

如需完整参数表、门禁结果解释和异常处理，请读项目根目录的 `docs/hermes-relay-use.md`。

## 角色请求

- `worker` / `fixer` 请求 Codex `gpt-6-luna`，effort 只能是 `high` 或 `max`。会话指令限制为只执行已批准任务，不启动子代理、不改架构。
- `tester` 启动原生 OpenCode，不传模型、agent、自动模式参数或模型环境变量。新请求标记 `requested_model=cli_default_unverified`，表示实际模型未核实；effort 请求值为 `high`，但 OpenCode 不接收插件传入的模型或 effort 参数。
- `reviewer` / `auditor` 不传 Claude `--model`，由 Claude Code 的原生配置选择默认模型；新请求同样标记 `requested_model=cli_default_unverified`。原有 `high` effort、只读工具和严格空 MCP 参数保持不变，实际模型仍由原主控核实。CLI 登录认证由本机原生配置处理，插件不读取或复制凭据。

`cli_default_unverified` 是“沿用 CLI 默认、尚未核实”的标记，不是模型名称。插件不设置、清除或注入模型环境变量；原主控必须核对实际模型、effort、权限和工作目录，不符合角色策略就停止派发。历史回执中的 DeepSeek 请求名称只作为旧记录读取，不会被改写。

### 辅助角色的上下文使用建议

给 tester、reviewer 或 auditor 的任务包保持短而具体；相关命令可以合并执行。完整日志保存到文件，交接时只给摘要和关键差异。审核分段进行，并在对应任务记录中归档任务边界、证据和结论。这些是使用建议，不会给插件增加自动 token 限额或新的状态规则。

所有创建成功结果仍带 `needs_owner_verification: true` 和 `CREATED_UNVERIFIED`。这表示新空 CLI 的 Herdr 身份与 readiness 已通过只读回读，不表示模型、effort、登录、权限或任务结果已认证。`SUBMITTED` 记录在观察仍启用时返回 `agent_busy`；`PREPARED`、`SENDING`、`UNKNOWN` 不得通过 unwatch 或更换 request 绕过 `agent_busy_unknown`。只升 revision 不能复活已退役任务。

### 更换主控后接续未派发的代理

旧 epoch 的创建回执不能由新主控通过 `relay_refresh_agent` 刷新；`epoch_changed` 表示旧创建记录仍属于原代次。若该代理确实从未调用过 `relay_dispatch_task`，且没有该代理的未结清派发占位，新主控可以继续核验后派发同一代理。先确认同 profile 的原 v2 任务和 `jev_model` 仍有有效门禁批准，再按主控既有流程核对代理的完整身份及实际模型、effort、权限、工作目录和就绪状态；`owner_checks` 由新主控据此重新声明。启动请求中的模型或 effort 不能代替实际核验。

从原 `relay_create_agent` 成功回执或可信历史记录原样取得完整七字段 `binding`：`herdr_session`、`name`、`agent_kind`、`workspace_id`、`tab_id`、`pane_id`、`agent_session_id`。不要从 `request_id` 推算身份字段，也不要改账本或要求用户手写 JSON。以新的唯一派发 `request_id` 调用 `relay_dispatch_task`；插件会重新读取实际代理身份并与完整 binding 比对。派发成功后仍按“任务结束确认 → `relay_ack_event` → `relay_unwatch_task`”顺序处理通知和观察。

若已有 `UNKNOWN`、`PREPARED`、`SENDING`、跨 epoch 未结清派发或 `agent_busy_unknown`，停止并核实原记录。不要换 request ID、重复创建或把旧 refresh 拒绝当作清除占位的方式。旧创建记录留在原 epoch，后续派发记录属于新 epoch；两者不会被迁移或改写。

## 运行边界

派发请求未知时不会自动重试或另建标签。状态文件损坏、资源上限、权限失效或身份不匹配会返回固定错误码。多个独立写入和外部 CLI 调用之间不构成全局原子事务。卸载插件不会清除 profile 账本，也不会杀死已创建的原生 CLI。后台回合不会自动显示在经典 Hermes CLI 界面。

单元测试中的 fake requester、runner 和 Gateway 回调只验证组件行为，与真实环境联调和独立产品验收分层。整产品 PASS 以交付附带的独立验收记录为准；本 README 不记录动态审核或放行状态。
