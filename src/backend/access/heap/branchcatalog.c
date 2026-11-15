/* Experimental branch-owned catalog identities and retained storage aliases.
 * Schema metadata is duplicated; unchanged user tuples/index pages are shared.
 * Only the guarded CREATE TABLE / nullable ADD COLUMN subset is supported. */
#include "postgres.h"
#include "access/branchheap.h"
#include "access/heapam.h"
#include "access/xact.h"
#include "catalog/indexing.h"
#include "catalog/namespace.h"
#include "catalog/pg_am_d.h"
#include "catalog/pg_attrdef.h"
#include "catalog/pg_attribute.h"
#include "catalog/pg_class.h"
#include "catalog/pg_constraint.h"
#include "catalog/pg_depend.h"
#include "catalog/pg_description.h"
#include "catalog/pg_index.h"
#include "catalog/pg_namespace_d.h"
#include "catalog/pg_type.h"
#include "catalog/storage.h"
#include "executor/spi.h"
#include "commands/defrem.h"
#include "miscadmin.h"
#include "nodes/parsenodes.h"
#include "nodes/makefuncs.h"
#include "parser/parse_type.h"
#include "storage/lock.h"
#include "utils/builtins.h"
#include "utils/hsearch.h"
#include "utils/inval.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/snapmgr.h"
#include "utils/syscache.h"

bool BranchCatalogEnabled = false;
bool BranchCatalogInternal = false;
static bool loading;
static bool bypass;
static int64 branch_id;
static Oid owners_oid;
static MemoryContext owner_context;

typedef struct OwnerKey { Oid catalog; Oid object; } OwnerKey;
typedef struct OwnerEntry { OwnerKey key; int64 owner; Oid source; } OwnerEntry;
static HTAB *owners;

static void
reject(const char *what)
{
	ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					errmsg("branch catalog milestone 3 does not support %s", what)));
}

static OwnerEntry *
lookup(Oid catalog, Oid object)
{
	OwnerKey key = {catalog, object};

	return owners ? hash_search(owners, &key, HASH_FIND, NULL) : NULL;
}

static void
remember(Oid catalog, Oid object, int64 owner, Oid source)
{
	OwnerKey key = {catalog, object};
	OwnerEntry *e = hash_search(owners, &key, HASH_ENTER, NULL);

	e->owner = owner;
	e->source = source;
}

static bool
object_visible(Oid catalog, Oid object)
{
	OwnerEntry *e;

	if (!OidIsValid(object) || object < FirstNormalObjectId)
		return true;
	e = lookup(catalog, object);
	/* Unknown user objects are hidden, never treated as globally visible.
	 * Another branch may have just committed DDL after our metadata snapshot. */
	return e && (e->owner < 0 || e->owner == branch_id);
}

Oid
BranchStorageRelation(Oid relid)
{
	OwnerEntry *e;

	if (!BranchCatalogEnabled)
		return relid;
	e = lookup(RelationRelationId, relid);
	return e && OidIsValid(e->source) ? e->source : relid;
}

bool
BranchCatalogScoped(Oid relid)
{
	if (!BranchCatalogEnabled || loading || bypass)
		return false;
	switch (relid)
	{
		case RelationRelationId: case TypeRelationId: case AttributeRelationId:
		case IndexRelationId: case ConstraintRelationId: case DependRelationId:
		case AttrDefaultRelationId: case DescriptionRelationId:
			return true;
		default:
			return false;
	}
}

