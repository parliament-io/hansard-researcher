"""SA member register builder.

Sources (verified live 2026-07-04):

- ``POST membersapp.parliament.sa.gov.au/api/members`` with ``memberType``
  ``"current"``/``"former"`` — one row per person (69 current + ~936 former):
  ``pm_Id``, names, DOB, ``mb_ElectedDate`` (``"Jul 12 1975 12:00AM"``),
  ``pm_ArchiveDate`` (ISO), ``electorate``, ``houseName``, ``pp_name``
  (full party name). **``pm_Id`` IS the Hansard talker ``@id``**
  (silver ``member_source_id``) — entity linking is a direct join, verified
  against Chapman/Marshall/Lucas/Koutsantonis/Maher. ``houseName`` matches
  silver house values verbatim ("House of Assembly"/"Legislative Council").
- ``GET contact-details-api.parliament.sa.gov.au/api/{HA,LC}MembersDetails``
  — current-parliament contacts/positions; snapshotted raw for future use
  (positions/portfolios), not yet normalized.

Fetch snapshots everything into ``data/reference/raw/sa/`` so the register
rebuilds offline (``--offline``) — same fetch-once-then-sandbox model as the
raw Hansard store. Limitation (accepted): the source is one-row-per-person,
so party is the person's latest affiliation, not time-sliced; the service
span is ``elected_date → archived_date``.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from parlhansard import __version__
from parlhansard.reference.register import member_id, write_register

MEMBERS_URL = "https://membersapp.parliament.sa.gov.au/api/members"
CONTACT_URLS = {
    "ha_contacts": "https://contact-details-api.parliament.sa.gov.au/api/HAMembersDetails",
    "lc_contacts": "https://contact-details-api.parliament.sa.gov.au/api/LCMembersDetails",
}
USER_AGENT = f"parlhansard/{__version__} (open-source Hansard analytics; polite harvester)"

# the members endpoint filters on this shape; nulls = no filter
_MEMBERS_PAYLOAD = {
    "firstname": None,
    "lastname": None,
    "party": None,
    "house": None,
    "electorate": None,
    "portfolio": None,
    "parliament": None,
    "speakerPremierFilter": None,
}

RAW_FILES = ("members_current.json", "members_former.json", "ha_contacts.json", "lc_contacts.json")


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, max=15), reraise=True)
def _fetch_json(client: httpx.Client, method: str, url: str, **kwargs):
    response = client.request(method, url, **kwargs)
    response.raise_for_status()
    return response.json()


def fetch_raw(raw_dir: Path, client: httpx.Client | None = None) -> None:
    """Snapshot all SA member sources into ``raw_dir`` (needs network)."""
    own_client = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60)
    try:
        payloads = {
            "members_current.json": _fetch_json(
                client, "POST", MEMBERS_URL,
                json=_MEMBERS_PAYLOAD | {"formerMembers": 0, "memberType": "current"},
            ),
            "members_former.json": _fetch_json(
                client, "POST", MEMBERS_URL,
                json=_MEMBERS_PAYLOAD | {"formerMembers": 1, "memberType": "former"},
            ),
            "ha_contacts.json": _fetch_json(client, "GET", CONTACT_URLS["ha_contacts"]),
            "lc_contacts.json": _fetch_json(client, "GET", CONTACT_URLS["lc_contacts"]),
        }
    finally:
        if own_client:
            client.close()
    raw_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        (raw_dir / name).write_text(
            json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8"
        )
    (raw_dir / "meta.json").write_text(
        json.dumps(
            {
                "retrieved_at": dt.datetime.now(dt.UTC).isoformat(),
                "sources": [MEMBERS_URL, *CONTACT_URLS.values()],
            },
            indent=1,
        ),
        encoding="utf-8",
    )


def _parse_date(value: str | None) -> dt.date | None:
    """Source date shapes: ISO ('1989-11-24T00:00:00') and 'Jul 12 1975 12:00AM'."""
    if not value or not value.strip():
        return None
    value = value.strip()
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        pass
    try:
        return dt.datetime.strptime(value, "%b %d %Y %I:%M%p").date()
    except ValueError:
        return None


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def build_rows(raw_dir: Path) -> list[dict]:
    """Normalize the membersapp snapshots into register rows (offline)."""
    meta = json.loads((raw_dir / "meta.json").read_text(encoding="utf-8"))
    retrieved_at = dt.datetime.fromisoformat(meta["retrieved_at"])

    people: dict[str, dict] = {}
    # former first — a person present in both lists keeps the current row
    for filename, is_current in (("members_former.json", False), ("members_current.json", True)):
        for person in json.loads((raw_dir / filename).read_text(encoding="utf-8")):
            source_id = str(person["pm_Id"])
            first = _clean(person.get("pm_FirstName"))
            last = _clean(person.get("pm_LastName"))
            people[source_id] = {
                "member_id": member_id("sa", source_id),
                "source_member_id": source_id,
                "display_name": " ".join(filter(None, (first, last))),
                "title": _clean(person.get("pm_Title")),
                "first_name": first,
                "other_names": _clean(person.get("pm_OtherNames")),
                "last_name": last,
                "date_of_birth": _parse_date(person.get("pm_DateOfBirth")),
                "house": _clean(person.get("houseName") or person.get("ho_name")),
                "electorate": _clean(person.get("electorate")),
                "party_name": _clean(person.get("pp_name")),
                "is_current": is_current,
                "deceased": person.get("pm_Deceased"),
                "elected_date": _parse_date(person.get("mb_ElectedDate")),
                "archived_date": _parse_date(person.get("pm_ArchiveDate")),
                "retrieved_at": retrieved_at,
                "jurisdiction": "sa",
            }
    return list(people.values())


def build(data_dir: Path, *, offline: bool = False) -> int:
    """Fetch (unless offline) + normalize the SA register; returns row count."""
    raw_dir = Path(data_dir) / "reference" / "raw" / "sa"
    if not offline:
        fetch_raw(raw_dir)
    elif not (raw_dir / "meta.json").exists():
        raise FileNotFoundError(
            f"no stored snapshot under {raw_dir} — run once without --offline first"
        )
    return write_register(build_rows(raw_dir), Path(data_dir) / "reference")
