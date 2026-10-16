-- Upgrade an existing v3 database to v4. Run once:
--
--   mysql -u root -p assettrack < migrate_v4.sql
--
-- A fresh install doesn't need this; schema.sql already creates v4.

USE assettrack;

-- 1. Loans no longer vanish when their asset is deleted. The old foreign key
--    cascaded; v3 didn't name it, so look its name up.
SELECT CONSTRAINT_NAME INTO @fk
FROM information_schema.REFERENTIAL_CONSTRAINTS
WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'assignments'
  AND REFERENCED_TABLE_NAME = 'assets';
SET @sql = CONCAT('ALTER TABLE assignments DROP FOREIGN KEY `', @fk, '`');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

ALTER TABLE assignments
  ADD CONSTRAINT fk_assignment_asset FOREIGN KEY (asset_id) REFERENCES assets(id);

-- 2. At most one open loan per asset.
--
-- If this step fails with a duplicate-entry error, an asset already has two
-- open loans. Find them, close the wrong one (set its returned_date), and
-- run just this statement and step 3 again:
--
--   SELECT asset_id, COUNT(*) FROM assignments
--   WHERE returned_date IS NULL GROUP BY asset_id HAVING COUNT(*) > 1;
ALTER TABLE assignments
  ADD COLUMN open_asset_id INT AS (IF(returned_date IS NULL, asset_id, NULL)) STORED,
  ADD UNIQUE KEY uq_one_open_loan (open_asset_id);

-- 3. people.active duplicated employment_status and was never read.
ALTER TABLE people DROP COLUMN active;
