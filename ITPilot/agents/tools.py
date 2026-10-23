"""Agent 工具定义与实现。

所有 Agent 工具集中在这里，编排器只负责：
  1. 根据 Agent 类型暴露工具白名单
  2. 执行 LLM 返回的 tool_use
  3. 将工具结果回传给 LLM

工具本身保持确定性、可测试，并明确区分：
  - 当前请求分析
  - 设备、网络和软件排障
  - 权限申请字段核验
  - IT 工单草稿
  - 共享知识库 RAG

资产查询、权限开通和工单提交等需要真实企业系统授权的动作不在这里伪造。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from agents.agent_orchestrator import Request


AgentToolHandler = Callable[["Request", Dict[str, Any]], Union[Any, Awaitable[Any]]]


@dataclass(frozen=True)
class AgentToolSpec:
    """Agent 可见工具的定义和执行函数。"""

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: AgentToolHandler


def make_tool(
    name: str,
    description: str,
    properties: Dict[str, Any],
    handler: AgentToolHandler,
    required: Optional[List[str]] = None,
) -> AgentToolSpec:
    """创建带 JSON Schema 的 Agent 工具。"""
    return AgentToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
        handler=handler,
    )


def inspect_request_context(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """IT 服务台工具：返回脱敏后的当前请求快照。"""
    return {
        "intent": req.intent.value if req.intent else None,
        "intent_group": req.intent_group,
        "urgency": req.urgency.name if req.urgency else None,
        "intent_confidence": round(req.intent_confidence, 4),
        "entities": req.entities or {},
        "context_available": bool(req.context),
        "requested_focus": str(args.get("focus", "general"))[:40],
    }


def suggest_required_fields(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """IT 服务台工具：按意图计算下一轮只需询问的字段。"""
    intent = req.intent.value if req.intent else "other"
    fields: List[str] = []
    if intent == "device_failure":
        fields = ["资产编号", "设备类型", "故障现象"]
    elif intent == "network_issue":
        fields = ["网络类型", "错误码", "影响范围"]
    elif intent == "software_install":
        fields = ["软件名称", "操作系统版本", "资产编号"]
    elif intent == "permission_request":
        fields = ["工号", "目标系统", "权限类型", "业务理由"]
    elif intent == "account_provisioning":
        fields = ["工号", "员工信息", "目标系统"]
    elif intent == "it_ticket":
        fields = ["故障现象", "影响范围", "联系方式"]
    elif intent in {"account", "account_security"}:
        fields = ["工号或账号标识", "目标系统", "问题发生时间"]
    elif intent in {"complaint", "request"}:
        fields = ["事件时间", "期望处理方式"]
    elif intent == "other":
        fields = ["希望解决的具体问题"]
    return {
        "intent": intent,
        "required_fields": fields,
        "known_entities": req.entities or {},
    }


def lookup_error_code(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """技术工具：解释常见 IT 错误码，不声称读取了企业后台或日志。"""
    code = str(args.get("error_code", "")).upper().strip()
    mapping = {
        "691": ("VPN 身份验证失败", ["确认账号未锁定或过期", "重新输入用户名和密码", "确认 VPN 域和认证方式"]),
        "651": ("网络连接或拨号适配器异常", ["检查网线或无线连接", "重启网络适配器", "确认驱动和拨号配置"]),
        "720": ("VPN 协议或 TCP/IP 配置异常", ["重建 VPN 连接", "检查 TCP/IP 协议栈", "确认客户端版本和系统兼容性"]),
        "401": ("认证失败", ["确认 Token/API Key 是否过期", "确认请求时间戳和签名", "确认账号登录状态"]),
        "403": ("权限不足", ["确认账号和目标资源权限", "确认访问范围和 IP 白名单"]),
        "404": ("资源或路径不存在", ["确认接口路径和环境", "确认资源标识是否正确"]),
        "500": ("服务端处理异常", ["记录 request_id 和发生时间", "检查依赖服务、参数格式和服务端日志"]),
        "0X80070005": ("Windows 拒绝访问", ["确认当前账号权限", "检查文件或目录 ACL", "如需管理员权限则提交审批"]),
    }
    meaning, steps = mapping.get(
        code,
        ("暂未识别的错误码", ["补充完整错误信息、发生时间和运行环境"]),
    )
    return {
        "error_code": code,
        "meaning": meaning,
        "next_steps": steps,
        "server_log_checked": False,
    }


def build_diagnostic_plan(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """技术工具：结合当前 IT 实体生成低风险排障顺序。"""
    environment = str(args.get("environment", "unknown"))[:80]
    reproduced = bool(args.get("reproduced", False))
    steps = [
        "复现并记录完整错误信息",
        "确认设备、操作系统和软件版本",
        "确认网络、DNS、代理、VPN 和证书",
        "确认配置与当前账号权限",
    ]
    if reproduced:
        steps.append("用最小请求复现并记录 request_id")
    return {
        "environment": environment,
        "reproduced": reproduced,
        "asset_ids": req.entities.get("asset_id", []),
        "device_types": req.entities.get("device_type", []),
        "os_versions": req.entities.get("os_version", []),
        "software_names": req.entities.get("software_name", []),
        "error_codes": req.entities.get("error_code", []),
        "diagnostic_steps": steps,
    }


def check_device_context(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """检查设备排障上下文是否齐全，不查询或伪造企业资产信息。"""
    fields = {
        "asset_id": bool(req.entities.get("asset_id")),
        "device_type": bool(req.entities.get("device_type")),
        "os_version": bool(req.entities.get("os_version")),
        "problem_description": bool(req.message.strip()),
    }
    return {
        "fields": fields,
        "missing_fields": [name for name, present in fields.items() if not present],
        "ready_for_diagnosis": fields["device_type"] and fields["problem_description"],
        "asset_system_checked": False,
        "reason": "当前工具只检查用户已提供的信息，不连接企业资产管理系统",
    }


def check_permission_fields(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """检查权限申请的最少必要字段，不查询账号目录或审批系统。"""
    employee_ids = req.entities.get("employee_id", [])
    software_names = req.entities.get("software_name", [])
    target_system = str(args.get("target_system", "")).strip() or (software_names[0] if software_names else "")
    fields = {
        "employee_id": bool(employee_ids),
        "target_system": bool(target_system),
        "permission_type": bool(str(args.get("permission_type", "")).strip()),
        "business_reason": bool(str(args.get("business_reason", "")).strip()),
        "duration": bool(str(args.get("duration", "")).strip()),
    }
    return {
        "fields": fields,
        "missing_fields": [name for name, present in fields.items() if not present],
        "employee_ids": employee_ids,
        "target_system": target_system,
        "directory_checked": False,
        "approval_required": True,
    }


def build_permission_request(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """生成权限申请草稿；明确标记尚未授权、尚未提交。"""
    checked = check_permission_fields(req, args)
    return {
        "status": "draft",
        "employee_ids": checked["employee_ids"],
        "target_system": checked["target_system"],
        "permission_type": str(args.get("permission_type", "")).strip(),
        "business_reason": str(args.get("business_reason", "")).strip(),
        "duration": str(args.get("duration", "")).strip(),
        "missing_fields": checked["missing_fields"],
        "approval_required": True,
        "actually_submitted": False,
        "actually_granted": False,
    }


def build_it_ticket(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """生成可交给 IT 支持人员的结构化工单草稿，不提交真实工单。"""
    return {
        "ticket_status": "draft",
        "actually_created": False,
        "request_id": req.request_id,
        "reason": str(args.get("reason", "需要 IT 支持人员继续处理"))[:120],
        "intent": req.intent.value if req.intent else "unknown",
        "urgency": req.urgency.name if req.urgency else "UNKNOWN",
        "employee_ids": req.entities.get("employee_id", []),
        "asset_ids": req.entities.get("asset_id", []),
        "device_types": req.entities.get("device_type", []),
        "os_versions": req.entities.get("os_version", []),
        "software_names": req.entities.get("software_name", []),
        "error_codes": req.entities.get("error_code", []),
        "problem_description": req.message,
        "impact_scope": str(args.get("impact_scope", "未提供"))[:120],
        "contact": str(args.get("contact", "未提供"))[:80],
        "sensitive_data_required": False,
    }


def build_shared_rag_tools(tool_manager: Any) -> Dict[str, AgentToolSpec]:
    """构建所有 Agent 可共享的 RAG 工具。"""

    async def search_knowledge_base(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or req.message or "").strip()
        top_k = int(args.get("top_k", 5) or 5)
        if not query:
            return {"success": False, "error": "query 不能为空", "results": []}
        if tool_manager is None:
            return {"success": False, "error": "RAG 工具未初始化", "results": []}

        result = await tool_manager.search_with_rewrite(
            "knowledge_search",
            query,
            top_k=top_k,
        )
        if not getattr(result, "success", False):
            return {
                "success": False,
                "query": query,
                "error": getattr(result, "error", "知识库检索失败"),
                "results": [],
                "reranked": False,
            }

        return {
            "success": True,
            "query": query,
            "top_k": top_k,
            "results": result.data,
            "reranked": bool(getattr(result, "reranked", False)),
        }

    return {
        "search_knowledge_base": make_tool(
            "search_knowledge_base",
            "检索知识库并返回最相关的文档片段；可用于 IT 咨询、技术排障、权限和工单场景。",
            {
                "query": {"type": "string", "description": "用户问题或检索关键词"},
                "top_k": {"type": "integer", "description": "返回结果条数"},
            },
            search_knowledge_base,
            required=["query"],
        )
    }


def general_tools() -> Dict[str, AgentToolSpec]:
    return {
        "inspect_request_context": make_tool(
            "inspect_request_context",
            "查看当前请求的意图、紧急度、实体和上下文可用性；不查询外部业务系统。",
            {"focus": {"type": "string", "description": "希望关注的业务方向"}},
            inspect_request_context,
        ),
        "suggest_required_fields": make_tool(
            "suggest_required_fields",
            "根据当前意图建议下一轮只需向用户补充的字段。",
            {},
            suggest_required_fields,
        ),
    }


def technical_tools() -> Dict[str, AgentToolSpec]:
    return {
        "lookup_error_code": make_tool(
            "lookup_error_code",
            "解释常见 VPN、Windows 和 HTTP 错误码的含义与低风险排查方向；不会读取后台日志。",
            {"error_code": {"type": "string", "description": "例如 691、720、401、0x80070005"}},
            lookup_error_code,
            required=["error_code"],
        ),
        "build_diagnostic_plan": make_tool(
            "build_diagnostic_plan",
            "根据运行环境和是否可复现生成排障顺序，不执行修改配置等操作。",
            {
                "environment": {"type": "string", "description": "App、浏览器、服务端或 Docker 等"},
                "reproduced": {"type": "boolean", "description": "问题是否可以稳定复现"},
            },
            build_diagnostic_plan,
            required=["environment", "reproduced"],
        ),
        "check_device_context": make_tool(
            "check_device_context",
            "检查当前请求是否包含资产编号、设备类型、操作系统和故障描述；不连接资产系统。",
            {},
            check_device_context,
        ),
    }


def permission_tools() -> Dict[str, AgentToolSpec]:
    return {
        "check_permission_fields": make_tool(
            "check_permission_fields",
            "检查权限申请所需的工号、目标系统、权限类型、业务理由和期限；不查询目录或审批系统。",
            {
                "target_system": {"type": "string", "description": "需要访问的系统或资源"},
                "permission_type": {"type": "string", "description": "需要申请的权限类型"},
                "business_reason": {"type": "string", "description": "申请权限的业务理由"},
                "duration": {"type": "string", "description": "永久、临时或具体截止时间"},
            },
            check_permission_fields,
        ),
        "build_permission_request": make_tool(
            "build_permission_request",
            "根据用户已提供的信息生成权限申请草稿；不会提交申请或实际授予权限。",
            {
                "target_system": {"type": "string", "description": "需要访问的系统或资源"},
                "permission_type": {"type": "string", "description": "需要申请的权限类型"},
                "business_reason": {"type": "string", "description": "申请权限的业务理由"},
                "duration": {"type": "string", "description": "永久、临时或具体截止时间"},
            },
            build_permission_request,
        ),
    }


def escalation_tools() -> Dict[str, AgentToolSpec]:
    return {
        "build_it_ticket": make_tool(
            "build_it_ticket",
            "生成交给 IT 支持人员的结构化工单草稿；不会向真实 ITSM 系统提交工单。",
            {
                "reason": {"type": "string", "description": "需要升级的原因"},
                "impact_scope": {"type": "string", "description": "影响个人、部门或全公司"},
                "contact": {"type": "string", "description": "用户愿意提供的联系方式"},
            },
            build_it_ticket,
        ),
    }
