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
GUS_TABLES = ("gus_units", "gus_indicators")

BASE_TABLES = (
    PARLIAMENT_TABLES + PREZYDENT_TABLES + PE_TABLES + REFERENDUM_TABLES + SEJMIK_TABLES + WBP_TABLES + RADA_TABLES
    + GUS_TABLES
)
"""Tables filled by the loader: each election family replaces its own tables one year at a time."""

DERIVED_RELATIONS = (
    "persons",
    "party_families",
    "senat_byelections",
    "v_seat_methods",
    "v_gus_units",
    "gmina_codes_pre1999",
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
    "v_precinct_results",
    "v_candidate_gmina_votes",
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

-- Seat allocation by a divisor method or Hare-Niemeyer, for checks of official seats and "what if" questions.
CREATE OR REPLACE FUNCTION mart.allocate_seats(votes numeric[], seats bigint, method text DEFAULT 'dhondt')
RETURNS int[] LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
BEGIN
  IF method NOT IN ('dhondt', 'sainte_lague', 'sainte_lague_modified', 'sainte_lague_modified_whole', 'hare_niemeyer') THEN
    RAISE EXCEPTION 'allocate_seats: unknown method %, use dhondt, sainte_lague, sainte_lague_modified, sainte_lague_modified_whole or hare_niemeyer', method;
  END IF;
  IF method = 'hare_niemeyer' THEN
    -- Whole parts of the quotas first, then the remaining seats by the largest remainders.
    RETURN (
      WITH v AS (SELECT i, coalesce(votes[i], 0) AS n FROM generate_subscripts(votes, 1) AS i),
      t AS (SELECT sum(n) AS total FROM v),
      q AS (SELECT v.i, v.n, CASE WHEN t.total > 0 THEN greatest(seats, 0) * v.n / t.total ELSE 0 END AS quota FROM v, t),
      f AS (SELECT i, n, floor(quota)::int AS whole, quota - floor(quota) AS remainder FROM q),
      extra AS (
        SELECT i FROM f WHERE n > 0 ORDER BY remainder DESC, n DESC, i
        LIMIT greatest(seats, 0) - (SELECT sum(whole) FROM f)
      )
      SELECT array_agg(f.whole + (SELECT count(*) FROM extra WHERE extra.i = f.i)::int ORDER BY f.i) FROM f
    );
  END IF;
  RETURN (
    WITH v AS (SELECT i, coalesce(votes[i], 0) AS n FROM generate_subscripts(votes, 1) AS i),
    q AS (
      SELECT i, n, CASE WHEN method = 'sainte_lague_modified_whole' THEN floor(quotient) ELSE quotient END AS quotient
      FROM (
        SELECT v.i, v.n, v.n / CASE WHEN method = 'dhondt' THEN k
                                    WHEN method IN ('sainte_lague_modified', 'sainte_lague_modified_whole') AND k = 1 THEN 1.4
                                    ELSE 2 * k - 1 END AS quotient
        FROM v CROSS JOIN generate_series(1, greatest(seats, 0)) AS k
        WHERE v.n > 0
      ) e
    ),
    won AS (SELECT i FROM q ORDER BY quotient DESC, n DESC, i LIMIT greatest(seats, 0))
    SELECT array_agg((SELECT count(*) FROM won WHERE won.i = v.i)::int ORDER BY v.i) FROM v
  );
END
$$;
COMMENT ON FUNCTION mart.allocate_seats(numeric[], bigint, text) IS 'Seats per list (same order as votes): dhondt (Sejm since 2005, sejmiki and councils since 2002, EP nationally), sainte_lague (inside 2006 list blocs), sainte_lague_modified (first divisor 1.4: Sejm 2001), sainte_lague_modified_whole (the same with quotients cut to whole numbers, which reproduces the 1994 gmina council seats), or hare_niemeyer (largest remainders: a committee''s EP seats between its district lists). Ties go to more votes, then to the earlier element. Pass only lists that passed the threshold, e.g. mart.allocate_seats(array_agg(votes ORDER BY committee_id), 12) per district. NULL or 0 votes get no seat.';

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
COMMENT ON COLUMN mart.precincts.teryt_gmina IS '6-digit TERYT code of the gmina (first 2 digits = voivodeship, first 4 = powiat), as valid in that year (2001: pre-2002 codes, but Warsaw as its districts 1465xx). NULL for precincts abroad and on ships, and in 1991, 1993 and 1997 (pre-1999 division, see teryt_gmina_pre1999).';
COMMENT ON COLUMN mart.precincts.gmina IS 'Gmina name exactly as published by PKW, e.g. ''m. Kraków'' (city), ''gm. Izabelin'' (rural), Warsaw districts as ''Mokotów''. Abroad: country name or ''zagranica''; ships: ''statki''. Prefer filtering by teryt_gmina or ILIKE ''%Kraków%''.';
COMMENT ON COLUMN mart.precincts.powiat IS 'Powiat name, e.g. ''bolesławiecki'' or ''Kraków'' for cities with powiat rights. Not unique across voivodeships (e.g. ''grodziski''): combine with wojewodztwo. NULL abroad, and in 1991, 1993 and 1997 (no powiats before 1999).';
COMMENT ON COLUMN mart.precincts.wojewodztwo IS 'Voivodeship in lower case, e.g. ''małopolskie''. 1991, 1993 and 1997: one of the 49 voivodeships of 1975-1998, e.g. ''krakowskie'', ''bielskie'' (not comparable with later voivodeships). NULL abroad and on ships.';
COMMENT ON COLUMN mart.precincts.sejm_district_no IS 'Sejm constituency number 1-41 (1-37 in 1991, 1-52 in 1993 and 1997).';
COMMENT ON COLUMN mart.precincts.senat_district_no IS 'Senate constituency number 1-100 (1-40 in 2001, 2005 and 2007; 1991-1997 the 49 voivodeships, numbered 1 warszawskie ... 49 zielonogórskie as in mart.senat_districts).';
COMMENT ON COLUMN mart.precincts.precinct_no IS 'Precinct number, unique within a gmina (abroad: unique nationally).';
COMMENT ON COLUMN mart.precincts.address IS 'Seat of the precinct electoral commission (address text).';
COMMENT ON COLUMN mart.precincts.precinct_type IS 'Precinct type as published, e.g. ''stały'' (regular), ''zakład leczniczy'' (hospital), ''dom pomocy społecznej'', ''zakład karny'', ''zagranica'', ''statek''. 2015 uses ''powszechny'' / ''specjalny''; 2005-2011 ''powszechny'' (regular), ''szpital'', ''zakład pomocy społecznej'', ''zakład karny'', ''zagranica'', ''statek''; 2001 ''powszechny'', ''zakład opieki zdrowotnej lub pomocy społecznej'', ''zakład karny lub areszt śledczy'', ''zagranica'', ''statek''; 1993 and 1997 the same as 2001; 1991 only ''zagranica'' (other precincts NULL).';
COMMENT ON COLUMN mart.precincts.area_type IS 'Urban/rural flag as published: ''miasto'' / ''wieś'' (2019+), ''gmina miejska'' / ''gmina wiejska'' / ''miasto w gminie miejsko-wiejskiej'' / ''obszar wiejski w gminie miejsko-wiejskiej'' (2001-2015; Warsaw districts ''dzielnica w m.st. Warszawa'' in 2005, 2007 and 2023); 1993: ''miasto'' / ''wieś''; NULL in 1991 and 1997.';
COMMENT ON COLUMN mart.precincts.residents IS 'Number of residents of the precinct area (Mieszkańcy).';
ALTER TABLE mart.precincts ADD COLUMN IF NOT EXISTS teryt_gmina_pre1999 TEXT;
ALTER TABLE mart.precincts ADD COLUMN IF NOT EXISTS boundary_description TEXT;
COMMENT ON COLUMN mart.precincts.boundary_description IS 'Area of the precinct as published by PKW (Opis granic): streets, often with house-number ranges and odd/even sides, or villages, e.g. ''Bolesławiec ulice: Agatowa, Bazaltowa, Ceramiczna 1 - 13, ...''. To find the precinct of an address, filter by teryt_gmina and boundary_description ILIKE ''%street%'' and check the house number in the text. Special precincts name their institution (hospital, care home, prison) and abroad precincts their country. 2011, 2015, 2019 and 2023 only; NULL in other years.';
COMMENT ON COLUMN mart.precincts.teryt_gmina_pre1999 IS '1991, 1993 and 1997 only: 5-digit gmina code of the 1975-1998 division (first 2 digits = old voivodeship, odd numbers 01-97) as published by PKW; no official key maps it to post-1999 TERYT, so teryt_gmina and powiat are NULL then. NULL abroad, on ships and from 2001.';

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
COMMENT ON TABLE mart.sejm_districts IS 'Sejm constituencies (okręgi wyborcze) 1-41 (1991: 1-37, 1993 and 1997: 1-52). People name them by seat_city, e.g. "okręg krakowski" = seat_city ''Kraków''. 1991-1997 also district_no 0: the national list (lista ogólnopolska, 69 seats), not a territorial district; exclude it (district_no > 0) when listing constituencies.';
COMMENT ON COLUMN mart.sejm_districts.seat_city IS 'Seat of the district electoral commission, e.g. ''Kraków''.';
COMMENT ON COLUMN mart.sejm_districts.wojewodztwo IS 'Voivodeship containing the district (most common voivodeship of its precincts).';
COMMENT ON COLUMN mart.sejm_districts.seats IS 'Number of Sejm seats elected in the district; sums to 460 per year (1991-1997: 391 in districts plus 69 in district_no 0, the national list).';
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
COMMENT ON COLUMN mart.sejm_committees.is_coalition IS 'Coalition committee (8% national threshold instead of 5%; 1993 and 1997 as named in PKW''s announcements; always false in 1991, which had no threshold).';
COMMENT ON COLUMN mart.sejm_committees.is_minority IS 'National-minority committee, exempt from the threshold (e.g. Mniejszość Niemiecka). 1991: minority national lists; 1993 and 1997: committees with at least one district list exempt from the threshold.';

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
COMMENT ON COLUMN mart.sejm_district_lists.votes IS 'Valid votes for the list in the district (= sum of its candidates'' votes). 0 for national lists (district_no 0, 1991-1997): they had no votes of their own; their seats were allocated by the committee''s district votes nationwide.';
COMMENT ON COLUMN mart.sejm_district_lists.pct_of_district IS 'Share of the district''s valid votes, in percent (0-100).';
COMMENT ON COLUMN mart.sejm_district_lists.in_seat_allocation IS 'Whether the list took part in seat allocation (its committee passed the national threshold). 1991: always true in districts (no threshold); national lists (district_no 0) 1993/1997 7% nationally, 1991 5% nationally or seats in at least 5 districts or a minority list.';
ALTER TABLE mart.sejm_district_lists ADD COLUMN IF NOT EXISTS list_group INT;
COMMENT ON COLUMN mart.sejm_district_lists.list_group IS '1991 only: lists joined in a bloc (blok list, art. 77 of the 1991 law) share list_group (district lists: the bloc number published by KBW; national lists, district_no 0: the bloc''s first list number). A bloc first won seats together, then split them among its lists. NULL otherwise.';

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
ALTER TABLE mart.sejm_candidates ADD COLUMN IF NOT EXISTS national_list_position INT;
ALTER TABLE mart.sejm_candidates ADD COLUMN IF NOT EXISTS national_list_seat BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE mart.sejm_candidates ADD COLUMN IF NOT EXISTS age INT;
COMMENT ON COLUMN mart.sejm_candidates.age IS 'Age in years as published in the candidate register (Wiek), 1991, 1993 and 1997 only (birth year is year - age or one less); NULL from 2001, when national registers no longer publish it.';
COMMENT ON COLUMN mart.sejm_candidates.national_list_position IS '1991-1997 only: position on the committee''s national list (lista ogólnopolska), for candidates who were also on it; every national-list candidate also ran on a district list (district_no). NULL otherwise.';
COMMENT ON COLUMN mart.sejm_candidates.national_list_seat IS '1991-1997 only: true when the seat (elected) came from the national list (district_no 0 in mart.sejm_districts), not from the candidate''s district list. False otherwise.';
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
COMMENT ON TABLE mart.sejm_precinct_protocols IS 'Sejm protocol numbers per precinct. Turnout (frekwencja) = SUM(valid_ballots) / SUM(eligible_voters); or use mart.v_sejm_turnout. Columns are NULL when PKW did not publish the precinct (a few military precincts abroad). 1991-1997: valid_ballots holds the votes cast (głosy oddane = valid + invalid votes), ballots_issued is NULL in 1993; 1993 eligible voters are 21,807 below PKW''s national figure (KBW''s files for districts 21 and 32).';
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
COMMENT ON TABLE mart.sejm_precinct_candidate_votes IS 'Votes for each Sejm candidate in each precinct of the candidate''s district (zeros included). Join mart.precincts to aggregate by gmina etc. No rows for 1991, 1993, 1997, 2001 and 2005 (PKW published candidate totals only).';
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
COMMENT ON TABLE mart.senat_districts IS 'Senate constituencies: from 2011 100 single-member districts; in 2001, 2005 and 2007 40 districts electing 2-4 senators each; 1991, 1993 and 1997 the 49 voivodeships of 1975-1998, 2 senators each, 3 in warszawskie (1) and katowickie (14) (seats = elected candidates of the district).';
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
ALTER TABLE mart.senat_candidates ADD COLUMN IF NOT EXISTS age INT;
COMMENT ON COLUMN mart.senat_candidates.age IS 'Age in years as published in the candidate register (Wiek), 1991, 1993 and 1997 only (birth year is year - age or one less); NULL from 2001, when national registers no longer publish it. Senators had to be 21 until 1997 and 30 from 2001.';
COMMENT ON TABLE mart.senat_candidates IS 'Senate candidates with official vote totals; elected = senator. Senate committees are not linked to Sejm committees (match on committee_name text if needed).';
COMMENT ON COLUMN mart.senat_candidates.full_name IS 'Given names and SURNAME.';
COMMENT ON COLUMN mart.senat_candidates.last_name IS 'Surname in upper case.';
COMMENT ON COLUMN mart.senat_candidates.gender IS '''Kobieta'' or ''Mężczyzna''.';
COMMENT ON COLUMN mart.senat_candidates.residence_teryt IS '6-digit TERYT code of the gmina of residence (TERYT m. z.); published from 2019, NULL in earlier years and for residence abroad.';
COMMENT ON COLUMN mart.senat_candidates.pct_of_district IS 'Share of valid votes in the Senate district, in percent. 1991-2007: share of valid ballots; each voter had as many votes as the district had seats, so shares add up to more than 100.';
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
COMMENT ON TABLE mart.senat_precinct_protocols IS 'Senate protocol numbers per precinct (same meaning as mart.sejm_precinct_protocols, separate ballot). 2001 and 2007: valid_votes counts ballots, not votes (multi-member districts; 2001 invalid_votes = valid ballots without a valid vote). No rows for 2005 (Senate results were published per powiat only) and 1991 (candidates only). 1993 and 1997: valid_ballots = votes cast, valid_votes = valid ballots, ballots_issued NULL; 1993 voivodeship 38 (skierniewickie) has candidate votes but an all-NULL protocol (not published per precinct).';

CREATE TABLE IF NOT EXISTS mart.senat_precinct_candidate_votes (
    year INT NOT NULL,
    precinct_id INT NOT NULL,
    candidate_id INT NOT NULL,
    votes INT NOT NULL,
    PRIMARY KEY (year, candidate_id, precinct_id)
);
COMMENT ON TABLE mart.senat_precinct_candidate_votes IS 'Votes for each Senate candidate in each precinct of the district. No rows for 2005 and 1991.';

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

-- Consistency checks a family-year passed when it was built (not queryable by generated SQL).
CREATE TABLE IF NOT EXISTS mart.build_checks (
    family TEXT NOT NULL,
    year INT NOT NULL,
    elections TEXT[] NOT NULL,
    check_no INT NOT NULL,
    label TEXT NOT NULL,
    failing_rows BIGINT NOT NULL,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (family, year, check_no)
);
COMMENT ON TABLE mart.build_checks IS 'Consistency checks against the official PKW/KBW totals that scripts/build_mart.py ran for one family-year (parliament, prezydent, pe, referendum, samorzad, gus) before loading it; a family-year is loaded only when every check returns 0 failing rows.';
COMMENT ON COLUMN mart.build_checks.elections IS 'Values of mart.elections.election the family-year loads (e.g. sejm and senat for parliament).';
COMMENT ON COLUMN mart.build_checks.label IS 'What the check verifies, in English (e.g. district lists whose precinct votes differ from the official total).';
COMMENT ON COLUMN mart.build_checks.failing_rows IS 'Rows that broke the check; always 0 for a loaded family-year.';

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
COMMENT ON TABLE mart.prezydent_candidates IS 'Presidential candidates per election year and round with national vote totals. Round 2 (run-off) has the two leading candidates; 2000 was decided in round 1 (no round 2). 1990 and 1995: candidates and totals as in PKW''s announcements (given names as announced); abroad and ships voted in round 1 only. For results per gmina/powiat/voivodeship use mart.v_prezydent_results.';
COMMENT ON COLUMN mart.prezydent_candidates.full_name IS 'Given names and SURNAME, e.g. ''Andrzej Sebastian DUDA''.';
COMMENT ON COLUMN mart.prezydent_candidates.last_name IS 'Surname in upper case.';
COMMENT ON COLUMN mart.prezydent_candidates.gender IS '''Kobieta'' or ''Mężczyzna''; NULL in 1990, 1995, 2000, 2005 and 2015 (not published).';
COMMENT ON COLUMN mart.prezydent_candidates.residence_teryt IS '6-digit TERYT code of the gmina of residence (TERYT m. z.); published from 2020, NULL in earlier years.';
COMMENT ON COLUMN mart.prezydent_candidates.votes IS 'Valid votes for the candidate nationwide in this round (= sum over precincts).';
COMMENT ON COLUMN mart.prezydent_candidates.pct IS 'Percent of valid votes nationwide (0-100).';
COMMENT ON COLUMN mart.prezydent_candidates.elected IS 'True for the candidate elected President in this round.';
COMMENT ON COLUMN mart.prezydent_candidates.advanced_to_round2 IS 'Round 1 only: the candidate advanced to the run-off (false for all candidates of 2000, when there was none).';
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
COMMENT ON COLUMN mart.prezydent_precinct_protocols.teryt_gmina IS '6-digit gmina TERYT (first 2 digits voivodeship, first 4 powiat); NULL abroad and on ships. 2000: pre-2002 codes, Warsaw gminas (Warszawa-Centrum included) as their districts 1465xx. NULL in 1990 and 1995 (pre-1999 division, see teryt_gmina_pre1999).';
ALTER TABLE mart.prezydent_precinct_protocols ADD COLUMN IF NOT EXISTS teryt_gmina_pre1999 TEXT;
COMMENT ON COLUMN mart.prezydent_precinct_protocols.teryt_gmina_pre1999 IS '1990 and 1995 only: 5-digit gmina code of the 1975-1998 division (49 voivodeships; first 2 digits = old voivodeship, odd numbers 01-97), as published by PKW (1990: the gmina of 1993 that KBW assigned the precinct to). No official key maps these codes to post-1999 TERYT, so teryt_gmina and powiat are NULL then. NULL abroad, on ships and from 2000.';
COMMENT ON COLUMN mart.prezydent_precinct_protocols.gmina IS 'Gmina name as published, e.g. ''m. Kraków'', ''gm. Izabelin''.';
COMMENT ON COLUMN mart.prezydent_precinct_protocols.wojewodztwo IS 'Voivodeship in lower case; NULL abroad and on ships. 1990 and 1995: one of the 49 voivodeships of 1975-1998 (e.g. ''warszawskie'', ''bielskie''); ''łódzkie'', ''lubelskie'' and ''opolskie'' then covered other areas than today''s voivodeships of that name, so compare voivodeships across 1999 only at country level.';
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
COMMENT ON TABLE mart.referendum_questions IS 'Questions of nationwide referendums (1996: 5 questions of two referendums held the same day, question 1 on universal enfranchisement (uwłaszczenie) and questions 2-5 on the use of state property; 1997: 1, the Constitution; 2003: 1 question, 2015: 3, 2023: 4). Texts come from the referendum orders, not from the PKW data files. A referendum is binding only with turnout above 50%.';
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
COMMENT ON TABLE mart.referendum_precinct_protocols IS 'Referendum precincts with geography and ballot numbers. Turnout = SUM(valid_ballots) / SUM(eligible_voters). 1996: ballots_issued and valid_ballots are NULL here because the two referendums had separate ballots; they are per question in mart.referendum_precinct_answers. 1997: without the three precincts whose vote the Supreme Court annulled, 2003: without two such precincts (PKW''s corrected results).';
COMMENT ON COLUMN mart.referendum_precinct_protocols.teryt_gmina IS '6-digit gmina TERYT; NULL abroad and on ships, and in 1996 and 1997 (pre-1999 division, see teryt_gmina_pre1999).';
COMMENT ON COLUMN mart.referendum_precinct_protocols.wojewodztwo IS 'Voivodeship in lower case; NULL abroad and on ships. 1996 and 1997: one of the 49 voivodeships of 1975-1998 (''łódzkie'', ''lubelskie'' and ''opolskie'' then covered other areas than today''s voivodeships of that name).';
ALTER TABLE mart.referendum_precinct_protocols ADD COLUMN IF NOT EXISTS teryt_gmina_pre1999 TEXT;
COMMENT ON COLUMN mart.referendum_precinct_protocols.teryt_gmina_pre1999 IS '1996 and 1997 only: 5-digit gmina code of the 1975-1998 division (first 2 digits = old voivodeship, odd numbers 01-97) as published by PKW; no official key maps it to post-1999 TERYT. NULL abroad, on ships and in other years.';

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
ALTER TABLE mart.referendum_precinct_answers ADD COLUMN IF NOT EXISTS ballots_issued INT;
ALTER TABLE mart.referendum_precinct_answers ADD COLUMN IF NOT EXISTS valid_ballots INT;
COMMENT ON COLUMN mart.referendum_precinct_answers.ballots_issued IS '1996 only: ballots issued for the ballot paper carrying this question (question 1: uwłaszczenie ballot, questions 2-5: state-property ballot). NULL in other years: use mart.referendum_precinct_protocols.';
COMMENT ON COLUMN mart.referendum_precinct_answers.valid_ballots IS '1996 only: valid ballots (voters who took part) of the ballot paper carrying this question; numerator of that referendum''s turnout. NULL in other years: use mart.referendum_precinct_protocols.';

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
    list_group INT,
    PRIMARY KEY (year, wojewodztwo, district_no, committee_id)
);
-- Added after the first release; existing databases get the column here.
ALTER TABLE mart.sejmik_district_lists ADD COLUMN IF NOT EXISTS list_group INT;
COMMENT ON TABLE mart.sejmik_district_lists IS 'Result of a committee list in a voivodeship assembly district: votes and seats won. District numbers restart in every voivodeship.';
COMMENT ON COLUMN mart.sejmik_district_lists.seats IS 'Seats won by the list in the district (count of elected candidates).';
COMMENT ON COLUMN mart.sejmik_district_lists.list_group IS '2006 only: lowest list number of the bloc of lists (grupa list) the list belonged to in this voivodeship; NULL when the list was in no bloc, and in all other years. In 2006 committees could join their lists into a bloc. A bloc with at least 15% of the voivodeship''s valid votes took part in the seat allocation as one list with the votes of all its lists (a list outside a bloc, or inside it, needed 5% on its own). In each district the seats were split among the blocs and the other lists by D''Hondt (a bloc only where one of its lists there passed 5%), then a bloc''s seats among its lists that passed 5% by Sainte-Laguë (divisors 1, 3, 5, ...). Source: PKW results pages (blocs are not in the KBW files).';

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
COMMENT ON COLUMN mart.wbp_candidates.votes_against IS 'Only when there was a single candidate: votes "przeciw" (against). 2002: valid votes minus votes for (not published separately).';
COMMENT ON COLUMN mart.wbp_candidates.pct IS 'Percent of valid votes in the gmina and round (0-100); 2002 and 2006: computed from votes and valid votes (not published).';
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
COMMENT ON TABLE mart.rada_committees IS 'Committees running for councils (rady gmin, rady powiatów, rady dzielnic) in local elections, identified by name within a year (local committees with identical names share one row). 1994: in cities above 40,000 inhabitants the list designation; in single-member districts the organisation supporting the candidate (several candidates of one district can share it); ''(NIE PODANO)'' where the source names none (candidates without a supporting organisation, lists without a designation). Several 1994 lists of one district with the same designation (or none) are told apart by '' (LISTA N)'' / ''(NIE PODANO, LISTA N)''.';
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
COMMENT ON TABLE mart.rada_districts IS 'Electoral districts (okręgi wyborcze) of councils: rady gmin/miast, rady powiatów and rady dzielnic m.st. Warszawy, with seats. A council is (year, council_level, teryt); district numbers restart in every council. Per-precinct results are not loaded. Years 1994 (gmina councils only, 1975-1998 division) and 1998-2024 (no KBW files for 1990).';
COMMENT ON COLUMN mart.rada_districts.council_level IS '''gmina'' (rada gminy / rada miasta, including cities with powiat rights and Rada m.st. Warszawy), ''powiat'' (rada powiatu) or ''dzielnica'' (rada dzielnicy m.st. Warszawy).';
COMMENT ON COLUMN mart.rada_districts.teryt IS 'Council unit: 6-digit gmina TERYT for gmina councils (Warsaw city council 146501), 6-digit Warsaw district TERYT (1465xx) for dzielnica councils, 4-digit powiat TERYT for powiat councils (same as teryt_powiat in other relations). 1994: the 5-digit gmina code of the 1975-1998 division (= teryt_gmina_pre1999 in other relations; Warsaw as its eleven gminas of 1994); join mart.gmina_codes_pre1999 on teryt_gmina_pre1999 = teryt for teryt_1999 and today''s units, and select it AS teryt_gmina_pre1999 for a map.';
COMMENT ON COLUMN mart.rada_districts.council_name IS 'Official council name, e.g. ''Rada Miasta Bolesławiec'', ''Rada Powiatu Bolesławieckiego''; NULL in 2002-2014 (not published in those files).';
COMMENT ON COLUMN mart.rada_districts.unit_name IS 'Name of the gmina (''m. Kraków'', ''gm. Bolesławiec''), powiat (''bolesławiecki'') or Warsaw district (''Bemowo''). 1994 and 1998: as published, without prefixes (''Kraków''; a rural gmina named like its town ''Bolesławiec gm.''). Filter with ILIKE.';
COMMENT ON COLUMN mart.rada_districts.teryt_powiat IS '4-digit TERYT of the powiat the council unit lies in (for cities with powiat rights the city itself). NULL in 1994 (no powiats before 1999; see mart.gmina_codes_pre1999).';
COMMENT ON COLUMN mart.rada_districts.wojewodztwo IS 'Voivodeship in lower case. 1994: one of the 49 voivodeships of 1975-1998 (e.g. ''katowickie''; old names such as ''łódzkie'' covered other territory than today).';
COMMENT ON COLUMN mart.rada_districts.city_with_powiat_rights IS 'Gmina council of a city with powiat rights (miasto na prawach powiatu); such a city has no separate rada powiatu. Always false in 1994.';
COMMENT ON COLUMN mart.rada_districts.voting_system IS '''majoritarian'': the candidates with most votes win (1994: single-member districts in gminas up to 40,000 inhabitants; 2002-2010: districts of 1-5 seats in gminas up to 20,000 inhabitants, each voter had as many votes as seats; 2014: single-member districts in gminas that are not cities with powiat rights; since 2018: single-member districts in gminas up to 20,000 inhabitants). ''proportional'': multi-member districts with committee lists (D''Hondt among lists that passed 5% in the council; 1994: cities above 40,000 inhabitants, districts of 5-10 seats, modified Sainte-Laguë without a threshold).';
COMMENT ON COLUMN mart.rada_districts.seats IS 'Seats to fill in the district (2002-2014: number of elected councillors, those files have no district register; 2006 Markuszów district 4 has 1 seat that stayed empty because no valid vote was cast).';
COMMENT ON COLUMN mart.rada_districts.candidates IS 'Registered candidates in the district.';
COMMENT ON COLUMN mart.rada_districts.contested IS 'False when no vote took place: the number of candidates did not exceed the seats, so they were elected without voting (or nobody ran and the seat stayed empty).';
COMMENT ON COLUMN mart.rada_districts.valid_votes IS 'Valid votes cast in the district for all lists / candidates; NULL when no vote took place. In 2002-2010 majoritarian districts with several seats this is the votes for all candidates, which exceeds the number of voters. 1994: votes for candidates; valid ballots without a choice (not marking any candidate) are not included.';

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
    list_group INT,
    PRIMARY KEY (year, council_level, teryt, district_no, committee_id)
);
-- Added after the first release; existing databases get the column here.
ALTER TABLE mart.rada_district_lists ADD COLUMN IF NOT EXISTS list_group INT;
COMMENT ON TABLE mart.rada_district_lists IS 'Result of a committee in a council district: votes and seats. In majoritarian districts a "list" is the committee''s single candidate. Aggregates per council, voivodeship and country are in mart.v_rada_results.';
COMMENT ON COLUMN mart.rada_district_lists.list_no IS 'List number on the ballot (NULL for 2024 majoritarian districts, whose ballots have no list numbers).';
COMMENT ON COLUMN mart.rada_district_lists.seats IS 'Seats won by the list in the district (count of elected candidates).';
COMMENT ON COLUMN mart.rada_district_lists.votes IS 'Votes for the list. A vote marked next to a candidate struck off after the ballots were printed counts for the list only, so it can exceed candidate_votes. NULL when no vote took place, and in the few districts where such votes cannot be assigned to one list (see docs, "Known gaps").';
COMMENT ON COLUMN mart.rada_district_lists.candidate_votes IS 'Sum of the votes of the list''s candidates (NULL when no vote took place).';
COMMENT ON COLUMN mart.rada_district_lists.list_group IS '2006 only: lowest list number of the bloc of lists (grupa list) the list belonged to in this council; NULL when the list was in no bloc, and in all other years. In 2006 committees could join their lists into a bloc. A bloc with at least 10% of the council''s valid votes took part in the seat allocation as one list with the votes of all its lists (a list outside a bloc, or inside it, needed 5% on its own). In each district the seats were split among the blocs and the other lists by D''Hondt (a bloc only where one of its lists there passed 5%), then a bloc''s seats among its lists that passed 5% by Sainte-Laguë (divisors 1, 3, 5, ...). Source: PKW results pages (blocs are not in the KBW files).';

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

