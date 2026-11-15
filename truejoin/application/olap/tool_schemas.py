"""Tool schema definitions in OpenAI function-calling and MCP formats.

The 16 tools: list_sources, describe_table, query_data, search_data,
aggregate_data, create_view, list_views, refresh_view, load_table, list_metrics,
query_metrics, cache_stats, check_readiness, and the term layer's search_terms,
get_term and answer_question.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_sources",
        "description": (
            "List all connected data sources and their tables with row counts. "
            "Use this to discover what data is available before querying. A source "
            "may carry notes from the people who reviewed its catalog: read them "
            "before answering about that data."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "describe_table",
        "description": (
            "Show detailed schema for a table: column names, data types, "
            "nullability, row count, the keys to join on and, where the catalog "
            "was reviewed, what each column means. Use format 'source.table'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Full table name in 'source.table' format (e.g. 'mydb.orders')",
                },
            },
            "required": ["table"],
        },
    },
    {
        "name": "query_data",
        "description": (
            "Execute an analytical SQL query on DuckDB. Only SELECT queries are allowed. "
            "Results are limited to 100 rows. Use this for complex joins, aggregations, "
            "window functions, and cross-source queries."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A SELECT SQL query to execute on DuckDB"},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "search_data",
        "description": (
            "Search for rows in a table matching a filter condition. "
            "Simpler than writing full SQL — just specify table, column, and value."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Full table name in 'source.table' format",
                },
                "filter_column": {"type": "string", "description": "Column name to filter on"},
                "filter_value": {
                    "type": "string",
                    "description": "Value to match (exact or LIKE pattern with %)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum rows to return (default 20)",
                    "default": 20,
                },
            },
            "required": ["table", "filter_column", "filter_value"],
        },
    },
    {
        "name": "aggregate_data",
        "description": (
            "Run a GROUP BY aggregation on a table. Specify the grouping column, "
            "the column to aggregate, and the function (SUM, AVG, COUNT, MIN, MAX)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Full table name in 'source.table' format",
                },
                "group_by": {"type": "string", "description": "Column to group by"},
                "agg_column": {"type": "string", "description": "Column to aggregate"},
                "agg_func": {
                    "type": "string",
                    "description": "Aggregation function: SUM, AVG, COUNT, MIN, or MAX",
                    "enum": ["SUM", "AVG", "COUNT", "MIN", "MAX"],
                },
            },
            "required": ["table", "group_by", "agg_column", "agg_func"],
        },
    },
    {
        "name": "create_view",
        "description": (
            "Create a materialized view (pre-computed table) from a SELECT query. "
            "Useful for caching expensive aggregations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "View name (alphanumeric and underscores only)",
                },
                "sql": {"type": "string", "description": "SELECT query to materialize"},
                "refresh": {
                    "type": "string",
                    "description": (
                        "Refresh interval: 'manual', 'hourly', 'daily', or 'every N minutes'"
                    ),
                    "default": "manual",
                },
            },
            "required": ["name", "sql"],
        },
    },
    {
        "name": "list_views",
        "description": (
            "List all materialized views with their refresh schedule and last refresh time."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "refresh_view",
        "description": "Manually refresh a materialized view to get the latest data.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the materialized view to refresh",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "load_table",
        "description": (
            "Load a table from its data source into DuckDB for querying. "
            "Use this if list_sources shows a table is not yet loaded. "
            "A table larger than the configured limit is refused unless you "
            "narrow it with 'where' and/or 'columns', which fetch only the "
            "matching rows and columns. After loading, the table is available "
            "to query_data and the other tools under its own name."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Full table name in 'source.table' format to load",
                },
                "where": {
                    "type": "string",
                    "description": (
                        "Optional filter sent to the source: an AND of simple "
                        "conditions comparing a column to a literal (=, !=, <, "
                        "<=, >, >=, LIKE, IN, IS NULL), e.g. "
                        "\"status = 'paid' AND created_at > '2026-01-01'\". "
                        "OR, NOT, functions and subqueries are not accepted."
                    ),
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of columns to fetch instead of all",
                },
            },
            "required": ["table"],
        },
    },
    {
        "name": "list_metrics",
        "description": (
            "Show the measures, dimensions, time grains and transforms available for a "
            "table, with ready-to-use metric expressions. Call this before query_metrics "
            "so you name measures the table actually has. Without 'table', lists every "
            "table that has a semantic model."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Full table name in 'source.table' format (optional)",
                }
            },
            "required": [],
        },
    },
    {
        "name": "query_metrics",
        "description": (
            "Answer a metric question without writing SQL. Give the table, the measures "
            "to compute and the dimensions to break them down by; Fusion aggregates, "
            "applies window transforms and returns at most 100 rows. A metric is "
            "'measure:aggregation' (e.g. 'revenue:sum', '*:count', "
            "'price:weighted_avg(weight=quantity)') and can be wrapped in transforms "
            "that nest, e.g. 'change_pct(cumsum(revenue:sum))'. Transforms: cumsum, "
            "change, change_pct, time_shift, lag, lead, rank, percent_rank, dense_rank, "
            "ntile. Unlike query_data this works on tables far too large to load, "
            "because only the columns and rows the metrics need are fetched. Call "
            "list_metrics first to see what a table offers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Full table name in 'source.table' format",
                },
                "metrics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Metric expressions, e.g. "
                        "['revenue:sum', 'change_pct(cumsum(revenue:sum))']"
                    ),
                },
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Columns to group by. A time column may carry a grain: "
                        "'order_date:month' (day, week, month, quarter, year). A column "
                        "of a table this one references is written 'table.column' "
                        "('customers.segment'); the join follows the declared keys, "
                        "and filters accept the same form. A table reached through a "
                        "link table of keys only (students -> enrollments -> courses) "
                        "works too: 'courses.title' counts each student once per course."
                    ),
                },
                "filters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "column": {"type": "string"},
                            "op": {
                                "type": "string",
                                "enum": [
                                    "eq",
                                    "ne",
                                    "gt",
                                    "gte",
                                    "lt",
                                    "lte",
                                    "like",
                                    "in",
                                    "is_null",
                                ],
                                "default": "eq",
                            },
                            "value": {
                                "description": (
                                    "Literal to compare against (string, number, boolean, "
                                    "or a list for 'in')"
                                )
                            },
                        },
                        "required": ["column"],
                    },
                    "description": (
                        "Conditions sent to the source, ANDed together. Narrowing a large "
                        "table with filters is what makes it queryable at all."
                    ),
                },
                "order_by": {
                    "type": "string",
                    "description": (
                        "Output column to sort by; prefix with '-' for descending. "
                        "Defaults to the first dimension."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum rows to return (default 100, max 100)",
                    "default": 100,
                },
            },
            "required": ["table", "metrics"],
        },
    },
    {
        "name": "cache_stats",
        "description": "Show query cache statistics: hit rate, entry count, and memory usage.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "check_readiness",
        "description": (
            "Check that this server can answer questions: data sources connected, "
            "tables discovered, the semantic layer present, catalogs reviewed. "
            "Call it first when something looks missing; each failed check says why."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "find_examples",
        "description": (
            "Find saved analyses close to a question: questions a reviewer answered "
            "with SQL that runs here, certified. Read them before writing SQL for "
            "query_data: they show which tables hold what and how values are spelled. "
            "Adapt one rather than copy it blindly; an empty question lists them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question, in words"},
                "limit": {"type": "integer", "description": "At most this many (1-20)"},
            },
            "required": [],
        },
    },
    {
        "name": "search_terms",
        "description": (
            "Find the business terms a word belongs to: what a table, a column or a "
            "stored value is called by the people who use it. Only terms a reviewer "
            "certified are searched. Use it to find which table a question is about."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Words to look for, e.g. 'cancelled payments'. "
                    "Empty lists every term.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_term",
        "description": (
            "Show one certified business term: its tables, the words for each column "
            "and stored value, its measures, and the tables its questions can also "
            "draw on. Accepts the term's name or a table name."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Term name or table name"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "check_question",
        "description": (
            "Before answering a question in words, see how much of it the certified "
            "vocabulary covers: for each word, the table, column, stored value or "
            "aggregation it was recognised as, and the words recognised as nothing. "
            "Nothing runs. 'covered' is 'all', 'part' or 'none'. When it is not 'all', "
            "answer_question would answer without the unknown words: check its reading, "
            "or write the request with query_metrics or query_data. 'unused' lists the "
            "words that were recognised but that the reading makes no use of, and "
            "'apart' the ones naming a table the reading's table does not reference: "
            "with any of those, answer_question refuses."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question, in words"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "answer_question",
        "description": (
            "Answer a question asked in plain words (Turkish or English), such as "
            "'total amount per channel for successful transactions'. The question is "
            "matched against the certified vocabulary without a model, so the same "
            "question always gets the same answer. The result includes the request "
            "it was read as and the SQL that ran: check them, and call query_metrics "
            "with a corrected request when the reading is not what was meant. "
            "Abbreviations in the catalog's glossary are written out first; the "
            "result lists any that were. A question that names nothing in the "
            "vocabulary is refused, with the words not read, rather than guessed: "
            "use query_metrics or query_data for it. So is one whose words name "
            "tables that no single request joins (a count of customers with a "
            "condition on their orders): the reading would answer another question. "
            "A question read in part is answered, and lists the words left out: "
            "unread (not in the vocabulary) and unused (recognised, but the reading "
            "makes no use of them: a number, a table, a breakdown). Check that the "
            "reading still asks what was meant. For a follow-up ('and by branch?', 'only "
            "the web ones'), pass the earlier answer's request as previous: it is "
            "kept, and changed only where the new words say."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question, in words"},
                "previous": {
                    "type": "object",
                    "description": (
                        "The request an earlier answer_question returned, when this "
                        "question follows up on it"
                    ),
                },
            },
            "required": ["question"],
        },
    },
]

#: The term layer's tools: offered only where ``olap.terms`` is on.
TERM_TOOLS = frozenset({"search_terms", "get_term", "check_question", "answer_question"})

TOOL_NAMES: tuple[str, ...] = tuple(d["name"] for d in TOOL_DEFINITIONS)


def get_openai_tools() -> list[dict[str, Any]]:
    """Definitions wrapped as OpenAI Chat Completions ``tools`` entries."""
    return [
        {
            "type": "function",
            "function": {
                "name": d["name"],
                "description": d["description"],
                "parameters": deepcopy(d["parameters"]),
            },
        }
        for d in TOOL_DEFINITIONS
    ]


def get_mcp_tools() -> list[dict[str, Any]]:
    """Definitions in MCP ``name/description/inputSchema`` format."""
    return [
        {
            "name": d["name"],
            "description": d["description"],
            "inputSchema": deepcopy(d["parameters"]),
        }
        for d in TOOL_DEFINITIONS
    ]
