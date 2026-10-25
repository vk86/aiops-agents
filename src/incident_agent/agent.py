"""The incident management agent: a tool-calling LangChain agent with conversation memory."""
from __future__ import annotations

from datetime import datetime

from .llm import get_model, text_of
from .tools import ALL_TOOLS

SYSTEM_PROMPT = """You are an incident management assistant for ServiceNow, used by on-call engineers and service desk staff.

How to work:
- Use the tools to look things up. Never invent incident numbers, field values, people, groups or changes.
- For "what's going on with INC...", call get_incident; for a write-up call summarize_incident.
- When triaging, check find_recent_changes and find_similar_incidents and say what they show.
- Before create_incident, collect at least a short description, impact and urgency. If the user is vague,
  propose values and ask them to confirm. Use list_assignment_groups to get an exact group name.
- Writes (create, update, note, resolve) ask the user for approval. If they decline, say nothing changed.
- Priority comes from impact x urgency: 1/1 = P1 Critical, 1/2 or 2/1 = P2 High, 2/2, 1/3, 3/1 = P3 Moderate.
- When someone starts a shift, logs in, says hello/good morning, or asks what to work on: call shift_briefing and
  present the headline, the top incidents (number, priority, age) and SLA risks, then suggest what to pick up first.
- When asked who can fix something or who fixed it before: call who_solved_this_before and name the people with
  how many similar incidents they resolved and the fix they used.
- On a new incident, offer duplicates (find_duplicate_incidents) and routing (suggest_assignment) if relevant.
- For "why did this happen", RCA, root cause or postmortem: call root_cause_analysis and present its result. Keep the
  evidence ids. Offer record_rca to post the summary (approval needed). Never claim more certainty than the evidence.
- For recurring issues use find_problem_candidates; create_problem and link_duplicate need approval like other writes.
- Keep answers short and scannable. Always show incident numbers. Never output passwords or credentials.

Engineering follow-up: draft_backlog_item / create_backlog_item turn what you found into a user story, feature, defect or spike in the ALM (SynthoGEN MCP, Jira, Rally, Azure DevOps); creating needs approval and writes the link back to ServiceNow.

Today is {today}."""


class SimpleToolAgent:
    """Plain LangChain tool-calling loop with per-thread memory.

    Used when LangGraph can't load, e.g. when Windows Application Control blocks one of its
    compiled dependencies (ormsgpack). Same behaviour for this use case: the model picks tools,
    we run them, feed results back, repeat until it answers.
    """

    def __init__(self, model, tools, system_prompt: str, max_steps: int = 12):
        self.model = model.bind_tools(tools)
        self.tools = {t.name: t for t in tools}
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.threads: dict[str, list] = {}

    def ask(self, message: str, thread_id: str) -> dict:
        history = self.threads.setdefault(thread_id, [])
        mark = len(history)
        try:
            return self._ask(message, thread_id)
        except Exception:
            del history[max(mark, 1):]  # roll back the failed turn so the thread stays valid
            raise

    def _ask(self, message: str, thread_id: str) -> dict:
        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
        history = self.threads[thread_id]
        if not history:
            history.append(SystemMessage(self.system_prompt))
        history.append(HumanMessage(message))
        calls = []
        for _ in range(self.max_steps):
            ai = self.model.invoke(history)
            history.append(ai)
            if not getattr(ai, "tool_calls", None):
                break
            for tc in ai.tool_calls:
                calls.append({"tool": tc["name"], "args": tc["args"]})
                tool = self.tools.get(tc["name"])
                try:
                    out = tool.invoke(tc["args"]) if tool else f"Unknown tool {tc['name']}"
                except Exception as e:  # bad arguments etc. go back to the model
                    out = f"Tool error: {e}"
                history.append(ToolMessage(content=str(out), tool_call_id=tc["id"]))
        else:
            history.append(self.model.invoke(history + [HumanMessage("Stop calling tools and answer with what you have.")]))
        return {"reply": text_of(history[-1]), "tool_calls": calls}


