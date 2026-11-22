ALTER TABLE tenants ADD CONSTRAINT tenant_scope CHECK (id > 1);
