"""Tests for ContractInterface._execute_transaction() revert detection and pre-flight."""

from __future__ import annotations

import concurrent.futures
from unittest.mock import MagicMock, Mock, patch

import pytest

from bnbagent.erc8004.contract import ContractInterface


def _make_contract(web3=None, paymaster=None):
    """Build a ContractInterface with all heavy dependencies mocked."""
    if web3 is None:
        web3 = MagicMock()
    wallet_provider = MagicMock()
    wallet_provider.address = "0xDeadBeef"

    with patch.object(ContractInterface, "_get_default_abi", return_value=[]):
        with patch("bnbagent.erc8004.contract.Web3.to_checksum_address", side_effect=lambda x: x):
            ci = ContractInterface(
                web3=web3,
                contract_address="0x1234",
                wallet_provider=wallet_provider,
                paymaster=paymaster,
            )
    return ci, web3, wallet_provider


def _make_function_mock(web3, gas_limit=100_000):
    """Build a contract function mock that produces a signed tx and hash."""
    fn = MagicMock()
    fn.estimate_gas.return_value = gas_limit
    fn.build_transaction.return_value = {
        "from": "0xDeadBeef",
        "to": "0x1234",
        "data": "0xdeadbeef",
        "value": 0,
        "gas": gas_limit,
        "gasPrice": 3_000_000_000,
        "nonce": 1,
        "chainId": 97,
    }
    signed = MagicMock()
    signed.__getitem__ = lambda self, key: b"\x00" * 32 if key == "rawTransaction" else None
    web3.eth.get_transaction_count.return_value = 1
    web3.eth.gas_price = 3_000_000_000
    web3.eth.chain_id = 97
    raw_tx = b"\x00" * 32
    signed_mock = MagicMock()
    signed_mock.__getitem__ = MagicMock(return_value=raw_tx)
    return fn, signed_mock


class TestExecuteTransactionReceiptRevert:
    def test_raises_on_receipt_status_zero(self, caplog):
        """receipt.status == 0 must raise RuntimeError and log an error."""
        web3 = MagicMock()
        ci, web3, wallet_provider = _make_contract(web3=web3)

        fn = MagicMock()
        fn.estimate_gas.return_value = 100_000
        fn.build_transaction.return_value = {
            "from": "0xDeadBeef", "to": "0x1234",
            "data": "0x", "value": 0, "gas": 100_000,
            "gasPrice": 3_000_000_000, "nonce": 1, "chainId": 97,
        }
        web3.eth.get_transaction_count.return_value = 1
        web3.eth.gas_price = 3_000_000_000
        web3.eth.chain_id = 97

        raw_bytes = b"\xab" * 32
        signed = MagicMock()
        signed.__getitem__ = lambda s, k: raw_bytes if k == "rawTransaction" else None
        wallet_provider.sign_transaction.return_value = signed

        sent_hash = b"\xab" * 32
        web3.eth.send_raw_transaction.return_value = sent_hash
        web3.eth.call.return_value = b""  # pre-flight passes

        revert_receipt = Mock()
        revert_receipt.__getitem__ = lambda s, k: {
            "status": 0,
            "blockNumber": 999,
            "gasUsed": 21000,
            "transactionHash": sent_hash,
        }[k]
        web3.eth.wait_for_transaction_receipt.return_value = revert_receipt

        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError, match="Transaction reverted on-chain"):
                ci._execute_transaction(fn, description="test-op")

        assert "[ContractInterface]" in caplog.text
        assert "test-op" in caplog.text

    def test_success_receipt_returns_normally(self):
        """receipt.status == 1 must return dict without raising."""
        web3 = MagicMock()
        ci, web3, wallet_provider = _make_contract(web3=web3)

        fn = MagicMock()
        fn.estimate_gas.return_value = 100_000
        fn.build_transaction.return_value = {
            "from": "0xDeadBeef", "to": "0x1234",
            "data": "0x", "value": 0, "gas": 100_000,
            "gasPrice": 3_000_000_000, "nonce": 1, "chainId": 97,
        }
        web3.eth.get_transaction_count.return_value = 1
        web3.eth.gas_price = 3_000_000_000
        web3.eth.chain_id = 97

        raw_bytes = b"\xab" * 32
        signed = MagicMock()
        signed.__getitem__ = lambda s, k: raw_bytes if k == "rawTransaction" else None
        wallet_provider.sign_transaction.return_value = signed

        sent_hash = b"\xab" * 32
        web3.eth.send_raw_transaction.return_value = sent_hash
        web3.eth.call.return_value = b""

        ok_receipt = Mock()
        ok_receipt.__getitem__ = lambda s, k: {
            "status": 1,
            "blockNumber": 1000,
            "gasUsed": 50000,
            "transactionHash": sent_hash,
        }[k]
        web3.eth.wait_for_transaction_receipt.return_value = ok_receipt

        result = ci._execute_transaction(fn, description="test-op")
        assert "transactionHash" in result
        assert "receipt" in result


