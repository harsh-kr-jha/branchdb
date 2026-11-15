-- Run once in an empty, disposable database as its owner. Not a core patch.
BEGIN;
CREATE SCHEMA branch_mvcc;
CREATE TABLE branch_mvcc.branches (
    name name PRIMARY KEY,
    revision bigint NOT NULL DEFAULT 0,
    fork_snapshot pg_snapshot NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE branch_mvcc.base_items (id bigint PRIMARY KEY, payload jsonb NOT NULL);
CREATE TABLE branch_mvcc.delta (
    branch name NOT NULL REFERENCES branch_mvcc.branches(name),
    id bigint NOT NULL,
    payload jsonb,
    deleted boolean NOT NULL,
    PRIMARY KEY (branch, id),
    CHECK (deleted OR payload IS NOT NULL)
);

CREATE FUNCTION branch_mvcc.guard_base() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'base is sealed after the first fork' USING ERRCODE = '55000';
END $$;

CREATE FUNCTION branch_mvcc.write_item() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    branch_name name := TG_TABLE_SCHEMA;
    row_id bigint;
    current_payload jsonb;
    present boolean;
BEGIN
    -- Serialize writers within a branch; different branches lock different rows.
    -- UPDATE also causes stale REPEATABLE READ writers to fail with 40001.
    UPDATE branch_mvcc.branches SET revision = revision + 1 WHERE name = branch_name;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown branch %', branch_name; END IF;
    IF TG_OP = 'INSERT' THEN row_id := NEW.id; ELSE row_id := OLD.id; END IF;
    IF row_id IS NULL THEN
        RAISE EXCEPTION 'id must not be null' USING ERRCODE = '23502';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'prototype requires immutable primary keys' USING ERRCODE = '0A000';
    END IF;
    SELECT payload INTO current_payload FROM (
        SELECT d.payload FROM branch_mvcc.delta d
        WHERE d.branch = branch_name AND d.id = row_id AND NOT d.deleted
        UNION ALL
        SELECT b.payload FROM branch_mvcc.base_items b WHERE b.id = row_id
        AND NOT EXISTS (SELECT FROM branch_mvcc.delta d
                        WHERE d.branch = branch_name AND d.id = b.id)
    ) visible;
    present := FOUND;
    IF TG_OP = 'INSERT' AND present THEN
        RAISE EXCEPTION 'duplicate id % in branch %', row_id, branch_name USING ERRCODE = '23505';
    END IF;
    IF TG_OP <> 'INSERT' AND (NOT present OR current_payload IS DISTINCT FROM OLD.payload) THEN
        RAISE EXCEPTION 'concurrent branch change; retry transaction' USING ERRCODE = '40001';
    END IF;
    IF TG_OP <> 'DELETE' AND NEW.payload IS NULL THEN
        RAISE EXCEPTION 'payload must not be null' USING ERRCODE = '23502';
    END IF;
    INSERT INTO branch_mvcc.delta AS d (branch, id, payload, deleted)
    VALUES (branch_name, row_id,
            CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE NEW.payload END,
            TG_OP = 'DELETE')
    ON CONFLICT (branch, id) DO UPDATE SET payload = EXCLUDED.payload, deleted = EXCLUDED.deleted;
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;

CREATE FUNCTION branch_mvcc.create_branch(branch_name text) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    -- A fresh READ COMMITTED snapshot after this lock includes every base writer
    -- that finished before the lock. Sealing and publication commit atomically.
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'create_branch requires READ COMMITTED' USING ERRCODE = '0A000';
    END IF;
    IF branch_name !~ '^[a-z][a-z0-9_]{0,62}$' OR branch_name IN ('public', 'information_schema', 'branch_mvcc')
       OR branch_name LIKE 'pg\_%' ESCAPE '\' THEN
        RAISE EXCEPTION 'invalid branch name %', branch_name;
    END IF;
    LOCK TABLE branch_mvcc.base_items IN ACCESS EXCLUSIVE MODE;
    IF NOT EXISTS (SELECT FROM pg_trigger WHERE tgrelid = 'branch_mvcc.base_items'::regclass
                   AND tgname = 'guard_base') THEN
        CREATE TRIGGER guard_base BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE
        ON branch_mvcc.base_items FOR EACH STATEMENT EXECUTE FUNCTION branch_mvcc.guard_base();
    END IF;
    INSERT INTO branch_mvcc.branches(name, fork_snapshot) VALUES (branch_name, pg_current_snapshot());
    EXECUTE format('CREATE SCHEMA %I', branch_name);
    EXECUTE format($view$
        CREATE VIEW %I.items AS
        SELECT b.id, b.payload FROM branch_mvcc.base_items b
        WHERE NOT EXISTS (SELECT FROM branch_mvcc.delta d WHERE d.branch = %L AND d.id = b.id)
        UNION ALL
        SELECT d.id, d.payload FROM branch_mvcc.delta d WHERE d.branch = %L AND NOT d.deleted
    $view$, branch_name, branch_name, branch_name);
    EXECUTE format('CREATE TRIGGER branch_write INSTEAD OF INSERT OR UPDATE OR DELETE ON %I.items
                    FOR EACH ROW EXECUTE FUNCTION branch_mvcc.write_item()', branch_name);
END $$;
REVOKE ALL ON SCHEMA branch_mvcc FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA branch_mvcc FROM PUBLIC;
COMMIT;
