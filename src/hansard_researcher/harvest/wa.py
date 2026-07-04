"""Western Australian Parliament adapter — live.

Source (verified live 2026-07-03): WA runs the same **Hansard Public API** as
SA (see :mod:`hansard_researcher.harvest.hansard_api` for the shared endpoint
surface; swagger at https://www.parliament.wa.gov.au/hansard/docs/api/v1/swagger.json,
copy at ``schemas/wa.swagger.json``). Serves per-subject extracts in the native
``hansard_1_0.xsd`` schema family — normalize is validate + load.

House codes: ``lh`` (Legislative Assembly), ``uh`` (Legislative Council),
``esta``/``estb`` (LA Estimates Committees).

Licensing: the API declares **Creative Commons BY-ND 4.0** — verbatim
redistribution permitted with attribution; no derivatives (see
LICENSES-DATA.md). The older per-file embedded conditions still accompany the
XML payloads themselves.

Legacy (pre-API) direct URL pattern, kept for reference/backfill checks:
``https://www.parliament.wa.gov.au/hansard/daily/{house}/{yyyy-MM-dd}/extract/{n}/download``
"""

from __future__ import annotations

from hansard_researcher.harvest.base import register
from hansard_researcher.harvest.hansard_api import HansardPublicApiAdapter
from hansard_researcher.model.canonical import Jurisdiction


@register
class WaAdapter(HansardPublicApiAdapter):
    jurisdiction = Jurisdiction.WA
    status = "live"
    source = "parliament.wa.gov.au/hansard/api - shared Hansard Public API (CC BY-ND 4.0)"
    base_url = "https://www.parliament.wa.gov.au/hansard/api"
