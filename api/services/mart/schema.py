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

PARLIAMENT_TABLES = (
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
PREZYDENT_TABLES = ("prezydent_candidates", "prezydent_precinct_protocols", "prezydent_precinct_candidate_votes")
PE_TABLES = (
    "pe_districts",
    "pe_committees",
    "pe_district_lists",
    "pe_candidates",
    "pe_precinct_protocols",
    "pe_precinct_list_votes",
    "pe_precinct_candidate_votes",
)
REFERENDUM_TABLES = ("referendum_questions", "referendum_precinct_protocols", "referendum_precinct_answers")
SEJMIK_TABLES = (
    "sejmik_committees",
    "sejmik_district_lists",
    "sejmik_candidates",
    "sejmik_gmina_protocols",
    "sejmik_gmina_list_votes",
)
WBP_TABLES = ("wbp_gmina_protocols", "wbp_candidates")
RADA_TABLES = ("rada_committees", "rada_districts", "rada_district_lists", "rada_candidates")

BASE_TABLES = (
    PARLIAMENT_TABLES + PREZYDENT_TABLES + PE_TABLES + REFERENDUM_TABLES + SEJMIK_TABLES + WBP_TABLES + RADA_TABLES
)
"""Tables filled by the loader: each election family replaces its own tables one year at a time."""

DERIVED_RELATIONS = (
    "persons",
    "entity_lexicon",
    "v_sejm_national_results",
    "v_sejm_list_results",
    "v_sejm_turnout",
    "v_person_history",
    "v_prezydent_results",
    "v_pe_national_results",
    "v_referendum_results",
    "v_sejmik_results",
    "v_rada_results",
    "v_turnout",
    "v_party_results",
    "v_party_change",
    "v_gmina_continuity",
)
"""Relations rebuilt from all loaded years after every load (see ``derived.py``)."""

QUERYABLE_RELATIONS = frozenset(BASE_TABLES + DERIVED_RELATIONS + ("elections",))
"""Everything the read-only role may select from; also the allowlist for LLM-generated SQL."""

TABLES_DDL = r"""
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE SCHEMA IF NOT EXISTS mart;
COMMENT ON SCHEMA mart IS 'Relational model of Polish elections (Sejm, Senate, President, European Parliament, referendums, voivodeship assemblies, mayors, councils) built from official PKW/KBW files. Every analytical answer must come from these relations.';

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
COMMENT ON COLUMN mart.precincts.senat_district_no IS 'Senate constituency number 1-100 (1-40 in 2005 and 2007).';
COMMENT ON COLUMN mart.precincts.precinct_no IS 'Precinct number, unique within a gmina (abroad: unique nationally).';
COMMENT ON COLUMN mart.precincts.address IS 'Seat of the precinct electoral commission (address text).';
COMMENT ON COLUMN mart.precincts.precinct_type IS 'Precinct type as published, e.g. ''stały'' (regular), ''zakład leczniczy'' (hospital), ''dom pomocy społecznej'', ''zakład karny'', ''zagranica'', ''statek''. 2015 uses ''powszechny'' / ''specjalny''; 2005-2011 ''powszechny'' (regular), ''szpital'', ''zakład pomocy społecznej'', ''zakład karny'', ''zagranica'', ''statek''.';
COMMENT ON COLUMN mart.precincts.area_type IS 'Urban/rural flag as published: ''miasto'' / ''wieś'' (2019+), ''gmina miejska'' / ''gmina wiejska'' / ''miasto w gminie miejsko-wiejskiej'' / ''obszar wiejski w gminie miejsko-wiejskiej'' (2005-2015; Warsaw districts ''dzielnica w m.st. Warszawa'' in 2005, 2007 and 2023).';
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
    residence_teryt TEXT,
    party_membership TEXT,
    party_support TEXT,
    votes BIGINT NOT NULL,
    pct_of_list NUMERIC(6, 2),
    pct_of_district NUMERIC(6, 2),
    elected BOOLEAN NOT NULL,
    person_id INT,
    PRIMARY KEY (year, candidate_id)
);
-- Added after the first release; existing databases get the column here.
ALTER TABLE mart.sejm_candidates ADD COLUMN IF NOT EXISTS residence_teryt TEXT;
COMMENT ON TABLE mart.sejm_candidates IS 'Sejm candidates with their official vote totals and whether they won a seat (elected = poseł). One row per candidacy; the same person in other years shares person_id.';
COMMENT ON COLUMN mart.sejm_candidates.full_name IS 'Given names and SURNAME, e.g. ''Jarosław Aleksander KACZYŃSKI''.';
COMMENT ON COLUMN mart.sejm_candidates.last_name IS 'Surname in upper case, e.g. ''KACZYŃSKI''. Match with ILIKE; inflected forms in questions (Kaczyńskiego) mean the same surname.';
COMMENT ON COLUMN mart.sejm_candidates.gender IS '''Kobieta'' (woman) or ''Mężczyzna'' (man).';
COMMENT ON COLUMN mart.sejm_candidates.residence IS 'Place of residence (Miejsce zamieszkania).';
COMMENT ON COLUMN mart.sejm_candidates.residence_teryt IS '6-digit TERYT code of the gmina of residence (TERYT m. z.); published from 2019, NULL in earlier years and for residence abroad.';
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
COMMENT ON TABLE mart.sejm_precinct_candidate_votes IS 'Votes for each Sejm candidate in each precinct of the candidate''s district (zeros included). Join mart.precincts to aggregate by gmina etc. No rows for 2005 (PKW published candidate totals only).';
CREATE INDEX IF NOT EXISTS idx_sejm_pcv_precinct ON mart.sejm_precinct_candidate_votes (year, precinct_id);
CREATE INDEX IF NOT EXISTS idx_precincts_teryt ON mart.precincts (teryt_gmina);

CREATE TABLE IF NOT EXISTS mart.senat_districts (
    year INT NOT NULL,
    district_no INT NOT NULL,
    seat_city TEXT,
    eligible_voters INT,
    description TEXT,
    PRIMARY KEY (year, district_no)
);
COMMENT ON TABLE mart.senat_districts IS 'Senate constituencies: from 2011 100 single-member districts; in 2005 and 2007 40 districts electing 2-4 senators each (seats = elected candidates of the district).';
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
    residence_teryt TEXT,
    party_membership TEXT,
    party_support TEXT,
    votes BIGINT NOT NULL,
    pct_of_district NUMERIC(6, 2),
    elected BOOLEAN NOT NULL,
    person_id INT,
    PRIMARY KEY (year, candidate_id)
);
-- Added after the first release; existing databases get the column here.
ALTER TABLE mart.senat_candidates ADD COLUMN IF NOT EXISTS residence_teryt TEXT;
COMMENT ON TABLE mart.senat_candidates IS 'Senate candidates with official vote totals; elected = senator. Senate committees are not linked to Sejm committees (match on committee_name text if needed).';
COMMENT ON COLUMN mart.senat_candidates.full_name IS 'Given names and SURNAME.';
COMMENT ON COLUMN mart.senat_candidates.last_name IS 'Surname in upper case.';
COMMENT ON COLUMN mart.senat_candidates.gender IS '''Kobieta'' or ''Mężczyzna''.';
COMMENT ON COLUMN mart.senat_candidates.residence_teryt IS '6-digit TERYT code of the gmina of residence (TERYT m. z.); published from 2019, NULL in earlier years and for residence abroad.';
COMMENT ON COLUMN mart.senat_candidates.pct_of_district IS 'Share of valid votes in the Senate district, in percent. 2005/2007: share of valid ballots; each voter had as many votes as the district had seats, so shares add up to more than 100.';
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
COMMENT ON TABLE mart.senat_precinct_protocols IS 'Senate protocol numbers per precinct (same meaning as mart.sejm_precinct_protocols, separate ballot). 2007: valid_votes counts valid ballots (multi-member districts). No rows for 2005 (Senate results were published per powiat only).';

CREATE TABLE IF NOT EXISTS mart.senat_precinct_candidate_votes (
    year INT NOT NULL,
    precinct_id INT NOT NULL,
    candidate_id INT NOT NULL,
    votes INT NOT NULL,
    PRIMARY KEY (year, candidate_id, precinct_id)
);
COMMENT ON TABLE mart.senat_precinct_candidate_votes IS 'Votes for each Senate candidate in each precinct of the district. No rows for 2005.';

CREATE TABLE IF NOT EXISTS mart.elections (
    election TEXT NOT NULL,
    year INT NOT NULL,
    round INT NOT NULL,
    election_date DATE,
    description TEXT,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (election, year, round)
);
COMMENT ON TABLE mart.elections IS 'Elections loaded into the mart. election: sejm, senat, prezydent, pe (European Parliament), referendum, sejmik (voivodeship assembly), wbp (wójt/burmistrz/prezydent miasta = mayor), rada (councils of gminas, powiats and Warsaw districts). round is 1, or 2 for run-offs (prezydent, wbp).';
COMMENT ON COLUMN mart.elections.loaded_at IS 'When the election was last loaded by scripts/build_mart.py.';

-- President ------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.prezydent_candidates (
    year INT NOT NULL,
    round INT NOT NULL,
    candidate_id INT NOT NULL,
    full_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    first_names TEXT NOT NULL,
    gender TEXT,
    occupation TEXT,
    residence TEXT,
    residence_teryt TEXT,
    party_membership TEXT,
    committee_name TEXT,
    votes BIGINT NOT NULL,
    pct NUMERIC(6, 2),
    elected BOOLEAN NOT NULL,
    advanced_to_round2 BOOLEAN,
    person_id INT,
    PRIMARY KEY (year, round, candidate_id)
);
-- Added after the first release; existing databases get the column here.
ALTER TABLE mart.prezydent_candidates ADD COLUMN IF NOT EXISTS residence_teryt TEXT;
COMMENT ON TABLE mart.prezydent_candidates IS 'Presidential candidates per election year and round with national vote totals. Round 2 (run-off) has the two leading candidates. For results per gmina/powiat/voivodeship use mart.v_prezydent_results.';
COMMENT ON COLUMN mart.prezydent_candidates.full_name IS 'Given names and SURNAME, e.g. ''Andrzej Sebastian DUDA''.';
COMMENT ON COLUMN mart.prezydent_candidates.last_name IS 'Surname in upper case.';
COMMENT ON COLUMN mart.prezydent_candidates.gender IS '''Kobieta'' or ''Mężczyzna''; NULL in 2005 and 2015 (not published).';
COMMENT ON COLUMN mart.prezydent_candidates.residence_teryt IS '6-digit TERYT code of the gmina of residence (TERYT m. z.); published from 2020, NULL in earlier years.';
COMMENT ON COLUMN mart.prezydent_candidates.votes IS 'Valid votes for the candidate nationwide in this round (= sum over precincts).';
COMMENT ON COLUMN mart.prezydent_candidates.pct IS 'Percent of valid votes nationwide (0-100).';
COMMENT ON COLUMN mart.prezydent_candidates.elected IS 'True for the candidate elected President in this round.';
COMMENT ON COLUMN mart.prezydent_candidates.advanced_to_round2 IS 'Round 1 only: the candidate advanced to the run-off.';
COMMENT ON COLUMN mart.prezydent_candidates.person_id IS 'Same person in other elections (mart.persons).';

CREATE TABLE IF NOT EXISTS mart.prezydent_precinct_protocols (
    year INT NOT NULL,
    round INT NOT NULL,
    precinct_id INT NOT NULL,
    teryt_gmina TEXT,
    gmina TEXT,
    powiat TEXT,
    wojewodztwo TEXT,
    precinct_no INT NOT NULL,
    address TEXT,
    eligible_voters INT,
    ballots_issued INT,
    valid_ballots INT,
    invalid_votes INT,
    valid_votes INT,
    PRIMARY KEY (year, round, precinct_id)
);
COMMENT ON TABLE mart.prezydent_precinct_protocols IS 'Presidential election precincts with their geography and protocol numbers, per year and round. Turnout = SUM(valid_ballots) / SUM(eligible_voters); or use mart.v_turnout.';
COMMENT ON COLUMN mart.prezydent_precinct_protocols.teryt_gmina IS '6-digit gmina TERYT (first 2 digits voivodeship, first 4 powiat); NULL abroad and on ships.';
COMMENT ON COLUMN mart.prezydent_precinct_protocols.gmina IS 'Gmina name as published, e.g. ''m. Kraków'', ''gm. Izabelin''.';
COMMENT ON COLUMN mart.prezydent_precinct_protocols.wojewodztwo IS 'Voivodeship in lower case; NULL abroad and on ships.';
COMMENT ON COLUMN mart.prezydent_precinct_protocols.valid_ballots IS 'Valid ballots (karty ważne); numerator of turnout.';
COMMENT ON COLUMN mart.prezydent_precinct_protocols.valid_votes IS 'Valid votes for all candidates; denominator of candidate support.';
CREATE INDEX IF NOT EXISTS idx_prez_pp_teryt ON mart.prezydent_precinct_protocols (teryt_gmina);

CREATE TABLE IF NOT EXISTS mart.prezydent_precinct_candidate_votes (
    year INT NOT NULL,
    round INT NOT NULL,
    precinct_id INT NOT NULL,
    candidate_id INT NOT NULL,
    votes INT NOT NULL,
    PRIMARY KEY (year, round, candidate_id, precinct_id)
);
COMMENT ON TABLE mart.prezydent_precinct_candidate_votes IS 'Votes for each presidential candidate in each precinct; join prezydent_precinct_protocols on (year, round, precinct_id).';

-- European Parliament ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.pe_districts (
    year INT NOT NULL,
    district_no INT NOT NULL,
    seat_city TEXT,
    seats INT NOT NULL,
    eligible_voters INT,
    description TEXT,
    PRIMARY KEY (year, district_no)
);
COMMENT ON TABLE mart.pe_districts IS 'European Parliament constituencies 1-13 (one or two voivodeships each). Seats per district follow from the national allocation.';
COMMENT ON COLUMN mart.pe_districts.seats IS 'Seats won in the district (sum of mart.pe_district_lists.seats).';
COMMENT ON COLUMN mart.pe_districts.description IS 'Official description of the district area (voivodeships).';

CREATE TABLE IF NOT EXISTS mart.pe_committees (
    year INT NOT NULL,
    committee_id INT NOT NULL,
    committee_name TEXT NOT NULL,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    is_coalition BOOLEAN NOT NULL,
    PRIMARY KEY (year, committee_id)
);
COMMENT ON TABLE mart.pe_committees IS 'Committees running in a European Parliament election. Compare parties across elections by aliases (same aliases as mart.sejm_committees).';
COMMENT ON COLUMN mart.pe_committees.aliases IS 'Colloquial short names, e.g. {PiS}, {KO,PO}. Filter with ''KO'' = ANY(aliases).';
COMMENT ON COLUMN mart.pe_committees.is_coalition IS 'Coalition committee (KKW).';

CREATE TABLE IF NOT EXISTS mart.pe_district_lists (
    year INT NOT NULL,
    district_no INT NOT NULL,
    committee_id INT NOT NULL,
    list_no INT NOT NULL,
    votes BIGINT NOT NULL,
    pct_of_district NUMERIC(6, 2),
    in_seat_allocation BOOLEAN NOT NULL,
    seats INT NOT NULL,
    PRIMARY KEY (year, district_no, committee_id)
);
COMMENT ON TABLE mart.pe_district_lists IS 'Official result of a committee list in a European Parliament district: votes and seats. National totals: mart.v_pe_national_results.';
COMMENT ON COLUMN mart.pe_district_lists.in_seat_allocation IS 'The committee passed the 5% national threshold.';

CREATE TABLE IF NOT EXISTS mart.pe_candidates (
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
    residence_teryt TEXT,
    party_membership TEXT,
    votes BIGINT NOT NULL,
    pct_of_list NUMERIC(6, 2),
    elected BOOLEAN NOT NULL,
    person_id INT,
    PRIMARY KEY (year, candidate_id)
);
-- Added after the first release; existing databases get the column here.
ALTER TABLE mart.pe_candidates ADD COLUMN IF NOT EXISTS residence_teryt TEXT;
COMMENT ON TABLE mart.pe_candidates IS 'European Parliament candidates with official vote totals; elected = MEP (europoseł).';
COMMENT ON COLUMN mart.pe_candidates.residence_teryt IS '6-digit TERYT code of the gmina of residence (TERYT m. z.); published from 2019, NULL in earlier years and for residence abroad.';
COMMENT ON COLUMN mart.pe_candidates.full_name IS 'Given names and SURNAME.';
COMMENT ON COLUMN mart.pe_candidates.last_name IS 'Surname in upper case.';
COMMENT ON COLUMN mart.pe_candidates.person_id IS 'Same person in other elections (mart.persons).';

CREATE TABLE IF NOT EXISTS mart.pe_precinct_protocols (
    year INT NOT NULL,
    precinct_id INT NOT NULL,
    district_no INT,
    teryt_gmina TEXT,
    gmina TEXT,
    powiat TEXT,
    wojewodztwo TEXT,
    precinct_no INT NOT NULL,
    address TEXT,
    eligible_voters INT,
    ballots_issued INT,
    valid_ballots INT,
    invalid_votes INT,
    valid_votes INT,
    PRIMARY KEY (year, precinct_id)
);
COMMENT ON TABLE mart.pe_precinct_protocols IS 'European Parliament precincts with geography and protocol numbers. district_no is the EP constituency. 2004 and 2009 were published per gmina only: each row is a whole gmina (precinct_no 0, address NULL; in 2004 Warsaw is one gmina, 146501), so do not count precincts for those years.';
COMMENT ON COLUMN mart.pe_precinct_protocols.teryt_gmina IS '6-digit gmina TERYT; NULL abroad and on ships.';
COMMENT ON COLUMN mart.pe_precinct_protocols.valid_votes IS 'Valid votes for all lists.';
CREATE INDEX IF NOT EXISTS idx_pe_pp_teryt ON mart.pe_precinct_protocols (teryt_gmina);

CREATE TABLE IF NOT EXISTS mart.pe_precinct_list_votes (
    year INT NOT NULL,
    precinct_id INT NOT NULL,
    committee_id INT NOT NULL,
    votes INT NOT NULL,
    PRIMARY KEY (year, precinct_id, committee_id)
);
COMMENT ON TABLE mart.pe_precinct_list_votes IS 'Votes for each committee list in each European Parliament precinct (2004 and 2009: in each gmina, see mart.pe_precinct_protocols).';

CREATE TABLE IF NOT EXISTS mart.pe_precinct_candidate_votes (
    year INT NOT NULL,
    precinct_id INT NOT NULL,
    candidate_id INT NOT NULL,
    votes INT NOT NULL,
    PRIMARY KEY (year, candidate_id, precinct_id)
);
COMMENT ON TABLE mart.pe_precinct_candidate_votes IS 'Votes for each European Parliament candidate in each precinct of the candidate''s district. No rows for 2004 and 2009 (not published).';
CREATE INDEX IF NOT EXISTS idx_pe_pcv_precinct ON mart.pe_precinct_candidate_votes (year, precinct_id);

-- Nationwide referendums ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.referendum_questions (
    year INT NOT NULL,
    question_no INT NOT NULL,
    topic TEXT NOT NULL,
    question_text TEXT NOT NULL,
    PRIMARY KEY (year, question_no)
);
COMMENT ON TABLE mart.referendum_questions IS 'Questions of nationwide referendums (2015: 3 questions, 2023: 4). Texts come from the referendum orders, not from the PKW data files. A referendum is binding only with turnout above 50%.';
COMMENT ON COLUMN mart.referendum_questions.topic IS 'Short Polish label, e.g. ''jednomandatowe okręgi wyborcze (JOW)'', ''wiek emerytalny''.';

CREATE TABLE IF NOT EXISTS mart.referendum_precinct_protocols (
    year INT NOT NULL,
    precinct_id INT NOT NULL,
    teryt_gmina TEXT,
    gmina TEXT,
    powiat TEXT,
    wojewodztwo TEXT,
    precinct_no INT NOT NULL,
    eligible_voters INT,
    ballots_issued INT,
    valid_ballots INT,
    PRIMARY KEY (year, precinct_id)
);
COMMENT ON TABLE mart.referendum_precinct_protocols IS 'Referendum precincts with geography and ballot numbers. Turnout = SUM(valid_ballots) / SUM(eligible_voters).';
COMMENT ON COLUMN mart.referendum_precinct_protocols.teryt_gmina IS '6-digit gmina TERYT; NULL abroad and on ships.';

CREATE TABLE IF NOT EXISTS mart.referendum_precinct_answers (
    year INT NOT NULL,
    precinct_id INT NOT NULL,
    question_no INT NOT NULL,
    valid_votes INT,
    invalid_votes INT,
    yes_votes INT,
    no_votes INT,
    PRIMARY KEY (year, question_no, precinct_id)
);
COMMENT ON TABLE mart.referendum_precinct_answers IS 'Answers per referendum question and precinct. For national or territorial results use mart.v_referendum_results.';
COMMENT ON COLUMN mart.referendum_precinct_answers.yes_votes IS 'Votes "TAK" (yes).';
COMMENT ON COLUMN mart.referendum_precinct_answers.no_votes IS 'Votes "NIE" (no).';

-- Voivodeship assemblies (sejmiki wojewódzkie) ------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.sejmik_committees (
    year INT NOT NULL,
    committee_id INT NOT NULL,
    committee_name TEXT NOT NULL,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (year, committee_id)
);
COMMENT ON TABLE mart.sejmik_committees IS 'Committees running for voivodeship assemblies (sejmiki województw) in local elections. Nationwide parties use the same aliases as mart.sejm_committees.';
COMMENT ON COLUMN mart.sejmik_committees.aliases IS 'Colloquial short names, e.g. {PiS}, {KO,PO}; empty for local committees.';

CREATE TABLE IF NOT EXISTS mart.sejmik_district_lists (
    year INT NOT NULL,
    wojewodztwo TEXT NOT NULL,
    district_no INT NOT NULL,
    committee_id INT NOT NULL,
    list_no INT,
    votes BIGINT NOT NULL,
    seats INT,
    PRIMARY KEY (year, wojewodztwo, district_no, committee_id)
);
COMMENT ON TABLE mart.sejmik_district_lists IS 'Result of a committee list in a voivodeship assembly district: votes and seats won. District numbers restart in every voivodeship.';
COMMENT ON COLUMN mart.sejmik_district_lists.seats IS 'Seats won by the list in the district (count of elected candidates).';

CREATE TABLE IF NOT EXISTS mart.sejmik_candidates (
    year INT NOT NULL,
    candidate_id INT NOT NULL,
    wojewodztwo TEXT NOT NULL,
    district_no INT NOT NULL,
    committee_id INT NOT NULL,
    list_no INT,
    list_position INT,
    full_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    first_names TEXT NOT NULL,
    gender TEXT,
    age INT,
    residence TEXT,
    votes BIGINT,
    elected BOOLEAN NOT NULL,
    person_id INT,
    PRIMARY KEY (year, candidate_id)
);
-- Added after the first release; existing databases get the column here.
ALTER TABLE mart.sejmik_candidates ADD COLUMN IF NOT EXISTS age INT;
COMMENT ON TABLE mart.sejmik_candidates IS 'Candidates for voivodeship assemblies; elected = radny sejmiku (regional councillor).';
COMMENT ON COLUMN mart.sejmik_candidates.full_name IS 'Given names and SURNAME.';
COMMENT ON COLUMN mart.sejmik_candidates.age IS 'Age in years on election day, as published in the candidate register (birth year is year - age or one less).';
COMMENT ON COLUMN mart.sejmik_candidates.votes IS 'Votes for the candidate; NULL when struck off after the ballots were printed (such votes count for the list only, so list votes in sejmik_district_lists can exceed the candidate sum).';
COMMENT ON COLUMN mart.sejmik_candidates.person_id IS 'Same person in national elections (mart.persons); a national person when the name is unambiguous and the birth years agree, otherwise a local person (same name, gender, birth year and powiat or voivodeship); NULL without age. See docs.';

CREATE TABLE IF NOT EXISTS mart.sejmik_gmina_protocols (
    year INT NOT NULL,
    teryt_gmina TEXT NOT NULL,
    gmina TEXT,
    powiat TEXT,
    wojewodztwo TEXT,
    eligible_voters INT,
    ballots_issued INT,
    valid_ballots INT,
    invalid_votes INT,
    valid_votes INT,
    PRIMARY KEY (year, teryt_gmina)
);
COMMENT ON TABLE mart.sejmik_gmina_protocols IS 'Voivodeship assembly ballot totals per gmina (local elections have no precincts abroad). Turnout = valid_ballots / eligible_voters.';

CREATE TABLE IF NOT EXISTS mart.sejmik_gmina_list_votes (
    year INT NOT NULL,
    teryt_gmina TEXT NOT NULL,
    committee_id INT NOT NULL,
    votes INT NOT NULL,
    PRIMARY KEY (year, teryt_gmina, committee_id)
);
COMMENT ON TABLE mart.sejmik_gmina_list_votes IS 'Votes for each committee in voivodeship assembly elections per gmina; percentages in mart.v_sejmik_results.';

-- Mayors: wójt / burmistrz / prezydent miasta -------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.wbp_gmina_protocols (
    year INT NOT NULL,
    round INT NOT NULL,
    teryt_gmina TEXT NOT NULL,
    gmina TEXT,
    powiat TEXT,
    wojewodztwo TEXT,
    office TEXT,
    eligible_voters INT,
    ballots_issued INT,
    valid_ballots INT,
    invalid_votes INT,
    valid_votes INT,
    PRIMARY KEY (year, round, teryt_gmina)
);
COMMENT ON TABLE mart.wbp_gmina_protocols IS 'Mayor elections (wójt, burmistrz, prezydent miasta) per gmina and round: ballot totals. Round 2 only in gminas where nobody got over 50% in round 1.';
COMMENT ON COLUMN mart.wbp_gmina_protocols.office IS 'Office: ''wójt'', ''burmistrz'' or ''prezydent miasta''.';

CREATE TABLE IF NOT EXISTS mart.wbp_candidates (
    year INT NOT NULL,
    round INT NOT NULL,
    candidate_id INT NOT NULL,
    teryt_gmina TEXT NOT NULL,
    full_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    first_names TEXT NOT NULL,
    gender TEXT,
    age INT,
    residence TEXT,
    committee_name TEXT,
    party_membership TEXT,
    votes INT,
    votes_against INT,
    pct NUMERIC(6, 2),
    elected BOOLEAN NOT NULL,
    person_id INT,
    PRIMARY KEY (year, round, candidate_id)
);
COMMENT ON TABLE mart.wbp_candidates IS 'Mayor candidates (wójt, burmistrz, prezydent miasta) per gmina and round with votes; elected = won the office. Join wbp_gmina_protocols on (year, round, teryt_gmina) for the gmina name.';
COMMENT ON COLUMN mart.wbp_candidates.votes IS 'Votes for the candidate (with a single candidate: votes "za" / for). NULL when the candidate withdrew or was struck off after the ballots were printed (in a run-off the remaining candidate then faced a for/against vote).';
COMMENT ON COLUMN mart.wbp_candidates.votes_against IS 'Only when there was a single candidate: votes "przeciw" (against).';
COMMENT ON COLUMN mart.wbp_candidates.pct IS 'Percent of valid votes in the gmina and round (0-100).';
COMMENT ON COLUMN mart.wbp_candidates.age IS 'Age in years on election day, as published in the candidate register (birth year is year - age or one less).';
COMMENT ON COLUMN mart.wbp_candidates.person_id IS 'Same person in national elections (mart.persons); a national person when the name is unambiguous and the birth years agree, otherwise a local person (same name, gender, birth year and powiat or voivodeship); NULL without age. See docs.';

-- Councils: rady gmin (incl. city councils), rady powiatów, rady dzielnic m.st. Warszawy ---------------
CREATE TABLE IF NOT EXISTS mart.rada_committees (
    year INT NOT NULL,
    committee_id INT NOT NULL,
    committee_name TEXT NOT NULL,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (year, committee_id)
);
COMMENT ON TABLE mart.rada_committees IS 'Committees running for councils (rady gmin, rady powiatów, rady dzielnic) in local elections, identified by name within a year (local committees with identical names share one row).';
COMMENT ON COLUMN mart.rada_committees.aliases IS 'Colloquial short names, e.g. {PiS}, {KO,PO}; only for committees that also ran for a voivodeship assembly that year under the same name (otherwise empty, also for local committees).';

CREATE TABLE IF NOT EXISTS mart.rada_districts (
    year INT NOT NULL,
    council_level TEXT NOT NULL,
    teryt TEXT NOT NULL,
    district_no INT NOT NULL,
    council_name TEXT,
    unit_name TEXT,
    teryt_powiat TEXT,
    wojewodztwo TEXT,
    city_with_powiat_rights BOOLEAN NOT NULL,
    voting_system TEXT NOT NULL,
    seats INT NOT NULL,
    candidates INT NOT NULL,
    contested BOOLEAN NOT NULL,
    valid_votes INT,
    PRIMARY KEY (year, council_level, teryt, district_no)
);
COMMENT ON TABLE mart.rada_districts IS 'Electoral districts (okręgi wyborcze) of councils: rady gmin/miast, rady powiatów and rady dzielnic m.st. Warszawy, with seats. A council is (year, council_level, teryt); district numbers restart in every council. Per-precinct results are not loaded.';
COMMENT ON COLUMN mart.rada_districts.council_level IS '''gmina'' (rada gminy / rada miasta, including cities with powiat rights and Rada m.st. Warszawy), ''powiat'' (rada powiatu) or ''dzielnica'' (rada dzielnicy m.st. Warszawy).';
COMMENT ON COLUMN mart.rada_districts.teryt IS 'Council unit: 6-digit gmina TERYT for gmina councils (Warsaw city council 146501), 6-digit Warsaw district TERYT (1465xx) for dzielnica councils, 4-digit powiat TERYT for powiat councils (same as teryt_powiat in other relations).';
COMMENT ON COLUMN mart.rada_districts.council_name IS 'Official council name, e.g. ''Rada Miasta Bolesławiec'', ''Rada Powiatu Bolesławieckiego''; NULL in 2010 and 2014 (not published in those files).';
COMMENT ON COLUMN mart.rada_districts.unit_name IS 'Name of the gmina (''m. Kraków'', ''gm. Bolesławiec''), powiat (''bolesławiecki'') or Warsaw district (''Bemowo'').';
COMMENT ON COLUMN mart.rada_districts.teryt_powiat IS '4-digit TERYT of the powiat the council unit lies in (for cities with powiat rights the city itself).';
COMMENT ON COLUMN mart.rada_districts.city_with_powiat_rights IS 'Gmina council of a city with powiat rights (miasto na prawach powiatu); such a city has no separate rada powiatu.';
COMMENT ON COLUMN mart.rada_districts.voting_system IS '''majoritarian'': the candidates with most votes win (2010: districts of 1-5 seats in gminas up to 20,000 inhabitants, each voter had as many votes as seats; 2014: single-member districts in gminas that are not cities with powiat rights; since 2018: single-member districts in gminas up to 20,000 inhabitants). ''proportional'': multi-member districts with committee lists (D''Hondt among lists that passed 5% in the council).';
COMMENT ON COLUMN mart.rada_districts.seats IS 'Seats to fill in the district (2010 and 2014: number of elected councillors, those files have no district register).';
COMMENT ON COLUMN mart.rada_districts.candidates IS 'Registered candidates in the district.';
COMMENT ON COLUMN mart.rada_districts.contested IS 'False when no vote took place: the number of candidates did not exceed the seats, so they were elected without voting (or nobody ran and the seat stayed empty).';
COMMENT ON COLUMN mart.rada_districts.valid_votes IS 'Valid votes cast in the district for all lists / candidates; NULL when no vote took place. In 2010 majoritarian districts with several seats this is the votes for all candidates, which exceeds the number of voters.';

CREATE TABLE IF NOT EXISTS mart.rada_district_lists (
    year INT NOT NULL,
    council_level TEXT NOT NULL,
    teryt TEXT NOT NULL,
    district_no INT NOT NULL,
    committee_id INT NOT NULL,
    list_no INT,
    candidates INT NOT NULL,
    seats INT NOT NULL,
    votes BIGINT,
    candidate_votes BIGINT,
    PRIMARY KEY (year, council_level, teryt, district_no, committee_id)
);
COMMENT ON TABLE mart.rada_district_lists IS 'Result of a committee in a council district: votes and seats. In majoritarian districts a "list" is the committee''s single candidate. Aggregates per council, voivodeship and country are in mart.v_rada_results.';
COMMENT ON COLUMN mart.rada_district_lists.list_no IS 'List number on the ballot (NULL for 2024 majoritarian districts, whose ballots have no list numbers).';
COMMENT ON COLUMN mart.rada_district_lists.seats IS 'Seats won by the list in the district (count of elected candidates).';
COMMENT ON COLUMN mart.rada_district_lists.votes IS 'Votes for the list. A vote marked next to a candidate struck off after the ballots were printed counts for the list only, so it can exceed candidate_votes. NULL when no vote took place, and in the few districts where such votes cannot be assigned to one list (see docs, "Known gaps").';
COMMENT ON COLUMN mart.rada_district_lists.candidate_votes IS 'Sum of the votes of the list''s candidates (NULL when no vote took place).';

CREATE TABLE IF NOT EXISTS mart.rada_candidates (
    year INT NOT NULL,
    candidate_id INT NOT NULL,
    council_level TEXT NOT NULL,
    teryt TEXT NOT NULL,
    district_no INT NOT NULL,
    committee_id INT NOT NULL,
    list_no INT,
    list_position INT,
    full_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    first_names TEXT NOT NULL,
    gender TEXT,
    age INT,
    residence TEXT,
    votes INT,
    elected BOOLEAN NOT NULL,
    person_id INT,
    PRIMARY KEY (year, candidate_id)
);
COMMENT ON TABLE mart.rada_candidates IS 'Candidates for councils (rady gmin/miast, rady powiatów, rady dzielnic m.st. Warszawy); elected = radny (councillor). Join mart.rada_districts on (year, council_level, teryt, district_no) for the council and mart.rada_committees on (year, committee_id) for the committee.';
COMMENT ON COLUMN mart.rada_candidates.full_name IS 'Given names and SURNAME.';
COMMENT ON COLUMN mart.rada_candidates.list_position IS 'Position on the list (NULL for 2024 majoritarian districts).';
COMMENT ON COLUMN mart.rada_candidates.votes IS 'Votes for the candidate; NULL when elected without voting (uncontested district) or when struck off after the ballots were printed (2024; the 2014 and 2018 files omit such candidates).';
COMMENT ON COLUMN mart.rada_candidates.age IS 'Age in years on election day, as published in the candidate register (birth year is year - age or one less).';
COMMENT ON COLUMN mart.rada_candidates.person_id IS 'Same person in national elections (mart.persons); a national person when the name is unambiguous and the birth years agree, otherwise a local person (same name, gender, birth year and powiat or voivodeship); NULL without age. See docs.';

CREATE TABLE IF NOT EXISTS mart.persons (
    person_id INT PRIMARY KEY,
    full_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    first_names TEXT NOT NULL,
    gender TEXT,
    match_rule TEXT NOT NULL,
    candidacies INT NOT NULL
);
COMMENT ON TABLE mart.persons IS 'People who ran in any loaded national election (Sejm, Senate, President, European Parliament); local candidates (sejmik, wbp, councils) are linked only when their name is unambiguous. Candidacies carry person_id; mart.v_person_history lists them. person_id is rebuilt on every load (not stable across loads).';
COMMENT ON COLUMN mart.persons.match_rule IS '''name+gender'': same full name and gender in every year; ''name+gender+residence'': the name was shared by several candidates in one election, so residence was also required; ''manual'': set by api/services/mart/person_overrides.csv; ''local:...'': only local candidacies (sejmik, mayors, councils) with the same name, gender, birth year (+-1) and powiat or voivodeship. Different people with identical names can still be merged; people who changed surname are not linked.';
COMMENT ON COLUMN mart.persons.candidacies IS 'Number of candidacies in all loaded elections.';

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


def loaded_elections() -> list[dict]:
    """Elections present in ``mart.elections`` (empty list when the mart is not built or unreachable)."""
    try:
        with psycopg.connect(MART_DATABASE_URL, connect_timeout=3) as conn:
            rows = conn.execute(
                "SELECT election, year, round, loaded_at FROM mart.elections ORDER BY election, year, round"
            ).fetchall()
    except psycopg.Error:
        return []
    return [{"election": e, "year": y, "round": r, "loaded_at": str(t)} for e, y, r, t in rows]
