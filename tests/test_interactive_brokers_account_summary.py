import pytest

from lumibot.brokers.interactive_brokers import InteractiveBrokers


class _FakeIB:
    def __init__(self, summary):
        self.summary = summary

    def get_account_summary(self):
        return self.summary


def _broker_with_summary(summary):
    broker = InteractiveBrokers.__new__(InteractiveBrokers)
    broker.ib = _FakeIB(summary)
    broker._reconnect_if_not_connected = lambda: False
    return broker


@pytest.mark.parametrize(
    "cash_tag,liquidation_tag",
    [
        ("TotalCashBalance", "NetLiquidationByCurrency"),
        ("$LEDGER-TotalCashBalance", "$LEDGER-NetLiquidationByCurrency"),
    ],
)
def test_get_balances_accepts_prefixed_and_unprefixed_ledger_tags(cash_tag, liquidation_tag):
    broker = _broker_with_summary(
        [
            {"Tag": cash_tag, "Currency": "BASE", "Value": "123.45"},
            {"Tag": liquidation_tag, "Currency": "BASE", "Value": "678.90"},
        ]
    )

    assert broker._get_balances_at_broker(quote_asset=None, strategy=None) == (
        123.45,
        678.90,
        678.90,
    )


def test_get_balances_keeps_base_currency_selection():
    broker = _broker_with_summary(
        [
            {"Tag": "$LEDGER-TotalCashBalance", "Currency": "USD", "Value": "999.99"},
            {"Tag": "$LEDGER-NetLiquidationByCurrency", "Currency": "USD", "Value": "999.99"},
            {"Tag": "$LEDGER-TotalCashBalance", "Currency": "BASE", "Value": "1.25"},
            {"Tag": "$LEDGER-NetLiquidationByCurrency", "Currency": "BASE", "Value": "2.50"},
        ]
    )

    assert broker._get_balances_at_broker(quote_asset=None, strategy=None) == (
        1.25,
        2.50,
        2.50,
    )


def test_get_balances_missing_required_tag_reports_sanitized_available_tags():
    broker = _broker_with_summary(
        [
            {"Account": "PRIVATE_ACCOUNT", "Tag": "$LEDGER-TotalCashBalance", "Currency": "BASE", "Value": "1"},
            {"Account": "PRIVATE_ACCOUNT", "Tag": "AvailableFunds", "Currency": "BASE", "Value": "2"},
        ]
    )

    with pytest.raises(ValueError) as exc_info:
        broker._get_balances_at_broker(quote_asset=None, strategy=None)

    message = str(exc_info.value)
    assert "NetLiquidationByCurrency" in message
    assert "$LEDGER-TotalCashBalance" in message
    assert "AvailableFunds" in message
    assert "PRIVATE_ACCOUNT" not in message
    assert "Value" not in message


def test_get_balances_non_numeric_value_suppresses_raw_value_in_exception_chain():
    broker = _broker_with_summary(
        [
            {"Tag": "$LEDGER-TotalCashBalance", "Currency": "BASE", "Value": "1,234.56"},
            {"Tag": "$LEDGER-NetLiquidationByCurrency", "Currency": "BASE", "Value": "2"},
        ]
    )

    with pytest.raises(ValueError) as exc_info:
        broker._get_balances_at_broker(quote_asset=None, strategy=None)

    message = str(exc_info.value)
    assert "TotalCashBalance" in message
    assert "1,234.56" not in message
    assert exc_info.value.__cause__ is None


def test_account_summary_tag_normalization_accepts_ledger_currency_scope():
    assert (
        InteractiveBrokers._normalize_account_summary_tag("$LEDGER:USD-TotalCashBalance")
        == "TotalCashBalance"
    )
