# 行业订阅源的跨发布者修订归档

这是一个 Python 数据采集项目。当前代码提供来源材料的基础读取入口，合成输入保存在 fixtures，已有读取行为由 tests 验证。

## 来源材料

Feed responses are stored separately by publisher. Entry identifiers are scoped to their feed. A deleted-entry element is a publisher tombstone and is not an Atom entry. The fixture files contain no live endpoints; the reader parses entry basics but does not retain revisions or infer syndication relationships.

上面的样例用于说明来源字段和当前读取边界。项目按原始来源保存材料；同一来源的版本变化和不同来源的同名记录需要分别核对。示例文件只用于本地调试，不代表真实企业、机构或设备数据。

## 本地运行

项目使用 Python 3.12 或更新版本，仅依赖标准库。从项目根目录执行：

```sh
python3 -m app.inspect
python3 -m unittest discover -s tests -v
python3 -m compileall -q app tests
```

示例只使用本地虚构资料。处理结果放在 runtime 目录，源文件应保持只读。若连接真实来源，先按来源方授权范围配置读取权限。
