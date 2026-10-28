-- Schema of the demo "Mini Shop" application.
-- Some relationships are declared as foreign keys, others on purpose are NOT
-- (orders.created_by -> users.id, audit_log.user_id -> users.id): the profiler discovers them
-- at runtime from the joins the application makes, and reports them as "candidate relationships".
-- audit_log.entity_id is polymorphic (it points to different tables) and is never joined: no tool can
-- find that one from the traffic, which is a realistic case too.

CREATE TABLE `users` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `username` varchar(40) NOT NULL,
  `password_hash` char(64) NOT NULL,
  `full_name` varchar(100) DEFAULT NULL,
  `role` varchar(20) NOT NULL DEFAULT 'clerk',
  `last_login` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `customers` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(100) NOT NULL,
  `email` varchar(100) DEFAULT NULL,
  `city` varchar(60) DEFAULT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `categories` (
  `code` varchar(20) NOT NULL,
  `description` varchar(100) NOT NULL,
  PRIMARY KEY (`code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `products` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `sku` varchar(20) NOT NULL,
  `name` varchar(100) NOT NULL,
  `category_code` varchar(20) NOT NULL,
  `price` decimal(10,2) NOT NULL,
  `stock` int(11) NOT NULL DEFAULT 0,
  `reorder_level` int(11) NOT NULL DEFAULT 5,
  PRIMARY KEY (`id`),
  CONSTRAINT `fk_products_category` FOREIGN KEY (`category_code`) REFERENCES `categories` (`code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `orders` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `customer_id` int(11) NOT NULL,
  `created_by` int(11) NOT NULL,
  `created_at` datetime NOT NULL,
  `status` varchar(20) NOT NULL DEFAULT 'open',
  `total` decimal(12,2) NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  CONSTRAINT `fk_orders_customer` FOREIGN KEY (`customer_id`) REFERENCES `customers` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `order_lines` (
  `order_id` int(11) NOT NULL,
  `line_no` int(11) NOT NULL,
  `product_id` int(11) NOT NULL,
  `qty` int(11) NOT NULL,
  `unit_price` decimal(10,2) NOT NULL,
  PRIMARY KEY (`order_id`, `line_no`),
  CONSTRAINT `fk_lines_order` FOREIGN KEY (`order_id`) REFERENCES `orders` (`id`),
  CONSTRAINT `fk_lines_product` FOREIGN KEY (`product_id`) REFERENCES `products` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `audit_log` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `ts` datetime NOT NULL,
  `user_id` int(11) DEFAULT NULL,
  `action` varchar(40) NOT NULL,
  `entity` varchar(40) DEFAULT NULL,
  `entity_id` int(11) DEFAULT NULL,
  `details` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