-- GUS context statistics (Bank Danych Lokalnych), not election results ------------------------------
CREATE TABLE IF NOT EXISTS mart.gus_units (
    year INT NOT NULL,
    level TEXT NOT NULL,
    unit_key TEXT NOT NULL,
    teryt TEXT,
    name TEXT NOT NULL,
    wojewodztwo TEXT,
    teryt_powiat TEXT,
    gmina_type TEXT,
    bdl_unit_id TEXT NOT NULL,
    PRIMARY KEY (year, level, unit_key)
);
COMMENT ON TABLE mart.gus_units IS 'Territorial units with official GUS statistics (mart.gus_indicators) per year 1995-2024, as published by GUS (Bank Danych Lokalnych): Poland, voivodeships, powiats (incl. cities with powiat rights) and gminas. Use gmina_type (''miejska'', ''wiejska'', ''miejsko-wiejska'', ''dzielnica Warszawy'') for "gminy wiejskie / miejskie / miejsko-wiejskie". Join election views on level + unit_key (= v_party_results.unit_key) or on teryt = teryt_gmina / teryt_powiat. Warsaw appears at gmina level both as the city (146501, gmina_type miejska) and as its 18 districts (1465xx, gmina_type ''dzielnica Warszawy''): exclude one of them when summing.';
COMMENT ON COLUMN mart.gus_units.year IS 'Statistical year: the unit as published for that year (population and unemployment as of 31 December of that year).';
COMMENT ON COLUMN mart.gus_units.level IS '''country'', ''wojewodztwo'', ''powiat'' or ''gmina'' (gmina level also holds Warsaw''s districts).';
COMMENT ON COLUMN mart.gus_units.unit_key IS 'Join key like v_party_results.unit_key: 6-digit gmina TERYT, 4-digit powiat TERYT, voivodeship name (e.g. ''mazowieckie'', as in the wojewodztwo columns) or ''country''.';
COMMENT ON COLUMN mart.gus_units.teryt IS 'TERYT code: 6 digits for gminas and Warsaw districts (= teryt_gmina elsewhere), 4 for powiats (= teryt_powiat), 2 for voivodeships; NULL for Poland. Codes of that year (before 2002 Warsaw''s gminas were 1431xx).';
COMMENT ON COLUMN mart.gus_units.name IS 'Unit name as published by GUS, e.g. ''Bochnia'', ''Powiat bocheński'', ''MAŁOPOLSKIE''; a city and its surrounding rural gmina share a name (tell them apart by gmina_type).';
COMMENT ON COLUMN mart.gus_units.wojewodztwo IS 'Voivodeship name (lower case, as in election tables); NULL for Poland.';
COMMENT ON COLUMN mart.gus_units.teryt_powiat IS '4-digit TERYT of the powiat of a gmina (for cities with powiat rights the city itself); NULL above powiat level.';
COMMENT ON COLUMN mart.gus_units.gmina_type IS 'Gmina type from the 7th TERYT digit: ''miejska'' (urban, 1), ''wiejska'' (rural, 2), ''miejsko-wiejska'' (urban-rural, 3), ''dzielnica Warszawy'' (8); NULL above gmina level. Can change over the years (a rural gmina whose seat got town rights becomes miejsko-wiejska).';
COMMENT ON COLUMN mart.gus_units.bdl_unit_id IS '12-digit unit id in the GUS Bank Danych Lokalnych (source reference; not needed in queries).';

CREATE TABLE IF NOT EXISTS mart.gus_indicators (
    year INT NOT NULL,
    level TEXT NOT NULL,
    unit_key TEXT NOT NULL,
    teryt TEXT,
    indicator TEXT NOT NULL,
    value NUMERIC(18, 2) NOT NULL,
    unit TEXT NOT NULL,
    bdl_variable_id INT NOT NULL,
    PRIMARY KEY (year, level, unit_key, indicator)
);
COMMENT ON TABLE mart.gus_indicators IS 'Official GUS statistics (Bank Danych Lokalnych) per territorial unit and year, long format: one row per (year, level, unit_key, indicator); indicator is population, population_urban, population_rural, area_ha, population_density, unemployed_registered, unemployed_share_working_age (gminas too), unemployment_rate and avg_gross_salary (both powiat level and above only). Not election data: relate them to results by joining v_turnout / v_party_results on teryt (teryt_gmina, teryt_powiat) or level + unit_key, and pick the GUS year closest to the election date (statistics refer to 31 December: for an October 2023 election use 2023, for a May 2025 election 2024; the latest GUS year is 2024). Unit names and gmina types are in mart.gus_units. Do not sum gmina rows of Warsaw''s city (146501) together with its districts (1465xx).';
COMMENT ON COLUMN mart.gus_indicators.year IS 'Statistical year (stocks such as population and unemployment as of 31 December; salary as the yearly average).';
COMMENT ON COLUMN mart.gus_indicators.level IS '''country'', ''wojewodztwo'', ''powiat'' or ''gmina'' (same as gus_units.level).';
COMMENT ON COLUMN mart.gus_indicators.unit_key IS 'Same as gus_units.unit_key: gmina or powiat TERYT, voivodeship name or ''country''.';
COMMENT ON COLUMN mart.gus_indicators.teryt IS 'Same as gus_units.teryt (6/4/2 digits, NULL for Poland).';
COMMENT ON COLUMN mart.gus_indicators.indicator IS 'One of: ''population'' (persons living in the unit, 31 Dec, 1995-2024, every level), ''population_urban'' and ''population_rural'' (persons living in towns / in rural areas; their sum is population), ''area_ha'' (area in hectares, 1995-2024; km2 = area_ha / 100), ''population_density'' (GUS persons per km2, 2002-2024; area and density of Warsaw''s districts are missing in 2006-2007), ''unemployed_registered'' (registered unemployed persons, 31 Dec, 2003-2024, every level incl. gminas; not for Warsaw''s districts, only for the whole city), ''unemployed_share_working_age'' (registered unemployed as % of the working-age population, 2003-2024, every level incl. gminas, not Warsaw''s districts; the only unemployment measure for gminas), ''unemployment_rate'' (registered unemployment rate in %, 31 Dec, 2004-2024, only powiat, wojewodztwo and country), ''avg_gross_salary'' (average gross monthly salary in PLN, 2002-2024, only powiat, wojewodztwo and country; excludes firms with up to 9 employees), ''age_preworking_pct'', ''age_working_pct'', ''age_postworking_pct'' (residents in pre-working 0-17, working and post-working age, women 60+ / men 65+, as % of population, 2002-2024, every level), ''gmina_income_per_capita'' (gmina budget income per resident in PLN, 2002-2024, every level), ''nsp2021_population_13plus'' and ''nsp2021_higher_education'' (census 2021: residents aged 13+ and those with higher education, year 2021 only, every level), ''nsp2011_population_13plus'' and ''nsp2011_higher_education'' (census 2011, year 2011 only, powiat level and above). The wide view mart.v_gus_units has them as columns (higher_education_pct).';
COMMENT ON COLUMN mart.gus_indicators.value IS 'Value in the unit of the unit column (percent values are 0-100).';
COMMENT ON COLUMN mart.gus_indicators.unit IS 'Unit of value: ''persons'', ''ha'', ''persons per km2'', ''percent'' or ''PLN''.';
COMMENT ON COLUMN mart.gus_indicators.bdl_variable_id IS 'Variable id in the GUS Bank Danych Lokalnych (source reference: https://bdl.stat.gov.pl/api/v1/variables/<id>).';
CREATE INDEX IF NOT EXISTS idx_gus_indicators_teryt ON mart.gus_indicators (teryt, indicator);

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
COMMENT ON COLUMN mart.persons.match_rule IS '''name+gender'': same full name and gender in every year; ''name+gender+residence'': the name was shared by several candidates in one election, so residence was also required; ''name+gender+voivodeship'': one given name, candidacies split by voivodeship of residence; ''name+gender+birth year'': split by register birth years more than 3 apart; ''surname+first given name+gender'': abbreviated given names before 2011 joined to the person with the full name; ''manual'': set by api/services/mart/person_overrides.csv; ''local:...'': only local candidacies (sejmik, mayors, councils) with the same name, gender, birth year (+-1) and powiat or voivodeship. Different people with identical names can still be merged; people who changed surname are not linked.';
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
    conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION mart.allocate_seats(numeric[], bigint, text) TO {}").format(role))


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
