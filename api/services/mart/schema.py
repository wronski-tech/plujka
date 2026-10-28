"""DDL, documentation comments and the read-only role of the ``mart`` schema.

Table and column ``COMMENT``s are part of the contract: ``dynamic_sql`` reads them at runtime and
sends them to the LLM as the schema description. Keep them precise, in English, and update them
whenever the meaning of a column changes.
"""

from __future__ import annotations

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from api.services.config import MART_DATABASE_URL

SCHEMA = "mart"

BASE_TABLES = (
    "precincts",
    "sejm_districts",
    "sejm_committees",
    "sejm_district_lists",
    "sejm_candidates",
    "sejm_precinct_protocols",
    "sejm_precinct_list_votes",
    "sejm_precinct_candidate_votes",
    "senat_districts",
    "senat_candidates",
    "senat_precinct_protocols",
    "senat_precinct_candidate_votes",
)
"""Tables filled per election year by the loader (delete + insert of one year at a time)."""

DERIVED_RELATIONS = (
    "persons",
    "entity_lexicon",
    "v_sejm_national_results",
    "v_sejm_list_results",
    "v_sejm_turnout",
    "v_person_history",
)
"""Relations rebuilt from all loaded years after every load (see ``derived.py``)."""

QUERYABLE_RELATIONS = frozenset(BASE_TABLES + DERIVED_RELATIONS)
"""Everything the read-only role may select from; also the allowlist for LLM-generated SQL."""

