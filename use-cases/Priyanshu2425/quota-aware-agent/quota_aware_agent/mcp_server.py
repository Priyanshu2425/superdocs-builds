"""The MCP surface. Band S1 says MCP, and agents are who this serves.

Everything here is a thin wrapper over `QuotaAwareAgent`, which is already
tested. No planning logic lives in this file -- if it did, the MCP path and the
library path could disagree about what fits inside an allowance, and only one
of them would be tested.

The three tools mirror how an agent actually works:

    check_allowance  -- what have I got? (free; the one authoritative read)
    plan_work        -- what fits inside it? (free; spends nothing, decides nothing)
    run_work         -- do the part that fits, and tell me what you left out.

`plan_work` exists as a separate tool on purpose. An agent that must commit to
work before it can find out what fits is the exact failure this build is about,
so the plan is readable without spending anything.
"""

from __future__ import annotations

import json
import os

from .agent import QuotaAwareAgent, Step
from .budget import estimate
from .client import HttpTransport, SuperDocsClient
from .idempotency import OperationLedger
from .policy import Policy

SERVER_NAME = "quota-aware-agent"

#: Where the operation ledger is kept between runs. It has to outlive the
#: process, because the failure it prevents -- paying twice for the same edit --
#: is caused by a process that died. Overridable so two agents driving different
#: accounts do not share one.
LEDGER_PATH = os.environ.get(
    "QUOTA_AWARE_AGENT_LEDGER",
    os.path.expanduser("~/.quota-aware-agent/operations.jsonl"),
)


def _ledger() -> OperationLedger:
    return OperationLedger(LEDGER_PATH)


def _client() -> SuperDocsClient:
    key = os.environ.get("SUPERDOCS_API_KEY")
    if not key:
        raise RuntimeError(
            "SUPERDOCS_API_KEY is not set. This server needs a SuperDocs API key; "
            "an agent account can be created with POST /v1/agents/signup."
        )
    return SuperDocsClient(HttpTransport(key))


def _steps(raw: list[dict]) -> list[Step]:
    out = []
    for i, s in enumerate(raw, 1):
        out.append(Step(
            step_id=str(s.get("id") or f"step-{i}"),
            instruction=str(s["instruction"]),
            sections=int(s.get("sections", 1)),
            severity=str(s.get("severity", "medium")),
        ))
    return out


# -- the three operations, as plain functions so they are testable without MCP --

def check_allowance() -> dict:
    agent = QuotaAwareAgent(_client(), ledger=_ledger())
    balance = agent.read_allowance()
    unresolved = agent.unresolved_operations()
    return {
        "remaining_operations": balance.ops,
        "authoritative": balance.authoritative,
        "resets_at": balance.as_of,
        "note": ("Authoritative right now. It stops being authoritative as soon as "
                 "work begins: the usage endpoints reject API keys, so the balance "
                 "is only readable as a side effect of making calls."),
        # Surfaced here rather than only inside run_work, because an agent that
        # is about to plan needs to know an earlier run left something in doubt
        # before it decides what to do, not after.
        "started_and_never_confirmed": unresolved,
        "budget": agent.budget_hint(),
    }


def plan_work(steps: list[dict], reserve: int = 1) -> dict:
    """Free. Spends nothing, changes nothing, and answers 'what fits?'."""
    agent = QuotaAwareAgent(_client(), policy=Policy(reserve=reserve))
    balance = agent.read_allowance()
    parsed = _steps(steps)
    plan = agent.plan(parsed)
    by_id = {s.step_id: s for s in parsed}
    return {
        "remaining_operations": balance.ops,
        "full_request_costs": estimate([s.as_change() for s in parsed]),
        "reserved": reserve,
        "will_run": [c.row_id for c in plan.publish],
        "will_defer": [c.row_id for c in plan.defer],
        "fits_completely": plan.complete,
        "explanation": plan.rationale or "The whole request fits inside the allowance.",
        "instructions": {k: v.instruction for k, v in by_id.items()},
        "policy": agent.policy.describe(),
        "budget": agent.budget_hint(),
    }


def run_work(session_id: str, filename: str, document_html: str,
             steps: list[dict], reserve: int = 1, max_steps: int | None = None,
             export_format: str = "docx") -> dict:
    agent = QuotaAwareAgent(_client(), policy=Policy(reserve=reserve, max_steps=max_steps),
                            ledger=_ledger())
    report = agent.run(session_id, filename, document_html.encode("utf-8"),
                       _steps(steps), export_format=export_format)
    return {
        "completed": report.completed,
        "deferred": report.deferred,
        "stopped_because": report.stopped_because or None,
        "plain_language": report.plain_language(),
        "allowance_at_start": {
            "operations": report.balance_at_start.ops,
            "authoritative": report.balance_at_start.authoritative,
        } if report.balance_at_start else None,
        "allowance_at_end": {
            "operations": report.balance_at_end.ops,
            "authoritative": report.balance_at_end.authoritative,
        } if report.balance_at_end else None,
        "export_warnings": report.export_warnings,
        "already_applied_by_an_earlier_run": report.already_applied,
        "started_and_never_confirmed": report.needs_a_person,
        "stop_reason": report.stop_reason.value,
        "why_it_stopped": report.stop_reason.explain(),
        # The line items and whether they add up. An agent that has to derive
        # its own spend from a transcript will get it wrong the same way a
        # person does.
        "receipt": report.receipt.as_dict(report.balance_at_start,
                                          report.balance_at_end),
        "budget": agent.budget_hint(),
    }


