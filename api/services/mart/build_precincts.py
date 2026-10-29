"""Helpers for elections whose result files carry their own precinct list (loader side, DuckDB).

Parliamentary years have a shared ``precincts`` table. Presidential, European Parliament and
referendum files instead repeat the geography and the protocol in every per-precinct result row,
so their protocol tables are built straight from those files with ``precinct_select`` and their
wide vote columns are unpivoted with ``unpivot_votes``.
"""

from __future__ import annotations

import duckdb

from api.services.mart.build_common import BuildError, column, columns_of, normalize_header, quote_ident, sql_literal

# Address text of the combined row that stands for several precincts (see ``rows_without_number``).
COMBINED_PRECINCTS = "%w ujęciu łącznym%"

GEO_ALIASES = {
    "teryt": ("TERYT gminy", "Kod TERYT", "TERYT Gminy", "Teryt Gminy", "Kod terytorialny gminy", "TERYT", "Kod gm."),
    "precinct_no": ("Numer obwodu", "Nr komisji", "Nr obwodu głosowania", "Nr obw.", "Numer", "Nr obwodu"),
    "gmina": ("Gmina", "Nazwa gminy"),
    "powiat": ("Powiat",),
    "wojewodztwo": ("Województwo",),
    "address": ("Siedziba", "Siedziba komisji", "Siedziba Obwodowej Komisji Wyborczej", "Adres", "Adres obwodu"),
}

PROTOCOL_ALIASES = {
    "eligible_voters": (
        "Liczba wyborców uprawnionych do głosowania",
        "Liczba osób uprawnionych do głosowania",
        "Liczba wyborców uprawnionych do głosowania (umieszczonych w spisie, z uwzględnieniem dodatkowych formularzy) w chwili zakończenia głosowania",
        "Liczba uprawnionych do głosowania",  # EP 2014
        "Uprawnieni do głosowania",  # president 2010
    ),
    "ballots_issued": (
        "Liczba wyborców, którym wydano karty do głosowania w lokalu wyborczym oraz w głosowaniu korespondencyjnym (łącznie)",
        "Liczba wyborców, którym wydano karty do głosowania",
        "Liczba osób, którym wydano karty do głosowania",
        "Wydane karty do głosowania",  # EP 2014, president 2010
        "Karty wydane (frekwencja)",  # president 2005, round 1
        "Wydane karty do głosowania - liczba",  # president 2005, round 2
    ),
    # President 2010 has no separate count of valid ballots: 'Głosy oddane' (votes cast) = valid + invalid votes.
    # President 2005 round 2: 'Karty wyjęte (głosy oddane)', ballots taken from the box (valid ballots).
    "valid_ballots": ("Liczba kart ważnych", "Karty ważne (głosy oddane)", "Głosy oddane", "Karty wyjęte (głosy oddane)"),
    "invalid_votes": (
        "Liczba głosów nieważnych", "Liczba głosów nieważnych (z kart ważnych)", "Głosy nieważne", "Głosy nieważne - liczba",
    ),
    "valid_votes": (
        "Liczba głosów ważnych oddanych łącznie na wszystkich kandydatów",
        "Liczba głosów ważnych oddanych łącznie na wszystkich kandydatów (z kart ważnych)",
        "Liczba głosów ważnych oddanych łącznie na obu kandydatów (z kart ważnych)",
        "Liczba głosów ważnych oddanych łącznie na wszystkie listy kandydatów",
        "Liczba głosów ważnych oddanych łącznie na wszystkie listy kandydatów (z kart ważnych)",
        "Liczba głosów ważnych",
        "Głosy ważne",  # EP 2014, president 2010
        "Głosy ważne - liczba",  # president 2005, round 2
    ),
}

# Columns that are never candidates or lists even when they follow the valid-votes column (president
# 2005 round 1 publishes invalid votes after valid votes).
NOT_VOTE_COLUMNS = {"razem", "year", "głosy nieważne"}

# 2005 files publish Warsaw as one gmina (146501) and give its district as a letter in the
# '7-ma cyfra TERYT' column (A = Bemowo ... R = Żoliborz, the order of the district TERYT codes).
WARSAW_DISTRICTS_2005 = {
    "A": ("146502", "Bemowo"), "B": ("146503", "Białołęka"), "C": ("146504", "Bielany"), "D": ("146505", "Mokotów"),
    "E": ("146506", "Ochota"), "F": ("146507", "Praga-Południe"), "G": ("146508", "Praga-Północ"),
    "H": ("146509", "Rembertów"), "I": ("146510", "Śródmieście"), "J": ("146511", "Targówek"),
    "K": ("146512", "Ursus"), "L": ("146513", "Ursynów"), "M": ("146514", "Wawer"), "N": ("146515", "Wesoła"),
    "O": ("146516", "Wilanów"), "P": ("146517", "Włochy"), "Q": ("146518", "Wola"), "R": ("146519", "Żoliborz"),
}

