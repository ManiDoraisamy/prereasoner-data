# Evaluation source

- Source: `millat/e-commerce-orders` on Hugging Face, `ecommerce_orders_clean.csv`.
- License: MIT.
- URL: https://huggingface.co/datasets/millat/e-commerce-orders
- Data: synthetic e-commerce orders generated with Faker; no customer data was copied from a real system.
- The checked-in `orders.csv` is a 10,000-row projection of the source containing only the fields used by this release gate (`order_id`, `price`, `quantity`, `delivery_status`, `payment_method`, `channel`, and `customer_segment`). Dropping unused address and timestamp fields keeps the CSV below the public 2 MB size limit without changing any expected metric. The public uploader accepts up to 10,000 data rows, so this full fixture exercises the upload boundary directly.
- Purpose: Neartail-style order, delivery, customer-segment, payment, and channel evaluation only; not a home-page example and not a training corpus.
