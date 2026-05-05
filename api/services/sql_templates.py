SQL_TEMPLATES = {
    "total_votes_by_candidate": """
        SELECT e.year, c.name AS candidate, SUM(r.votes) AS votes
        FROM results r
        JOIN candidates c ON c.id = r.candidate_id
        JOIN elections e ON e.id = r.election_id
        WHERE (%(year)s::int IS NULL OR e.year = %(year)s::int)
        GROUP BY e.year, c.name
        ORDER BY e.year DESC, votes DESC
        LIMIT %(limit)s;
    """,
    "votes_for_candidate": """
        SELECT e.year, c.name AS candidate, SUM(r.votes) AS votes
        FROM results r
        JOIN candidates c ON c.id = r.candidate_id
        JOIN elections e ON e.id = r.election_id
        WHERE c.name ILIKE %(candidate_pattern)s
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
        WHERE c.name ILIKE %(candidate_pattern)s
          AND e.year IN (%(year_1)s::int, %(year_2)s::int)
        GROUP BY e.year, c.name
        ORDER BY e.year ASC, votes DESC;
    """,
    "elected_candidates_sejm": """
        SELECT year, district, committee_name, candidate_name, candidate_votes, list_position
        FROM elected_candidates
        WHERE (%(year)s::int IS NULL OR year = %(year)s::int)
          AND (%(candidate_pattern)s::text IS NULL OR committee_name ILIKE %(candidate_pattern)s::text)
          AND (
            %(district_csv)s::text IS NULL
            OR trim(%(district_csv)s) = ''
            OR district = ANY(string_to_array(%(district_csv)s, ','))
          )
        ORDER BY year DESC, CAST(district AS INT), candidate_votes DESC;
    """,
    "trend_by_district_for_candidate": """
        SELECT e.year, r.district, SUM(r.votes) AS votes
        FROM results r
        JOIN candidates c ON c.id = r.candidate_id
        JOIN elections e ON e.id = r.election_id
        WHERE c.name ILIKE %(candidate_pattern)s
          AND (%(year)s::int IS NULL OR e.year = %(year)s::int)
        GROUP BY e.year, r.district
        ORDER BY e.year DESC, CAST(r.district AS INT);
    """,
}
