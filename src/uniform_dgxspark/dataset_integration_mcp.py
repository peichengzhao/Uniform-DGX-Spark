"""Only bound dataset inspection, proposals and interpretation; no approval/execution tool."""

from __future__ import annotations

import json
from typing import Any

from .accuracy_mcp import AccuracyMCP, main
from .services.dataset_integration import DatasetIntegrationAnalysis, DatasetIntegrationProposal


class DatasetIntegrationMCP(AccuracyMCP):
    server_name = "uniform_dataset"

    def __init__(self, base: str, session_id: str, token: str) -> None:
        super().__init__(base, session_id, token)
        self.base = self.base.removesuffix("accuracy") + "datasets"

    @staticmethod
    def definitions() -> list[dict[str, Any]]:
        empty = {"type": "object", "properties": {}, "additionalProperties": False}
        tools = [
            (
                "dataset_inspect",
                "Inspect the bound dataset, audited adapter, environment and requirements.",
                empty,
            ),
            (
                "dataset_propose_plan",
                "Propose a fixed two-sample integration probe for Web UI confirmation; "
                "never executes it.",
                DatasetIntegrationProposal.model_json_schema(),
            ),
            (
                "dataset_status",
                "Read this integration's real validation state and canonical report.",
                empty,
            ),
            (
                "dataset_record_analysis",
                "Record interpretation of actual checks/evidence, not scores or activation.",
                DatasetIntegrationAnalysis.model_json_schema(),
            ),
        ]
        return [
            {
                "name": name,
                "description": description,
                "inputSchema": schema,
                "annotations": {"readOnlyHint": name in {"dataset_inspect", "dataset_status"}},
            }
            for name, description, schema in tools
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        actions = {
            "dataset_inspect": ("GET", "inspect"),
            "dataset_status": ("GET", "status"),
            "dataset_propose_plan": ("POST", "plan"),
            "dataset_record_analysis": ("POST", "analysis"),
        }
        if name not in actions:
            raise ValueError("unknown dataset integration tool")
        if name == "dataset_propose_plan":
            arguments = DatasetIntegrationProposal.model_validate(arguments).model_dump(mode="json")
        elif name == "dataset_record_analysis":
            arguments = DatasetIntegrationAnalysis.model_validate(arguments).model_dump(mode="json")
        elif arguments:
            raise ValueError("this tool accepts no arguments")
        method, suffix = actions[name]
        response = self.client.request(
            method, f"{self.base}/{suffix}", json=arguments if method == "POST" else None
        )
        if response.is_redirect:
            raise ValueError("platform redirects are not allowed")
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": "platform returned a non-JSON response"}
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": response.status_code >= 400,
        }


if __name__ == "__main__":
    main(DatasetIntegrationMCP)
