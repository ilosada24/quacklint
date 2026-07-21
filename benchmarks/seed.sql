-- Benchmark fixture: a wide table where checks touch only a few columns.
-- Wide on purpose: it is what makes `SELECT *` visible against projection pruning.
DROP TABLE IF EXISTS bench_orders;
CREATE TABLE bench_orders AS
SELECT
    i                                                        AS id,
    (i % 50000)                                              AS customer_id,
    (ARRAY['new','paid','shipped','cancelled'])[1 + i % 4]   AS status,
    round((random() * 900)::numeric, 2)                      AS amount,
    'c-' || lpad((i % 1000)::text, 4, '0')                   AS coupon_code,
    now() - ((i % 90) || ' days')::interval                  AS order_ts,
    now() - ((i % 45) || ' days')::interval                  AS shipped_ts,
    -- payload columns: never referenced by any check
    md5(i::text)      AS note_1,  md5((i+1)::text)  AS note_2,
    md5((i+2)::text)  AS note_3,  md5((i+3)::text)  AS note_4,
    md5((i+4)::text)  AS note_5,  md5((i+5)::text)  AS note_6,
    md5((i+6)::text)  AS note_7,  md5((i+7)::text)  AS note_8,
    repeat('x', 120)  AS description,
    repeat('y', 80)   AS address,
    'region-' || (i % 12)::text AS region,
    (i % 7)           AS priority,
    (i % 3 = 0)       AS is_gift,
    (random() * 100)::int AS score
FROM generate_series(1, 1000000) AS s(i);

-- Deliberate violations so the checks do real work on the failure path.
UPDATE bench_orders SET status = 'bogus' WHERE id % 100000 = 0;
UPDATE bench_orders SET amount = -5 WHERE id % 150000 = 0;
UPDATE bench_orders SET coupon_code = NULL WHERE id % 200000 = 0;

ALTER TABLE bench_orders ADD PRIMARY KEY (id);
ANALYZE bench_orders;
