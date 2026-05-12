-- 002_soft_delete.sql — soft-delete columns + views (already applied)

ALTER TABLE billed_details  ADD COLUMN deleted_at TIMESTAMPTZ;
ALTER TABLE billed_articles ADD COLUMN deleted_at TIMESTAMPTZ;

ALTER TABLE billed_details  DROP CONSTRAINT billed_details_route_invoice_uk;
CREATE UNIQUE INDEX billed_details_route_invoice_uk
    ON billed_details (route, invoice_no) WHERE deleted_at IS NULL;

ALTER TABLE billed_articles DROP CONSTRAINT billed_articles_invoice_slot_uk;
CREATE UNIQUE INDEX billed_articles_invoice_slot_uk
    ON billed_articles (billed_details_id, article_index) WHERE deleted_at IS NULL;

CREATE OR REPLACE VIEW v_billed_details  AS
    SELECT * FROM billed_details  WHERE deleted_at IS NULL;
CREATE OR REPLACE VIEW v_billed_articles AS
    SELECT * FROM billed_articles WHERE deleted_at IS NULL;
