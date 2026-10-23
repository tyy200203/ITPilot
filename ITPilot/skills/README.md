# ITPilot Skills 文档

ITPilot 启动时从 `ITPILOT_SKILLS_DIR` 扫描 Skills，并按 Agent 类型、用户消息关键词和 `enabled` 开关决定是否注入 system prompt。Skill 描述业务处理规范，不负责查询事实，也不代表后台操作已经执行。

当前内置四类 IT Skills：

```text
skills/it_service_desk/SKILL.md  # GeneralAgent：IT 接待、澄清和分流
skills/technical_support/SKILL.md # TechnicalAgent：设备、网络和软件排障
skills/permission_support/SKILL.md # PermissionAgent：账号、权限和审批边界
skills/it_escalation/SKILL.md     # EscalationAgent：IT 工单分级和交接
```

## Skill 文件格式

每个 Skill 使用独立目录，主文件命名为 `SKILL.md`：

```text
skills/<skill_name>/SKILL.md
```

文件顶部使用轻量 front matter。多个值使用逗号分隔：

```markdown
---
name: 账号权限申请规范
description: 适用于 PermissionAgent 的账号和权限处理规范
keywords: 权限,GitLab,VPN权限,企业邮箱,工号
agents: permission
enabled: true
---
```

字段说明：

- `name`：Skill 展示名称。
- `description`：供 `/skills` 接口展示的简短说明。
- `keywords`：触发关键词；为空时对指定 Agent 的所有请求生效。
- `agents`：可填 `general`、`technical`、`permission`、`escalation`。
- `enabled`：是否启用，支持 `true/false`。

## 编写要求

- 重要规则放在前半部分，因为加载器会限制单个 Skill 和总 prompt 长度。
- 一类 Skill 只描述一个角色的业务规范，不混入其他角色的操作流程。
- 必须说明角色定位、处理流程、升级条件、安全边界和禁止事项。
- 禁止要求员工提供密码、验证码、完整 Token、完整密钥或私钥。
- 未接入真实系统时，必须明确“草稿”“未提交”“未授权”“未查询”。
- 设备、权限和工单事实必须来自用户输入或真实工具结果，不能由模型编造。

## 热加载

修改 Skill 文件后调用：

```bash
curl -X POST http://localhost:8000/skills/reload
```

查看加载结果和解析错误：

```bash
curl http://localhost:8000/skills
```

预期加载四个 Skill，`errors` 应为空。