# Precinct type codes of the 2005/2007 files ('Kody' sheet of the 2005 presidential precinct file).
PRECINCT_TYPES_2005 = {
    "P": "powszechny", "L": "szpital", "K": "zakład karny", "A": "areszt śledczy", "Z": "zakład pomocy społecznej",
    "O": "oddział zewnętrzny zakładu karnego lub aresztu śledczego", "G": "zagranica", "S": "statek",
}


def create_warsaw_districts(con: duckdb.DuckDBPyConnection) -> None:
    """Create ``warsaw_districts_2005(letter, teryt, gmina)`` from WARSAW_DISTRICTS_2005."""
    rows = ", ".join(f"({sql_literal(k)}, {sql_literal(t)}, {sql_literal(n)})" for k, (t, n) in WARSAW_DISTRICTS_2005.items())
    con.execute(f"CREATE OR REPLACE TABLE warsaw_districts_2005 AS SELECT * FROM (VALUES {rows}) t(letter, teryt, gmina)")


def precinct_select(con: duckdb.DuckDBPyConnection, source: str, protocol: tuple[str, ...] = tuple(PROTOCOL_ALIASES)) -> str:
    """SELECT list turning a per-precinct result file into pkey + geography + protocol columns.

    Optional geography columns that a file lacks become NULL; ``wojewodztwo`` is derived from the
    TERYT code when present (some files carry a placeholder text instead of the name).
    """
    teryt = column(con, source, *GEO_ALIASES["teryt"])
    number = column(con, source, *GEO_ALIASES["precinct_no"])
    parts = [
        f"pkey({teryt}, {number}) AS pkey",
        f"clean_teryt({teryt}) AS teryt_gmina",
        f"CAST(int_({number}) AS INT) AS precinct_no",
    ]
    for name in ("gmina", "powiat", "address"):
        col = column(con, source, *GEO_ALIASES[name], required=False)
        parts.append(f"{col or 'CAST(NULL AS VARCHAR)'} AS {name}")
    woj = column(con, source, *GEO_ALIASES["wojewodztwo"], required=False)
    parts.append(f"coalesce(voivodeship(clean_teryt({teryt})), {f'lower({woj})' if woj else 'NULL'}) AS wojewodztwo")
    for name in protocol:
        parts.append(f"CAST(int_({column(con, source, *PROTOCOL_ALIASES[name])}) AS INT) AS {name}")
    return ",\n".join(parts)


def rows_without_number(con: duckdb.DuckDBPyConnection, files_by_district: dict[int, str], target: str) -> None:
    """Create ``target(district_no, address)`` from rows of per-district result files without a precinct number.

    The 2011 Sejm and Senate candidate files publish the military precincts abroad (numbers 1-8, 29,
    116 and 264) as one combined row ('Wybrane obwody głosowania o numerach ..., w ujęciu łącznym').
    Such rows have no precinct; builders count their votes towards the official totals only, and a
    check fails on any other row without a number.
    """
    con.execute(f"CREATE OR REPLACE TABLE {target} (district_no INT, address VARCHAR)")
    for district, path in files_by_district.items():
        source = f"read_parquet({sql_literal(path)}, hive_partitioning = false)"
        number = column(con, source, *GEO_ALIASES["precinct_no"])
        address = column(con, source, *GEO_ALIASES["address"])
        con.execute(f"INSERT INTO {target} SELECT {district}, {address} FROM {source} WHERE int_({number}) IS NULL")


def vote_columns(con: duckdb.DuckDBPyConnection, source: str) -> list[str]:
    """Headers after the valid-votes column (candidates or lists), without totals and without the
    percent columns of tables with a unit row (``'<name> - %'``, president 2005 round 2)."""
    columns = columns_of(con, source)
    valid = column(con, source, *PROTOCOL_ALIASES["valid_votes"]).strip('"').replace('""', '"')
    start = columns.index(valid) + 1
    return [c for c in columns[start:] if normalize_header(c) not in NOT_VOTE_COLUMNS and not c.endswith(" - %")]


def unpivot_votes(
    con: duckdb.DuckDBPyConnection, source: str, mapping: dict[str, int], target: str, key_name: str
) -> None:
    """Create ``target(pkey, <key_name>, votes)`` from the mapped wide columns of ``source``.

    Several headers may map to one key (their votes are summed). Raises BuildError when the mapping
    is empty, which would silently produce no votes.
    """
    if not mapping:
        raise BuildError(f"{source}: no vote columns mapped")
    teryt = column(con, source, *GEO_ALIASES["teryt"])
    number = column(con, source, *GEO_ALIASES["precinct_no"])
    con.execute(f"CREATE OR REPLACE TEMP TABLE {target}_map (header VARCHAR, key INT)")
    con.executemany(f"INSERT INTO {target}_map VALUES (?, ?)", list(mapping.items()))
    cols = ", ".join(quote_ident(h) for h in mapping)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {target} AS
        SELECT u.pkey, m.key AS {key_name}, CAST(sum(coalesce(int_(u.votes), 0)) AS INT) AS votes
        FROM (UNPIVOT (SELECT pkey({teryt}, {number}) AS pkey, {cols} FROM {source})
              ON {cols} INTO NAME header VALUE votes) u
        JOIN {target}_map m USING (header)
        GROUP BY ALL
        """
    )
