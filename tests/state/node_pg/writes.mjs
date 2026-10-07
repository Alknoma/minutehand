// An agent written against node-postgres, writing through Minutehand's relay at DATABASE_URL: unnamed statements
// with text parameters, a named prepared statement reused, a transaction with a savepoint rolled back to, one
// rolled back whole, and a pool of two connections. tests/state/test_postgres_drivers.py replays what it committed.
import pg from "pg";

const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 2 });
const at = new Date("2026-08-24T09:00:00Z");

const db = await pool.connect();
await db.query(
  "CREATE TABLE item(id serial PRIMARY KEY, at timestamptz NOT NULL, body jsonb, price numeric(12, 2), tags text[])",
);
const { rows } = await db.query(
  "INSERT INTO item(at, body, price, tags) VALUES ($1, $2, $3, $4) RETURNING id",
  [at, JSON.stringify({ who: "rosa" }), "12.50", ["venue", "offsite"]],
);
const first = rows[0].id;
await db.query("BEGIN");
await db.query("INSERT INTO item(at) VALUES ($1)", [new Date(at.getTime() + 3600e3)]);
await db.query("SAVEPOINT draft");
await db.query("INSERT INTO item(at) VALUES ($1)", [new Date(at.getTime() + 7200e3)]);
await db.query("ROLLBACK TO SAVEPOINT draft");
await db.query("COMMIT");
await db.query("BEGIN");
await db.query("INSERT INTO item(at) VALUES ($1)", [at]);
await db.query("ROLLBACK");
for (const n of [1, 2, 3]) {
  await db.query({ name: "bump", text: "UPDATE item SET price = coalesce(price, 0) + $1 WHERE id = $2", values: [n, first] });
}
const other = await pool.connect();
await other.query("DELETE FROM item WHERE price IS NULL AND id > $1", [first + 1]);
other.release();
db.release();
await pool.end();
