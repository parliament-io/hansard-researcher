"""South Australian Parliament adapter — live.

Source (verified 2026-07-03): the **Hansard Public API** (SA authored the
original ``Hansard_1_0.xsd``; WA runs the same API — see
:mod:`parlhansard.harvest.hansard_api` for the shared endpoint surface).
Swagger at https://hansardsearch.parliament.sa.gov.au/docs/api/v1/swagger.json,
copy at ``schemas/sa.swagger.json``. No auth.

House codes: ``lh`` (House of Assembly), ``uh`` (Legislative Council),
``eca``/``ecaatq``/``ecb``/``ecbatq`` (Estimates Committees + Answers to
Questions).

Licensing: parliamentary copyright, API "as is" — no licence declared in the
swagger (unlike WA's CC BY-ND) — see LICENSES-DATA.md.
"""

from __future__ import annotations

from parlhansard.harvest.base import register
from parlhansard.harvest.hansard_api import HansardPublicApiAdapter
from parlhansard.model.canonical import Jurisdiction


@register
class SaAdapter(HansardPublicApiAdapter):
    jurisdiction = Jurisdiction.SA
    status = "live"
    source = "hansardsearch.parliament.sa.gov.au/api - shared Hansard Public API"
    base_url = "https://hansardsearch.parliament.sa.gov.au/api"
