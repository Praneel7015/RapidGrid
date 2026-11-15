"""A showcase must distinguish simulated notifications from real delivery."""
import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.communication import CommunicationAgent, bus
from models.schemas import CommunicationChannel, CommunicationRequest


def test_normal_showcase_does_not_claim_delivery_or_force_a_gateway_outage(monkeypatch):
    monkeypatch.delenv("RAPIDGRID_SIMULATE_SMS_FAILURE", raising=False)
    with patch.object(bus, "publish", new_callable=AsyncMock) as publish:
        result = asyncio.run(CommunicationAgent().dispatch_plan(CommunicationRequest(action_plan={})))
    assert result.simulated is True
    assert result.overall_status == "success"
    assert len(result.receipts) == 4
    assert all(receipt.status == "simulated" for receipt in result.receipts)
    assert publish.await_args.kwargs["payload"]["simulated"] is True


def test_optional_sms_outage_does_not_block_other_simulated_channels(monkeypatch):
    monkeypatch.setenv("RAPIDGRID_SIMULATE_SMS_FAILURE", "1")
    with patch.object(bus, "publish", new_callable=AsyncMock):
        result = asyncio.run(CommunicationAgent().dispatch_plan(CommunicationRequest(action_plan={})))
    assert result.simulated and result.overall_status == "completed_with_errors"
    sms = next(receipt for receipt in result.receipts if receipt.channel == CommunicationChannel.SMS)
    assert sms.status == "failed" and "Simulated" in sms.error_message
    assert all(receipt.status == "simulated" for receipt in result.receipts if receipt is not sms)
