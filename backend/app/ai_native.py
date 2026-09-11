"""System-wide AI assistant backed by generic, registry-validated tool calls."""
import json
import os
import re

import requests

from .ai_capabilities import GENERIC_FUNCTIONS, prompt_catalog, public_registry, validate_action


FUNCTION_OPERATIONS = {
    "respond_to_user": "answer",
    "navigate_tool": "navigate", "configure_tool": "configure", "create_task": "create",
    "control_task": "control", "query_tool": "query", "export_result": "export",
}


def _json_object(text):
    text = str(text or "").strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1]).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        return json.loads(match.group(0)) if match else {"answer": text}


def _tool_result(message):
    calls = message.get("tool_calls") if isinstance(message, dict) else None
    for call in calls or []:
        function = call.get("function") if isinstance(call, dict) else None
        name = function.get("name") if isinstance(function, dict) else ""
        operation = FUNCTION_OPERATIONS.get(name)
        if not operation:
            continue
        raw = function.get("arguments") or "{}"
        arguments = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(arguments, dict):
            raise ValueError("AI 工具参数格式错误")
        if operation == "answer":
            answer = str(arguments.get("answer") or "").strip()
            if not answer:
                raise ValueError("AI 答复为空")
            return {"answer": answer, "suggestions": arguments.get("suggestions") or []}
        tool_id = str(arguments.get("tool_id") or "")
        if operation == "control":
            parameters = {"task_id": arguments.get("task_id"), "action": arguments.get("action")}
        elif operation == "query":
            parameters = arguments.get("parameters") or {}
        elif operation == "export":
            parameters = {"task_id": arguments.get("task_id")}
        else:
            parameters = arguments.get("parameters") if operation != "navigate" else {}
        normalized = validate_action(operation, tool_id, parameters)
        manifest = public_registry()[tool_id]
        content = str(message.get("content") or "").strip()
        if operation == "navigate":
            fallback = f"已为你找到“{manifest['name']}”。"
            if manifest.get("manual_reason"):
                fallback += f"{manifest['manual_reason']}。"
            return {"answer": content or fallback, "navigate_to": tool_id}
        if operation == "configure":
            return {
                "answer": content or f"已生成“{manifest['name']}”配置，可应用到页面后继续检查。",
                "navigate_to": tool_id,
                "config_patch": {"target": tool_id, "summary": f"配置{manifest['name']}", "values": normalized["parameters"]},
            }
        action = {key: normalized[key] for key in ("operation", "tool_id", "parameters", "requires_confirmation", "label")}
        fallback = f"已准备好“{manifest['name']}”操作，请确认后执行。" if normalized["requires_confirmation"] else f"正在读取“{manifest['name']}”的真实数据。"
        result = {"answer": content or fallback, "navigate_to": tool_id, "action": action}
        if operation == "create":
            result["config_patch"] = {"target": tool_id, "summary": f"创建{manifest['name']}任务", "values": normalized["parameters"]}
        return result
    return _json_object(message.get("content", "") if isinstance(message, dict) else "")