class TestPreflightEthCall:
    def _setup(self, web3=None):
        if web3 is None:
            web3 = MagicMock()
        ci, web3, wallet_provider = _make_contract(web3=web3)
        fn = MagicMock()
        fn.estimate_gas.return_value = 100_000
        fn.build_transaction.return_value = {
            "from": "0xDeadBeef", "to": "0x1234",
            "data": "0x", "value": 0, "gas": 100_000,
            "gasPrice": 3_000_000_000, "nonce": 1, "chainId": 97,
        }
        web3.eth.get_transaction_count.return_value = 1
        web3.eth.gas_price = 3_000_000_000
        web3.eth.chain_id = 97

        raw_bytes = b"\xab" * 32
        signed = MagicMock()
        signed.__getitem__ = lambda s, k: raw_bytes if k == "rawTransaction" else None
        wallet_provider.sign_transaction.return_value = signed

        sent_hash = b"\xab" * 32
        web3.eth.send_raw_transaction.return_value = sent_hash

        ok_receipt = Mock()
        ok_receipt.__getitem__ = lambda s, k: {
            "status": 1, "blockNumber": 1, "gasUsed": 1,
            "transactionHash": sent_hash,
        }[k]
        web3.eth.wait_for_transaction_receipt.return_value = ok_receipt
        return ci, web3, wallet_provider, fn, sent_hash

    def test_raises_on_preflight_revert(self):
        """eth_call revert with revert data must raise before send_raw_transaction."""
        ci, web3, wallet_provider, fn, _ = self._setup()
        web3.eth.call.side_effect = Exception("execution reverted: Unauthorized")

        with pytest.raises(RuntimeError, match="Transaction would revert"):
            ci._execute_transaction(fn, description="pre-flight-test")

        web3.eth.send_raw_transaction.assert_not_called()

    def test_proceeds_on_opaque_0x_preflight(self):
        """eth_call returning opaque '0x' must log warning and continue to send."""
        ci, web3, wallet_provider, fn, _ = self._setup()
        web3.eth.call.side_effect = Exception("('0x', ...)")

        result = ci._execute_transaction(fn, description="opaque-test")
        web3.eth.send_raw_transaction.assert_called_once()
        assert "transactionHash" in result

    def test_proceeds_on_preflight_timeout(self):
        """eth_call timeout must log warning and continue to send."""
        ci, web3, wallet_provider, fn, _ = self._setup()

        def slow_call(params):
            raise concurrent.futures.TimeoutError()

        web3.eth.call.side_effect = slow_call

        # Patch ThreadPoolExecutor so TimeoutError propagates correctly
        original_executor = concurrent.futures.ThreadPoolExecutor

        class ImmediateTimeoutExecutor:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def submit(self, fn, *args, **kwargs):
                f = concurrent.futures.Future()
                f.set_exception(concurrent.futures.TimeoutError())
                return f

        with patch("bnbagent.erc8004.contract._cf.ThreadPoolExecutor", ImmediateTimeoutExecutor):
            result = ci._execute_transaction(fn, description="timeout-test")

        web3.eth.send_raw_transaction.assert_called_once()
        assert "transactionHash" in result


class TestRegisterAgentPropagatesRevert:
    def test_register_agent_raises_on_revert(self):
        """register_agent must raise RuntimeError (not return success=True) on revert."""
        web3 = MagicMock()
        ci, web3, wallet_provider = _make_contract(web3=web3)

        # Mock the contract and its functions
        register_fn = MagicMock()
        register_fn.estimate_gas.return_value = 100_000
        register_fn.build_transaction.return_value = {
            "from": "0xDeadBeef", "to": "0x1234",
            "data": "0x", "value": 0, "gas": 100_000,
            "gasPrice": 3_000_000_000, "nonce": 1, "chainId": 97,
        }
        ci.contract = MagicMock()
        ci.contract.functions.register.return_value = register_fn

        web3.eth.get_transaction_count.return_value = 1
        web3.eth.gas_price = 3_000_000_000
        web3.eth.chain_id = 97
        web3.eth.call.return_value = b""

        raw_bytes = b"\xab" * 32
        signed = MagicMock()
        signed.__getitem__ = lambda s, k: raw_bytes if k == "rawTransaction" else None
        wallet_provider.sign_transaction.return_value = signed

        sent_hash = b"\xab" * 32
        web3.eth.send_raw_transaction.return_value = sent_hash

        revert_receipt = Mock()
        revert_receipt.__getitem__ = lambda s, k: {
            "status": 0, "blockNumber": 999, "gasUsed": 21000,
            "transactionHash": sent_hash,
        }[k]
        web3.eth.wait_for_transaction_receipt.return_value = revert_receipt

        with pytest.raises(RuntimeError, match="Agent registration failed"):
            ci.register_agent(agent_uri="https://example.com/agent")