TABLES_DDL = r"""
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE SCHEMA IF NOT EXISTS mart;
COMMENT ON SCHEMA mart IS 'Relational model of Polish parliamentary elections (Sejm and Senate) built from official PKW/KBW files. Every analytical answer must come from these relations.';

-- Folding used by entity resolution: lower case, no Polish diacritics, no "m. " / "gm. " prefix.
CREATE OR REPLACE FUNCTION mart.fold_name(t text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT translate(lower(regexp_replace(coalesce(t, ''), '^(m\.\s?st\.\s|m\.\s|gm\.\s)', '', 'i')),
                   'ąćęłńóśźż', 'acelnoszz')
$$;

CREATE TABLE IF NOT EXISTS mart.precincts (
    year INT NOT NULL,
    precinct_id INT NOT NULL,
    teryt_gmina TEXT,
    gmina TEXT,
    powiat TEXT,
    wojewodztwo TEXT,
    sejm_district_no INT NOT NULL,
    senat_district_no INT,
    precinct_no INT NOT NULL,
    address TEXT,
    precinct_type TEXT,
    area_type TEXT,
    residents INT,
    PRIMARY KEY (year, precinct_id)
);
COMMENT ON TABLE mart.precincts IS 'Polling stations (obwody głosowania) of one election year with their geography. Shared by Sejm and Senate facts; join on (year, precinct_id). Aggregate by gmina / powiat / wojewodztwo / sejm_district_no / senat_district_no.';
COMMENT ON COLUMN mart.precincts.teryt_gmina IS '6-digit TERYT code of the gmina (first 2 digits = voivodeship, first 4 = powiat). NULL for precincts abroad and on ships.';
COMMENT ON COLUMN mart.precincts.gmina IS 'Gmina name exactly as published by PKW, e.g. ''m. Kraków'' (city), ''gm. Izabelin'' (rural), Warsaw districts as ''Mokotów''. Abroad: country name or ''zagranica''; ships: ''statki''. Prefer filtering by teryt_gmina or ILIKE ''%Kraków%''.';
COMMENT ON COLUMN mart.precincts.powiat IS 'Powiat name, e.g. ''bolesławiecki'' or ''Kraków'' for cities with powiat rights. Not unique across voivodeships (e.g. ''grodziski''): combine with wojewodztwo. NULL abroad.';
COMMENT ON COLUMN mart.precincts.wojewodztwo IS 'Voivodeship in lower case, e.g. ''małopolskie''. NULL abroad and on ships.';
COMMENT ON COLUMN mart.precincts.sejm_district_no IS 'Sejm constituency number 1-41.';
COMMENT ON COLUMN mart.precincts.senat_district_no IS 'Senate constituency number 1-100.';
COMMENT ON COLUMN mart.precincts.precinct_no IS 'Precinct number, unique within a gmina (abroad: unique nationally).';
COMMENT ON COLUMN mart.precincts.address IS 'Seat of the precinct electoral commission (address text).';
COMMENT ON COLUMN mart.precincts.precinct_type IS 'Precinct type as published, e.g. ''stały'' (regular), ''zakład leczniczy'' (hospital), ''dom pomocy społecznej'', ''zakład karny'', ''zagranica'', ''statek''. 2015 uses ''powszechny'' / ''specjalny''.';
COMMENT ON COLUMN mart.precincts.area_type IS 'Urban/rural flag as published: ''miasto'' / ''wieś'' (2019+), ''gmina miejska'' / ''gmina wiejska'' / ''gmina miejsko-wiejska'' (2015).';
COMMENT ON COLUMN mart.precincts.residents IS 'Number of residents of the precinct area (Mieszkańcy).';

CREATE TABLE IF NOT EXISTS mart.sejm_districts (
    year INT NOT NULL,
    district_no INT NOT NULL,
    seat_city TEXT,
    wojewodztwo TEXT,
    seats INT NOT NULL,
    eligible_voters INT,
    description TEXT,
    PRIMARY KEY (year, district_no)
);
COMMENT ON TABLE mart.sejm_districts IS 'Sejm constituencies (okręgi wyborcze) 1-41. People name them by seat_city, e.g. "okręg krakowski" = seat_city ''Kraków''.';
COMMENT ON COLUMN mart.sejm_districts.seat_city IS 'Seat of the district electoral commission, e.g. ''Kraków''.';
COMMENT ON COLUMN mart.sejm_districts.wojewodztwo IS 'Voivodeship containing the district (most common voivodeship of its precincts).';
COMMENT ON COLUMN mart.sejm_districts.seats IS 'Number of Sejm seats elected in the district; sums to 460 per year.';
COMMENT ON COLUMN mart.sejm_districts.eligible_voters IS 'Registered voters in the district as published with the district list (before election day); for turnout use mart.v_sejm_turnout.';
COMMENT ON COLUMN mart.sejm_districts.description IS 'Official description of the district area (powiats and cities).';

CREATE TABLE IF NOT EXISTS mart.sejm_committees (
    year INT NOT NULL,
    committee_id INT NOT NULL,
    committee_name TEXT NOT NULL,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    is_coalition BOOLEAN NOT NULL,
    is_minority BOOLEAN NOT NULL,
    PRIMARY KEY (year, committee_id)
);
COMMENT ON TABLE mart.sejm_committees IS 'Electoral committees (komitety wyborcze) running for the Sejm in a year. committee_id is only meaningful together with year.';
COMMENT ON COLUMN mart.sejm_committees.committee_name IS 'Official committee name in upper case, e.g. ''KOMITET WYBORCZY PRAWO I SPRAWIEDLIWOŚĆ''. Names of the same party differ between years (e.g. PLATFORMA OBYWATELSKA RP in 2015, KOALICJA OBYWATELSKA later): compare parties across years by aliases.';
COMMENT ON COLUMN mart.sejm_committees.aliases IS 'Colloquial short names, e.g. {PiS}, {KO,PO}, {TD,PSL,Polska 2050}. Filter with ''PiS'' = ANY(aliases).';
COMMENT ON COLUMN mart.sejm_committees.is_coalition IS 'Coalition committee (8% national threshold instead of 5%).';
COMMENT ON COLUMN mart.sejm_committees.is_minority IS 'National-minority committee, exempt from the threshold (e.g. Mniejszość Niemiecka).';

CREATE TABLE IF NOT EXISTS mart.sejm_district_lists (
    year INT NOT NULL,
    district_no INT NOT NULL,
    committee_id INT NOT NULL,
    list_no INT NOT NULL,
    candidates_count INT,
    votes BIGINT NOT NULL,
    pct_of_district NUMERIC(6, 2),
    in_seat_allocation BOOLEAN NOT NULL,
    seats INT NOT NULL,
    PRIMARY KEY (year, district_no, committee_id)
);
COMMENT ON TABLE mart.sejm_district_lists IS 'Official result of one committee list in one Sejm district: votes and seats won. For national totals use mart.v_sejm_national_results.';
COMMENT ON COLUMN mart.sejm_district_lists.list_no IS 'Ballot list number. Numbers of nationwide committees are the same in every district; local committees may share a number (e.g. 11) across districts.';
COMMENT ON COLUMN mart.sejm_district_lists.votes IS 'Valid votes for the list in the district (= sum of its candidates'' votes).';
COMMENT ON COLUMN mart.sejm_district_lists.pct_of_district IS 'Share of the district''s valid votes, in percent (0-100).';
COMMENT ON COLUMN mart.sejm_district_lists.in_seat_allocation IS 'Whether the list took part in seat allocation (its committee passed the national threshold).';

CREATE TABLE IF NOT EXISTS mart.sejm_candidates (
    year INT NOT NULL,
    candidate_id INT NOT NULL,
    district_no INT NOT NULL,
    committee_id INT NOT NULL,
    list_no INT NOT NULL,
    list_position INT NOT NULL,
    full_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    first_names TEXT NOT NULL,
    gender TEXT,
    occupation TEXT,
    residence TEXT,
    party_membership TEXT,
    party_support TEXT,
    votes BIGINT NOT NULL,
    pct_of_list NUMERIC(6, 2),
    pct_of_district NUMERIC(6, 2),
    elected BOOLEAN NOT NULL,
    person_id INT,
    PRIMARY KEY (year, candidate_id)
);
COMMENT ON TABLE mart.sejm_candidates IS 'Sejm candidates with their official vote totals and whether they won a seat (elected = poseł). One row per candidacy; the same person in other years shares person_id.';
COMMENT ON COLUMN mart.sejm_candidates.full_name IS 'Given names and SURNAME, e.g. ''Jarosław Aleksander KACZYŃSKI''.';
COMMENT ON COLUMN mart.sejm_candidates.last_name IS 'Surname in upper case, e.g. ''KACZYŃSKI''. Match with ILIKE; inflected forms in questions (Kaczyńskiego) mean the same surname.';
COMMENT ON COLUMN mart.sejm_candidates.gender IS '''Kobieta'' (woman) or ''Mężczyzna'' (man).';
COMMENT ON COLUMN mart.sejm_candidates.residence IS 'Place of residence (Miejsce zamieszkania).';
COMMENT ON COLUMN mart.sejm_candidates.party_membership IS 'Party membership text as published, e.g. ''członek partii politycznej: Prawo i Sprawiedliwość'' or ''nie należy do partii politycznej''.';
COMMENT ON COLUMN mart.sejm_candidates.party_support IS 'Support declared by a party (Poparcie), if any.';
COMMENT ON COLUMN mart.sejm_candidates.pct_of_list IS 'Share of the list''s votes in the district, in percent.';
COMMENT ON COLUMN mart.sejm_candidates.pct_of_district IS 'Share of all valid votes in the district, in percent.';
COMMENT ON COLUMN mart.sejm_candidates.elected IS 'True when the candidate was awarded a Sejm seat.';
COMMENT ON COLUMN mart.sejm_candidates.person_id IS 'Same person across years and chambers (mart.persons); heuristic, see mart.persons.match_rule.';

CREATE TABLE IF NOT EXISTS mart.sejm_precinct_protocols (
    year INT NOT NULL,
    precinct_id INT NOT NULL,
    eligible_voters INT,
    ballots_issued INT,
    valid_ballots INT,
    invalid_votes INT,
    valid_votes INT,
    PRIMARY KEY (year, precinct_id)
);
COMMENT ON TABLE mart.sejm_precinct_protocols IS 'Sejm protocol numbers per precinct. Turnout (frekwencja) = SUM(valid_ballots) / SUM(eligible_voters); or use mart.v_sejm_turnout. Columns are NULL when PKW did not publish the precinct (a few military precincts abroad).';
COMMENT ON COLUMN mart.sejm_precinct_protocols.eligible_voters IS 'Voters entitled to vote (uprawnieni).';
COMMENT ON COLUMN mart.sejm_precinct_protocols.ballots_issued IS 'Voters who received a ballot (wydane karty); 2023 includes postal voting.';
COMMENT ON COLUMN mart.sejm_precinct_protocols.valid_ballots IS 'Valid ballots taken from the ballot box (karty ważne); numerator of official turnout.';
COMMENT ON COLUMN mart.sejm_precinct_protocols.invalid_votes IS 'Invalid votes (głosy nieważne).';
COMMENT ON COLUMN mart.sejm_precinct_protocols.valid_votes IS 'Valid votes cast for all lists; denominator of list support in percent.';

CREATE TABLE IF NOT EXISTS mart.sejm_precinct_list_votes (
    year INT NOT NULL,
    precinct_id INT NOT NULL,
    committee_id INT NOT NULL,
    votes INT NOT NULL,
    PRIMARY KEY (year, precinct_id, committee_id)
);
COMMENT ON TABLE mart.sejm_precinct_list_votes IS 'Votes for each committee list in each precinct. For gmina/powiat/voivodeship/district totals and percentages prefer mart.v_sejm_list_results.';

CREATE TABLE IF NOT EXISTS mart.sejm_precinct_candidate_votes (
    year INT NOT NULL,
    precinct_id INT NOT NULL,
    candidate_id INT NOT NULL,
    votes INT NOT NULL,
    PRIMARY KEY (year, candidate_id, precinct_id)
);
COMMENT ON TABLE mart.sejm_precinct_candidate_votes IS 'Votes for each Sejm candidate in each precinct of the candidate''s district (zeros included). Join mart.precincts to aggregate by gmina etc.';
CREATE INDEX IF NOT EXISTS idx_sejm_pcv_precinct ON mart.sejm_precinct_candidate_votes (year, precinct_id);

CREATE TABLE IF NOT EXISTS mart.senat_districts (
    year INT NOT NULL,
    district_no INT NOT NULL,
    seat_city TEXT,
    eligible_voters INT,
    description TEXT,
    PRIMARY KEY (year, district_no)
);
COMMENT ON TABLE mart.senat_districts IS 'Senate constituencies 1-100; each elects one senator.';
COMMENT ON COLUMN mart.senat_districts.seat_city IS 'Seat of the district electoral commission, e.g. ''Legnica''.';
COMMENT ON COLUMN mart.senat_districts.description IS 'Official description of the district area.';

CREATE TABLE IF NOT EXISTS mart.senat_candidates (
    year INT NOT NULL,
    candidate_id INT NOT NULL,
    district_no INT NOT NULL,
    committee_name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    first_names TEXT NOT NULL,
    gender TEXT,
    occupation TEXT,
    residence TEXT,
    party_membership TEXT,
    party_support TEXT,
    votes BIGINT NOT NULL,
    pct_of_district NUMERIC(6, 2),
    elected BOOLEAN NOT NULL,
    person_id INT,
    PRIMARY KEY (year, candidate_id)
);
COMMENT ON TABLE mart.senat_candidates IS 'Senate candidates with official vote totals; elected = senator. Senate committees are not linked to Sejm committees (match on committee_name text if needed).';
COMMENT ON COLUMN mart.senat_candidates.full_name IS 'Given names and SURNAME.';
COMMENT ON COLUMN mart.senat_candidates.last_name IS 'Surname in upper case.';
COMMENT ON COLUMN mart.senat_candidates.gender IS '''Kobieta'' or ''Mężczyzna''.';
COMMENT ON COLUMN mart.senat_candidates.pct_of_district IS 'Share of valid votes in the Senate district, in percent.';
COMMENT ON COLUMN mart.senat_candidates.person_id IS 'Same person across years and chambers (mart.persons).';

CREATE TABLE IF NOT EXISTS mart.senat_precinct_protocols (
    year INT NOT NULL,
    precinct_id INT NOT NULL,
    eligible_voters INT,
    ballots_issued INT,
    valid_ballots INT,
    invalid_votes INT,
    valid_votes INT,
    PRIMARY KEY (year, precinct_id)
);
COMMENT ON TABLE mart.senat_precinct_protocols IS 'Senate protocol numbers per precinct (same meaning as mart.sejm_precinct_protocols, separate ballot).';

CREATE TABLE IF NOT EXISTS mart.senat_precinct_candidate_votes (
    year INT NOT NULL,
    precinct_id INT NOT NULL,
    candidate_id INT NOT NULL,
    votes INT NOT NULL,
    PRIMARY KEY (year, candidate_id, precinct_id)
);
COMMENT ON TABLE mart.senat_precinct_candidate_votes IS 'Votes for each Senate candidate in each precinct of the district.';

CREATE TABLE IF NOT EXISTS mart.persons (
    person_id INT PRIMARY KEY,
    full_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    first_names TEXT NOT NULL,
    gender TEXT,
    match_rule TEXT NOT NULL,
    candidacies INT NOT NULL
);
COMMENT ON TABLE mart.persons IS 'People who ran for the Sejm or Senate in any loaded year. Candidacies are linked through sejm_candidates.person_id / senat_candidates.person_id; mart.v_person_history lists them. person_id is rebuilt on every load (not stable across loads).';
COMMENT ON COLUMN mart.persons.match_rule IS '''name+gender'': same full name and gender in every year; ''name+gender+residence'': the name was shared by several candidates in one election, so residence was also required. Different people with identical names can still be merged; people who changed surname are not linked.';
COMMENT ON COLUMN mart.persons.candidacies IS 'Number of candidacies (Sejm + Senate, all loaded years).';

CREATE TABLE IF NOT EXISTS mart.entity_lexicon (
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    folded TEXT NOT NULL,
    ref TEXT,
    detail TEXT,
    years INT[] NOT NULL
);
COMMENT ON TABLE mart.entity_lexicon IS 'Lookup of names that appear in questions (gmina, powiat, wojewodztwo, Sejm district seat, person surname, committee alias) used by entity resolution before SQL generation. Not needed in analytical queries.';
COMMENT ON COLUMN mart.entity_lexicon.folded IS 'mart.fold_name(name): lower case, no diacritics, no ''m. ''/''gm. '' prefix. For persons: folded surname.';
COMMENT ON COLUMN mart.entity_lexicon.ref IS 'Key to use in SQL: teryt code (gmina/powiat), district number, person_id, or committee name.';
CREATE INDEX IF NOT EXISTS idx_entity_lexicon_folded_trgm ON mart.entity_lexicon USING gin (folded gin_trgm_ops);
"""


