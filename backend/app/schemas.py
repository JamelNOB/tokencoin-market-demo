"""Public request and status schemas.

OpenAI adds optional request fields over time. The passthrough request models
therefore validate the stable core and retain unknown fields for the upstream
provider.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PassthroughRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1)
    stream: bool = False

    def upstream_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class ChatCompletionRequest(PassthroughRequest):
    messages: list[dict[str, Any]] = Field(min_length=1)


class ResponsesRequest(PassthroughRequest):
    input: Any = None


class ModelObject(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str


class ModelListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelObject]


class GatewayStatus(BaseModel):
    mode: Literal["live", "unconfigured"]
    version: str
    configuredProviders: int
    supportedModels: list[str]


class StatusSummary(BaseModel):
    totalRequests: int
    successRate: float
    pendingCny: float
    lastProbeAt: str | None


class SupplyStatus(BaseModel):
    id: str
    name: str
    provider: str
    models: list[str]
    enabled: bool
    circuit: Literal["healthy", "degraded", "half_open", "open", "disabled"]
    consecutiveFailures: int
    inFlight: int
    totalAttempts: int
    successRate: float
    lastSuccessAt: str | None
    lastFailureAt: str | None
    healthStatus: str
    reliability: float
    latencyMs: float | None
    available: bool


class StatusResponse(BaseModel):
    status: Literal["ok", "degraded", "unconfigured"]
    gateway: GatewayStatus
    summary: StatusSummary
    supplies: list[SupplyStatus]
    recentAudits: list[dict[str, Any]]
