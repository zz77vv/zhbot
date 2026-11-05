# OKPay 证据边界与离线协议核实（2026-11-05）

## 当前原始来源与取得证据

用户发布新云环境后，受支持的 environment_status 返回 observations_current=true、network_policy.state=enforced，允许 docs.okaypay.me；本地网络策略一致。此前拒绝不再代表本环境状态。本轮仅一次使用继承代理和 TLS 校验访问各 URL，两个目标均返回 HTTP 200，无重定向、无替代通道。

| 来源 | 获取时间（UTC） | SHA-256 |
| --- | --- | --- |
| [官方对接文档](https://docs.okaypay.me/doc.html) | 2026-11-05 07:49:17 | `ed7e5746eb06ca4df6c9a0a5c81cc565bbeb048865c3ef22d60fea738a4ac32e` |
| [官方五语言 SDK ZIP](https://docs.okaypay.me/okpay-sdk.zip) | 2026-11-05 07:49:18 | `80201f8e7c43512f842fadc6baaea340335c9774f04891d95ddaf5386512eead` |

文档页脚更新日期 2026-07-06，HTTP Last-Modified 为 2026-07-06 11:17:14 GMT；SDK 8,118 字节，HTTP Last-Modified 为 2026-07-15 10:25:50 GMT。日期不同，不能假定版本完全同步。文件在执行器 `/tmp/okpay-official/` 留作本次检查；临时文件不保证跨环境保留。未提交下载原件，也未加载、导入或执行任何 SDK 代码，仅用 zipfile 读取五份源码作静态审阅。

SDK 源码 SHA-256：

| ZIP 路径 | SHA-256 |
| --- | --- |
| sdk/python/okpay_sign.py | `a43fb7061e29a85ba76b7f02b27bb50e621b7440aaaf921a2230893ace20bbfc` |
| sdk/nodejs/okpaySign.js | `c2617e487b5fb4c6ac6447a6102726c6ca72ba4c777e5902f5d8a9f11b0aa31f` |
| sdk/php/OkpaySign.php | `496309b6a0ad4346c6069c7ed925703289eeb0e3f87b695f05d39acd548d1060` |
| sdk/go/okpaysign.go | `270985da240c67ccbbb9c7bb7e6cbbecc9e35e0dd2021f85a61c9a0a4f2e41f7` |
| sdk/java/OkpaySign.java | `4e561e77b20a839545300f73a00195fcfb0be989395e8522d3ca2288b1659861` |

## 已核实的文档契约与实现边界

依据第 1–4、6.1–6.3、7.1、9 节：

| 项目 | 原始资料明确内容 | 离线实现 |
| --- | --- | --- |
| 传输 | HTTPS，POST form，UTF-8，API 根地址 api.okaypay.me | 仅生成 path 和 form 字节，无 HTTP 客户端；未访问 API 根地址 |
| 鉴权 | id、timestamp、nonce、sign；密钥 token 不传输；HMAC-SHA256 大写 hex | 本地签名；成功信封须验签、code=200、商户匹配；恒定时间比较，hex 大小写兼容 SDK |
| 防重放 | 请求 timestamp 与服务端偏差 ±300 秒；nonce 8–128 字符、600 秒内不可重复 | 调用者显式提供时间与 nonce；保守限制 nonce 为 ASCII 字母、数字、下划线、连字符；不声称本地检查服务端时钟或实现持久化 nonce 去重。真实传输层尚未实现 |
| payLink | amount、coin、unique_id；响应 order_id、pay_url、status=0 | 请求仅使用整数 USDT 和本地单号；显式关联的请求上下文提供内部 unique_id，不能声称响应回显了单号或确认付款人/金额 |
| checkDeposit | 请求 unique_id；响应 order_id、unique_id、status、amount | 无平台单号时可构造；缺 payer/coin 不补造，只核实 |
| checkTransferByTxid | 请求 txid，即平台 order_id；已付响应含 unique_id、pay_user_id、amount、coin、status=1 | 已知平台号时构造；付款人按 Telegram ID 解析，金额保持字符串、币种/状态/ID 保留给状态机匹配 |
| 充值回调 | JSON，type=deposit，含上述已付事实 | 离线解析，须明确 deposit；仅 status=1 可产生可用已付业务事实，异常/其他状态保留身份转核实；拒绝 withdraw；没有 HTTP 回调监听器 |
| 订单身份 | order_id 被称为平台订单号/txid；充值按 order_id 幂等 | 映射到 Evidence.platform_id 并沿用持久化认领/冲突隔离；不凭术语把它升级为独立结算身份，transaction_id=None |
| 实际支付时间 | 充值响应及回调没有定义可信时间字段 | paid_at=None；即使出现未知 paid_at/time/timestamp 也忽略其业务意义，禁止推断 |
| 平台到期 | 未承诺充值链接 600 秒后失效，也未提供充值取消接口 | 本地 expires_at 不发送、不延长；不将出款审核 24 小时超时用于充值 |

`OfflineOKPayAdapter` 继承关闭的 create/check；`enabled=False`。应用入口仍固定使用 DisabledGateway，没有凭据读取或启用开关。所有新签名测试仅使用第 9 节公开 TEST token。测试内 FixtureGateway 才允许驱动模拟任务，不进入生产代码。

解析器的 verified 仅表示完整支持子集通过 HMAC、成功信封、商户及本地单号关联检查。金额/币种/付款人/订单号的已认证矛盾仍传给 Payments 核实和隔离，不能把 verified 理解为已经满足入账条件。签名失败、外部商户、他单 unique_id、非充值通知或无法支持的规范化输入拒绝生成受信证据；任务遇这些解析异常沿用 unknown 核实路径。已验签且商户/单号/平台身份有效，但业务字段类型或链接异常时，保留有效身份，将业务事实组（status/payer/amount/coin 或 status/pay_url）置 None 送状态机 review，确保矛盾平台号先持久化隔离；None 只表示内部不可用，不是新增的远端状态。不保留异常原文或凭据。

创建响应不含 unique_id，必须由未来传输层将单次请求与响应可靠关联，不能将收到的任意成功 payLink 报文绑定任意本地单。本模块只接受调用者显式给定的上下文，不建立联网来源证明。查询也必须由未来传输层绑定到实际请求的接口；现有离线解析和模拟流程不能代替传输来源验证。

## 规范化差异审计与保守支持子集

文档要求仅移除顶层 sign，去 null/空串但保留 0/false；对象用点号展开，ASCII 键排序，未 URL 编码的 key=value 用 & 拼接。内部嵌套 sign 仍参与签名。请求最终 form 的 URL 编码发生在签名之后，两者不可混淆。

五语言源码在文档标量/对象向量范围内一致，但范围外存在差异。以下是静态阅读所得，并非执行 SDK 后的结果：

| 输入类型 | SDK 差异 | 本模块策略 |
| --- | --- | --- |
| 数组，例如 data=[1,false] | Node/PHP 递归到 data.0、data.1；Python 把 list 转成 `[1, False]`；Go fmt 得到 `[1 false]`；Java 非 Map 走 String.valueOf，取决于 List/数组类型。空数组也可能被丢弃或变成 `[]` | 全部拒绝，包括嵌套和空数组。TransactionHistory 含数组，明确不支持，不能挑一个 SDK 猜平台实现 |
| JSON 小数、指数、大整数 | Python/Node 浮点解析可能重排数字；Go 文档要求 UseNumber，Java 要求避免 Double | 严格 JSON 解析保留数字词法；直接 dict 的 Python float 拒绝。金额业务字段只接收有界十进制字符串/数字词法，不接受指数；绝不四舍五入 |
| 点号键与嵌套同名 | 展开可覆盖叶子，PHP 的合并规则还可能与其他语言不同 | 原始键仅 ASCII 字母/下划线开头，后接字母/数字/下划线；拒绝所有点号键、非 ASCII 键和非字符串键 |
| 重复 JSON 键 | 各解析器选择/覆盖行为不属于签名规范 | 拒绝任意层重复键 |
| 值含 & | 未编码原文可能伪装额外参数 | 保守拒绝；值中的 = 本身允许（支付 URL 需要）。不对签名原文做 URL 编码；包含多参数 & 的合法 URL 也可能被拒绝，这是刻意的支持范围限制 |
| Unicode 键/错误编码 | 文档只定义 ASCII 排序；无效代理字符无法统一 UTF-8 | 拒绝非 ASCII 键、错误 UTF-8/代理字符；正常 Unicode 值保持原文 |

额外本地界限：原始 JSON 和规范化原文最多 64 KiB、最多 256 个成员、嵌套深度最多 8、单值最多 4096 字符；ID 为有界正整数或最多 128 个可打印 ASCII 字符的订单号。code 必须是 JSON 整数；业务 status 的布尔/数字字符串不可作为有效状态，认证身份保留并以内部 status=None 转核实。创建链接仅支持 HTTPS t.me/okpay、无用户信息/端口/片段且含 query 的子集，未知主机/格式只核实。这些是本地安全边界，不声称官方也施加这些限制。

## 验证与仍需材料

第 9 节 A（请求）、B（嵌套充值回调）、C（0/false/null/空值）三个原文和固定签名逐一一致；B 也通过原始 JSON 读取后的验签。其余测试使用独立、测试内的模拟服务器签名夹具；不把自行构造的成功响应当成真实接口兼容证据。完整命令与数量见 TEST_RESULTS.md。

仍需官方澄清实际付款时间的认证字段/单位/时区、远端到期/取消能力、跨语言数组的权威表示，以及现有内部独立结算身份要求如何与官方按 order_id 幂等对齐。不能自行降低现有账本入账门槛。即使所有已文档化事实都匹配，当前适配结果仍缺时间与独立结算身份，只能人工核实；没有强制加分或退款功能。

未使用真实凭据，未访问任何金融端点，未做真实 Telegram/OKPay 联调、部署、启用或合并。后续启用必须另行审查和授权。

---

## 历史记录（本轮原始来源取得之前）

以下保留前次二次资料核实状态；其中“未取得官方文档/SDK”描述的是当时情况，当前以以上记录为准。


## 已读取来源

Library `libfile_543b47f687188191a405259ad4f263b7`，文件名 `OKPay_通用对接文档(1).docx`，整理日期 2026-11-04。本轮通过 Library 读取全部 565 行正文；文件字节下载失败，尚未在执行器中获得可读的原始 DOCX。

这是二次整理，文中声称依据一份 17 页 PDF 和 SDK，但本轮未获得或验证原始 PDF、SDK 或测试向量。不能把其中的“官方”措辞升级为已确认的接口契约。

## 来源声称与实现约束

| 项目 | 二次资料的说法 | 当前判断 |
| --- | --- | --- |
| API 地址 | `https://api.okaypay.me`，POST form 到 `/shop` 下接口 | 未确认官方原始版本；不进行真实请求 |
| payLink | 返回 order_id、pay_url；支持 unique_id | 尚未验证字段、重试和幂等语义；创建不确定时禁止换号重试 |
| checkDeposit | 返回订单号、状态、金额，未明确承诺付款人或币种 | 单独使用不足以确认本业务充值 |
| checkTransferByTxid | 已支付时有 pay_user_id、amount、coin | 只有二次资料声称；付款人身份能力仍需原始文档和脱敏样本核实 |
| 支付时间 | 没有明确的可信 paid_at 字段或定义 | 无法证明付款发生在本地 10 分钟内；缺失时只人工核实 |
| 平台有效期 | 未承诺链接 10 分钟后失效 | 只能实现本地截止，不向用户承诺平台会拒绝晚付款 |
| 签名 | HMAC-SHA256，嵌套字段展开、ASCII 排序、完整成功响应验签 | 需原始 SDK/向量对照；不能只靠自己构造的签名测试证明兼容 |
| 状态码 | 充值 0 待支付、1 已支付 | 未以原始契约确认；其他状态只可视作本地状态 |

本轮公开检索 `site:okaypay.me "checkTransferByTxid"` 和付款人/时间字段未返回结果；打开文档列出的 API 根地址及主站均未能通过浏览工具访问。这些结果不证明接口不存在，也不证明兼容性。

## 真实支付启用前的必要材料

- 从可确认的官方渠道获得当前商户接口原始文档、SDK 及签名测试向量，并确定接口版本和商户权限。
- 核实付款人字段语义：它是否确为实际付款人的 Telegram ID，以及查单何时能稳定获得它。
- 核实是否提供签名覆盖的实际支付时间、时间单位与时区；不能把查询时间、回调到达时间或订单创建时间当作支付时间。
- 明确 late/unknown/review 的人工核实流程与审计要求；不自动接受截图，不猜测付款归属，不自动退款。
- 用户批准服务器变更和真实支付激活后，另行安排有边界的小额联调与对账。此前仅用模拟数据，真实支付适配器保持禁用。

查询终态即使在截止前被观察到，也必须先确认所有身份、金额、币种与来源字段。截止后第一次看到“已支付”不能自动推定其付款发生时间。

## 本地实现不构成官方契约

离线状态机已移除 `transaction_id == platform_id` 的要求；两者是内部适配器分别认证的身份，可以相同或不同，但各自必须唯一关联本地单。真实 OKPay 是否使用一个字段表达两种身份、是否提供独立结算身份，仍须原始资料核实，不从本模拟器反推。

本地过期不是平台过期，发现较晚不是实际晚付。异常单的 `/payment_close` 只解除本地待核实阻塞，既不确认收款也不撤销上游订单。完整政策见 [PAYMENT_STATE_MACHINE.md](PAYMENT_STATE_MACHINE.md)；真实时间字段、认证方式及该业务政策在真实启用前仍需确认。
