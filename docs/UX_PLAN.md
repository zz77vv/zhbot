# 交互优化实施计划

目标：落实已确认的七项交互建议及取消报单确认，保留现有支付核验、订单归属和可靠投递。

## 分工与接口

- 主智能体负责 app.py 集成、客户逐项填写、余额不足后保留草稿、预览更新、取消确认、按身份和步骤分流输入；修改 store.py 并保留事务与版本检查。
- 支付子智能体只修改 payment_ui.py、新的支付界面迁移及支付界面测试。提供 PaymentUI.topup_view(u)、topup_input(u,text)、payment_view(u,order,message_id=None)、payment_check(u,pid,message_id=None)、payment_credit_notice(event,payload)。app.py 通过继承调用，credited 事件继续使用原事件状态机，只替换显示方式。
- 后台子智能体只修改 admin_ui.py 及新的后台交互测试。提供 AdminUI.pending_view(u,page=0,message_id=None)、batch_view(u,batch,page=0,message_id=None)、receipt_preview(u,order_id)、receipt_confirm(u,key)。完成按钮先预览，确认内容变化必须重新预览。
- 公共约定：App.card(u,text,rows=None,message_id=None) 编辑已有短卡，明确400才补发，网络不明不重试；App.send 继续分段完整展示长文本。每页8项。普通用户不增加订单、进度、充值历史入口。

## 顺序与检查

- [x] 并行实现支付卡/持久到账通知和后台分页/回单预览，文件独立。
- [x] 客户字段自动推进，明确照片/文件追加与完成；草稿余额不足保留编辑状态，恢复时重新检查商品版本，必须重新预览确认。
- [x] 资料预览尽量编辑原消息；明确取消报单并二次确认；向导优先于管理员普通回复。
- [x] 集成新模块并补齐真实行为测试，运行完整回归。
- [x] 独立审查支付重复查询、旧按钮、改价、回单变动及不确定网络边界。
- [x] 备份测试数据库/代码，部署隔离测试实例并确认服务，清理临时文件。
- [x] 同步已有 GitHub 分支和 PR 说明。

追加要求：最初切换为 yingzihuiyuan9，随后按用户最新选择改回 NewsEmoji；重新核对实际图案、编号与备用符号，统一购物、货币、确认、回单和管理图标。

约束：Python 3.12 标准库与 SQLite；少量统一的自定义表情；保留原始回单文本；支付签名核验和唯一入账流水不改；投递不明仍人工核实；仅更新测试机器人，不合并正式分支。

验证结果：本地 122 项无失败（4 项 Windows/权限跳过）；服务器 Linux 122 项全部通过。迁移至 8 后账本和钱包逐行一致，测试服务稳定，其他机器人进程未变。
