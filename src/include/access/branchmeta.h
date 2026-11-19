/* Typed access to the protected, ordinary-heap native metadata tables. */
#ifndef BRANCHMETA_H
#define BRANCHMETA_H

#include "access/htup.h"
#include "utils/relcache.h"
#include "utils/snapshot.h"

typedef struct BranchContext
{
	int64		id;
	int64		cutoff;
} BranchContext;

typedef struct BranchVersion
{
	int64		id;
	int64		root;
	ItemPointerData tid;
	bool		deleted;
	int64		branch;
} BranchVersion;

typedef void (*BranchVersionVisitor) (const BranchVersion *version, void *arg);

extern BranchContext BranchMetaContext(void);
extern int64 BranchMetaReserve(void);
extern void BranchMetaVisit(Relation rel, Snapshot snapshot, BranchContext branch,
							int64 root, BranchVersionVisitor visitor, void *arg);
extern bool BranchMetaVisibleTid(Relation rel, Snapshot snapshot, BranchContext branch,
								 ItemPointer tid, BranchVersion *version);
extern void BranchMetaInsert(Relation rel, BranchContext branch, int64 id,
							 int64 root, int64 predecessor, ItemPointer tid);
extern void BranchMetaRemove(Relation rel, ItemPointer tid);
#endif
