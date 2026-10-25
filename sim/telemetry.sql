-- The dashboard of deploy/README.md section 6: one query per chart, read-only
-- on the escrow's database. The simulation runs them on every simulated
-- world; Grafana can run the same ones on the real database. :now is the
-- dashboard's current time (ISO 8601, UTC).

-- name: sales_per_hour
-- Completed sales per hour.
SELECT substr(at, 1, 13) AS hour, COUNT(*) AS completed
FROM sale_events WHERE to_state = 'COMPLETED'
GROUP BY hour ORDER BY hour;

-- name: money_per_day
-- Volume, fees, network costs, losses and profit per day, in msat.
SELECT substr(at, 1, 10) AS day,
       SUM(CASE WHEN account = 'assets:lightning' AND amount_msat > 0 THEN amount_msat ELSE 0 END) AS volume,
       -SUM(CASE WHEN account = 'revenue:fees' THEN amount_msat ELSE 0 END) AS fees,
       SUM(CASE WHEN account = 'expense:routing' THEN amount_msat ELSE 0 END) AS routing,
       SUM(CASE WHEN account = 'expense:losses' THEN amount_msat ELSE 0 END) AS losses,
       -SUM(CASE WHEN account = 'revenue:fees' THEN amount_msat ELSE 0 END)
         - SUM(CASE WHEN account IN ('expense:routing', 'expense:losses') THEN amount_msat ELSE 0 END) AS profit
FROM ledger GROUP BY day ORDER BY day;

-- name: needs_attention
-- Sales a person has to look at.
SELECT sale_id, failure_reason, updated_at FROM sales WHERE state = 'ERROR' ORDER BY updated_at;

-- name: fronted_now
-- Money paid to sellers and not yet collected from buyers, in msat.
SELECT COALESCE(SUM(amount_msat), 0) AS fronted FROM ledger WHERE account = 'clearing:sales';

-- name: stuck_now
-- Sales that have not moved for ten minutes and are not finished.
SELECT sale_id, state, updated_at FROM sales
WHERE state NOT IN ('COMPLETED', 'CANCELLED', 'ERROR', 'RESOLVED')
  AND updated_at < strftime('%Y-%m-%dT%H:%M:%f', :now, '-10 minutes');

-- name: outcomes
-- How the sales ended, by reason.
SELECT state, COALESCE(failure_reason, '') AS reason, COUNT(*) AS n FROM sales GROUP BY state, reason ORDER BY n DESC;
