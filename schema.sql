-- IT Asset Tracking System — database schema (v3)
-- MySQL 8.x
--
-- v2 added: employee IDs and employment lifecycle, loan due dates with
-- extensions, an approval queue for device requests, and a notification
-- outbox that records every alert the system raises.
-- v3 added: procurement and disposal fields, and a full audit trail
-- (asset_events) behind the reporting pages.
-- v4: at most one open loan per asset, enforced by a unique key; the unused
-- people.active column dropped. Existing v3 databases: run migrate_v4.sql.
--
-- Safe to re-run: every table is CREATE TABLE IF NOT EXISTS.

CREATE DATABASE IF NOT EXISTS assettrack
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE assettrack;

-- ---------------------------------------------------------------
-- Admin accounts (single role in v1/v2)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  username      VARCHAR(50)  NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  full_name     VARCHAR(100) NOT NULL,
  created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- ---------------------------------------------------------------
-- People. employee_id (EMP-0042) is the human-facing key: it goes on
-- a badge, and it's what someone types to look themselves up.
-- employment_status drives the offboarding alerts.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS people (
  id                INT AUTO_INCREMENT PRIMARY KEY,
  employee_id       VARCHAR(20)  UNIQUE,
  name              VARCHAR(100) NOT NULL,
  department        VARCHAR(100),
  job_title         VARCHAR(100),
  email             VARCHAR(150),
  phone             VARCHAR(40),
  start_date        DATE,
  exit_date         DATE NULL,
  employment_status ENUM('Active','Leaving','Left') NOT NULL DEFAULT 'Active',
  INDEX idx_emp_status (employment_status)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------
-- The asset register
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  asset_tag       VARCHAR(20)  UNIQUE,
  asset_type      VARCHAR(30)  NOT NULL,
  make            VARCHAR(60),
  model           VARCHAR(100) NOT NULL,
  serial_number   VARCHAR(100) UNIQUE,
  location        VARCHAR(100),
  purchase_date   DATE,
  purchase_cost   DECIMAL(10,2),
  supplier        VARCHAR(100),
  invoice_ref     VARCHAR(60),
  warranty_expiry DATE,
  warranty_provider VARCHAR(100),
  warranty_ref    VARCHAR(60),
  item_condition  ENUM('New','Good','Fair','Poor','Faulty') NOT NULL DEFAULT 'Good',
  status          ENUM('In Use','In Storage','Under Repair','Retired') NOT NULL DEFAULT 'In Storage',
  notes           TEXT,
  disposal_date   DATE NULL,
  disposal_method ENUM('Sold','Recycled','Donated','Destroyed','Returned to supplier','Lost','Stolen') NULL,
  disposal_note   VARCHAR(255),
  disposal_value  DECIMAL(10,2) NULL,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_status (status),
  INDEX idx_type (asset_type),
  INDEX idx_warranty (warranty_expiry)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------
-- Assignments are loans. due_date NULL = open-ended (e.g. a desk
-- monitor). returned_date NULL = still held.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assignments (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  asset_id       INT NOT NULL,
  person_id      INT NOT NULL,
  assigned_date  DATE NOT NULL,
  due_date       DATE NULL,
  returned_date  DATE NULL,
  times_extended INT NOT NULL DEFAULT 0,
  note           VARCHAR(255),
  -- asset_id while the loan is open, NULL once returned. The unique key on
  -- it means an asset can't have two open loans, whatever the app does.
  open_asset_id  INT AS (IF(returned_date IS NULL, asset_id, NULL)) STORED,
  -- No cascade: an asset with loan history can't be deleted (retire it),
  -- so the ownership record can't disappear with it.
  CONSTRAINT fk_assignment_asset FOREIGN KEY (asset_id) REFERENCES assets(id),
  FOREIGN KEY (person_id) REFERENCES people(id),
  UNIQUE KEY uq_one_open_loan (open_asset_id),
  INDEX idx_current (asset_id, returned_date),
  INDEX idx_due (due_date, returned_date)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------
-- Audit trail for every due-date change, so "it was extended twice"
-- is provable rather than asserted.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assignment_extensions (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  assignment_id INT NOT NULL,
  previous_due  DATE NULL,
  new_due       DATE NOT NULL,
  reason        VARCHAR(255),
  decided_by    INT NULL,
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (assignment_id) REFERENCES assignments(id) ON DELETE CASCADE,
  FOREIGN KEY (decided_by) REFERENCES users(id)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------
-- Approval queue. Someone who already holds a device and wants
-- another applies here; nothing is issued until it is approved.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS requests (
  id                 INT AUTO_INCREMENT PRIMARY KEY,
  reference          VARCHAR(20) UNIQUE,
  person_id          INT NOT NULL,
  request_type       ENUM('Additional device','Replacement','Keep longer') NOT NULL,
  asset_type         VARCHAR(30),   -- what they are asking for
  current_asset_id   INT NULL,      -- what they already hold
  assignment_id      INT NULL,      -- the loan being extended
  requested_until    DATE NULL,     -- new due date they are asking for
  reason             TEXT NOT NULL,
  status             ENUM('Pending','Approved','Declined','Fulfilled') NOT NULL DEFAULT 'Pending',
  decision_note      TEXT,
  decided_by         INT NULL,
  decided_at         TIMESTAMP NULL,
  fulfilled_asset_id INT NULL,
  created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (person_id)          REFERENCES people(id) ON DELETE CASCADE,
  FOREIGN KEY (current_asset_id)   REFERENCES assets(id) ON DELETE SET NULL,
  FOREIGN KEY (assignment_id)      REFERENCES assignments(id) ON DELETE SET NULL,
  FOREIGN KEY (fulfilled_asset_id) REFERENCES assets(id) ON DELETE SET NULL,
  FOREIGN KEY (decided_by)         REFERENCES users(id),
  INDEX idx_req_status (status, created_at)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------
-- Notification outbox. Every alert is written here first, then sent
-- if SMTP is configured. dedupe_key stops the daily check raising the
-- same alert twice.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  dedupe_key  VARCHAR(160) UNIQUE,
  kind        VARCHAR(40)  NOT NULL,   -- due_soon | overdue | offboarding | warranty | request
  audience    ENUM('Admin','Employee') NOT NULL,
  person_id   INT NULL,
  email       VARCHAR(150),
  subject     VARCHAR(200) NOT NULL,
  body        TEXT NOT NULL,
  status      ENUM('Queued','Sent','Failed','Skipped') NOT NULL DEFAULT 'Queued',
  error       VARCHAR(255),
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  sent_at     TIMESTAMP NULL,
  FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE,
  INDEX idx_notif (status, created_at)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------
-- Audit trail. Every registration, edit, status change, allocation,
-- return, extension and disposal lands here. asset_id goes NULL if an
-- asset is deleted, but asset_tag is kept as text so the trail survives.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_events (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  asset_id    INT NULL,
  asset_tag   VARCHAR(20),
  event_type  VARCHAR(30) NOT NULL,
  from_value  VARCHAR(120),
  to_value    VARCHAR(120),
  note        VARCHAR(500),
  person_id   INT NULL,
  user_id     INT NULL,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (asset_id)  REFERENCES assets(id) ON DELETE SET NULL,
  FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE SET NULL,
  FOREIGN KEY (user_id)   REFERENCES users(id),
  INDEX idx_event_asset (asset_id, created_at),
  INDEX idx_event_type (event_type, created_at)
) ENGINE=InnoDB;
