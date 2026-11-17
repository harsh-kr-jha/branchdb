# Milestone 6: authenticated branch connections

The native engine now supports non-superuser migration and application logins.
A login is bound to a database-local branch ID in protected metadata. Startup
resolves an omitted branch selector from this binding and rejects a conflicting
selector. The branch GUC remains immutable; DISCARD ALL does not switch branches.
Administrative connections remain superusers in this milestone.

Use a fresh disposable database and load `setup.sql` with the modified server.
Metadata format 1 is checked at connection startup. Older prototype databases
without the roles table retain their superuser-only branch access; they are not
an upgrade path to this beta format.

An administrator creates login roles, inserts role-OID/branch-ID mappings into
`branch_native.roles`, and grants normal PostgreSQL privileges. Assign inherited
logical table ownership to the branch migration role using an administrative
connection to that branch. Grant the application role SELECT/INSERT/UPDATE/DELETE
on the branch's tables. Grant CREATE on public only to migration roles. Those
schema privileges are database-global; native object visibility and immutable
connection identity determine the branch for new objects.

Example after creating branch a and roles migrate_a/app_a:

```sql
-- Administrative main connection:
INSERT INTO branch_native.roles
SELECT r.oid,b.id FROM pg_roles r CROSS JOIN branch_native.branches b
WHERE r.rolname IN ('migrate_a','app_a') AND b.name='a';
GRANT CREATE ON SCHEMA public TO migrate_a;
-- Administrative connection with startup branch=a:
ALTER TABLE users OWNER TO migrate_a;
GRANT SELECT,INSERT,UPDATE,DELETE ON users TO app_a;
```

Internal metadata queries execute with a restricted identity and fixed pg_catalog
search path, restoring the caller's identity and GUC state on success and error.
This helper is a C engine interface, not a SQL entry point. Ordinary SQL retains
normal ACL checks. Metadata schemas, branch creation and maintenance remain
unavailable to application roles. Role membership cannot switch branch identity;
normal PostgreSQL rules still determine privileges of SET ROLE.

Run the full build and earlier acceptance suites with
`contrib/branch_mvcc/native_m6/run.sh /tmp/branch-mvcc-m6`.

Run security acceptance against an installed modified server:

```sh
python3 -B contrib/branch_mvcc/native_m6/test.py --bindir /path/to/native/bin
```

The test verifies default/conflicting selectors, unbound login rejection,
independent sibling DML, migration ownership, application DDL denial, protected
metadata, admin-function denial, immutable GUC/reset behavior, prepared SQL,
search-path changes, forced metadata failure with privilege restoration,
maintenance and crash recovery. The baseline application and maintenance suites
also run against the changed engine. This remains a development prototype with
the SQL and maintenance restrictions documented in milestone 5. This milestone
adds no branch deletion or framework compatibility.
