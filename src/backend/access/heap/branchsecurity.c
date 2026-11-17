/* Native metadata access is privileged only inside fixed engine queries. */
#include "postgres.h"
#include "access/branchheap.h"
#include "catalog/namespace.h"
#include "catalog/pg_authid_d.h"
#include "executor/spi.h"
#include "miscadmin.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/guc_tables.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/snapmgr.h"

static bool bound_session;

int
BranchInternalQuery(const char *sql, Snapshot snapshot, bool readonly)
{
	Oid			saved;
	int			security,
				nest;
	int			result = 0;

	GetUserIdAndSecContext(&saved, &security);
	nest = NewGUCNestLevel();
	PG_TRY();
	{
		SetUserIdAndSecContext(BOOTSTRAP_SUPERUSERID, security | SECURITY_LOCAL_USERID_CHANGE | SECURITY_RESTRICTED_OPERATION);
		set_config_option("search_path", "pg_catalog", PGC_USERSET, PGC_S_SESSION, GUC_ACTION_SAVE, true, 0, false);
		if (snapshot)
		{
			SPIPlanPtr	plan = SPI_prepare(sql, 0, NULL);

			if (!plan)
				elog(ERROR, "cannot prepare native metadata query");
			result = SPI_execute_snapshot(plan, NULL, NULL, snapshot, InvalidSnapshot, readonly, true, 0);
			SPI_freeplan(plan);
		}
		else
			result = SPI_execute(sql, readonly, 0);
		AtEOXact_GUC(false, nest);
		SetUserIdAndSecContext(saved, security);
	}
	PG_CATCH();
	{
		AtEOXact_GUC(false, nest);
		SetUserIdAndSecContext(saved, security);
		PG_RE_THROW();
	}
	PG_END_TRY();
	if (result < 0)
		elog(ERROR, "native metadata query failed: %d", result);
	return result;
}

bool
BranchSessionAuthorized(void)
{
	return bound_session || superuser();
}

void
BranchSecurityStartup(void)
{
	Oid			ns = get_namespace_oid("branch_native", true);
	char	   *name;
	bool		isnull;
	struct config_generic *option;

	if (!OidIsValid(ns) || !OidIsValid(get_relname_relid("roles", ns)))
		return;
	PushActiveSnapshot(GetTransactionSnapshot());
	SPI_connect();
	BranchInternalQuery("SELECT version FROM branch_native.format", NULL, true);
	if (SPI_processed != 1 || DatumGetInt32(SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1, &isnull)) != 1)
		ereport(FATAL, (errmsg("incompatible native branch metadata format")));
	if (superuser_arg(GetAuthenticatedUserId()))
	{
		SPI_finish();
		PopActiveSnapshot();
		return;
	}
	BranchInternalQuery(psprintf("SELECT b.name FROM branch_native.roles r JOIN branch_native.branches b ON b.id=r.branch WHERE r.role=%u", GetAuthenticatedUserId()), NULL, true);
	if (SPI_processed != 1)
		ereport(FATAL, (errcode(ERRCODE_INSUFFICIENT_PRIVILEGE), errmsg("login role is not bound to a native branch")));
	name = MemoryContextStrdup(TopMemoryContext, TextDatumGetCString(SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1, &isnull)));
	option = find_option("branch", false, false, ERROR);
	if (option->source != PGC_S_DEFAULT && strcmp(BranchName, name) != 0)
		ereport(FATAL, (errcode(ERRCODE_INSUFFICIENT_PRIVILEGE), errmsg("requested branch does not match login role binding")));
	SetConfigOption("branch", name, PGC_BACKEND, PGC_S_OVERRIDE);
	pfree(name);
	bound_session = true;
	SPI_finish();
	PopActiveSnapshot();
}
