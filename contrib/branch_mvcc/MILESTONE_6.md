# Milestone 6 — authenticated native branch connections

Implemented the first checkpoint of the approved developer-beta plan. Native
branches now accept non-superuser logins bound to a branch by authenticated role
OID. An omitted startup selector resolves to that binding; conflicting selectors
and unbound logins fail. PostgreSQL's immutable backend GUC preserves the branch
through prepared SQL, role changes, resets and reconnects.

Migration roles use normal table ownership and public-schema CREATE privileges.
Application roles receive ordinary CRUD grants. Administrators can transfer a
branch's inherited logical table ownership without modifying sibling descriptors
or shared data. Administration and maintenance still require a separate trusted
superuser. Role provisioning is administrative SQL in this checkpoint; packaged
credential management remains milestone 10.

The internal metadata query boundary temporarily installs a restricted identity
and pg_catalog search path and restores both on normal return and ERROR. All
application statements keep their ordinary ACL checks. No elevated SQL entry
point is exposed. The new security module separates session authorization from
row visibility, catalog visibility and physical maintenance. Privilege checks in
utility guards run only in valid transaction state.

Metadata format 1 is introduced for fresh databases through native_m6/setup.sql.
Unsupported versions fail at startup, including administrative connections.
Legacy fixtures retain their earlier superuser-only native branch behavior;
there is no automatic upgrade of an existing prototype database.

## Validation

- All 243 core SQL regressions passed. One resource-limited run exhausted disk
  during join_hash; the separate reduced-concurrency rerun passed completely.
- Original milestone 1, 2 and 3 suites passed.
- Milestone 4 maintenance, real autovacuum, CLOG retirement, epoch simulation and
  crash recovery passed, as did the milestone 5 application acceptance suite.
- New security acceptance covers default and conflicting branch selection,
  unbound login rejection, isolated sibling CRUD, separate migration/application
  privileges, protected metadata and sequence access, administrative function
  denial, session authorization denial, role membership changes, DISCARD ALL,
  prepared SQL, search_path changes, COPY, VACUUM and crash recovery.
- A forced primary-key failure inside an elevated version-metadata INSERT proves
  the connection returns to its application identity and cannot read metadata
  afterward. Unknown metadata format rejection is also tested.

These tests use disposable clusters with local trust authentication. They test
engine role/branch enforcement; SCRAM packaging and credential distribution
remain milestone 10. This is a development checkpoint, not a production security
certification. Existing SQL and maintenance limits remain in force.

Build and usage: [native_m6/README.md](native_m6/README.md).
Remaining approved checkpoints: [DEVELOPER_BETA_PLAN.md](DEVELOPER_BETA_PLAN.md).
Next is milestone 7: durable storage references, safe deletion and reclamation.
