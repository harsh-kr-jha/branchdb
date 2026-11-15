\set ON_ERROR_STOP on
INSERT INTO branch_mvcc.base_items VALUES (1, '{"value":"base"}'), (2, '{"value":"shared"}');
SELECT branch_mvcc.create_branch('branch_a');
SELECT branch_mvcc.create_branch('branch_b');
SET search_path = branch_a, public;
UPDATE items SET payload = '{"value":"a"}' WHERE id = 1;
DELETE FROM items WHERE id = 2;
INSERT INTO items VALUES (3, '{"value":"only-a"}');
TABLE items;
SET search_path = branch_b, public;
TABLE items;
UPDATE items SET payload = '{"value":"b"}' WHERE id = 1;
INSERT INTO items VALUES (3, '{"value":"only-b"}');
TABLE items;
TABLE branch_a.items;
