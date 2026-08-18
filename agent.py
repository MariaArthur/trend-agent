"""
agent.py
--------
Orchestration loop for the market trend research agent, using Groq's
OpenAI-compatible chat completions API for fast open-model inference with
tool calling.

Same hand-rolled pattern as before (no LangChain): send messages + tool
schemas -> check if the model wants a tool call -> execute it for real ->
feed the result back -> repeat until a final answer comes out. Groq's
tool-calling format is OpenAI-style (function calling with a `tools` array
and `tool_calls` on the response), which differs slightly from Anthropic's
`tool_use` content blocks - see the inline comments below for the specific
differences if you're coming from an Anthropic-based agent.
"""

import json
import os

from dotenv import load_dotenv
from groq import Groq

from tools import TOOL_SCHEMAS, TOOL_FUNCTIONS

load_dotenv()

MODEL = "openai/gpt-oss-120b"

SYSTEM_PROMPT = """You are a market & trend research assistant. You help the user understand
what's trending, how a topic/brand/product is being talked about, and how it compares to
competitors, using real web search - not your own guesses.

Rules:
- Never state a trend, sentiment, or statistic unless it came from a tool call. If you don't
  have it, call the appropriate tool.
- Every trend report includes a `confidence` field (high/medium/low/none). ALWAYS surface this
  to the user in your own words. If confidence is "low", explicitly say the signal is thin and
  shouldn't be treated as a confirmed trend - don't state it plainly as fact.
- Cite your sources by name/domain (e.g. "according to Reuters and TechCrunch") when giving
  findings, so the user can verify.
- Use build_trend_report for "what's trending" / market-read questions, search_web for quick
  one-off factual lookups, and compare_topics when the user wants two things compared
  (e.g. two competing brands).
- If the user wants ongoing monitoring, use the watchlist tools (add_to_watchlist,
  list_watchlist, get_watchlist_history, run_watchlist_scan) rather than just answering once.
- Keep responses concise and structured (short paragraphs or bullet points), not walls of text.
- If the request is unrelated to market/trend research, politely decline and steer back to what
  you can help with.
"""

MAX_TOOL_ROUNDS = 6  # a bit higher than a simple lookup agent since compare/watchlist flows chain more tool calls


def get_client():
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY environment variable is not set. Get a free key at "
            "https://console.groq.com/keys and set it before running the app."
        )
    return Groq(api_key=api_key)


def run_agent(messages: list[dict], on_tool_call=None) -> tuple[str, list[dict]]:
    """
    Run one turn of the agent loop.

    Args:
        messages: full conversation history in OpenAI/Groq message format
                  (list of {"role": ..., "content": ...})
        on_tool_call: optional callback(tool_name, tool_args) invoked right
                      before each tool executes - used by the UI to show a
                      "researching..." indicator

    Returns:
        (final_text, updated_messages)
    """
    client = get_client()

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            max_tokens=1024,
        )

        choice = response.choices[0]
        msg = choice.message

        # Append the assistant's turn as-is (Groq's OpenAI-style message
        # objects serialize cleanly into the next request)
        assistant_turn = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_turn["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_turn)

        if not msg.tool_calls:
            # Final natural-language answer - no more tools requested
            return msg.content or "", messages

        # Execute every requested tool call and append tool result messages
        # (OpenAI/Groq format requires one "tool" role message per tool_call_id,
        # unlike Anthropic which batches them into a single user turn)
        for tc in msg.tool_calls:
            tool_name = tc.function.name
            try:
                tool_args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                tool_args = {}

            if on_tool_call:
                on_tool_call(tool_name, tool_args)

            func = TOOL_FUNCTIONS.get(tool_name)
            if func is None:
                result = {"error": f"Unknown tool: {tool_name}"}
            else:
                try:
                    result = func(**tool_args)
                except Exception as e:  # never let a bad tool call crash the agent
                    result = {"error": f"Tool execution failed: {e}"}

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tool_name,
                    "content": json.dumps(result, default=str),
                }
            )

        # loop again so the model can either call another tool or answer

    return (
        "Sorry, I'm having trouble completing that research request right now. "
        "Try narrowing the topic or asking again in a moment.",
        messages,
    )
