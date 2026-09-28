class DBError(Exception):
    """数据库统一错误基类：携带阶段、消息和 SQL 行列位置。"""
    def __init__(self, stage: str, message: str, line: int = 1, column: int = 1):
        self.stage, self.message, self.line, self.column = stage, message, line, column
        super().__init__(f"{stage}Error at line {line}, column {column}: {message}")

# 以下子类把错误归类到词法、语法、语义或存储阶段，便于前端展示。
class LexicalError(DBError):
    def __init__(self, message, line=1, column=1): super().__init__("Lexical", message, line, column)

class SyntaxError_(DBError):
    def __init__(self, message, line=1, column=1): super().__init__("Syntax", message, line, column)

class SemanticError(DBError):
    def __init__(self, message, line=1, column=1): super().__init__("Semantic", message, line, column)

class StorageError(DBError):
    def __init__(self, message): super().__init__("Storage", message)

__all__ = ["DBError", "LexicalError", "SyntaxError_", "SemanticError", "StorageError"]