bool
BranchCatalogVisible(HeapTuple tuple)
{
	if (!BranchCatalogScoped(tuple->t_tableOid))
		return true;
	switch (tuple->t_tableOid)
	{
		case RelationRelationId:
		{
			Form_pg_class c = (Form_pg_class) GETSTRUCT(tuple);
			return c->relnamespace != PG_PUBLIC_NAMESPACE || object_visible(RelationRelationId, c->oid);
		}
		case TypeRelationId:
		{
			Form_pg_type t = (Form_pg_type) GETSTRUCT(tuple);
			return t->typnamespace != PG_PUBLIC_NAMESPACE || object_visible(TypeRelationId, t->oid);
		}
		case AttributeRelationId:
			return object_visible(RelationRelationId, ((Form_pg_attribute) GETSTRUCT(tuple))->attrelid);
		case IndexRelationId:
			return object_visible(RelationRelationId, ((Form_pg_index) GETSTRUCT(tuple))->indexrelid);
		case ConstraintRelationId:
			return object_visible(ConstraintRelationId, ((Form_pg_constraint) GETSTRUCT(tuple))->oid);
		case AttrDefaultRelationId:
			return object_visible(RelationRelationId, ((Form_pg_attrdef) GETSTRUCT(tuple))->adrelid);
		case DescriptionRelationId:
		{
			Form_pg_description d = (Form_pg_description) GETSTRUCT(tuple);
			if (d->classoid != RelationRelationId && d->classoid != TypeRelationId && d->classoid != ConstraintRelationId)
				return true;
			return object_visible(d->classoid, d->objoid);
		}
		case DependRelationId:
		{
			Form_pg_depend d = (Form_pg_depend) GETSTRUCT(tuple);
			OwnerEntry *e = lookup(d->classid, d->objid);
			OwnerEntry *r = lookup(d->refclassid, d->refobjid);
			bool managed = d->classid == RelationRelationId || d->classid == TypeRelationId || d->classid == ConstraintRelationId;
			bool refmanaged = d->refclassid == RelationRelationId || d->refclassid == TypeRelationId || d->refclassid == ConstraintRelationId;

			if (e && e->owner >= 0) return e->owner == branch_id;
			if (!e && managed && d->objid >= FirstNormalObjectId) return false;
			if (r && r->owner >= 0) return r->owner == branch_id;
			if (!r && refmanaged && d->refobjid >= FirstNormalObjectId) return false;
			return true;
		}
		default:
			return true;
	}
}

/* Run outside buffer locks before parsing/revalidating user SQL. */
void
BranchCatalogPrepare(void)
{
	Oid ns;
	HASHCTL ctl = {0};
	MemoryContext old;
	MemoryContext previous_context = owner_context;
	HTAB *previous_owners = owners;
	bool pushed = false;
	int64 next_branch;
	uint64 i;

	if (loading || BranchCatalogInternal || !IsTransactionState() || IsAbortedTransactionBlockState())
		return;
	ns = get_namespace_oid("branch_native", true);
	if (!OidIsValid(ns) || !OidIsValid(get_relname_relid("objects", ns)))
		return;
	owners_oid = get_relname_relid("objects", ns);
	loading = true;
	PG_TRY();
	{
		/* Refresh after a branch DDL lock wait, even if the outer command
		 * established its statement snapshot before the wait. */
		PushActiveSnapshot(GetLatestSnapshot());
		pushed = true;
		SPI_connect();
		SPI_execute(psprintf("SELECT id FROM branch_native.branches WHERE name=%s",
							 quote_literal_cstr(BranchName)), true, 0);
		if (SPI_processed != 1)
		{
			/* During setup, no main descriptor exists yet. */
			if (!BranchCatalogEnabled && strcmp(BranchName, "main") == 0)
			{
				SPI_finish();
				if (pushed) PopActiveSnapshot();
				loading = false;
				goto prepared_done;
			}
			ereport(ERROR, (errcode(ERRCODE_UNDEFINED_OBJECT), errmsg("unknown native branch \"%s\"", BranchName)));
		}
		{
			bool isnull;
			next_branch = DatumGetInt64(SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1, &isnull));
		}
		owner_context = AllocSetContextCreate(TopMemoryContext, "branch catalog ownership", ALLOCSET_DEFAULT_SIZES);
		old = MemoryContextSwitchTo(owner_context);
		ctl.keysize = sizeof(OwnerKey);
		ctl.entrysize = sizeof(OwnerEntry);
		ctl.hcxt = owner_context;
		owners = hash_create("branch object ownership", 256, &ctl, HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
		MemoryContextSwitchTo(old);
		/* Shared system/metadata/TOAST descriptors are global even when their
		 * dynamically allocated OIDs exceed the bootstrap OID range. */
		SPI_execute("SELECT 'pg_class'::regclass::oid,oid FROM pg_class WHERE relnamespace <> 'public'::regnamespace "
					"UNION ALL SELECT 'pg_type'::regclass::oid,oid FROM pg_type WHERE typnamespace <> 'public'::regnamespace "
					"UNION ALL SELECT 'pg_constraint'::regclass::oid,oid FROM pg_constraint WHERE connamespace <> 'public'::regnamespace", true, 0);
		for (i = 0; i < SPI_processed; i++)
		{
			bool n;
			Oid cat = DatumGetObjectId(SPI_getbinval(SPI_tuptable->vals[i], SPI_tuptable->tupdesc, 1, &n));
			Oid obj = DatumGetObjectId(SPI_getbinval(SPI_tuptable->vals[i], SPI_tuptable->tupdesc, 2, &n));
			remember(cat, obj, -1, InvalidOid);
		}
		SPI_execute("SELECT catalog,object,owner,source FROM branch_native.objects", true, 0);
		for (i = 0; i < SPI_processed; i++)
		{
			bool n;
			HeapTuple t = SPI_tuptable->vals[i];
			TupleDesc d = SPI_tuptable->tupdesc;
			remember(DatumGetObjectId(SPI_getbinval(t,d,1,&n)), DatumGetObjectId(SPI_getbinval(t,d,2,&n)),
					 DatumGetInt64(SPI_getbinval(t,d,3,&n)), DatumGetObjectId(SPI_getbinval(t,d,4,&n)));
		}
		SPI_finish();
		if (pushed) PopActiveSnapshot();
		if (previous_context) MemoryContextDelete(previous_context);
		branch_id = next_branch;
		BranchCatalogEnabled = true;
		loading = false;
prepared_done:
		;
	}
	PG_CATCH();
	{
		if (owner_context != previous_context) MemoryContextDelete(owner_context);
		owner_context = previous_context;
		owners = previous_owners;
		loading = false;
		PG_RE_THROW();
	}
	PG_END_TRY();
}

