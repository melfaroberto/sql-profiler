from .mysql import MySQLDecoder
from .postgres import PostgresDecoder

DECODERS = {"mysql": MySQLDecoder, "mariadb": MySQLDecoder, "postgres": PostgresDecoder, "postgresql": PostgresDecoder}
DEFAULT_PORTS = {"mysql": 3306, "mariadb": 3306, "postgres": 5432, "postgresql": 5432}
