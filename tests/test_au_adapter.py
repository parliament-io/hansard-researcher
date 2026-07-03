import datetime as dt
from pathlib import Path

import httpx
import pytest

from parlhansard.harvest.au import AuAdapter

FIXTURES = Path(__file__).parent / "fixtures"

# raw calendar HTML shapes observed live 2026-07-03: modern numeric bid with
# aria-label, GUID bid with widget-suffixed aria-label, a pre-2018 section
# (NO aria-label — date is structural: year heading + month row + day text),
# and a legacy ParlInfo date-id link
CALENDAR_HTML = """
<td><a href="https://www.aph.gov.au/Parliamentary_Business/Hansard/Hansard_Display?bid=chamber/hansards/29209/&sid=0000" aria-label="11-Mar-2026">11</a></td>
<td><a href="https://www.aph.gov.au/Parliamentary_Business/Hansard/Hansard_Display?bid=chamber/hansards/07129367-a45a-448c-b34c-f10d11b0374a/&sid=0000" aria-label="12-Feb-2019 - open in a new tab">12</a></td>
<h3><a name="2017"></a>2017</h3>
<table><tbody>
<tr><td><p>February</p></td>
<td><a href="http://www.aph.gov.au/Parliamentary_Business/Hansard/Hansard_Display?bid=chamber/hansards/6a0d5952-cd19-47eb-bed0-cdff4080ad6d/&amp;sid=0000" target="_blank">7</a>&nbsp;&nbsp;</td></tr>
</tbody></table>
<td><a href="http://parlinfo.aph.gov.au/parlInfo/search/display/display.w3p;query%3DId%3A%22chamber/hansards/2000-02-15/0000%22">15</a></td>
"""


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "Hanssen261110" in url:
        return httpx.Response(200, text=CALENDAR_HTML)
    if "Hansreps_2011" in url:
        return httpx.Response(200, text="<html>no sittings</html>")
    if "api/hansard/link" in url and "29209" in url:
        return httpx.Response(
            200,
            content=(FIXTURES / "au_daily.xml").read_bytes(),
            headers={"content-type": "text/xml"},
        )
    return httpx.Response(404)


@pytest.fixture
def adapter():
    return AuAdapter(client=httpx.Client(transport=httpx.MockTransport(_handler)))


def test_discover_all_id_shapes(adapter):
    events = list(adapter.discover(dt.date(2000, 1, 1), dt.date(2026, 12, 31)))
    by_date = {e.date: e for e in events}
    assert by_date[dt.date(2026, 3, 11)].source_doc_id == "29209"
    assert by_date[dt.date(2019, 2, 12)].source_doc_id == "07129367-a45a-448c-b34c-f10d11b0374a"
    # pre-2018 structural date: year heading + month row + day text — the
    # verified first day of the modern federal XML era
    assert by_date[dt.date(2017, 2, 7)].source_doc_id == "6a0d5952-cd19-47eb-bed0-cdff4080ad6d"
    assert by_date[dt.date(2000, 2, 15)].source_doc_id == "2000-02-15"
    assert all(e.house == "senate" for e in events)
    assert all("fulltranscript=True" in e.url for e in events)


def test_discover_filters_range(adapter):
    events = list(adapter.discover(dt.date(2026, 3, 1), dt.date(2026, 3, 31)))
    assert [e.date for e in events] == [dt.date(2026, 3, 11)]


def test_fetch_and_normalize_end_to_end(adapter):
    (event,) = adapter.discover(dt.date(2026, 3, 1), dt.date(2026, 3, 31))
    docs = list(adapter.fetch(event))
    assert [d.name for d in docs] == ["daily.xml"]
    (fragment,) = adapter.normalize(docs)
    assert fragment.date == dt.date(2026, 3, 11)
    assert fragment.house == "Senate"
    assert len(fragment.proceedings) == 2
