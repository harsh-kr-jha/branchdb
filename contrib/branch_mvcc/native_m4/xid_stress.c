/*
 * Test-only helper, loaded exclusively in the disposable template1 database.
 * Allocate real subtransaction IDs without a million client round trips.
 */
#include "postgres.h"
#include "access/xact.h"
#include "fmgr.h"
#include "miscadmin.h"
#include "utils/memutils.h"
#include "utils/resowner.h"

PG_MODULE_MAGIC;
PG_FUNCTION_INFO_V1(branch_test_consume_xids);

Datum
branch_test_consume_xids(PG_FUNCTION_ARGS)
{
	int32		count = PG_GETARG_INT32(0);
	MemoryContext caller = CurrentMemoryContext;
	ResourceOwner owner = CurrentResourceOwner;
	int32		i;

	if (!superuser() || count < 0 || count > 1200000)
		elog(ERROR, "invalid test-only XID stress request");
	for (i = 0; i < count; i++)
	{
		BeginInternalSubTransaction(NULL);
		(void) GetCurrentTransactionId();
		ReleaseCurrentSubTransaction();
		MemoryContextSwitchTo(caller);
		CurrentResourceOwner = owner;
		if ((i & 1023) == 0)
			CHECK_FOR_INTERRUPTS();
	}
	PG_RETURN_INT32(count);
}
