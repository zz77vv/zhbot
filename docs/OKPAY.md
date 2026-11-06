# OKPay：阻塞与接入验收

当前未实现真实 OKPay HTTP/官方 SDK 适配，不可宣称真实接入成功。`Payments` 是入账边界；`tests/test_payments.py` 的 Gateway 是离线测试替身，不会被运行入口加载。充值 UI 的两按钮分支已写入，但缺少真实 provider，运行中充值会明确提示未开放，而不生成虚假支付链接。

## 已有依据与查证结果

已读取完整 Notion 需求：https://app.notion.com/p/40fafec1362381239ce0cf53ec1b7ebc 。其中提及整理稿《OKPay 通用对接文档.docx》、`/shop/payLink`、`/shop/checkDeposit`、`/shop/checkTransferByTxid` 和 HMAC-SHA256。整理稿本体未提供给当前会话。Notion 明确称它不是已验证生产契约。

2026-11-06 公开检索遇到多个同名服务。找到 Dujiao-Next 项目自身的 OKPay 支付配置与源码说明：
- https://www.dujiao-next.com/en/payment/guide
- https://github.com/dujiao-next/dujiao-next/issues/294

这些属于使用方资料，不能替代 OKPay 官方契约或 SDK。未将其复制为支付协议，更没有拿另一个同名平台的 SDK 冒充。未取得可确认的平台官方文档、官方 SDK、签名测试向量或商户凭据，未发送真实支付请求、未收款。

## 需要提供

1. 所用 OKPay 的官方商户平台、文档与 SDK 链接/版本（不要在聊天或仓库贴密钥）。
2. 通过部署环境的安全变量注入商户 ID、令牌；确认权限与允许的来源地址。
3. 官方查单成功状态、金额单位、币种、商户订单/平台订单关联、响应验签规则、时间戳/nonce规则和测试向量。
4. 测试机器人 Token 与管理员 ID、可访问 Telegram/OKPay 的测试环境。真实小额付款需单独安排，不代表授权部署正式收费。

## 继续实现时的契约

`gateway.create(order)` 调官方 SDK 建单，返回经验证的 HTTPS 付款链接；必须以本地订单 ID 建立归属。异常保留本地充值记录以便对账。

`gateway.verify_query(order)` 通过官方 SDK 主动向平台查单，并先验证响应真实性及请求关联，再返回 merchant/order_id/platform_id/amount/currency/paid。不得把来自 Telegram、截图、URL访问、用户JSON或未验签回调的值传入该可信边界。SDK 函数和字段必须按官方实际契约落地，当前不猜测。

`Payments.check` 二次核对本地客户归属、商户、商户订单号、整数USDT金额、币种及唯一平台交易号；事务内原子入账，单充值/单平台交易不得重复加分。归属仅由创建充值订单的客户决定，无付款人TG ID限制，支持代付。已付凭证错误/验签失败/不确定响应均不入账。

必须补充官方 SDK 测试向量、真实建单、待付/实付/过期查单、错误金额/币种/串单、第三方代付、并发重复检查、网络超时与对账恢复测试。通过后再接入运行入口，并手动验收「支付订单」「检查支付状态」两个按钮。当前不提供任何配置开关绕过该缺口，也不接收回调自动入账。
