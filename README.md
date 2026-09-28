# MiniDBMS：大型平台软件设计实习

这是一个可运行的教学型数据库系统原型，采用分层模块架构。运行时使用 `cli` 入口。

项目代码按编译器、存储、索引、执行器、事务和 Web 控制台分层组织。

## 目录分工

```text
sql_compiler/       SQL 编译器接口：Lexer、Parser、AST、语义检查、Catalog、Plan
storage/            Page、Buffer Pool、行编码、槽页和表堆
index/              页式 B+树索引
engine/             Database 编排、表达式求值、物理算子和 Plan 执行器
cli/                正式命令行入口
web/                本地 Web 控制台和 JSON API
tests/              自动化测试
```

各模块按职责协作：`sql_compiler` 负责 SQL 编译，`storage` 负责页和表堆，`index` 负责 B+树，`transaction` 负责事务、锁与 WAL，`engine` 负责组件编排并按照物理 Plan 调度执行算子。

正式运行请使用 `python -m cli.main`。嵌入式调用可以使用 `with Database(path) as db:`，退出上下文会自动回滚未提交事务、释放锁、刷盘并刷新 WAL。

当前实现已覆盖：页式磁盘与 Buffer Pool（Pin 页不可淘汰/删除，脏页刷回受锁保护，写入未分配页会被拒绝）、RID/槽页表堆（删除后回收空数据页并同步页列表）、页式 B+ 树索引（等值与开闭边界正确的 `>、>=、<、<=` 范围扫描，提供 `validate()` 结构校验接口）、分页 Catalog、`SHOW TABLES`/`DESCRIBE` 元数据查询、稳定的空结果集列模式、无 GROUP BY 聚合的单行结果与空输入 COUNT 语义、列级或表级 `PRIMARY KEY`、列级或表级 `UNIQUE` 与 `NOT NULL` 约束、事务状态机（重复 BEGIN、无事务 COMMIT/ROLLBACK 会明确报错）、BEGIN/COMMIT/ROLLBACK（回滚按记录所在页恢复并重建索引）、WAL REDO/UNDO、检查点及无活动事务时的物理 WAL 截断、DROP TABLE/INDEX 的数据文件清理、DDL 排他表锁与大小写不敏感的索引名检查、表级锁 + RID 行锁、等待图死锁检测（超时/死锁后会清理等待图）、同一数据目录下多 `Database` 会话共享锁表、表/索引级 DDL 权限检查（用户标识符不区分大小写）、三种隔离级别枚举（READ COMMITTED 会在语句结束释放共享锁，REPEATABLE READ/SERIALIZABLE 保持读锁并使用事务开始快照）、FLOAT/NULL/完整三值 NOT/AND/OR 逻辑、解析错误恢复、用户/授权、可按未投影列排序的 ORDER BY、非负整数 LIMIT/OFFSET 分页、常量折叠/布尔化简/按查询所需列实际裁剪扫描、SELECT/UPDATE/DELETE 等值索引定位与索引执行计划，以及随机 SQL 稳定性验证。范围 UPDATE/DELETE 会安全回退顺序扫描，避免漏处理边界外记录。当前仍未实现完整的 MVCC、谓词/范围锁和权限角色继承。

普通终端模式会把查询结果渲染为表格，并显示执行耗时、扫描行数、页读取和 Buffer Pool 命中。需要结构化结果时使用 `--json`。`EXPLAIN ANALYZE` 会实际执行查询并返回执行指标。

本地 Web 控制台启动方式：

```powershell
python -m web.server --data demo_data --port 8080
```

然后打开 `http://127.0.0.1:8080`。Web 控制台提供 SQL 执行、表格结果、计划查看、Buffer Pool 状态和优化前后对比。优化对比接口会使用冷缓存运行 5 次并取中位数，同时校验两种计划的结果一致性。

