/* Experimental native branch heap, milestone 1. */
#ifndef BRANCHHEAP_H
#define BRANCHHEAP_H
#include "nodes/nodes.h"
#include "utils/snapshot.h"
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

extern void BranchSecurityStartup(void);
extern bool BranchSessionAuthorized(void);
extern int BranchInternalQuery(const char *sql, Snapshot snapshot, bool readonly);

extern void BranchLifecycleAdmission(void);
extern bool BranchLifecycleEnabled;
extern bool BranchObjectRetired(Oid catalog, Oid object);
extern void BranchLifecycleSession(int64 id);
extern int64 BranchLifecycleSessionId(void);
extern bool BranchLifecycleUtility(Node *node);
extern bool BranchStorageMayUnlink(Relation relation);
extern void BranchLifecycleRefresh(void);

#endif
