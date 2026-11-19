# Developer beta implementation checkpoints

Completed: [6](MILESTONE_6.md), [7](MILESTONE_7.md), [8](MILESTONE_8.md),
[9](MILESTONE_9.md), [10](MILESTONE_10.md). No checkpoints remain in this plan.

Approved defaults: Prisma first with migrate dev/shadow database, then Django;
macOS ARM64 and Linux x86_64 native runtimes plus Linux container; developer beta
with maintenance pauses retained. Branch deletion refuses connected sessions by
default; explicit force fences and disconnects them. No Headwig integration,
branch merge, nested forks or production certification in this scope.

6. Authenticated branch-bound roles, separate migration/application ACLs,
   restricted native metadata queries, metadata format versioning and security
   acceptance. Administrator remains a separate trusted PostgreSQL superuser.
7. Durable storage identities and references independent of catalog aliases,
   retained tuple descriptors, transactional branch deletion, connection fencing,
   idempotent cleanup queue, safe DROP/TRUNCATE and crash tests.
8. Pinned Prisma application and real migrate dev/deploy/introspection/reset;
   independent sequence state, defaults, common schema evolution, validated
   constraints, foreign keys and upsert. Separate disposable shadow database.
   Validate equivalent Django application next; advertise only tested versions.
9. Release-build profiling, invalidation-based ownership caching, indexed typed
   metadata access instead of repeated dynamic SPI, hash TID membership, and
   measurements at 1/10/100 branches and 1K/100K/1M rows. Publish overhead and
   require repeatable improvement without >10% regression in unaffected baseline
   workloads. No promise of stock PostgreSQL performance parity.
10. Relocatable standalone branchdb runtime/CLI: init/start/stop/status/logs,
    branch create/list/drop/url and JSON output; bundled dependencies, manifests,
    checksums, local SCRAM credentials, private credential files and correct
    process ownership. Linux non-root container with explicit persistent volume.
    Clean-machine framework and lifecycle acceptance on both native targets.

Every engine checkpoint runs core regressions and earlier native suites plus
appropriate SQL/isolation/recovery/security tests. Complete and report each
checkpoint before waiting for the user's go-ahead, as requested. Partial work
must remain explicitly partial. Full raw-XID wrap validation and concurrent
maintenance are separate later engineering tracks.