/* Capture identities before catalog B-tree insertion; never called while its
 * page locks are held. Metadata uses ordinary heap/index WAL and transaction
 * rollback. Ownership is independent of catalog tuple XID freezing. */
void
BranchCatalogInserted(Relation rel, HeapTuple tuple)
{
	Oid cat = RelationGetRelid(rel);
	Oid obj, source = InvalidOid;
	int64 owner = branch_id;
	Relation meta;
	Datum values[4];
	bool nulls[4] = {false,false,false,false};
	HeapTuple record;

	if (!BranchCatalogEnabled || loading)
		return;
	if (cat == RelationRelationId)
	{
		Form_pg_class c = (Form_pg_class) GETSTRUCT(tuple);
		obj = c->oid;
		if (c->relnamespace != PG_PUBLIC_NAMESPACE) owner = -1;
		if (c->relam == BRANCH_HEAP_TABLE_AM_OID) source = obj;
	}
	else if (cat == TypeRelationId)
	{
		Form_pg_type t = (Form_pg_type) GETSTRUCT(tuple);
		obj = t->oid;
		if (t->typnamespace != PG_PUBLIC_NAMESPACE) owner = -1;
	}
	else if (cat == ConstraintRelationId)
	{
		Form_pg_constraint c = (Form_pg_constraint) GETSTRUCT(tuple);
		obj = c->oid;
		if (c->connamespace != PG_PUBLIC_NAMESPACE) owner = -1;
	}
	else return;
	remember(cat, obj, owner, source);
	meta = table_open(owners_oid, RowExclusiveLock);
	values[0] = ObjectIdGetDatum(cat); values[1] = ObjectIdGetDatum(obj);
	values[2] = Int64GetDatum(owner); values[3] = ObjectIdGetDatum(source);
	record = heap_form_tuple(RelationGetDescr(meta), values, nulls);
	CatalogTupleInsert(meta, record);
	heap_freetuple(record);
	table_close(meta, RowExclusiveLock);
}

