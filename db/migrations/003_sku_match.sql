-- 003_sku_match.sql — add SKU-match denorm columns to billed_articles
-- Source SKU table is `all_sku` in the same warehouse_db. The matched_sku_id
-- references all_sku(sku_id) but we do NOT add a hard FK constraint because
-- all_sku is owned by another service and its rows may be deleted out-of-band.
-- A soft FK with periodic reconciliation is the right contract.

ALTER TABLE billed_articles ADD COLUMN matched_sku_id      BIGINT;
ALTER TABLE billed_articles ADD COLUMN matched_item_type   TEXT;
ALTER TABLE billed_articles ADD COLUMN matched_item_group  TEXT;
ALTER TABLE billed_articles ADD COLUMN matched_sub_group   TEXT;
ALTER TABLE billed_articles ADD COLUMN matched_uom         NUMERIC(15,3);
ALTER TABLE billed_articles ADD COLUMN matched_gst         NUMERIC(15,3);
ALTER TABLE billed_articles ADD COLUMN matched_sale_group  TEXT;

-- Index for joining/filtering on matched SKU
CREATE INDEX billed_articles_matched_sku_id_idx
    ON billed_articles (matched_sku_id) WHERE matched_sku_id IS NOT NULL;

-- Refresh the v_billed_articles view to include the new columns
DROP VIEW IF EXISTS v_billed_articles;
CREATE VIEW v_billed_articles AS
    SELECT * FROM billed_articles WHERE deleted_at IS NULL;
