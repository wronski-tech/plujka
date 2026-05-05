SQL_TEMPLATES = {
    "total_votes_by_candidate": """
        SELECT e.year, c.name AS candidate, SUM(r.votes) AS votes
        FROM results r
        JOIN candidates c ON c.id = r.candidate_id
        JOIN elections e ON e.id = r.election_id
        WHERE e.type = 'sejm'
          AND (%(year)s::int IS NULL OR e.year = %(year)s::int)
        GROUP BY e.year, c.name
        ORDER BY e.year DESC, votes DESC
        LIMIT %(limit)s;
    """,
    "votes_for_candidate": """
        SELECT e.year, c.name AS candidate, SUM(r.votes) AS votes
        FROM results r
        JOIN candidates c ON c.id = r.candidate_id
        JOIN elections e ON e.id = r.election_id
        WHERE e.type = 'sejm'
          AND c.name ILIKE %(candidate_pattern)s
          AND (%(year)s::int IS NULL OR e.year = %(year)s::int)
        GROUP BY e.year, c.name
        ORDER BY e.year DESC, votes DESC
        LIMIT %(limit)s;
    """,
    "votes_for_candidate_compare_years": """
        SELECT e.year, c.name AS candidate, SUM(r.votes) AS votes
        FROM results r
        JOIN candidates c ON c.id = r.candidate_id
        JOIN elections e ON e.id = r.election_id
        WHERE e.type = 'sejm'
          AND c.name ILIKE %(candidate_pattern)s
          AND e.year IN (%(year_1)s::int, %(year_2)s::int)
        GROUP BY e.year, c.name
        ORDER BY e.year ASC, votes DESC;
    """,
    "votes_for_candidate_all_years": """
        SELECT e.year, c.name AS candidate, SUM(r.votes) AS votes
        FROM results r
        JOIN candidates c ON c.id = r.candidate_id
        JOIN elections e ON e.id = r.election_id
        WHERE e.type = 'sejm'
          AND c.name ILIKE %(candidate_pattern)s
        GROUP BY e.year, c.name
        ORDER BY e.year ASC, votes DESC;
    """,
    "elected_candidates_sejm": """
        SELECT year, district, committee_name, candidate_name, candidate_votes, list_position
        FROM elected_candidates
        WHERE (%(year)s::int IS NULL OR year = %(year)s::int)
          AND (
            %(candidate_pattern)s::text IS NULL
            OR trim(%(candidate_pattern)s::text) = ''
            OR committee_name ILIKE %(candidate_pattern)s::text
            OR candidate_name ILIKE %(candidate_pattern)s::text
          )
          AND (
            %(district_csv)s::text IS NULL
            OR trim(%(district_csv)s) = ''
            OR district = ANY(string_to_array(%(district_csv)s, ','))
          )
        ORDER BY year DESC, CAST(district AS INT), candidate_votes DESC;
    """,
    # Kto z list krajowych startował (gmina CSV → sumy po okręgu). Nie obejmuje Senatu ani samorządu.
    "candidate_sejm_participation": """
        SELECT DISTINCT year, district, committee_name, candidate_name, total_votes, list_position
        FROM sejm_candidate_ballots
        WHERE candidate_name ILIKE %(name_pattern)s
        ORDER BY year ASC, CAST(district AS INT), committee_name, list_position NULLS LAST;
    """,
    # Preferencyjne / imienne — tylko zwycięzcy mandatu w `elected_candidates` (nie pełna suma krajowa z CSV).
    "sejm_candidate_personal_votes": """
        SELECT year, district, committee_name, candidate_name, candidate_votes AS personal_votes, list_position
        FROM elected_candidates
        WHERE (%(year)s::int IS NULL OR year = %(year)s::int)
          AND candidate_name ILIKE %(candidate_pattern)s
        ORDER BY year DESC, CAST(district AS INT);
    """,
    "trend_by_district_for_candidate": """
        SELECT e.year, r.district, SUM(r.votes) AS votes
        FROM results r
        JOIN candidates c ON c.id = r.candidate_id
        JOIN elections e ON e.id = r.election_id
        WHERE e.type = 'sejm'
          AND c.name ILIKE %(candidate_pattern)s
          AND (%(year)s::int IS NULL OR e.year = %(year)s::int)
        GROUP BY e.year, r.district
        ORDER BY e.year DESC, CAST(r.district AS INT);
    """,
    # Official PKW rollups (`sejm_aggregate_results`). Committee labels match each CSV year (2019 includes ZPOW suffixes).
    "sejm_votes_by_powiat": """
        SELECT e.year, r.powiat, r.wojewodztwo, r.committee_name, r.metric_value::bigint AS votes
        FROM sejm_aggregate_results r
        JOIN elections e ON e.id = r.election_id
        WHERE e.type = 'sejm'
          AND r.geography_level = 'powiat'
          AND r.is_percentage = FALSE
          AND (%(year)s::int IS NULL OR e.year = %(year)s::int)
          AND (
            %(committee_pattern)s::text IS NULL
            OR trim(%(committee_pattern)s::text) = ''
            OR r.committee_name ILIKE %(committee_pattern)s::text
          )
          AND (
            %(powiat_pattern)s::text IS NULL
            OR trim(%(powiat_pattern)s::text) = ''
            OR r.powiat ILIKE %(powiat_pattern)s::text
          )
        ORDER BY r.powiat, votes DESC
        LIMIT %(limit)s;
    """,
    "sejm_votes_by_gmina": """
        SELECT e.year, r.gmina, r.powiat, r.wojewodztwo, r.sejm_district,
               r.committee_name, r.metric_value::bigint AS votes
        FROM sejm_aggregate_results r
        JOIN elections e ON e.id = r.election_id
        WHERE e.type = 'sejm'
          AND r.geography_level = 'gmina'
          AND r.is_percentage = FALSE
          AND (%(year)s::int IS NULL OR e.year = %(year)s::int)
          AND (
            %(committee_pattern)s::text IS NULL
            OR trim(%(committee_pattern)s::text) = ''
            OR r.committee_name ILIKE %(committee_pattern)s::text
          )
          AND (
            %(gmina_pattern)s::text IS NULL
            OR trim(%(gmina_pattern)s::text) = ''
            OR r.gmina ILIKE %(gmina_pattern)s::text
          )
        ORDER BY r.gmina, votes DESC
        LIMIT %(limit)s;
    """,
    "sejm_votes_by_wojewodztwo": """
        SELECT e.year, r.wojewodztwo, r.committee_name, r.metric_value::bigint AS votes
        FROM sejm_aggregate_results r
        JOIN elections e ON e.id = r.election_id
        WHERE e.type = 'sejm'
          AND r.geography_level = 'voivodeship'
          AND r.is_percentage = FALSE
          AND (%(year)s::int IS NULL OR e.year = %(year)s::int)
          AND (
            %(committee_pattern)s::text IS NULL
            OR trim(%(committee_pattern)s::text) = ''
            OR r.committee_name ILIKE %(committee_pattern)s::text
          )
          AND (
            %(wojewodztwo_pattern)s::text IS NULL
            OR trim(%(wojewodztwo_pattern)s::text) = ''
            OR r.wojewodztwo ILIKE %(wojewodztwo_pattern)s::text
          )
        ORDER BY r.wojewodztwo, votes DESC
        LIMIT %(limit)s;
    """,
}
