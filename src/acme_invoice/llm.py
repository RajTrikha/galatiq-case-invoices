from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from .models import ToolCallRecord

ResponseT = TypeVar("ResponseT", bound=BaseModel)
PROMPT_VERSION = "v1"


class LLMConfigurationError(RuntimeError):
    pass


class ReplayUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]

    def as_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
                "strict": True,
            },
        }


@dataclass(frozen=True)
class StructuredResponse(Generic[ResponseT]):
    data: ResponseT
    origin: str
    replayed: bool
    tool_calls: list[ToolCallRecord]


class LLMClient(ABC):
    @abstractmethod
    def structured(
        self,
        *,
        agent: str,
        source_hash: str,
        messages: list[dict[str, str]],
        response_model: type[ResponseT],
        tools: list[ToolDefinition] | None = None,
        tool_handlers: dict[str, Callable[..., dict[str, Any]]] | None = None,
    ) -> StructuredResponse[ResponseT]:
        raise NotImplementedError


class GrokClient(LLMClient):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        resolved_key = api_key or os.getenv("XAI_API_KEY") or os.getenv("LLM_API_KEY")
        resolved_model = model or os.getenv("LLM_MODEL")
        if not resolved_key or not resolved_model:
            raise LLMConfigurationError(
                "Live mode requires XAI_API_KEY (or LLM_API_KEY) and LLM_MODEL."
            )
        self.model = resolved_model
        self.client = OpenAI(
            api_key=resolved_key,
            base_url=base_url or os.getenv("LLM_BASE_URL", "https://api.x.ai/v1"),
        )

    def structured(
        self,
        *,
        agent: str,
        source_hash: str,
        messages: list[dict[str, str]],
        response_model: type[ResponseT],
        tools: list[ToolDefinition] | None = None,
        tool_handlers: dict[str, Callable[..., dict[str, Any]]] | None = None,
    ) -> StructuredResponse[ResponseT]:
        del source_hash
        working_messages: list[dict[str, Any]] = list(messages)
        executed_tools: list[ToolCallRecord] = []
        if tools:
            first = self.client.chat.completions.create(
                model=self.model,
                messages=working_messages,
                tools=[tool.as_openai() for tool in tools],
                tool_choice="required",
                temperature=0,
            )
            assistant = first.choices[0].message
            working_messages.append(assistant.model_dump(exclude_none=True))
            handlers = tool_handlers or {}
            for call in assistant.tool_calls or []:
                name = call.function.name
                if name not in handlers:
                    raise RuntimeError(f"{agent} requested unavailable tool {name!r}")
                arguments = json.loads(call.function.arguments or "{}")
                result = handlers[name](**arguments)
                executed_tools.append(ToolCallRecord(name=name, arguments=arguments, result=result))
                working_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, default=str),
                    }
                )
            working_messages.append(
                {
                    "role": "user",
                    "content": "Return the final decision as JSON matching the supplied schema.",
                }
            )
        schema = response_model.model_json_schema()
        response = self.client.chat.completions.create(
            model=self.model,
            messages=working_messages,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": f"{agent}_{response_model.__name__}",
                    "strict": True,
                    "schema": schema,
                },
            },
            temperature=0,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError(f"{agent} returned no structured content")
        return StructuredResponse(
            data=response_model.model_validate_json(content),
            origin="grok",
            replayed=False,
            tool_calls=executed_tools,
        )


class ReplayLLMClient(LLMClient):
    def __init__(self, replay_path: Path) -> None:
        payload = json.loads(replay_path.read_text())
        if payload.get("prompt_version") != PROMPT_VERSION:
            raise LLMConfigurationError("Replay prompt version does not match the application")
        self.entries: dict[str, Any] = payload.get("entries", {})

    def structured(
        self,
        *,
        agent: str,
        source_hash: str,
        messages: list[dict[str, str]],
        response_model: type[ResponseT],
        tools: list[ToolDefinition] | None = None,
        tool_handlers: dict[str, Callable[..., dict[str, Any]]] | None = None,
    ) -> StructuredResponse[ResponseT]:
        del messages, tools
        recording = self.entries.get(source_hash, {}).get(agent)
        if not recording:
            raise ReplayUnavailable(f"No {agent} replay exists for source hash {source_hash[:12]}.")
        handlers = tool_handlers or {}
        executed_tools: list[ToolCallRecord] = []
        for request in recording.get("tool_requests", []):
            name = request["name"]
            if name not in handlers:
                raise ReplayUnavailable(f"Replay requires unavailable tool {name!r}")
            arguments = request.get("arguments", {})
            result = handlers[name](**arguments)
            executed_tools.append(ToolCallRecord(name=name, arguments=arguments, result=result))
        return StructuredResponse(
            data=response_model.model_validate(recording["response"]),
            origin=recording.get("origin", "curated_fixture"),
            replayed=True,
            tool_calls=executed_tools,
        )


class FakeLLMClient(LLMClient):
    def __init__(self, responses: dict[str, list[dict[str, Any]] | dict[str, Any]]) -> None:
        self.responses = responses

    def structured(
        self,
        *,
        agent: str,
        source_hash: str,
        messages: list[dict[str, str]],
        response_model: type[ResponseT],
        tools: list[ToolDefinition] | None = None,
        tool_handlers: dict[str, Callable[..., dict[str, Any]]] | None = None,
    ) -> StructuredResponse[ResponseT]:
        del source_hash, messages, tools
        payloads = self.responses.get(agent)
        if payloads is None:
            raise ReplayUnavailable(f"Fake client has no response for {agent}")
        if isinstance(payloads, list):
            if not payloads:
                raise ReplayUnavailable(f"Fake client exhausted responses for {agent}")
            payload = payloads.pop(0)
        else:
            payload = payloads
        executed_tools: list[ToolCallRecord] = []
        handlers = tool_handlers or {}
        for request in payload.get("tool_requests", []):
            name = request["name"]
            arguments = request.get("arguments", {})
            result = handlers[name](**arguments)
            executed_tools.append(ToolCallRecord(name=name, arguments=arguments, result=result))
        response_payload = payload.get("response", payload)
        return StructuredResponse(
            data=response_model.model_validate(response_payload),
            origin="fake_test",
            replayed=True,
            tool_calls=executed_tools,
        )


def make_llm_client(mode: str, replay_path: Path) -> LLMClient:
    if mode == "live":
        return GrokClient()
    if mode == "replay":
        return ReplayLLMClient(replay_path)
    if mode != "auto":
        raise LLMConfigurationError(f"Unknown LLM mode: {mode}")
    if (os.getenv("XAI_API_KEY") or os.getenv("LLM_API_KEY")) and os.getenv("LLM_MODEL"):
        return GrokClient()
    return ReplayLLMClient(replay_path)