static void
validate_column(ColumnDef *c, bool adding)
{
	ListCell *lc;
	Oid typid;
	int32 typmod;
	HeapTuple t;

	if (c->raw_default || c->cooked_default || c->identity || c->generated || c->collClause || c->storage_name || c->compression)
		reject("defaults, identity/generated columns, custom storage/compression or collations");
	if (adding && c->is_not_null) reject("ADD COLUMN NOT NULL");
	foreach(lc, c->constraints)
	{
		Constraint *con = lfirst_node(Constraint, lc);
		if (adding && con->contype != CONSTR_NULL) reject("non-nullable or constrained ADD COLUMN");
		if (con->contype == CONSTR_NOTNULL && con->conname) reject("custom-named NOT NULL constraints");
		if (con->deferrable || (con->contype != CONSTR_NULL && con->contype != CONSTR_NOTNULL &&
			con->contype != CONSTR_PRIMARY && con->contype != CONSTR_UNIQUE)) reject("column constraints beyond immediate primary/unique/not-null");
	}
	typenameTypeIdAndMod(NULL, c->typeName, &typid, &typmod);
	t = SearchSysCache1(TYPEOID, ObjectIdGetDatum(typid));
	if (!HeapTupleIsValid(t)) elog(ERROR,"type cache lookup failed");
	if (((Form_pg_type) GETSTRUCT(t))->typnamespace != PG_CATALOG_NAMESPACE) reject("user-defined column types");
	ReleaseSysCache(t);
}

void
BranchCatalogValidateCreate(Node *node)
{
	ListCell *lc;
	if (IsA(node, CreateStmt))
	{
		CreateStmt *c = (CreateStmt *) node;
		if (c->relation->relpersistence != RELPERSISTENCE_PERMANENT || c->inhRelations || c->partbound || c->partspec || c->ofTypename)
			reject("temporary/unlogged, inherited, partitioned or typed tables");
		if (c->relation->schemaname && strcmp(c->relation->schemaname,"public") != 0)
			reject("application schemas other than public");
		if (c->tablespacename) reject("custom tablespaces");
		foreach(lc,c->options)
		{
			DefElem *option = lfirst_node(DefElem,lc);
			if (strcmp(option->defname,"autovacuum_enabled") != 0 || defGetBoolean(option))
				reject("table storage options other than autovacuum_enabled=false");
		}
		c->relation->schemaname = pstrdup("public");
		if (c->options == NIL)
			c->options = list_make1(makeDefElem("autovacuum_enabled",(Node *)makeString("false"),-1));
		if (c->accessMethod && strcmp(c->accessMethod,"branch_heap") != 0) reject("application tables outside branch_heap");
		c->accessMethod = pstrdup("branch_heap");
		foreach(lc, c->tableElts)
		{
			Node *elt = lfirst(lc);
			if (IsA(elt,ColumnDef)) validate_column((ColumnDef *) elt,false);
			else if (IsA(elt,Constraint))
			{
				Constraint *con = (Constraint *) elt;
				if (con->deferrable || (con->contype != CONSTR_PRIMARY && con->contype != CONSTR_UNIQUE)) reject("table constraints beyond immediate primary/unique");
			}
			else reject("LIKE and other table definition clauses");
		}
	}
	else if (IsA(node,AlterTableStmt) || IsA(node,IndexStmt))
	{
		RangeVar *rv = IsA(node,AlterTableStmt) ? ((AlterTableStmt *)node)->relation : ((IndexStmt *)node)->relation;
		Oid relid = RangeVarGetRelid(rv,AccessShareLock,false);
		Relation rel = table_open(relid,NoLock);

		if (rel->rd_rel->relnamespace != PG_PUBLIC_NAMESPACE || rel->rd_rel->relam != BRANCH_HEAP_TABLE_AM_OID)
			reject("DDL outside branch-owned public tables");
		table_close(rel,NoLock);
		if (IsA(node,IndexStmt)) return;
		foreach(lc, ((AlterTableStmt *) node)->cmds)
		{
			AlterTableCmd *cmd = lfirst_node(AlterTableCmd, lc);
			if (cmd->subtype == AT_AddIndex) continue;
			if (cmd->subtype != AT_AddColumn || !IsA(cmd->def,ColumnDef)) reject("ALTER TABLE beyond nullable ADD COLUMN");
			validate_column((ColumnDef *) cmd->def,true);
		}
	}
}

/* Repoint an empty descriptor's storage, retaining source files. DDL that
 * unlinks/rebuilds shared files remains prohibited. Only empty, newly created
 * destination files are scheduled for deletion; abort never deletes source. */