def _planned_result(plan):
    """Run a model-produced structured plan through the exact same registry path.

    Some OpenAI-compatible providers intermittently ignore ``tool_choice`` on
    follow-up turns.  This is a protocol adapter, not an intent parser: the
    model must still name one registered function and provide its arguments.
    """
    if not isinstance(plan, dict):
        raise ValueError("结构化计划格式错误")
    name = str(plan.get("function") or plan.get("name") or "").strip()
    if not name:
        answer = str(plan.get("answer") or "").strip()
        if answer:
            return {"answer": answer, "suggestions": plan.get("suggestions") or []}
    arguments = plan.get("arguments")
    if name not in FUNCTION_OPERATIONS or not isinstance(arguments, dict):
        raise ValueError("结构化计划未选择已注册函数")
    return _tool_result({
        "content": str(plan.get("answer") or "").strip(),
        "tool_calls": [{"function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}],
    })


def _request_structured_plan(url, headers, body, agent_messages, last_error, require_function=False):
    function_specs = {
        item["function"]["name"]: item["function"]["parameters"]
        for item in GENERIC_FUNCTIONS
    }
    requirement = "必须选择并返回一个已注册函数，不能只返回 answer。" if require_function else "普通对话可以只返回 answer。"
    instruction = f"""标准 Function Calling 未被当前模型服务正确返回：{last_error}。
请重新规划，并只输出一个 JSON 对象，不要 Markdown：
{{"function":"已注册函数名","arguments":{{...}},"answer":"给用户的简洁说明"}}
可用函数及参数 schema：{json.dumps(function_specs, ensure_ascii=False)}
必须依据 schema 选择函数和参数，不得虚构；副作用仍由系统确认。{requirement}"""
    fallback_body = {
        "model": body["model"], "messages": [*agent_messages, {"role": "user", "content": instruction}],
        "temperature": 0.1, "response_format": {"type": "json_object"},
    }
    response = requests.post(url, headers=headers, json=fallback_body, timeout=60)
    response.raise_for_status()
    content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
    plan = _json_object(content)
    if require_function and not str(plan.get("function") or plan.get("name") or "").strip():
        raise ValueError("模型未为可执行请求选择函数")
    return _planned_result(plan)


def _request_route_decision(url, headers, model, messages, catalog, draft):
    """Let the model decide whether the request needs a portal capability.

    This deliberately contains no keyword routing.  It is a second model
    decision used when an OpenAI-compatible provider returns prose despite
    receiving tool schemas.
    """
    instruction = f"""你是工程工具门户的请求路由器。判断对话中最后一个真实用户请求是否需要使用门户能力完成。
若用户要求创建、下载、转换、采集、查询真实任务/结果、修改配置或打开具体工具，requires_tool 为 true。
若只是打招呼、闲聊、询问概念或使用说明，requires_tool 为 false。
能力目录：{json.dumps(catalog, ensure_ascii=False)}
模型刚才的草稿答复：{draft[:2000]}
必须调用 route_request 返回判断。不要根据草稿中“无法完成”的说法改变对真实请求的判断。"""
    route_tool = {
        "type": "function",
        "function": {
            "name": "route_request",
            "description": "返回用户请求是否必须使用门户工具能力",
            "parameters": {
                "type": "object",
                "properties": {
                    "requires_tool": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["requires_tool", "reason"],
                "additionalProperties": False,
            },
        },
    }
    response = requests.post(url, headers=headers, json={
        "model": model,
        "messages": [*messages[-10:], {"role": "user", "content": instruction}],
        "temperature": 0,
        "tools": [route_tool],
        "tool_choice": {"type": "function", "function": {"name": "route_request"}},
    }, timeout=60)
    response.raise_for_status()
    message = response.json().get("choices", [{}])[0].get("message", {})
    calls = message.get("tool_calls") or []
    if calls:
        raw = calls[0].get("function", {}).get("arguments") or "{}"
        decision = json.loads(raw) if isinstance(raw, str) else raw
    else:
        decision = _json_object(message.get("content", ""))
    value = decision.get("requires_tool")
    if isinstance(value, str):
        value = value.strip().lower() in {"true", "1", "yes"}
    return value is True


def _request_conversational_answer(url, headers, model, messages):
    """Ask the provider for a clean answer without exposing action tools.

    This path is used only when the provider chose not to call a tool.  It can
    never execute an operation, so conversational fallback remains read-only.
    """
    conversation_system = """你是工程工具门户的 AI 工作助手。请直接、友好、简洁地回答用户。
你可以介绍自己的身份和门户能力，也可以进行普通交流。不要因为问题不需要调用工具而道歉或声称无法完成。
不得虚构已经执行了任何工具、任务或数据操作。"""
    response = requests.post(url, headers=headers, json={
        "model": model,
        "messages": [{"role": "system", "content": conversation_system}, *messages[-10:]],
        "temperature": 0.2,
    }, timeout=60)
    response.raise_for_status()
    answer = str(response.json().get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
    if not answer:
        raise ValueError("模型未返回普通答复")
    return {"answer": answer}


def _clean_result(result, current_view):
    if not isinstance(result, dict):
        result = {}
    answer = str(result.get("answer") or "").strip()[:12000] or "暂时没有生成有效答复。"
    navigate_to = str(result.get("navigate_to") or "").strip()
    if navigate_to not in public_registry():
        navigate_to = ""
    suggestions = [str(item).strip()[:120] for item in (result.get("suggestions") or []) if str(item).strip()][:3]
    patch = result.get("config_patch") if isinstance(result.get("config_patch"), dict) else None
    if patch:
        target = str(patch.get("target") or current_view)
        try:
            checked = validate_action("configure", target, patch.get("values") or {})
            patch = {"target": target, "values": checked["parameters"], "summary": str(patch.get("summary") or "应用 AI 建议配置")[:160]}
        except ValueError:
            patch = None
    action = result.get("action") if isinstance(result.get("action"), dict) else None
    if action:
        try:
            checked = validate_action(action.get("operation"), action.get("tool_id"), action.get("parameters") or {})
            action = {key: checked[key] for key in ("operation", "tool_id", "parameters", "requires_confirmation", "label")}
        except ValueError:
            action = None
    return {"answer": answer, "navigate_to": navigate_to or None, "suggestions": suggestions, "config_patch": patch, "action": action}


def chat(payload):
    url = (os.getenv("AI_NATIVE_LLM_URL") or os.getenv("CRAWLER_LLM_URL") or "").strip()
    key = (os.getenv("AI_NATIVE_LLM_API_KEY") or os.getenv("CRAWLER_LLM_API_KEY") or "").strip()
    model = (os.getenv("AI_NATIVE_LLM_MODEL") or os.getenv("CRAWLER_LLM_MODEL") or "glm-4-flash").strip()
    if not url or not key:
        raise ValueError("尚未配置系统 AI 模型，请在 .env 中配置 AI_NATIVE_LLM_URL 和 AI_NATIVE_LLM_API_KEY")

    current_view = str(payload.get("current_view") or "home")
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    messages = []
    for item in (payload.get("messages") if isinstance(payload.get("messages"), list) else [])[-10:]:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            continue
        content = str(item.get("content") or "").strip()[:4000]
        if content:
            messages.append({"role": item["role"], "content": content})

    system = f"""你是“工程工具门户”的系统级 AI 工作助手，负责理解用户目标、选择门户工具、生成配置、查询真实状态，并在用户确认后执行有副作用的操作。当前页面：{current_view}。
当前页面安全上下文：{json.dumps(context, ensure_ascii=False)[:8000]}
能力注册表：{json.dumps(prompt_catalog(), ensure_ascii=False)}

必须完全依据能力注册表选择通用函数，禁止虚构工具、参数或已执行结果。用户问“你是谁”、打招呼、询问使用方法或进行无需工具的普通交流时，调用 respond_to_user 正常回答，不要道歉或声称无法完成。询问榜单、任务结果、进度或状态时调用 query_tool 获取真实数据；用户要求改变数量、范围、格式或其他配置时不能仅查询。若上一轮已有待确认的 create_task，用户追问修改参数时必须继承未变参数并再次调用 create_task，以新计划替换旧计划；仅当用户明确只想填写或调整页面表单、不要求执行时才调用 configure_tool。要求进入页面时调用 navigate_tool；明确采集、下载、转换或创建任务时调用 create_task。对话中的“Agent 上下文”是上一轮真实工具调用和执行结果，后续修改应继承其中未被用户改变的参数。副作用操作由系统统一确认，不能用文字替代函数。上传文件或连接凭据缺失时，只导航并说明需在页面补充，不能调用未注册的 create。回答使用简洁中文。"""
    try:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        request_messages = [{"role": "system", "content": system}, *messages]
        body = {"model": model, "messages": request_messages, "tools": GENERIC_FUNCTIONS, "tool_choice": "auto", "temperature": 0.1}
        agent_messages = request_messages
        last_error = ""
        last_content = ""
        for attempt in range(3):
            response = requests.post(url, headers=headers, json={**body, "messages": agent_messages}, timeout=60)
            response.raise_for_status()
            message = response.json().get("choices", [{}])[0].get("message", {})
            last_content = str(message.get("content") or "").strip()
            try:
                if not message.get("tool_calls"):
                    if last_content:
                        needs_tool = _request_route_decision(
                            url, headers, model, messages, prompt_catalog(), last_content
                        )
                        if needs_tool:
                            return _clean_result(_request_structured_plan(
                                url, headers, body, agent_messages,
                                "模型返回了普通文本，但路由模型判定该请求需要门户工具",
                                require_function=True,
                            ), current_view)
                        return _clean_result(_request_conversational_answer(url, headers, model, messages), current_view)
                    raise ValueError("没有使用标准 Function Calling 协议")
                return _clean_result(_tool_result(message), current_view)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                if attempt == 2:
                    break
                # Feed the observation back to the model so it can revise its
                # own action. The server never guesses intent from keywords.
                agent_messages = [
                    *agent_messages,
                    {"role": "assistant", "content": str(message.get("content") or "")[:2000]},
                    {"role": "user", "content": f"工具调用未通过能力注册表校验：{last_error}。请观察错误后重新规划，并只通过一个已注册函数返回修正结果。不要在普通文本中书写函数名或 JSON。"},
                ]
        # Compatibility path for providers that accept OpenAI-style tools but
        # occasionally drop tool_calls on conversational follow-up turns.
        try:
            return _clean_result(_request_structured_plan(url, headers, body, agent_messages, last_error), current_view)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, requests.RequestException):
            # A provider may support conversational completions while ignoring
            # both tools and JSON mode. Plain text is safe to return because it
            # cannot trigger an action; all mutations still require a validated
            # registry call and explicit confirmation.
            if last_content:
                return _clean_result({"answer": last_content}, current_view)
            return _clean_result({
                "answer": "你好，我是工程工具门户的 AI 工作助手。你可以直接告诉我想完成的事情，我会帮你选择工具、准备配置，并在执行写入或删除操作前征求确认。"
            }, current_view)
    except requests.RequestException as exc:
        raise ValueError("AI 服务暂时不可用，请检查模型地址、密钥或网络连接") from exc
    except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"AI 返回的操作无法执行：{exc}") from exc


def capabilities():
    configured = bool((os.getenv("AI_NATIVE_LLM_URL") or os.getenv("CRAWLER_LLM_URL")) and (os.getenv("AI_NATIVE_LLM_API_KEY") or os.getenv("CRAWLER_LLM_API_KEY")))
    return {"configured": configured, "model": os.getenv("AI_NATIVE_LLM_MODEL") or os.getenv("CRAWLER_LLM_MODEL") or "", "tools": public_registry(), "functions": list(FUNCTION_OPERATIONS)}
