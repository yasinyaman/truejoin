"""The command whitelist every raw SQL statement passes before it reaches a database."""

import re


class QueryValidator:
    """Validates SQL queries against whitelist.

    Security measure to prevent dangerous SQL operations.
    """

    def __init__(self, whitelist: list[str] | None = None):
        """Initialize the validator.

        Args:
            whitelist: List of allowed SQL commands (e.g., ['SELECT', 'INSERT']).
                      If None, only SELECT is allowed.
        """
        self.whitelist = [cmd.upper() for cmd in (whitelist or ["SELECT"])]

    def validate(self, query: str) -> bool:
        """Validate a SQL query against the whitelist.

        Args:
            query: SQL query string.

        Returns:
            True if query is allowed.

        Raises:
            ValueError: If query uses a disallowed command.
        """
        # Normalize query
        normalized = query.strip().upper()

        # Reject multiple statements (a single optional trailing ';' is allowed).
        # Blocks stacked queries such as "SELECT ...; DROP TABLE ...".
        without_trailing = query.strip().rstrip(";").rstrip()
        if ";" in without_trailing:
            raise ValueError("Multiple SQL statements are not allowed")

        # Check for dangerous patterns
        dangerous_patterns = [
            r";\s*(DROP|DELETE|TRUNCATE|ALTER|CREATE|GRANT|REVOKE)",
            r"--",  # SQL comments
            r"/\*",  # Multi-line comments
            r"EXEC\s*\(",
            r"EXECUTE\s+",
            r"xp_",  # SQL Server extended procedures
        ]

        for pattern in dangerous_patterns:
            if re.search(pattern, normalized):
                raise ValueError("Query contains potentially dangerous patterns")

        # Check if query starts with allowed command
        for allowed in self.whitelist:
            if normalized.startswith(allowed):
                return True

        raise ValueError(f"Query must start with one of: {', '.join(self.whitelist)}")
