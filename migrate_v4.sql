-- Upgrade an existing v3 database to v4.
-- Fresh installs should use schema.sql
USE assettrack;


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

-- Each asset has a loan once at a time
-- Close loan if duplication exist
ALTER TABLE assignments
  ADD COLUMN open_asset_id INT AS (IF(returned_date IS NULL, asset_id, NULL)) STORED,
  ADD UNIQUE KEY uq_one_open_loan (open_asset_id);

-- People.active duplicated employment_status and was never read.
ALTER TABLE people DROP COLUMN active;
