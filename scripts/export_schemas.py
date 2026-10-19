"""Generate the distributed contracts from the same models used by the service."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from atr.agent_adapter import definitions
from atr.models import (
    CreateRequest,
    Event,
    ExecuteRequest,
    ExecutionResult,
    Manifest,
    Program,
    ReviewReport,
)

destination = Path(__file__).resolve().parents[1] / "atr" / "schemas"
destination.mkdir(exist_ok=True)
contracts = {"manifest": Manifest, "program": Program, "execute-request": ExecuteRequest,
             "execution-result": ExecutionResult, "create-request": CreateRequest,
             "review-report": ReviewReport, "event": Event}
for name, model in contracts.items():
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", **model.model_json_schema()}
    Draft202012Validator.check_schema(schema)
    (destination / (name + ".json")).write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
(destination / "interfaces.json").write_text(json.dumps(definitions(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"Generated {len(contracts)} data contracts and the fixed five interfaces")
