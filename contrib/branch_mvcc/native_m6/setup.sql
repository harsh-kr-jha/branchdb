\set ON_ERROR_STOP on
\ir ../native_m3/setup.sql
CREATE TABLE branch_native.format(version integer PRIMARY KEY);
INSERT INTO branch_native.format VALUES(1);
CREATE TABLE branch_native.roles(role oid PRIMARY KEY, branch bigint NOT NULL REFERENCES branch_native.branches(id));
REVOKE ALL ON branch_native.format, branch_native.roles FROM PUBLIC;