def build_agent(checkpointer=None, tools=None, system_prompt: str | None = None):
    """LangChain 1.x create_agent (LangGraph); falls back to SimpleToolAgent if LangGraph can't load.
    tools/system_prompt default to the incident agent; the RCA agent passes its own."""
    prompt = (system_prompt or SYSTEM_PROMPT).format(today=datetime.now().strftime("%A %d %B %Y"))
    ALL = tools or ALL_TOOLS
    model = get_model()
    try:
        if checkpointer is None:
            try:
                from langgraph.checkpoint.memory import InMemorySaver as Saver
            except ImportError:
                from langgraph.checkpoint.memory import MemorySaver as Saver
            checkpointer = Saver()
        try:
            from langchain.agents import create_agent
            return create_agent(model, ALL, system_prompt=prompt, checkpointer=checkpointer)
        except ImportError:
            from langgraph.prebuilt import create_react_agent
            return create_react_agent(model, ALL, prompt=prompt, checkpointer=checkpointer)
    except ImportError as e:
        import logging
        logging.getLogger("incident_agent").warning("LangGraph unavailable (%s); using SimpleToolAgent", e)
        return SimpleToolAgent(model, ALL, prompt)


RECURSION_LIMIT = 40           # LangGraph steps per turn (each model call and each tool round is a step)
_THREAD_ALIAS: dict[str, str] = {}   # UI thread id -> current LangGraph thread id (rotated if a thread breaks)

STEP_LIMIT_REPLY = ("I took too many steps without reaching an answer, so I stopped. Try a narrower request, "
                    "for example name the incident number (\"Investigate INC0012345\").")


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": _THREAD_ALIAS.get(thread_id, thread_id)}, "recursion_limit": RECURSION_LIMIT}


def ask(agent, message: str, thread_id: str = "default") -> dict:
    """Send one user message. Returns the reply and the tools that were called this turn."""
    if isinstance(agent, SimpleToolAgent):
        return agent.ask(message, thread_id)
    config = _config(thread_id)
    if not _repair_thread(agent, config):
        config = _rotate(thread_id)
    state = agent.get_state(config)
    before = len(state.values.get("messages", [])) if state else 0
    try:
        result = agent.invoke({"messages": [{"role": "user", "content": message}]}, config)
    except Exception as e:
        if not _repair_thread(agent, config):
            _rotate(thread_id)
        if type(e).__name__ == "GraphRecursionError":  # a looping turn is an answer, not a crash
            msgs = agent.get_state(_config(thread_id)).values.get("messages", [])[before:]
            calls = [{"tool": c["name"], "args": c["args"]} for m in msgs for c in (getattr(m, "tool_calls", None) or [])]
            return {"reply": STEP_LIMIT_REPLY, "tool_calls": calls}
        raise
    new = result["messages"][before:]
    calls = [{"tool": c["name"], "args": c["args"]} for m in new for c in (getattr(m, "tool_calls", None) or [])]
    return {"reply": text_of(result["messages"][-1]), "tool_calls": calls}


def _rotate(thread_id: str) -> dict:
    """Last resort: start a fresh LangGraph thread for this UI thread so one broken turn cannot poison the chat."""
    import uuid
    _THREAD_ALIAS[thread_id] = f"{thread_id}~{uuid.uuid4().hex[:6]}"
    return _config(thread_id)


def _dangling(msgs) -> list[str]:
    answered = {getattr(m, "tool_call_id", None) for m in msgs}
    return [tc["id"] for m in msgs for tc in (getattr(m, "tool_calls", None) or []) if tc["id"] not in answered]


def _repair_thread(agent, config) -> bool:
    """If a previous turn died after the model asked for tools, answer the dangling tool calls so the
    provider does not reject the whole conversation ("tool_calls must be followed by tool messages").
    Returns True when the thread is valid afterwards."""
    try:
        from langchain_core.messages import ToolMessage
        state = agent.get_state(config)
        msgs = state.values.get("messages", []) if state else []
        ids = _dangling(msgs)
        if not ids:
            return True
        fix = {"messages": [ToolMessage(content="Tool not run (the previous turn stopped).", tool_call_id=i) for i in ids]}
        for node in ("tools", None):  # the update must be attributed to a node; "tools" is the agent's tool node
            try:
                agent.update_state(config, fix, as_node=node) if node else agent.update_state(config, fix)
                if not _dangling(agent.get_state(config).values.get("messages", [])):
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False