Web 控制台的每个独立标签页都会建立一个独立数据库会话，但共享 Catalog、Buffer Pool、WAL 和锁管理器。因此可以同时打开两个页面，分别执行 `BEGIN`、查询和更新，实时观察共享锁、排他锁、阻塞等待、超时、提交唤醒以及死锁检测。右侧“会话与锁”区域会显示本页会话号、事务号、锁资源和等待关系，并可调整演示用锁超时。新建标签页会生成新会话，普通刷新会保留当前标签页的会话。

并发实现采用表级意向协调与 RID 行锁：SELECT/UPDATE/DELETE 先取得兼容的表级共享锁，实际更新或删除时再取得行级排他锁，所以两个事务可以并行修改不同行；DDL 和 INSERT 仍使用表级排他锁。锁超时或检测到等待图中的环时，当前事务会自动回滚自身的行级修改并释放锁，不会恢复整库快照，也不会覆盖其他会话已经提交的数据。当前是单服务进程内的锁式并发实现，不应表述为跨进程分布式事务、完整 MVCC 或谓词/范围锁；显式事务内也不支持 DDL。

## 运行环境

- Python 3.10 或更高版本
- 仅使用 Python 标准库

## 启动方式

最简方式：

```powershell
cd D:\Database-project -2.1
python -m cli.main
```

进入 `MiniDB >` 后输入 SQL；退出输入 `quit`。一次性 SQL、调试和测试命令见下文。

拆分后的正式命令行入口是 `cli/main.py`，因此不需要直接运行根目录的兼容文件：

```powershell
python -m cli.main
```

也可以直接运行：

```powershell
python cli/main.py
```

不要运行 `database_system.py.bak`，它只是迁移前的备份文件。

数据默认保存在 `data/`。可通过参数调整数据目录、缓存页数和替换策略：

```powershell
python -m cli.main --data demo_data --buffer-size 8 --policy FIFO
```

直接执行一段 SQL：

```powershell
python -m cli.main --data demo_data --sql "CREATE TABLE student(id INT, name VARCHAR, age INT)"
```

注意：`--sql` 是启动命令的参数，必须在看到 `PS D:\Database-project>` 的系统终端中执行；进入 `MiniDB >` 交互提示符后只输入 SQL 或 `.stats`、`.debug ...` 等内置命令。

若要查看编译流水线（Token、AST、Semantic、Plan、Optimized Plan）：

```powershell
python -m cli.main --data demo_data --sql "SELECT name FROM student WHERE age <= 18" --debug
```

交互模式中也可以输入 `.debug SELECT name FROM student WHERE age <= 18`。

交互终端中输入 `.stats` 可查看 Buffer Pool 命中率、缓存帧数、脏页数和 pinned 页数，输入 `quit` 或 `exit` 退出。

## 示例

```sql
CREATE TABLE student(id INT, name VARCHAR, age INT);
INSERT INTO student VALUES (1, 'Alice', 20);
INSERT INTO student VALUES (2, 'Bob', 17);
SELECT name FROM student WHERE age > 18 ORDER BY name;
UPDATE student SET age = age + 1 WHERE id = 1;
SELECT * FROM student;
DELETE FROM student WHERE id = 2;
EXPLAIN SELECT * FROM student WHERE age >= 18;
CREATE INDEX idx_age ON student(age);
```

## 测试

```powershell
python -m unittest discover -s tests -v
```

当前自动化测试覆盖：词法位置与非法字符、AST/Logical Plan、语义检查、页分配/释放/读写、LRU/FIFO 与 Pin、跨重启持久化、SELECT/UPDATE/DELETE、JOIN、GROUP BY/聚合、事务提交/回滚、索引创建与维护、模块边界导入。额外手工验收覆盖：B+ 树多层分裂/合并和范围边界、WAL REDO/UNDO 与检查点、主键/UNIQUE/NOT NULL、SHOW TABLES/DESCRIBE、权限拒绝、READ COMMITTED/REPEATABLE READ 锁生命周期、随机 SQL 稳定性。

执行阶段会在计划生成前显式检查表/列存在性、表达式类型、INSERT/UPDATE 类型匹配和约束条件。当前仍未实现完整 MVCC、谓词/范围锁、权限角色继承；这些属于指导书中的高级扩展，不应在答辩中表述为已完成。
