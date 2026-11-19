/*
 * Framework SQL guard. Physical rewrite operations are handled separately from
 * metadata-only changes; unsupported commands continue to fail closed.
 */
#include "postgres.h"
#include "access/branchheap.h"
#include "access/heapam.h"
#include "access/xact.h"
#include "catalog/namespace.h"
#include "catalog/pg_am_d.h"
#include "catalog/pg_namespace_d.h"
#include "catalog/pg_namespace.h"
#include "catalog/pg_proc.h"
#include "catalog/index.h"
#include "miscadmin.h"
#include "nodes/parsenodes.h"
#include "nodes/makefuncs.h"
#include "executor/spi.h"
#include "utils/snapmgr.h"
#include "utils/acl.h"
#include "utils/memutils.h"
#include "nodes/nodeFuncs.h"
#include "utils/fmgroids.h"
#include "utils/builtins.h"
#include "utils/lsyscache.h"

bool
BranchSchemaGuard(Node *node)
{
	RangeVar   *rv = NULL;
	Oid			id,
				table;

	if (!IsTransactionState() || IsAbortedTransactionBlockState())
		return false;
	if (IsA(node, ConstraintsSetStmt))
		return true;
	if (IsA(node, CreateSeqStmt))
	{
		CreateSeqStmt *stmt = (CreateSeqStmt *) node;

		rv = stmt->sequence;
		if (rv->schemaname && strcmp(rv->schemaname, "public"))
			return false;
		if (rv->relpersistence != RELPERSISTENCE_PERMANENT)
			ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("native application sequences must be permanent and public")));
		rv->schemaname = pstrdup("public");
		BranchSchemaAdmission();
		return true;
	}
	if (IsA(node, AlterSeqStmt))
		rv = ((AlterSeqStmt *) node)->sequence;
	else if (IsA(node, RenameStmt))
		rv = ((RenameStmt *) node)->relation;
	else if (IsA(node, CreateSchemaStmt))
	{
		CreateSchemaStmt *stmt = (CreateSchemaStmt *) node;

		if (stmt->schemaname && !strcmp(stmt->schemaname, "public") && stmt->if_not_exists && stmt->schemaElts == NIL && stmt->authrole == NULL)
			return true;
	}
	else if (IsA(node, AlterTableStmt) && ((AlterTableStmt *) node)->objtype == OBJECT_SEQUENCE)
		rv = ((AlterTableStmt *) node)->relation;
	if (!rv)
		return false;
	id = RangeVarGetRelid(rv, AccessExclusiveLock, false);
	if (get_rel_namespace(id) != PG_PUBLIC_NAMESPACE)
		return false;
	table = get_rel_relkind(id) == RELKIND_INDEX ? IndexGetRelation(id, false) : id;
	if (get_rel_relkind(id) != RELKIND_SEQUENCE && get_rel_relam(table) != BRANCH_HEAP_TABLE_AM_OID)
		return false;
	/* Core performs ownership checks and dependency updates. */
	BranchSchemaAdmission();
	return true;
}

static bool
unsafe_cooked_default(Node *node, void *context)
{
	if (!node)
		return false;
	if (IsA(node, FuncExpr))
	{
		Oid			function = ((FuncExpr *) node)->funcid;
		char	   *name = get_func_name(function);

		if (get_func_namespace(function) != PG_CATALOG_NAMESPACE || !name ||
			(((FuncExpr *) node)->funcformat == COERCE_EXPLICIT_CALL && strcmp(name, "nextval") && strcmp(name, "now") && strcmp(name, "statement_timestamp") && strcmp(name, "transaction_timestamp") && strcmp(name, "gen_random_uuid")))
			return true;
	}
	if (IsA(node, OpExpr) || IsA(node, DistinctExpr) || IsA(node, NullIfExpr))
	{
		Oid			function = get_opcode(((OpExpr *) node)->opno);

		if (!OidIsValid(function) || get_func_namespace(function) != PG_CATALOG_NAMESPACE)
			return true;
	}
	if (IsA(node, FuncExpr) && ((FuncExpr *) node)->funcid == F_NEXTVAL)
	{
		FuncExpr   *call = (FuncExpr *) node;
		Node	   *argument = linitial(call->args);
		Oid			id;

		if (!IsA(argument, Const) || ((Const *) argument)->constisnull)
			return true;
		id = DatumGetObjectId(((Const *) argument)->constvalue);
		if (get_rel_namespace(id) != PG_PUBLIC_NAMESPACE || get_rel_relkind(id) != RELKIND_SEQUENCE)
			return true;
	}
	return expression_tree_walker(node, unsafe_cooked_default, context);
}

void
BranchValidateDefault(Relation rel, Node *expr)
{
	if (BranchSchemaEnabled && rel->rd_rel->relam == BRANCH_HEAP_TABLE_AM_OID && unsafe_cooked_default(expr, NULL))
		ereport(ERROR, (errcode(ERRCODE_INSUFFICIENT_PRIVILEGE), errmsg("native default contains an unaudited function or invisible sequence")));
}

/*
 * Framework reset drops only the selected branch's objects. Public is a shared
 * namespace, so its logical reset must never physically drop that namespace.
 */
bool
BranchSchemaUtility(Node *node)
{
	DropStmt   *stmt;
	DropStmt   *drop;
	ListCell   *cell;
	MemoryContext outer = CurrentMemoryContext;
	uint64		i;

	if (!BranchSchemaEnabled || BranchCatalogInternal || !IsA(node, DropStmt) || !IsTransactionState() || IsAbortedTransactionBlockState())
		return false;
	stmt = (DropStmt *) node;
	if (stmt->removeType != OBJECT_SCHEMA)
		return false;
	foreach(cell, stmt->objects)
	{
		Node	   *name = lfirst(cell);

		if (!IsA(name, String) || strcmp(strVal(name), "public"))
			ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("native schema reset supports public only")));
	}
	if (!object_ownercheck(NamespaceRelationId, PG_PUBLIC_NAMESPACE, GetUserId()))
		aclcheck_error(ACLCHECK_NOT_OWNER, OBJECT_SCHEMA, "public");
	if (stmt->behavior != DROP_CASCADE)
		ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("native public reset requires CASCADE")));
	drop = makeNode(DropStmt);
	drop->removeType = OBJECT_TABLE;
	drop->behavior = DROP_CASCADE;
	SPI_connect();
	BranchInternalQuery("SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r'", GetLatestSnapshot(), true);
	MemoryContextSwitchTo(outer);
	for (i = 0; i < SPI_processed; i++)
		drop->objects = lappend(drop->objects, list_make2(makeString(pstrdup("public")), makeString(pstrdup(SPI_getvalue(SPI_tuptable->vals[i], SPI_tuptable->tupdesc, 1)))));
	SPI_finish();
	BranchLifecycleUtility((Node *) drop);
	CommandCounterIncrement();
	BranchCatalogPrepare();
	drop = makeNode(DropStmt);
	drop->removeType = OBJECT_SEQUENCE;
	drop->behavior = DROP_CASCADE;
	SPI_connect();
	BranchInternalQuery("SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='S'", GetLatestSnapshot(), true);
	MemoryContextSwitchTo(outer);
	for (i = 0; i < SPI_processed; i++)
		drop->objects = lappend(drop->objects, list_make2(makeString(pstrdup("public")), makeString(pstrdup(SPI_getvalue(SPI_tuptable->vals[i], SPI_tuptable->tupdesc, 1)))));
	SPI_finish();
	BranchLifecycleUtility((Node *) drop);
	return true;
}
