"""MySQL identifier compatibility without rewriting historical migration files."""
import re


_TOKENS = re.compile(
    r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`(?:``|[^`])*`"
    r"|--[^\r\n]*|/\*.*?\*/|\bcursor\b", re.IGNORECASE | re.DOTALL)


def quote_mysql_identifiers(sql):
    """CURSOR is a reserved word in MySQL; preserve strings/comments/quoted names."""
    return _TOKENS.sub(lambda match: "`cursor`" if match[0].lower() == "cursor" else match[0], sql)


def migration_statement(name, sql):
    sql = quote_mysql_identifiers(sql)
    if name == "002_trusted_execution_and_observations.sql":
        # Seven utf8mb4 source identity columns exceed InnoDB's 3072-byte key
        # limit. Hash the complete typed tuple instead of truncating identifiers.
        sql = re.sub(r"ADD UNIQUE KEY uq_evidence_source\s*\(\s*tenant_id,\s*project_id,\s*source_type,\s*provider_id,\s*run_id,\s*source_evidence_id,\s*source_revision\s*\)",
            """ADD COLUMN source_identity_sha256 BINARY(32) GENERATED ALWAYS AS
                (UNHEX(SHA2(CAST(JSON_ARRAY(tenant_id,project_id,source_type,provider_id,
                    run_id,source_evidence_id,source_revision) AS CHAR CHARACTER SET utf8mb4),256))) STORED,
              ADD UNIQUE KEY uq_evidence_source (source_identity_sha256),
              ADD KEY ix_evidence_source_lookup (tenant_id,project_id,source_type,provider_id)""", sql)
    return sql
