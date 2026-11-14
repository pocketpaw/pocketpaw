-- Eval schema: a small online shop (customers with PII, products, orders, line items).
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, email TEXT, phone TEXT, city TEXT,
  created_at TEXT);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price_cents INTEGER, category TEXT,
  stock INTEGER);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
  total_cents INTEGER, status TEXT, created_at TEXT);
CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(id),
  product_id INTEGER REFERENCES products(id), qty INTEGER, price_cents INTEGER);
INSERT INTO customers VALUES (1, 'Ada Lovelace', 'ada@x.io', '+44 1', 'London', '2026-09-01'),
  (2, 'Bo Diddley', 'bo@x.io', '+1 2', 'Chicago', '2026-09-15');
INSERT INTO products VALUES (1, 'Mug', 1200, 'kitchen', 40), (2, 'Tee', 2500, 'apparel', 12);
INSERT INTO orders VALUES (1, 1, 3700, 'paid', '2026-10-01'), (2, 2, 1200, 'paid', '2026-10-02'),
  (3, 1, 2500, 'refunded', '2026-10-03');
INSERT INTO order_items VALUES (1, 1, 1, 1, 1200), (2, 1, 2, 1, 2500), (3, 2, 1, 1, 1200),
  (4, 3, 2, 1, 2500);