static void
alias_file(Oid destid, Oid sourceid)
{
	Relation src, dst, classes;
	HeapTuple tuple;
	Form_pg_class form;
	Oid filenode, tablespace;
	TransactionId frozen;
	MultiXactId minmulti;
	int32 pages;
	float4 tuples;

	bypass = true;
	src = relation_open(sourceid, AccessShareLock);
	filenode = src->rd_rel->relfilenode;
	tablespace = src->rd_rel->reltablespace;
	frozen = src->rd_rel->relfrozenxid;
	minmulti = src->rd_rel->relminmxid;
	pages = src->rd_rel->relpages;
	tuples = src->rd_rel->reltuples;
	relation_close(src, NoLock);
	bypass = false;
	dst = relation_open(destid, AccessExclusiveLock);
	RelationDropStorage(dst);
	relation_close(dst, NoLock);
	classes = table_open(RelationRelationId, RowExclusiveLock);
	tuple = SearchSysCacheCopy1(RELOID, ObjectIdGetDatum(destid));
	if (!HeapTupleIsValid(tuple)) elog(ERROR,"missing branch destination relation");
	form = (Form_pg_class) GETSTRUCT(tuple);
	form->relfilenode = filenode;
	form->reltablespace = tablespace;
	form->relfrozenxid = frozen;
	form->relminmxid = minmulti;
	form->relpages = pages;
	form->reltuples = tuples;
	CatalogTupleUpdate(classes, &tuple->t_self, tuple);
	heap_freetuple(tuple);
	table_close(classes, RowExclusiveLock);
	CommandCounterIncrement();
	/* Every storage alias, including indexes and TOAST, must use the same
	 * physical extension lock as its source, despite distinct logical OIDs. */
	{
		OwnerEntry *entry = lookup(RelationRelationId,destid);
		int64 owner = entry ? entry->owner : branch_id;
		Oid root = BranchStorageRelation(sourceid);

		SPI_connect();
		SPI_execute(psprintf("UPDATE branch_native.objects SET source=%u WHERE catalog='pg_class'::regclass AND object=%u",root,destid),false,0);
		SPI_finish();
		remember(RelationRelationId,destid,owner,root);
	}
}

typedef struct CloneIndex { Oid source; char *name; char *ddl; bool standalone; } CloneIndex;
typedef struct CloneTable
{
	Oid source;
	Oid ns;
	Oid toast;
	char *name;
	char *ddl;
	int nindexes;
	CloneIndex *indexes;
} CloneTable;

