SQL_TEMPLATES = {
    "total_votes_by_candidate": """
        SELECT e.year, c.name AS candidate, SUM(r.votes) AS votes
        FROM results r
        JOIN candidates c ON c.id = r.candidate_id
        JOIN elections e ON e.id = r.election_id
        WHERE (%(year)s IS NULL OR e.year = %(year)s)
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
          AND (%(year)s IS NULL OR e.year = %(year)s)
        GROUP BY e.year, c.name
        ORDER BY e.year DESC, votes DESC
        LIMIT %(limit)s;
    """,
    "trend_by_district_for_candidate": """
        SELECT e.year, r.district, SUM(r.votes) AS votes
        FROM results r
        JOIN candidates c ON c.id = r.candidate_id
        JOIN elections e ON e.id = r.election_id
        WHERE c.name ILIKE %(candidate_pattern)s
          AND (%(year)s IS NULL OR e.year = %(year)s)
        GROUP BY e.year, r.district
        ORDER BY e.year DESC, CAST(r.district AS INT);
    """,
}
