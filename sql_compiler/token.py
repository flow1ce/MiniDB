from dataclasses import dataclass, asdict
import re
from common.errors import LexicalError

# SQL 关键字表：Lexer 用它区分保留字与普通表名/列名。
KEYWORDS = {x.upper() for x in '''SELECT FROM WHERE CREATE TABLE INSERT INTO VALUES DELETE UPDATE SET DROP
ORDER BY GROUP JOIN ON AS AND OR NOT NULL TRUE FALSE INT FLOAT VARCHAR BOOL DISTINCT ASC DESC IS
LIMIT OFFSET EXPLAIN ANALYZE BEGIN COMMIT ROLLBACK INDEX UNIQUE SHOW TABLES DESCRIBE DESC COUNT SUM AVG MIN MAX
PRIMARY KEY NOT USER GRANT REVOKE TO ISOLATION LEVEL READ_COMMITTED REPEATABLE_READ SERIALIZABLE'''.split()}

@dataclass(frozen=True)
class Token:
    """词法单元：保存类别、原文以及出错定位所需的行列号。"""
    type: str; lexeme: str; line: int; column: int
    def to_dict(self): return asdict(self)
