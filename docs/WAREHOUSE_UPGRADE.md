# Upgrading your PostgreSQL warehouse

The warehouse is the PostgreSQL database that several machines sync to (see
[Sync](SYNC.md)). Two kinds of upgrade touch it:

1. **Upgrading m3.** m3 updates the warehouse's tables by itself on the first sync.
   It needs one thing from you: the database account m3 connects as must own those
   tables.
2. **Upgrading the PostgreSQL server** to a new major version (for example 15 to 18).
   You do this; m3 is not involved.

**Supported:** PostgreSQL 16 or newer. m3 is tested on 16, 17 and 18.

## Before either upgrade: back up

```bash
pg_dump -Fc -d <warehouse database> -f warehouse-$(date -u +%Y%m%d).dump
```

Run it as an account that can read every table (the owner, or a superuser). A
sync that runs during an upgrade fails, says so in its log, and retries on its
next run; nothing is lost, because each sync sends only what changed since the
last one.

## 1. Upgrading m3

### What happens

Before each sync, m3 checks the warehouse's schema version
(`m3_warehouse.schema_versions`) and applies any changes it is missing. Changes
are additive: a machine still on an older m3 keeps syncing against a newer
warehouse. Two machines syncing at the same time cannot both apply a change.

### What it needs: the m3 account owns the tables

PostgreSQL lets only a table's owner add a column or an index to it. If someone
created the warehouse tables as a different account (an administrator, or
`postgres`), the first sync that needs a change stops with:

```
warehouse table m3_warehouse.memory_items needs column variant, and role m3 does not own the table (owner: postgres). fix: ALTER TABLE m3_warehouse.memory_items OWNER TO m3;
```

Nothing is changed when this happens; the sync stops until the owner is fixed.

**Check, before you upgrade** (in `psql`, connected to the warehouse database):

```sql
SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'm3_warehouse';
```

Every row should show the account in your warehouse address (`M3_CDW_PG_URL`).
From a source checkout, `python bin/pg_setup.py --check` reports the same and exits
3 when tables belong to another account.

**Fix**, as the current owner or a superuser (replace `m3` with your account):

```sql
BEGIN;
ALTER SCHEMA m3_warehouse OWNER TO m3;
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'm3_warehouse' AND tableowner <> 'm3' LOOP
    EXECUTE format('ALTER TABLE m3_warehouse.%I OWNER TO m3', r.tablename);
  END LOOP;
END $$;
COMMIT;
```

Indexes follow their table. No data changes.

### Checking it worked

After the first sync on the new m3:

```sql
SELECT version, filename, applied_at FROM m3_warehouse.schema_versions ORDER BY version;
```

and the sync log ends with `sync_all complete: all systems synced`.

## 2. Upgrading the PostgreSQL server

Pick the route your platform supports:

- **Debian or Ubuntu** (the `postgresql-common` tools): install the new version from
  the PostgreSQL apt repository, then
  `pg_upgradecluster -v <new> --method=upgrade <old> main`. It copies the data into a
  new cluster on the same port, carries over your access rules (`pg_hba.conf`) and
  listen addresses, and leaves the old cluster stopped on another port as your way
  back. It needs free disk space about the size of the data.
- **Any platform:** `pg_dump` from the old server, `pg_restore` into the new one,
  then recreate the roles and access rules.
- **A managed service:** use the provider's major-version upgrade.

Install the same extensions on the new version before upgrading (`postgres_fdw`
ships with PostgreSQL; `pgvector` is a separate package).

### Verify before you rely on it

1. Row counts match, table by table, before and after.
2. Your access rules and listen addresses are in the new cluster's configuration.
3. From each client machine, a sync completes.

Keep the old cluster until the new one has run cleanly for a few days, then remove
it (`pg_dropcluster <old> main` on Debian/Ubuntu).
