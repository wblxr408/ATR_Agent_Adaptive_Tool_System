from pathlib import Path

from test_acceptance import runtime
from test_proxies import EvidenceAdapter, research_program

from atr.models import Cost, ExecuteRequest, Settings
from atr.policy import Policy


def test_actual_receipt_settles_once_replaces_unknown_and_releases_budget(tmp_path: Path) -> None:
    class UnknownBilling(EvidenceAdapter):
        max_cost_usd = .75

        def call(self, inputs, **kwargs):
            response = super().call(inputs, **kwargs)
            response.costs = [Cost(category="external_api")]
            return response
    rt = runtime(tmp_path, settings=Settings(max_task_cost=1),
        policy=Policy(network_capabilities=["research"]), adapters={"research": UnknownBilling()})
    request = ExecuteRequest(root_task_id="task", program=research_program())
    assert rt.execute(request).status == "succeeded"
    assert rt.execute(request).error_code == "cost_budget"
    reservation = rt.ledger.report("task")["unsettled_reservations"][0]["id"]
    receipt = Cost(category="external_api", amount=.1, billing_ref="actual-receipt")
    rt.ledger.settle(reservation, receipt)
    rt.ledger.settle(reservation, receipt)
    assert rt.ledger.report("task")["known_totals"]["USD"] == .1
    assert rt.execute(request).status == "succeeded"
