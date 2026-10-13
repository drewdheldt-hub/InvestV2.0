"""Flex statements parse into accounts, positions, tax lots and trades; several statements merge."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from investtree.portfolio import holdings

STATEMENT = """<FlexQueryResponse queryName="holdings" type="AF">
<FlexStatements count="2">
<FlexStatement accountId="U1111111" fromDate="20251013" toDate="20261012" whenGenerated="20261013;061500">
  <AccountInformation accountId="U1111111" acctAlias="Brokerage" accountType="Individual" currency="USD"/>
  <OpenPositions>
    <OpenPosition accountId="U1111111" symbol="VTI" conid="12345" assetCategory="STK" description="VANGUARD TOTAL"
      position="30" markPrice="300" positionValue="9000" costBasisMoney="7500" currency="USD" side="Long"
      levelOfDetail="SUMMARY"/>
    <OpenPosition accountId="U1111111" symbol="VTI" conid="12345" assetCategory="STK" position="10" markPrice="300"
      costBasisMoney="2000" openDateTime="20240115;093000" levelOfDetail="LOT"/>
    <OpenPosition accountId="U1111111" symbol="VTI" conid="12345" assetCategory="STK" position="20" markPrice="300"
      costBasisMoney="5500" openDateTime="2026-03-02 10:00:00" levelOfDetail="LOT"/>
  </OpenPositions>
  <Trades>
    <Trade accountId="U1111111" symbol="VTI" conid="12345" tradeDate="20260302" quantity="20" tradePrice="275"
      buySell="BUY" fifoPnlRealized="0" levelOfDetail="EXECUTION"/>
    <Trade accountId="U1111111" symbol="BND" conid="999" tradeDate="20261001" quantity="-50" tradePrice="70"
      buySell="SELL" fifoPnlRealized="-400" levelOfDetail="EXECUTION"/>
    <Trade accountId="U1111111" symbol="BND" conid="999" tradeDate="20261001" quantity="-50" tradePrice="70"
      buySell="SELL" fifoPnlRealized="-400" levelOfDetail="CLOSED_LOT"/>
  </Trades>
  <CashReport>
    <CashReportCurrency currency="BASE_SUMMARY" endingCash="1500.25"/>
  </CashReport>
</FlexStatement>
<FlexStatement accountId="U2222222" fromDate="20251013" toDate="20261012">
  <AccountInformation accountId="U2222222" acctAlias="IRA" accountType="IRA" currency="USD"/>
  <OpenPositions>
    <OpenPosition accountId="U2222222" symbol="BND" conid="999" assetCategory="STK" position="100" markPrice="72"
      positionValue="7200" costBasisMoney="7000" levelOfDetail="SUMMARY"/>
  </OpenPositions>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""


def test_parse_accounts_positions_lots_trades_and_cash() -> None:
    h = holdings.parse(STATEMENT, "s.xml")
    assert h.as_of == date(2026, 10, 12)
    assert {a.account_id: a.account_type for a in h.accounts.values()} == {"U1111111": "Individual", "U2222222": "IRA"}
    vti = h.for_account("U1111111")[0]
    assert (vti.symbol, vti.quantity, vti.value, vti.unrealized) == ("VTI", 30, 9000, 1500)
    lots = h.lots_of("U1111111", "VTI")
    assert [(lot.opened, lot.quantity, lot.cost_per_share) for lot in lots] == [
        (date(2024, 1, 15), 10, 200), (date(2026, 3, 2), 20, 275)]
    # closed-lot rows repeat the execution and are not counted twice
    assert [(t.symbol, t.quantity, t.realized) for t in h.trades] == [("VTI", 20, 0), ("BND", -50, -400)]
    assert h.cash == {"U1111111": 1500.25}


def test_a_short_position_is_negative() -> None:
    long = 'position="30" markPrice="300" positionValue="9000" costBasisMoney="7500" currency="USD" side="Long"'
    short = 'position="-30" markPrice="300" positionValue="-9000" costBasisMoney="-7500" currency="USD" side="Short"'
    text = STATEMENT.replace(long, short)
    assert holdings.parse(text).for_account("U1111111")[0].quantity == -30


@pytest.mark.parametrize("text", ["not xml", "<Other/>", "<FlexQueryResponse/>"])
def test_not_a_statement(text: str) -> None:
    with pytest.raises(holdings.FlexError):
        holdings.parse(text)


def test_merge_keeps_the_latest_positions_and_every_trade_once() -> None:
    old = holdings.parse(STATEMENT.replace('toDate="20261012" whenGenerated', 'toDate="20261001" whenGenerated'), "a")
    new = holdings.parse(STATEMENT.replace('position="30"', 'position="31"'), "b")
    h = holdings.merge([new, old])
    assert h.for_account("U1111111")[0].quantity == 31
    assert len(h.trades) == 2 and h.as_of == date(2026, 10, 12)


def test_load_reads_every_saved_statement(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        holdings.load(tmp_path)
    folder = holdings.flex_folder(tmp_path)
    folder.mkdir(parents=True)
    (folder / "2026-10-13_1.xml").write_text(STATEMENT, encoding="utf-8")
    assert len(holdings.load(tmp_path).positions) == 2
