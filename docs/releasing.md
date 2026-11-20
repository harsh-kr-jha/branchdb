# Releasing BranchDB

`main` contains the current BranchDB engine, runtime, and docs.
`upstream-postgres` preserves the original unmodified PostgreSQL baseline.
The `upstream` remote points to `https://github.com/postgres/postgres.git`.
Feature work branches from `main` and returns through pull requests. Keep upstream
updates separate from feature work so reviewers can inspect the engine changes.

## Publish a beta

1. Commit release notes as `docs/releases/vX.Y.Z-beta.N.md` on `main`.
2. Run the **BranchDB runtime acceptance** workflow on `main`, providing that
   version as its optional `release_tag` input. Leaving the input empty runs
   acceptance without publishing.
3. The workflow builds Linux x86_64 and macOS ARM64, runs the core/native suites,
   packages and checks each archive, exercises real framework workflows, runs
   the Linux container gate, and tests the shipped demo.
4. Only after both platform jobs pass does it create the version tag and publish
   a GitHub prerelease containing those exact archives, checksums, and manifests.
5. Download the published files into a fresh directory, verify checksums and
   payload integrity, and run the README demo before announcing.

Pushing an existing beta tag also runs the same build and publication gates.
Release notes must match the tag. Never move an already published release tag;
use a new beta number for corrections. The initial `v0.1.0-beta.1` tag records a
failed CI candidate, with no published binaries.

Archives retain the `developer-beta` filename label; the GitHub release/tag and
manifest's source commit identify their exact version. Release jobs need
`contents: write`; build and pull-request jobs have read-only permissions.

Keep milestone notes as development history rather than rewriting commits or
using them as the primary quickstart.
