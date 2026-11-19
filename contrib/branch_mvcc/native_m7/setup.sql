\set ON_ERROR_STOP on
\if :{?branch_setup_outer_transaction}
\else
BEGIN;
\endif
\ir ../native_m6/setup.sql
ALTER TABLE branch_native.branches ALTER COLUMN name DROP NOT NULL;
CREATE TABLE branch_native.retired(catalog oid NOT NULL,object oid NOT NULL,PRIMARY KEY(catalog,object));
CREATE SEQUENCE branch_native.storage_ids;
CREATE TABLE branch_native.storage(id bigint PRIMARY KEY,root oid NOT NULL,generation bigint NOT NULL,UNIQUE(root,generation));
CREATE TABLE branch_native.storage_refs(object oid PRIMARY KEY,storage bigint NOT NULL REFERENCES branch_native.storage(id));
CREATE TABLE branch_native.cleanup(object oid PRIMARY KEY);
REVOKE ALL ON ALL TABLES IN SCHEMA branch_native FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA branch_native FROM PUBLIC;
REVOKE ALL ON FUNCTION branch_drop(text,bool),branch_cleanup() FROM PUBLIC;
UPDATE branch_native.format SET version=2;
\if :{?branch_setup_outer_transaction}
\else
COMMIT;
\endif
