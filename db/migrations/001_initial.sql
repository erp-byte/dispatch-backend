-- 001_initial.sql — initial Dispatch DB schema (already applied to warehouse_db)

CREATE TABLE billed_details (
    id                  VARCHAR(20)   PRIMARY KEY,
    route               TEXT          NOT NULL,
    invoice_no          TEXT          NOT NULL,
    invoice_date        DATE          NOT NULL,
    invoice_type        TEXT          NOT NULL,
    customer            TEXT          NOT NULL,
    location            TEXT          NOT NULL,
    pin_code            TEXT,
    customer_gstin      TEXT,
    po_no               TEXT,
    transporter         TEXT,
    lr_no               TEXT,
    warehouse           TEXT,
    e_way_bill_no       TEXT,
    payment_terms       TEXT,
    approx_distance_km  INTEGER,
    irn                 TEXT,
    total_taxable       NUMERIC(14,2) NOT NULL,
    total_gross         NUMERIC(14,2) NOT NULL,
    freight_charge      NUMERIC(14,2) NOT NULL DEFAULT 0,
    transport_per_kg    NUMERIC(10,4),
    dispatch_unit       TEXT,
    source_pdf          TEXT,
    sheet_start_row     INTEGER,
    sheet_end_row       INTEGER,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT billed_details_route_chk    CHECK (route IN ('CFPL','CDPL','SERVICE_CFPL')),
    CONSTRAINT billed_details_id_fmt_chk   CHECK (id ~ '^[0-9]{8}-[0-9]+$'),
    CONSTRAINT billed_details_route_invoice_uk UNIQUE (route, invoice_no)
);

CREATE INDEX billed_details_invoice_date_idx ON billed_details (invoice_date);
CREATE INDEX billed_details_customer_idx     ON billed_details (customer);
CREATE INDEX billed_details_po_no_idx        ON billed_details (po_no);
CREATE INDEX billed_details_route_date_idx   ON billed_details (route, invoice_date);

CREATE TABLE billed_articles (
    id                  VARCHAR(20)   PRIMARY KEY,
    billed_details_id   VARCHAR(20)   NOT NULL,
    article_index       INTEGER       NOT NULL,
    article             TEXT          NOT NULL,
    boxes               INTEGER       NOT NULL DEFAULT 0,
    net_wt_kg           NUMERIC(12,3) NOT NULL DEFAULT 0,
    gr_wt_kg            NUMERIC(12,3),
    taxable_amount      NUMERIC(14,2) NOT NULL DEFAULT 0,
    hsn_code            TEXT,
    rate_per_unit       NUMERIC(14,4),
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT billed_articles_id_fmt_chk      CHECK (id ~ '^[0-9]{8}-[0-9]+$'),
    CONSTRAINT billed_articles_index_chk       CHECK (article_index >= 0),
    CONSTRAINT billed_articles_invoice_slot_uk UNIQUE (billed_details_id, article_index),
    CONSTRAINT billed_articles_invoice_fk      FOREIGN KEY (billed_details_id)
        REFERENCES billed_details (id) ON DELETE CASCADE
);

CREATE INDEX billed_articles_invoice_idx ON billed_articles (billed_details_id);
CREATE INDEX billed_articles_hsn_idx     ON billed_articles (hsn_code);

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER billed_details_touch_updated_at  BEFORE UPDATE ON billed_details
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER billed_articles_touch_updated_at BEFORE UPDATE ON billed_articles
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
