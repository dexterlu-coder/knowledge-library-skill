# 飞书操作流程

## 绑定保存位置

对用户明确授权的每个位置执行 `lark-cli wiki +node-get --node-token <飞书链接> --as user`，取得真实的空间与节点标识。依据用户指定的名称或飞书空间名称准备 JSON。命令示例中的 `delivery.py` 使用包内 `scripts/delivery.py` 的路径，Windows 可用 `py -3.11`。

```json
{"targets":[{"name":"资料库","space_id":"空间标识","parent_node_token":"节点标识","url":"https://example.feishu.cn/wiki/节点标识"}]}
```

用 `python delivery.py configure <位置.json>` 逐一核对并保存默认位置。用 `python delivery.py config` 读取。旧版单位置配置仍可读取。状态目录为 Windows 的 `LOCALAPPDATA/knowledge-library`，其他系统的 `XDG_CONFIG_HOME/knowledge-library`，相应环境变量缺失时用 `~/.config/knowledge-library`。联调使用 `KNOWLEDGE_LIBRARY_STATE_DIR` 指向独立目录。用该目录保存状态和附件快照。

## 收录、修订和补写

1. 为原始文件传 `source_file`，脚本按文件字节计算来源标识。链接或内联笔记传稳定的 `source_id`，内联笔记可使用原文 UTF-8 字节的 SHA-256。始终沿用同一种来源编码。脚本按首次分卡结果固定卡片顺序和标识。再次收录同一来源时复用已记录的分卡。正文使用 Markdown，附件通过独立字段传入。

```json
{"source_file":"原稿.pdf","cards":[{"title":"卡片标题","body":"正文及出处","attachments":["原稿.pdf"]}]}
```

2. 文件路径相对于请求 JSON。“仅这次”在收录请求中加入和配置格式相同的 `targets` 数组。用 `python delivery.py prepare <收录.json>` 保存目标、正文及附件快照。随后列出返回的实际库名，用 `python delivery.py deliver <delivery_id>` 写入。逐卡逐库报告已核验的页面链接和版本、已核验的附件及待补附件。若结果同时有 `url` 和 `error`，说明正文已核验、附件待补，保留两者和投递 ID。成功的项在重试时回读核对。
3. 用 `python delivery.py retry <delivery_id>` 续写中断的投递，沿用原始目标及版本。已记录节点优先按 token 查询。明确被拒的创建或上传可在恢复条件后重试。结果不明时先回读，无法确认时保留待核对状态。重试附件时比较下载件摘要，并从原始快照续传。重投原版遇到已核验的新版时，回执说明现行版本。尝试投递已被新版取代的修订时，回执报告该修订已被取代。
4. 用户明确修订时，在 JSON 中传原 `source_id` 或 `source_file`、作为修订基线的 `delivery_id` 和新 `body`。用 `python delivery.py revise <修订.json> --card <原卡号>` 准备指定卡片。默认继承基线的目标。如用户只选其中一部分，传入对应的 `targets` 数组。提示实际目标后，用返回的新 `delivery_id` 写入。基线应取该卡在所选目标上的已核验版本。检测到手工正文改动时展示差异。旧版账本若留有整组版本的待写稿，先核对已写页面，再为打算修订的卡片准备新投递。
5. 本机沙盒报飞书登录存储锁错误时，按宿主审批机制运行具体的投递命令。返回错误时保留投递 ID。原版手工投递记录位于 `deliveries/`，保留该目录。遇到同一来源的旧记录，先回查已有页面或完成旧投递，再处理修订。

## 检索

在绑定的飞书入口下列出候选页面，按需遍历子节点。使用 `lark-cli wiki +node-list` 和 `docs +fetch`，带 `--as user`。标题匹配不足时，读取候选正文，按同义词和项目名匹配。回读命中卡片及所引用的原文，核对数字、版本和出处。使用飞书搜索时，确认命中节点属于授权查询范围。