def reader_credentials() -> tuple[str, str]:
    """User and password of the read-only role, taken from ``MART_DATABASE_URL``."""
    params = conninfo_to_dict(MART_DATABASE_URL)
    return params["user"], params.get("password") or ""


def ensure_schema(conn: psycopg.Connection) -> None:
    """Create the mart schema, tables and comments if missing (idempotent)."""
    conn.execute(TABLES_DDL)


def ensure_reader_role(conn: psycopg.Connection, statement_timeout_ms: int = 5000) -> None:
    """Create/refresh the login role used for LLM-generated SQL and grant it read access to mart only.

    The role cannot read other schemas (public tables are not granted), runs every transaction
    read-only, and has a server-side statement timeout, so a bad query cannot write data or hang
    the database even if it slips past ``safe_sql`` validation. Requires a superuser or CREATEROLE.
    """
    user, password = reader_credentials()
    role = sql.Identifier(user)
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,)).fetchone()
    if not exists:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(role))
    conn.execute(sql.SQL("ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(role, sql.Literal(password)))
    conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role))
    conn.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(role, sql.Literal(f"{statement_timeout_ms}ms")))
    conn.execute(sql.SQL("ALTER ROLE {} SET search_path = mart").format(role))
    database = conn.execute("SELECT current_database()").fetchone()[0]
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), role))
    conn.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(role))
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA mart TO {}").format(role))
    grant_reader_select(conn)


def grant_reader_select(conn: psycopg.Connection) -> None:
    """Grant SELECT on every current mart relation (tables, views, materialized views) to the reader."""
    role = sql.Identifier(reader_credentials()[0])
    conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA mart TO {}").format(role))
    conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION mart.fold_name(text) TO {}").format(role))
