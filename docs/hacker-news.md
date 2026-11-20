# Hacker News submission

Submit after the prerelease's two platform jobs pass and the public download
links and quickstart have been checked from a fresh directory.

Title:

> Show HN: BranchDB – writable database branches inside PostgreSQL

URL:

<https://github.com/harsh-kr-jha/branchdb>

Opening comment draft:

Hi HN, I'm Harsh. I built BranchDB to experiment with giving feature environments
independent database state without copying an entire dataset or running a server
for each branch.

It's an open-source PostgreSQL fork. A branch inherits main's schema and rows at
creation time, then supports independent writes and supported migrations through
a normal PostgreSQL connection URL. Unchanged row storage is shared inside one
cluster. Main remains writable. New catalogs, indexes, and some destructive
migrations still have their own storage costs.

The release includes macOS ARM64 and Linux x86_64 runtimes with PostgreSQL and
Python bundled. The README's demo changes main and one branch, checks that a
sibling retains the original value, and cleans up its temporary instance. No
signup is needed. I've exercised the pinned Prisma and Django fixtures too.

This is an early developer beta on PostgreSQL 20devel, for disposable data.
There's no branch merging or nested branching, and SQL support is limited.
Large scans, first writes, memory use, and maintenance locking still need work;
the repo includes measurements and raw results. The published speedups compare
against my earlier implementation, not stock PostgreSQL.

I'd appreciate feedback on the storage/visibility design and which real migration
patterns would make this useful in your development workflow.

[Show HN guidelines](https://news.ycombinator.com/showhn.html) ask for something
people can try easily and an author who is available to discuss it. Submit from
your HN account when you can spend time answering questions.
