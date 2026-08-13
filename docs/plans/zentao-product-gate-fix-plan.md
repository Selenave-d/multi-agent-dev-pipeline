# 禅道产品归属闸门修复计划

1. 为 `ZenTaoRequirementSource` 增加可选的期望产品 code。
2. 配置期望值时复用认证 GET 查询 `/product-view-<id>.json`，精确比较 `product.code`。
3. 不匹配时抛出 `product_mismatch`，匹配时在原始数据中记录 `product_code`。
4. 从 `project.zentao_product` 注入适配器，并更新真实配置示例。
5. 覆盖匹配、不匹配、未配置三条路径，运行全量测试和静态检查。
