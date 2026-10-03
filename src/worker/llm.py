"""Thin chat-with-tools client. OpenAI today; the agent only depends on `chat()`."""

from __future__ import annotations

import json
import time

from openai import APIError, OpenAI

from worker import config

# Approximate USD per 1M tokens (input, output), for the cost line in reports. Check current pricing.
PRICES = {"gpt-5.4-mini": (0.75, 4.5), "gpt-5.4": (2.5, 15.0), "gpt-5.4-nano": (0.2, 1.25), "gpt-4.1-mini": (0.4, 1.6)}


class LLM:
    def __init__(self, model: str = config.MODEL) -> None:
        self.model = model
        self.client = OpenAI()
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    def chat(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False) -> dict:
        kwargs: dict = {"model": self.model, "messages": messages}
        if tools:
            kwargs.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        for attempt in range(4):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                break
            except APIError:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        self.usage["calls"] += 1
        self.usage["input_tokens"] += resp.usage.prompt_tokens
        self.usage["output_tokens"] += resp.usage.completion_tokens
        msg = resp.choices[0].message
        out: dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            out["tool_calls"] = [{"id": tc.id, "type": "function",
                                  "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                 for tc in msg.tool_calls]
        return out

    def json(self, system: str, user: str) -> dict:
        reply = self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=True)
        return json.loads(reply["content"])

    def cost_usd(self) -> float:
        pin, pout = PRICES.get(self.model, (0, 0))
        return round(self.usage["input_tokens"] / 1e6 * pin + self.usage["output_tokens"] / 1e6 * pout, 4)
