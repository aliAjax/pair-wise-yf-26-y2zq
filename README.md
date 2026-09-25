# 数字档案长期保存服务

仅使用 Python 3.11+ 标准库实现的独立档案保存项目，支持清单校验、真实 SHA-256 内容校验、多个离线副本、损坏检测与自动修复、格式迁移、保留期限、访问控制、保全登记与到期销毁审批。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

服务地址为 <http://127.0.0.1:8102>，默认数据库 `preservation.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`owner`、`archivist`、`auditor`、`outsider`。API 使用 `X-User-Id`。文件通过 Base64 提交，单文件上限 10 MiB；这是为了保持示例自包含，生产部署应换成对象存储和流式上传。

## 主要接口

- `POST /api/archives`：创建受限档案。
- `POST /api/archives/{id}/members`：所有者授予 read/write 权限。
- `POST /api/archives/{id}/versions`：提交文件清单，服务端重新计算哈希和大小。
- `GET /api/versions/{id}`：查看版本、文件清单和副本状态。
- `POST /api/versions/{id}/copies`：创建独立副本内容。
- `POST /api/copies/{id}/verify`：校验副本；发现损坏时从健康副本修复。
- `POST /api/copies/{id}/simulate-corruption`：演示/测试介质损坏，仅 owner 或 archivist 可用。
- `POST /api/versions/{id}/migrate`：生成格式迁移后的新版本并保留派生关系。
- `POST /api/archives/{id}/holds`：登记保全（负责人/档案管理员，需写权限）。
- `POST /api/holds/{id}/revoke`：撤销生效保全。
- `POST /api/archives/{id}/destruction-requests`：申请销毁（负责人/档案管理员，需写权限）。
- `POST /api/destruction-requests/{id}/approve`：审计员批准销毁（需该档案访问权限）。
- `GET /api/archives/{id}/status`：保留期限、版本状态、保全与销毁申请、审计记录。

档案路径拒绝绝对路径和 `..`；同一版本副本位置唯一；没有健康副本时版本标记为 `degraded`；所有变更写入审计日志。

## 销毁规则

- 保留期限到期前不受理销毁申请；存在生效保全时拒绝申请；同一档案同时只允许一条待审批申请。
- 审批时重新核查保全：待审批期间新登记的保全会让审批失败，撤销后方可再次审批。
- 批准后清空档案全部文件内容与副本（保留版本元数据），档案标记为已销毁，销毁后禁止再写入内容、登记保全或重复申请。
- 处置与保全变动记录（`hold.register`、`hold.revoke`、`destruction.request`、`destruction.approve`）保留在审计日志中，可在状态接口查看。
