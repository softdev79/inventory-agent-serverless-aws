"""Inventory planning agent: LangChain 1.x create_agent + Bedrock (OpenAI-compatible endpoint)."""
from __future__ import annotations

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

import settings
from tools.inventory_lookup import lookup_inventory
from tools.reorder_calculator import calculate_reorder

SYSTEM_PROMPT = """You are an expert Inventory Planning Agent. Your job is to help users manage inventory and decide when and how much to reorder.

You have access to two tools:
1. lookup_inventory: retrieves live inventory data from the database for a given SKU.
2. calculate_reorder: computes whether a reorder is needed and the recommended quantity.

Guidelines:
- Always look up the SKU from the database first if the user mentions one.
- For any reorder question, ALWAYS call calculate_reorder with the actual numbers (from DB or user input).
- Report the reorder point and the recommended order quantity EXACTLY as calculate_reorder returns them.
  Never recompute or substitute your own formula.
- Explain your reasoning clearly and in plain English.
- If information is missing (e.g., demand, lead time), ask the user for it.
- Keep responses concise but actionable: plain text or simple markdown (bold, bullet lists).
  Never use LaTeX or math notation such as \\[ \\] or \\text{}.
"""

TOOLS = [lookup_inventory, calculate_reorder]

_agent = None
_agent_key: str | None = None


def get_agent():
    """Build once per LLM credential; rebuilt automatically when the Bedrock token rotates."""
    global _agent, _agent_key
    key = settings.llm_api_key()
    if _agent is None or key != _agent_key:
        llm = ChatOpenAI(
            model=settings.OPENAI_MODEL,
            api_key=key,
            base_url=settings.OPENAI_BASE_URL,
            default_headers={"OpenAI-Project": settings.OPENAI_PROJECT_ID} if settings.OPENAI_PROJECT_ID else None,
            temperature=0,
            timeout=45,
            max_retries=2,
        )
        _agent = create_agent(model=llm, tools=TOOLS, system_prompt=SYSTEM_PROMPT)
        _agent_key = key
    return _agent


def chat(query: str, history: list[dict] | None = None) -> str:
    return chat_with_trace(query, history)[0]


def chat_with_trace(query: str, history: list[dict] | None = None) -> tuple[str, list[str]]:
    """history: list of {"role": "user"|"assistant", "content": "..."} dicts."""
    messages = []
    for msg in (history or [])[-20:]:
        if msg.get("role") == "user":
            messages.append(HumanMessage(content=msg["content"]))
        elif msg.get("role") == "assistant":
            messages.append(AIMessage(content=msg["content"]))
    messages.append(HumanMessage(content=query))

    result = get_agent().invoke({"messages": messages}, config={"recursion_limit": 12})
    tool_calls = [
        f"{c['name']}({c['args']})"
        for m in result["messages"]
        if isinstance(m, AIMessage)
        for c in (m.tool_calls or [])
    ]
    return str(result["messages"][-1].text).strip(), tool_calls
