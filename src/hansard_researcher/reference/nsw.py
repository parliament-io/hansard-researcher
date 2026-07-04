"""NSW member register builder (scrape — no member API).

Sources (verified live 2026-07-04):

- ``members/pages/all-members.aspx?house=both&tab=browse`` — current members
  (135 = 93 LA + 42 LC) in one table: name link ``a.prl-name-link`` with
  ``href="...Member-details.aspx?pk={id}"``, plus clean House (LA/LC),
  Party, Gender and Ministry columns; electorate is parsed from the
  "Member for {X}" line of the Position cell. **``pk`` IS the Hansard talker
  id** (silver ``member_source_id``) — verified Sharpe=28, Mitchell=93,
  Tudehope=115, Graham=2224.
- ``members/formermembers/pages/former-members-index.aspx?filter=A..Z`` —
  ``table#formerMembersTable``: name link (pk), date of birth, status,
  gender. **No reliable party**: the detail pages' "Political Party
  Activity" is free text (operator-confirmed), so former members enter the
  register as identity-only; party backfill comes from a curated table /
  Wikidata later. Detail pages do carry a structured service-span table
  (Position/Start/End) for a later pass.

Raw HTML snapshots land in ``data/reference/raw/nsw/`` so the register
rebuilds offline (``--offline``). Fetching is 27 pages, politely throttled.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import string
import time
from pathlib import Path

import httpx
from lxml import html
from tenacity import retry, stop_after_attempt, wait_exponential

from hansard_researcher.harvest.base import BROWSER_USER_AGENT
from hansard_researcher.reference.register import member_id, write_register

BASE = "https://www.parliament.nsw.gov.au"
CURRENT_URL = f"{BASE}/members/pages/all-members.aspx?&house=both&tab=browse"
FORMER_URL = f"{BASE}/members/formermembers/pages/former-members-index.aspx?filter={{letter}}"

HOUSES = {"LA": "Legislative Assembly", "LC": "Legislative Council"}


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, max=15), reraise=True)
def _fetch(client: httpx.Client, url: str) -> bytes:
    response = client.get(url)
    response.raise_for_status()
    return response.content


def fetch_raw(
    raw_dir: Path, client: httpx.Client | None = None, throttle_seconds: float = 0.5
) -> None:
    """Snapshot the current-members page + 26 former-index pages (needs network)."""
    own_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": BROWSER_USER_AGENT}, timeout=60, follow_redirects=True
    )
    raw_dir.mkdir(parents=True, exist_ok=True)
    try:
        (raw_dir / "all_members.html").write_bytes(_fetch(client, CURRENT_URL))
        for letter in string.ascii_uppercase:
            time.sleep(throttle_seconds)
            content = _fetch(client, FORMER_URL.format(letter=letter))
            (raw_dir / f"former_index_{letter}.html").write_bytes(content)
    finally:
        if own_client:
            client.close()
    (raw_dir / "meta.json").write_text(
        json.dumps(
            {
                "retrieved_at": dt.datetime.now(dt.UTC).isoformat(),
                "sources": [CURRENT_URL, FORMER_URL],
            },
            indent=1,
        ),
        encoding="utf-8",
    )


def _pk(href: str | None) -> str | None:
    match = re.search(r"[?&]pk=(\d+)", href or "")
    return match.group(1) if match else None


def _clean(value: str | None) -> str | None:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value or None


def _split_indexed_name(indexed: str) -> tuple[str | None, str | None]:
    """'Aitchison, Jenny' / 'Aplin, Mr Gregory John' -> (last, first-part)."""
    last, _, first = indexed.partition(",")
    return _clean(last), _clean(first)


def parse_current(content: bytes) -> list[dict]:
    """Rows from the all-members table (keyed by its header columns)."""
    doc = html.fromstring(content)
    rows: list[dict] = []
    for link in doc.xpath("//a[contains(@class,'prl-name-link')]"):
        source_id = _pk(link.get("href"))
        if not source_id:
            continue
        row = link.xpath("ancestor::tr[1]")
        if not row:
            continue
        table = row[0].xpath("ancestor::table[1]")[0]
        headers = [
            _clean(th.text_content()) for th in table.xpath(".//tr[1]/th | .//tr[1]/td")
        ]
        # header -> cell element (trailing headers may lack cells and vice versa)
        cells = dict(zip(headers, row[0].xpath("./td | ./th"), strict=False))

        def cell(name: str, cells: dict = cells) -> str | None:
            element = cells.get(name)
            return _clean(element.text_content()) if element is not None else None

        # the Position cell holds one line per role; electorate is the
        # "Member for {X}" line (LC members have none)
        electorate = None
        position = cells.get("Position")
        if position is not None:
            for line in position.itertext():
                match = re.match(r"\s*Member for (.+?)\s*$", line)
                if match:
                    electorate = match.group(1)
                    break
        last, first = _split_indexed_name(_clean(link.text_content()) or "")
        rows.append(
            {
                "source_member_id": source_id,
                "display_name": " ".join(filter(None, (first, last))),
                "first_name": first,
                "last_name": last,
                "house": HOUSES.get(cell("House") or "", cell("House")),
                "electorate": electorate,
                "party_name": cell("Party"),
                "is_current": True,
            }
        )
    return rows


def parse_former_index(content: bytes) -> list[dict]:
    """Identity rows from one former-members index page (no party — see module doc)."""
    doc = html.fromstring(content)
    rows: list[dict] = []
    for tr in doc.xpath("//table[@id='formerMembersTable']//tr[td]"):
        link = tr.xpath(".//a[contains(@href,'former-member-details')]")
        if not link:
            continue
        source_id = _pk(link[0].get("href"))
        if not source_id:
            continue
        cells = [_clean(td.text_content()) for td in tr.xpath("./td")]
        name, born, status = (cells + [None, None, None])[:3]
        last, first = _split_indexed_name(name or "")
        title = None
        if first:  # 'Mr Gregory John' — leading honorific
            match = re.match(
                r"(Mr|Mrs|Ms|Miss|Dr|The Hon\.?|Hon\.?|Rev(?:erend)?|Sir|Dame|Lady|Prof\.?)"
                r"\s+(.*)",
                first,
            )
            if match:
                title, first = match.group(1), _clean(match.group(2))
        date_of_birth = None
        if born:
            try:
                date_of_birth = dt.datetime.strptime(born, "%d/%m/%Y").date()
            except ValueError:
                pass
        rows.append(
            {
                "source_member_id": source_id,
                "display_name": " ".join(filter(None, (first, last))),
                "title": title,
                "first_name": first,
                "last_name": last,
                "date_of_birth": date_of_birth,
                "deceased": "deceased" in status.lower() if status else None,
                "is_current": False,
            }
        )
    return rows


def build_rows(raw_dir: Path) -> list[dict]:
    """Normalize the snapshots into register rows (offline)."""
    meta = json.loads((raw_dir / "meta.json").read_text(encoding="utf-8"))
    retrieved_at = dt.datetime.fromisoformat(meta["retrieved_at"])

    people: dict[str, dict] = {}
    # former first — anyone also on the current page keeps the current row
    for path in sorted(raw_dir.glob("former_index_*.html")):
        for row in parse_former_index(path.read_bytes()):
            people[row["source_member_id"]] = row
    for row in parse_current((raw_dir / "all_members.html").read_bytes()):
        people[row["source_member_id"]] = row

    return [
        row
        | {
            "member_id": member_id("nsw", row["source_member_id"]),
            "retrieved_at": retrieved_at,
            "jurisdiction": "nsw",
        }
        for row in people.values()
    ]


def build(data_dir: Path, *, offline: bool = False) -> int:
    """Fetch (unless offline) + normalize the NSW register; returns row count."""
    raw_dir = Path(data_dir) / "reference" / "raw" / "nsw"
    if not offline:
        fetch_raw(raw_dir)
    elif not (raw_dir / "meta.json").exists():
        raise FileNotFoundError(
            f"no stored snapshot under {raw_dir} — run once without --offline first"
        )
    return write_register(build_rows(raw_dir), Path(data_dir) / "reference")