void
BranchCatalogFork(const char *name)
{
	CloneTable *tables;
	int ntables;
	MemoryContext caller = CurrentMemoryContext;
	char *saved_name = BranchName;
	int64 saved_id = branch_id;
	int64 child;
	uint64 i;

	if (!BranchCatalogEnabled) return;
	SPI_connect();
	SPI_execute(psprintf("SELECT id FROM branch_native.branches WHERE name=%s",quote_literal_cstr(name)),false,0);
	if (SPI_processed != 1) elog(ERROR,"new branch descriptor is not visible");
	{
		bool n;
		child = DatumGetInt64(SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc,1,&n));
	}
	SPI_execute("SELECT c.oid,c.relnamespace,c.reltoastrelid,c.relname,"
		"format('CREATE TABLE public.%I (%s%s) USING branch_heap WITH (autovacuum_enabled=false)',c.relname,"
		"(SELECT string_agg(format('%I %s%s',a.attname,format_type(a.atttypid,a.atttypmod),"
		"CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END),',' ORDER BY a.attnum) FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped),"
		"COALESCE((SELECT ','||string_agg(format('CONSTRAINT %I %s',k.conname,pg_get_constraintdef(k.oid)),',' ORDER BY k.oid) FROM pg_constraint k WHERE k.conrelid=c.oid AND k.contype IN ('p','u')),'')) "
		"FROM pg_class c JOIN branch_native.objects o ON o.catalog='pg_class'::regclass AND o.object=c.oid "
		"WHERE o.owner=0 AND c.relam=(SELECT oid FROM pg_am WHERE amname='branch_heap') AND c.relkind='r' ORDER BY c.oid",false,0);
	ntables = SPI_processed;
	tables = MemoryContextAllocZero(caller,sizeof(CloneTable)*Max(ntables,1));
	for (i=0;i<SPI_processed;i++)
	{
		HeapTuple t=SPI_tuptable->vals[i]; TupleDesc d=SPI_tuptable->tupdesc; bool n;
		tables[i].source=DatumGetObjectId(SPI_getbinval(t,d,1,&n));
		tables[i].ns=DatumGetObjectId(SPI_getbinval(t,d,2,&n));
		tables[i].toast=DatumGetObjectId(SPI_getbinval(t,d,3,&n));
		tables[i].name=MemoryContextStrdup(caller,SPI_getvalue(t,d,4));
		tables[i].ddl=MemoryContextStrdup(caller,SPI_getvalue(t,d,5));
	}
	for(i=0;i<ntables;i++)
	{
		uint64 j;
		SPI_execute(psprintf("SELECT i.indexrelid,c.relname,pg_get_indexdef(i.indexrelid),"
			"NOT EXISTS(SELECT FROM pg_constraint k WHERE k.conindid=i.indexrelid) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid WHERE i.indrelid=%u ORDER BY i.indexrelid",tables[i].source),false,0);
		tables[i].nindexes=SPI_processed;
		tables[i].indexes=MemoryContextAllocZero(caller,sizeof(CloneIndex)*Max(SPI_processed,1));
		for(j=0;j<SPI_processed;j++)
		{
			HeapTuple t=SPI_tuptable->vals[j]; TupleDesc d=SPI_tuptable->tupdesc; bool n;
			tables[i].indexes[j].source=DatumGetObjectId(SPI_getbinval(t,d,1,&n));
			tables[i].indexes[j].name=MemoryContextStrdup(caller,SPI_getvalue(t,d,2));
			tables[i].indexes[j].ddl=MemoryContextStrdup(caller,SPI_getvalue(t,d,3));
			tables[i].indexes[j].standalone=DatumGetBool(SPI_getbinval(t,d,4,&n));
		}
	}
	SPI_finish();
	BranchCatalogInternal = true;
	PG_TRY();
	{
		BranchName = (char *) name;
		branch_id = child;
		InvalidateSystemCaches();
		for(i=0;i<ntables;i++)
		{
			Oid dest, desttoast;
			uint64 j;
			HeapTuple t;

			SPI_connect();
			if(SPI_execute(tables[i].ddl,false,0)<0) elog(ERROR,"cannot create branch schema descriptor");
			SPI_finish();
			for(j=0;j<tables[i].nindexes;j++)
				if(tables[i].indexes[j].standalone)
				{
					SPI_connect();
					if(SPI_execute(tables[i].indexes[j].ddl,false,0)<0) elog(ERROR,"cannot clone branch index descriptor");
					SPI_finish();
				}
			dest=get_relname_relid(tables[i].name,tables[i].ns);
			t=SearchSysCache1(RELOID,ObjectIdGetDatum(dest));
			if (!HeapTupleIsValid(t)) elog(ERROR,"missing cloned table %s (%u)",tables[i].name,dest);
			desttoast=((Form_pg_class)GETSTRUCT(t))->reltoastrelid;
			ReleaseSysCache(t);
			if(OidIsValid(desttoast) && OidIsValid(tables[i].toast))
			{
				Oid srcindex,dstindex;
				Relation r;
				bypass=true;
				r=table_open(tables[i].toast,AccessShareLock);
				srcindex=linitial_oid(RelationGetIndexList(r));
				table_close(r,NoLock);
				bypass=false;
				r=table_open(desttoast,AccessExclusiveLock);
				dstindex=linitial_oid(RelationGetIndexList(r));
				table_close(r,NoLock);
				alias_file(desttoast,tables[i].toast);
				alias_file(dstindex,srcindex);
			}
			alias_file(dest,tables[i].source);
			for(j=0;j<tables[i].nindexes;j++)
			{
				Oid dstindex=get_relname_relid(tables[i].indexes[j].name,tables[i].ns);
				alias_file(dstindex,tables[i].indexes[j].source);
			}
			SPI_connect();
			SPI_execute(psprintf("UPDATE branch_native.objects SET source=%u WHERE catalog='pg_class'::regclass AND object=%u",BranchStorageRelation(tables[i].source),dest),false,0);
			SPI_finish();
			remember(RelationRelationId,dest,child,BranchStorageRelation(tables[i].source));
		}
		BranchName=saved_name;
		branch_id=saved_id;
		BranchCatalogInternal=false;
		InvalidateSystemCaches();
	}
	PG_CATCH();
	{
		bypass=false;
		BranchName=saved_name;
		branch_id=saved_id;
		BranchCatalogInternal=false;
		InvalidateSystemCaches();
		PG_RE_THROW();
	}
	PG_END_TRY();
}
