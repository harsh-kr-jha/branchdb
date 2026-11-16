/* Experimental native branch heap, milestone 1. */
#ifndef BRANCHHEAP_H
#define BRANCHHEAP_H
#include "nodes/nodes.h"
#include "access/htup.h"
#include "utils/relcache.h"
extern PGDLLIMPORT char *BranchName;
extern void BranchGuardUtility(Node *node);
extern bool BranchCatalogEnabled;
extern bool BranchCatalogInternal;
extern void BranchCatalogPrepare(void);
extern bool BranchCatalogScoped(Oid relid);
extern bool BranchCatalogVisible(HeapTuple tuple);
extern void BranchCatalogInserted(Relation rel, HeapTuple tuple);
extern Oid BranchStorageRelation(Oid relid);
extern void BranchCatalogFork(const char *name);
extern void BranchCatalogValidateCreate(Node *node);
extern void BranchCatalogBypass(bool enabled);
extern void BranchMaintenanceAdmission(void);
extern bool BranchMaintenanceWorker(void);
extern bool BranchMaintenanceUtility(Node *node, bool top);
#endif
