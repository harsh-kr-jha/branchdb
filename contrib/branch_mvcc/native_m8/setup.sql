\set ON_ERROR_STOP on
\set branch_setup_outer_transaction on
BEGIN;
\ir ../native_m7/setup.sql
CREATE TABLE branch_native.schema_features(version integer NOT NULL);
INSERT INTO branch_native.schema_features VALUES(3);
REVOKE ALL ON branch_native.schema_features FROM PUBLIC;
UPDATE branch_native.format SET version=3;
COMMIT;
