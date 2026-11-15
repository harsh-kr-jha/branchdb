\set ON_ERROR_STOP on
CREATE FUNCTION branch_version_spike() RETURNS text
AS '$libdir/branch_version_spike', 'branch_version_spike'
LANGUAGE C STRICT;
SELECT branch_version_spike();
DROP FUNCTION branch_version_spike();
