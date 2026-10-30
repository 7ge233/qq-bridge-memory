# -*- coding: utf-8 -*-
"""HTTP 请求/响应模型（Pydantic v2）。

字段口径与设计稿一致；`scope` 只接受 type/user_id/group_id，
scope_key 由服务端计算（6.4）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Scope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["private", "group"]
    user_id: str | int | None = None
    group_id: str | int | None = None


class Speaker(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | int | None = None
    name: str | None = None


class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account: str | int
    scope: Scope
    query: str
    # 查询上下文：短查询（“大后天呢”“出去”）单独召回会被门控挡掉，
    # 调用方可以把最近几轮对话拼进来，让召回按「事件所处的语境」去检索。
    context: str = ""
    limit: int = 6
    max_chars: int = 800
    exclude_ids: list[str | int] = Field(default_factory=list)
    now: str | None = None
    include_past: bool = False


class WriteRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account: str | int
    scope: Scope
    message_id: str | int | None = None
    role: Literal["user", "assistant"] = "user"
    speaker: Speaker | None = None
    content: str
    happened_at: str | None = None
    source: Literal["auto", "llm", "import", "console"] = "auto"
    meta: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    generation: int | None = None
    fact_key: str | None = None
    fact_scope: str | None = None
    attachment: list[Any] | None = None
    event_at: str | None = None
    valid_until: str | None = None
    event_status: Literal["planned", "done", "cancelled"] | None = None


class HandoffQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account: str | int
    scope: str
    limit: int = 20
