import datetime as dt

import pytest

from hansard_researcher.harvest import all_adapters, get_adapter
from hansard_researcher.harvest.hansard_api import HansardPublicApiAdapter
from hansard_researcher.model.canonical import Jurisdiction


def test_all_six_jurisdictions_registered():
    registered = {a.jurisdiction for a in all_adapters()}
    assert registered == set(Jurisdiction)


@pytest.mark.parametrize("jurisdiction", [Jurisdiction.NZ, Jurisdiction.SCOT])
def test_unimplemented_stubs_raise_not_implemented(jurisdiction):
    adapter = get_adapter(jurisdiction)
    with pytest.raises(NotImplementedError):
        list(adapter.discover(dt.date(2026, 1, 1), dt.date(2026, 1, 31)))


@pytest.mark.parametrize("jurisdiction", [Jurisdiction.WA, Jurisdiction.SA])
def test_wa_sa_share_the_public_api_adapter(jurisdiction):
    adapter = get_adapter(jurisdiction)
    assert isinstance(adapter, HansardPublicApiAdapter)
    assert adapter.base_url.startswith("https://")


def test_get_adapter_accepts_string():
    assert get_adapter("wa").jurisdiction is Jurisdiction.WA