TOOLS = [
    {
        "name": "check_allowance",
        "description": (
            "Read how many SuperDocs operations remain before planning any work. "
            "Free. This is the one moment the number is authoritative -- afterwards "
            "it can only be inferred from the responses to work you have already done."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "plan_work",
        "description": (
            "Price a list of edits against the remaining allowance and report what "
            "fits, what would be deferred, and why -- WITHOUT doing any of it. Free. "
            "Call this before run_work so you never start a job you cannot finish."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "The edits you want to make.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "instruction": {"type": "string",
                                            "description": "A natural-language edit instruction."},
                            "sections": {"type": "integer", "minimum": 1,
                                         "description": "Roughly how many document sections it "
                                                        "touches. SuperDocs bills one operation "
                                                        "per 25 sections edited."},
                            "severity": {"type": "string",
                                         "enum": ["critical", "high", "medium", "low"],
                                         "description": "Highest severity runs first when not "
                                                        "everything fits."},
                        },
                        "required": ["instruction"],
                    },
                },
                "reserve": {"type": "integer", "minimum": 0, "default": 1,
                            "description": "Operations held back so finished work can still "
                                           "be exported."},
            },
            "required": ["steps"],
        },
    },
    {
        "name": "run_work",
        "description": (
            "Upload a document, apply the edits that fit inside the remaining "
            "allowance, approve the proposed changes, and export the result. "
            "Degrades rather than failing halfway: it never starts a step it cannot "
            "finish, and it reports in plain language what it left out."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "filename": {"type": "string"},
                "document_html": {"type": "string",
                                  "description": "The document as HTML. SuperDocs takes "
                                                 "documents and HTML, never raw Word XML."},
                "steps": {"type": "array", "items": {"type": "object"}},
                "reserve": {"type": "integer", "default": 1},
                "max_steps": {"type": ["integer", "null"],
                              "description": "Small-sample bound: run at most this many steps."},
                "export_format": {"type": "string",
                                  "enum": ["docx", "pdf", "html", "markdown", "txt"],
                                  "default": "docx"},
            },
            "required": ["session_id", "filename", "document_html", "steps"],
        },
    },
]

_HANDLERS = {
    "check_allowance": lambda a: check_allowance(),
    "plan_work": lambda a: plan_work(a["steps"], a.get("reserve", 1)),
    "run_work": lambda a: run_work(
        a["session_id"], a["filename"], a["document_html"], a["steps"],
        a.get("reserve", 1), a.get("max_steps"), a.get("export_format", "docx"),
    ),
}


def dispatch(name: str, arguments: dict) -> dict:
    """The whole surface, callable without an MCP client -- which is what makes
    it testable offline."""
    if name not in _HANDLERS:
        raise KeyError(f"no tool named {name!r}")
    return _HANDLERS[name](arguments or {})


async def serve() -> None:
    """Serve the three tools over stdio.

    Written against the MCP SDK's constructor-callback API (2.x). The older
    `@server.list_tools()` decorators do not exist there, and a server that
    imports cleanly but cannot start is worse than one that fails loudly, so
    `test_the_server_starts_and_advertises_its_tools` drives a real client.
    """
    import mcp.types as types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    def _tool(spec: dict) -> "types.Tool":
        return types.Tool(
            name=spec["name"],
            description=spec["description"],
            input_schema=spec["inputSchema"],
        )

    async def on_list_tools(ctx, params) -> "types.ListToolsResult":
        return types.ListToolsResult(tools=[_tool(t) for t in TOOLS])

    async def on_call_tool(ctx, params) -> "types.CallToolResult":
        try:
            result = dispatch(params.name, params.arguments or {})
            failed = False
        except Exception as e:
            # An agent must be able to READ the failure. Letting it propagate
            # drops the connection and tells the caller nothing actionable.
            result, failed = {"error": str(e)}, True
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, indent=2))],
            is_error=failed,
        )

    server = Server(
        SERVER_NAME,
        version="1.0.0",
        instructions=(
            "Call check_allowance first, then plan_work to see what fits, then "
            "run_work. The first two are free and change nothing."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    import asyncio

    asyncio.run(serve())


if __name__ == "__main__":
    main()
