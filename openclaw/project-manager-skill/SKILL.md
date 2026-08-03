---
name: shenhuo-internal-service-manager
description: 通过受控脚本管理 Mac 上的伸活内部服务助手，包括状态、运营摘要、错误、测试、知识状态、业务路由、备份和版本升级。
---

# 伸活内部服务助手项目管家

项目根目录固定为：

`/Users/smallworks/services/ai-customer-service-deploy-v0.2/ai-customer-service-deploy`

只允许通过以下入口执行项目运维：

```bash
scripts/macos/project-control.sh help
scripts/macos/project-control.sh status
scripts/macos/project-control.sh summary
scripts/macos/project-control.sh errors
scripts/macos/project-control.sh test
scripts/macos/project-control.sh knowledge-status
scripts/macos/project-control.sh routes
scripts/macos/project-control.sh backup
scripts/macos/project-control.sh queue-upgrade v0.10
```

## 自然语言映射

- “机器人现在正常吗 / 检查状态” → `status`
- “项目做到什么程度 / 给我运营摘要” → `summary`
- “最近有没有报错 / 看日志” → `errors`
- “测试一下机器人 / 验收问答链路” → `test`
- “知识库录入得怎么样 / 检查知识” → `knowledge-status`
- “负责人怎么路由 / 查看业务配置” → `routes`
- “先备份一下机器人” → `backup`
- “升级机器人到 vX.Y” → 确认对应包在 Downloads 或 Desktop，再调用 `queue-upgrade`

## 安全规则

- 不把用户自然语言直接拼接进 Shell。
- 不输出 `.env`、密钥、Token、Cookie 或证书内容。
- 状态、摘要、错误、测试、知识状态、路由查询可以直接执行。
- 备份可以直接执行，但必须回报备份路径。
- 升级必须使用明确版本号和固定命名安装包；由脚本完成校验、备份、测试和回滚。
- 删除数据、修改知识、变更负责人、回滚版本、编辑生产代码目前不直接执行；先说明影响并等待对应的受控工具上线。
- 命令失败时回报原始错误摘要，不尝试任意替代 Shell 命令。
