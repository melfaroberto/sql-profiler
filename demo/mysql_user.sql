-- Prepares the installed MySQL server for the Mini Shop demo: the database and a user that can only use it.
-- Run it once as the MySQL administrator:
--
--     Linux:    sudo mysql < demo/mysql_user.sql
--     Windows:  mysql -u root -p < demo\mysql_user.sql      (or open it in MySQL Workbench and run it)

CREATE DATABASE IF NOT EXISTS shop_demo DEFAULT CHARSET utf8mb4;

CREATE USER IF NOT EXISTS 'shop'@'localhost' IDENTIFIED BY 'Shop-Demo-2026';
CREATE USER IF NOT EXISTS 'shop'@'127.0.0.1' IDENTIFIED BY 'Shop-Demo-2026';
GRANT ALL PRIVILEGES ON shop_demo.* TO 'shop'@'localhost';
GRANT ALL PRIVILEGES ON shop_demo.* TO 'shop'@'127.0.0.1';
